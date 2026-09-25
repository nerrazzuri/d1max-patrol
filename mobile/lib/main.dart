/// D1 Max 巡检 app 的入口。
///
/// **操作界面是原生做的，不套 WebView。** 遥控这一屏有三件事：视频要铺满、摇杆要跟手、
/// 松手要立刻停。在 WebView 里这三件都得过一层 JS 桥，而那层桥的延迟正好加在「松手到狗停住」
/// 这条最要命的路径上。
///
/// **手机只连站点，不直连狗**（总设计 §1；W00c5e 删掉了过渡用的「现场直连」）。打开就是站点列表
/// （`ui/site_page.dart`）：登录、看狗、派巡检、叫停、值守与告警、画面、遥控（决策 7：经站点、
/// 持租约）、记录与判读、地图、版本。站点地址与证书指纹存在 `store/site_store.dart`。
library;

import 'package:flutter/material.dart';

import 'store/paths.dart';
import 'store/site_store.dart';
import 'ui/site_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(PatrolApp(siteStore: await openSiteStore()));
}

class PatrolApp extends StatelessWidget {
  final SiteStore siteStore;

  const PatrolApp({super.key, required this.siteStore});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'D1 Max 巡检',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.teal),
      ),
      home: SiteListPage(store: siteStore),
    );
  }
}
