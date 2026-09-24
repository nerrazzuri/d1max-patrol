/// D1 Max 巡检 app 的入口。
///
/// **岔路口已经过了：操作界面是原生做的，不套 WebView。** 这段话原先写的是
/// 「操作界面那一层还没定，也许手机浏览器直接开狗身上那个网页版就够了」——
/// 第 7 卷把答案做出来了，走的是原生这一边。
///
/// **为什么不嵌网页。** 遥控这一屏有三件事：视频要铺满、摇杆要跟手、松手要
/// 立刻停。在 WebView 里这三件都得过一层 JS 桥，而那层桥的延迟正好加在
/// 「松手到狗停住」这条最要命的路径上（`app/teleop.py` 的守死人是 0.6 秒，
/// 我们自己还要在松手那一刻主动补发一次全零停）。规格 §7.7 定的也是原生。
///
/// **首屏二选一（W00c4）**：「站点」——连庄园现场的站点主机（`ui/site_page.dart`，登录、看狗、
/// 派巡检、叫停、事件、排程）；「现场直连（过渡）」——下面这几屏，连狗的热点。遥控、视频在站点
/// 通路做出来之前只有直连能用（总设计 §1 的「手机不直连狗」是目标，见 W00c4 设计）。
///
/// **直连那几屏**（都在 `ui/` 下）：
///
/// * `roster_page.dart` —— 名册。我手上有哪几只狗、各自的热点和 PIN，
///   并从这里进遥控屏和盘况屏。
/// * `teleop_page.dart` —— 遥控。视频铺满 + 双虚拟摇杆（左平移吸单轴、
///   右只转向），持续发拍、松手主动发一次全零停；没有控制权或视频掉线即变灰。
/// * `control_panel.dart` —— 控制权面板。取／还／接管，倒计时读狗给的
///   `expires_in_ms` 相对量，绝不拿绝对时刻去减手机自己的钟（狗上没有 NTP）。
/// * `storage_page.dart` —— 盘况。**只读**，不给清盘入口。
/// * `ui/widget/live_video.dart` —— MJPEG 直播，在线与否走 health 轮询判定，
///   不看图片加载状态。
///
/// **底下这几层**：名册模型 `model/registry.dart`；PIN 存在系统钥匙串里
/// （`store/secure_pin_vault.dart`，名册文件里从头到尾看不见它）；跟狗说话走
/// `net/patrol_client.dart`（PIN 只走质询-应答，从不上网）；狗发过来的报文形状
/// 在 `net/wire.dart`，由 `test/fixtures/*.json` 这组契约夹具钉着 —— 那些文件是
/// 狗那头生成的，字段名一改，Dart 这侧当场红。
library;

import 'package:flutter/material.dart';

import 'store/paths.dart';
import 'store/pin_vault.dart';
import 'store/registry_store.dart';
import 'store/secure_pin_vault.dart';
import 'store/site_store.dart';
import 'ui/roster_page.dart';
import 'ui/site_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final store = await openRegistryStore();
  final sites = await openSiteStore();
  runApp(PatrolApp(store: store, vault: SecurePinVault(), siteStore: sites));
}

class PatrolApp extends StatelessWidget {
  final RegistryStore store;
  final PinVault vault;

  /// 给了就先让人选「站点 / 现场直连」；不给（老测试）直接进直连的名册。
  final SiteStore? siteStore;

  const PatrolApp(
      {super.key, required this.store, required this.vault, this.siteStore});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'D1 Max 巡检',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.teal),
      ),
      home: siteStore == null
          ? RosterPage(store: store, vault: vault)
          : ModePage(store: store, vault: vault, siteStore: siteStore!),
    );
  }
}


/// 首屏：连站点，还是现场直连（过渡）。
class ModePage extends StatelessWidget {
  final RegistryStore store;
  final PinVault vault;
  final SiteStore siteStore;
  const ModePage(
      {super.key, required this.store, required this.vault, required this.siteStore});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('D1 Max 巡检')),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          FilledButton.icon(
            key: const Key('mode-site'),
            icon: const Icon(Icons.hub),
            label: const Text('站点'),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteListPage(store: siteStore))),
          ),
          const SizedBox(height: 8),
          const Text('连庄园现场的站点主机：看狗、派巡检、叫停、事件与排程。'),
          const SizedBox(height: 24),
          OutlinedButton.icon(
            key: const Key('mode-direct'),
            icon: const Icon(Icons.wifi_tethering),
            label: const Text('现场直连（过渡）'),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => RosterPage(store: store, vault: vault))),
          ),
          const SizedBox(height: 8),
          const Text('连狗自己的热点：遥控、视频、盘况。站点有了这些之后撤掉。'),
        ]),
      ),
    );
  }
}
