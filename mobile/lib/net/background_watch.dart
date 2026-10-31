/// 后台值守（W17，决策 30）的开关。真正值守的是 Android 原生服务（`WatchService.kt`）：app 退到后台、
/// 被划掉之后照样连着站点、P1 弹通知、响铃那一档闹钟声响到有人处理。凭据是站点发的**值守令牌**
/// （只能看告警、30 天、注销即作废），不是登录会话（12 小时就到期）。
///
/// 桌面版、测试里没有后台服务：[supported] 为假，界面上不显示开关（桌面版本来就开着窗口值守）。
library;

import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

abstract class BackgroundWatch {
  bool get supported;

  /// 开始（或换一套凭据接着）值守。
  Future<void> start(
      {required String site, required String url, required String fingerprint, required String token});

  /// 停：服务那边顺手注销值守令牌（站点上当场作废）。
  Future<void> stop();

  Future<bool> running();
}

class ChannelBackgroundWatch implements BackgroundWatch {
  static const MethodChannel _ch = MethodChannel('d1max/watch');
  const ChannelBackgroundWatch();

  @override
  bool get supported => Platform.isAndroid;

  @override
  Future<void> start(
      {required String site,
      required String url,
      required String fingerprint,
      required String token}) async {
    if (!supported) return;
    await _ch.invokeMethod<bool>(
        'start', <String, String>{'site': site, 'url': url, 'fingerprint': fingerprint, 'token': token});
  }

  @override
  Future<void> stop() async {
    if (!supported) return;
    await _ch.invokeMethod<bool>('stop');
  }

  @override
  Future<bool> running() async => supported && (await _ch.invokeMethod<bool>('running') ?? false);
}

/// 后台值守开关的状态（W55：开关从机器人栏顶上挪到「设置」页）。机器人栏（`SiteRobotsPage`）一直活着，管真正的
/// 开、停（进站点自动开，离开站点就停）；设置页看它、点它。
class WatchControl extends ChangeNotifier {
  bool _on = false;
  bool _busy = false;
  Future<void> Function()? _toggle;

  bool get on => _on;
  bool get busy => _busy;

  /// 这台设备、这个站点能不能后台值守（桌面版没有）。
  bool get available => _toggle != null;

  void bind(Future<void> Function() toggle) {
    _toggle = toggle;
    notifyListeners();
  }

  /// 机器人栏关掉时解开（不通知：那时候整棵树在拆）。
  void unbind() => _toggle = null;

  void report({required bool on, required bool busy}) {
    if (on == _on && busy == _busy) return;
    _on = on;
    _busy = busy;
    notifyListeners();
  }

  Future<void> toggle() async => _toggle?.call();
}
