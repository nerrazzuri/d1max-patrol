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
/// **只许横屏**（用户 2026-09-26）：遥控时两只手握着手机，两根杆在两边、画面在中间。安卓的
/// 清单里也锁了（`sensorLandscape`），app 起来之前的启动画面就是横的。
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'store/paths.dart';
import 'store/site_store.dart';
import 'ui/site_page.dart';

/// 横屏，两个方向都行（手机怎么拿都能转过来）。
const landscapeOnly = <DeviceOrientation>[
  DeviceOrientation.landscapeLeft,
  DeviceOrientation.landscapeRight,
];

Future<void> lockLandscape() => SystemChrome.setPreferredOrientations(landscapeOnly);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await lockLandscape();
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
      // 横屏时刘海、三键导航栏在左右两边（安卓 15 起强制全屏到边）：每一页都躲开。顶上归各页的 AppBar。
      builder: (context, child) => SafeArea(top: false, child: child ?? const SizedBox.shrink()),
    );
  }
}
