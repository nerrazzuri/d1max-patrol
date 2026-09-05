/// D1 Max 巡检 app 的入口。
///
/// 现在只有名册这一屏。**操作界面那一层还没定**：机器人自己身上已经跑着一个
/// 完整的网页版 app（`src/d1max_patrol/app/`，建图、标点、编任务、跑任务、
/// 判读、出报告全在里面）。手机浏览器能不能直接打开它，周一碰到机器的两分钟
/// 就有答案：
///
/// * 能 —— 这个 app 的活就是名册 + 连热点 + 存 PIN + 把那个页面嵌进来；
/// * 不能 —— 才需要在这里原生重做那二十几个接口的界面。
///
/// 在答案出来之前不去猜。先把无论哪边都跑不掉的那部分做扎实：名册。
library;

import 'package:flutter/material.dart';

import 'store/paths.dart';
import 'store/pin_vault.dart';
import 'store/registry_store.dart';
import 'store/secure_pin_vault.dart';
import 'ui/roster_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final store = await openRegistryStore();
  runApp(PatrolApp(store: store, vault: SecurePinVault()));
}

class PatrolApp extends StatelessWidget {
  final RegistryStore store;
  final PinVault vault;

  const PatrolApp({super.key, required this.store, required this.vault});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'D1 Max 巡检',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.teal),
      ),
      home: RosterPage(store: store, vault: vault),
    );
  }
}
