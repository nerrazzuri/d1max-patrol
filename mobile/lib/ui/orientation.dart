/// 屏幕方向（App V2，决策 44）：**普通页面竖屏横屏都行**（跟着手机的自动旋转设置，关了自动旋转就是竖屏），
/// **遥控、建图遥控只许横屏**：两只手握着手机，两根杆在两边、画面在中间。
library;

import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

/// 横屏，两个方向都行（手机怎么拿都能转过来）。
const landscapeOnly = <DeviceOrientation>[
  DeviceOrientation.landscapeLeft,
  DeviceOrientation.landscapeRight,
];

Future<void> lockLandscape() => SystemChrome.setPreferredOrientations(landscapeOnly);

/// 空表 = 不限制，交还给系统（自动旋转开着就跟着转）。
Future<void> unlockOrientation() =>
    SystemChrome.setPreferredOrientations(const <DeviceOrientation>[]);

/// 包住只许横屏的页面：进来锁横屏，离开放开。
class LandscapeOnly extends StatefulWidget {
  final Widget child;
  const LandscapeOnly({super.key, required this.child});

  @override
  State<LandscapeOnly> createState() => _LandscapeOnlyState();
}

class _LandscapeOnlyState extends State<LandscapeOnly> {
  @override
  void initState() {
    super.initState();
    lockLandscape();
  }

  @override
  void dispose() {
    unlockOrientation();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
