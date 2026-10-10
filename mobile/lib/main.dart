/// D1 Max 巡检 app 的入口。
///
/// **操作界面是原生做的，不套 WebView。** 遥控这一屏有三件事：视频要铺满、摇杆要跟手、
/// 松手要立刻停。在 WebView 里这三件都得过一层 JS 桥，而那层桥的延迟正好加在「松手到狗停住」
/// 这条最要命的路径上。
///
/// **手机只连站点，不直连狗**（总设计 §1；W00c5e 删掉了过渡用的「现场直连」）。打开就是站点列表
/// （`ui/site_page.dart`）：登录、看狗、派巡检、叫停、值守与告警、画面、遥控（决策 7：经站点、
/// 持租约）、记录与判读、地图、版本。站点地址与证书指纹存在 `store/site_store.dart`。
///
/// **普通页面竖屏横屏都行，遥控只许横屏**（App V2，决策 44；2026-09-26 起到 V2 之前是全横屏）。
/// 见 `ui/orientation.dart`。
///
/// **专业版界面、英文为主中文可选**（App V2）：主题在 `ui/theme.dart`，文字在 `l10n.dart`。
library;

import 'package:flutter/foundation.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'l10n.dart';
import 'store/paths.dart';
import 'store/site_store.dart';
import 'ui/site_page.dart';
import 'ui/theme.dart';

export 'ui/orientation.dart' show landscapeOnly, lockLandscape, unlockOrientation;

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  LicenseRegistry.addLicense(() async* {
    yield LicenseEntryWithLineBreaks(
        const <String>['Atkinson Hyperlegible Next', 'Atkinson Hyperlegible Mono'],
        await rootBundle.loadString('assets/fonts/OFL.txt'));
  });
  final langStore = await openLangStore();
  appLang.value = await langStore.load();
  runApp(PatrolApp(siteStore: await openSiteStore(), langStore: langStore));
}

class PatrolApp extends StatelessWidget {
  final SiteStore siteStore;

  /// 语言存哪儿；测试不给（不落盘）。
  final LangStore? langStore;

  const PatrolApp({super.key, required this.siteStore, this.langStore});

  @override
  Widget build(BuildContext context) {
    // 换语言整棵重建（key 跟着语言变）：每一页的文字都是画的时候取的，不重建就还是旧的。
    // 换语言的入口只在站点列表（根页面），所以重建不会把人从别的页面踢出来。
    return ValueListenableBuilder<AppLang>(
        valueListenable: appLang,
        builder: (context, lang, _) => MaterialApp(
              key: ValueKey<AppLang>(lang),
              title: tr('D1 Max Patrol', 'D1 Max 巡检'),
              // 桌面版（W15）：鼠标拖也能滚、能「下拉刷新」（Flutter 桌面默认只认触摸、触控板拖动）。
              scrollBehavior: const MaterialScrollBehavior().copyWith(dragDevices: {
                PointerDeviceKind.touch,
                PointerDeviceKind.mouse,
                PointerDeviceKind.trackpad,
                PointerDeviceKind.stylus,
              }),
              theme: proTheme(),
              home: SiteListPage(store: siteStore, langStore: langStore),
              // 刘海、三键导航栏（安卓 15 起强制全屏到边）：每一页都躲开。顶上归各页的 AppBar。
              builder: (context, child) =>
                  SafeArea(top: false, child: child ?? const SizedBox.shrink()),
            ));
  }
}
