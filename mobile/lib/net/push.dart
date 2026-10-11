/// P1 推到手机（商业化 A6，决策 53：极光推送，只推标题）。
///
/// 手机进了站点（登录）就把这台手机的推送号报给站点，站点有没确认的 P1 就经极光推过来；
/// App 不在前台、被系统清掉也收得到（厂商通道要在极光控制台配好）。离开站点（注销）之前注销推送号。
/// 跟后台值守（W17）是两条路：值守是 App 自己连着站点，推送是站点经极光推；被省电杀掉时还有推送兜底。
///
/// AppKey 打包时给：Android 用 `-PjpushAppKey=…`（`android/app/build.gradle.kts`），
/// iOS 用 `--dart-define=JPUSH_APPKEY=…`。没给：[supported] 为假，什么都不做。
library;

import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:jpush_flutter/jpush_flutter.dart';
import 'package:jpush_flutter/jpush_interface.dart';

import '../l10n.dart';

abstract class PushRegistrar {
  bool get supported;

  /// 报给站点的平台名（`android` / `ios` / `harmony`）。
  String get platform;

  /// 这台手机的推送号；拿不到（没配 AppKey、还没注册上）回 null。
  Future<String?> registrationId();
}

/// 桌面版、测试里：不推。
class NoPush implements PushRegistrar {
  const NoPush();
  @override
  bool get supported => false;
  @override
  String get platform => '';
  @override
  Future<String?> registrationId() async => null;
}

class JPushRegistrar implements PushRegistrar {
  JPushRegistrar();

  static const String _appKey = String.fromEnvironment('JPUSH_APPKEY');
  JPushFlutterInterface? _jpush;

  @override
  bool get supported => Platform.isAndroid || Platform.isIOS;

  @override
  String get platform => Platform.isIOS ? 'ios' : 'android';

  @override
  Future<String?> registrationId() async {
    if (!supported) return null;
    try {
      final j = _jpush ??= (JPush.newJPush()
        ..setup(appKey: _appKey, channel: 'developer-default', production: true));
      final id = await j.getRegistrationID();
      if (id.isNotEmpty) return id;                       // 还没注册上：PushSession 过一会儿再要
    } catch (_) {
      // 没配 AppKey、插件起不来：不推（后台值守照旧）
    }
    return null;
  }
}

PushRegistrar defaultPush() =>
    Platform.isAndroid || Platform.isIOS ? JPushRegistrar() : const NoPush();


// ------------------------------------------------------------ 一次登录的推送登记（A6 外审 F2、F3）

enum PushState { off, waiting, ready }

/// 一次进站点（登录）期间的推送登记：拿不到推送号、登记请求失败都**退避重试**，直到登记上或者离开站点；
/// 离开时先不许再登记、等在路上的那一次落定，登记上了的（包括离开那一刻才登记上的）注销。
/// 站点那头推送号还跟登记它的那次登录绑着（退出了就不推），这里的注销是尽力而为。
class PushSession {
  PushSession(this.registrar, {required this.register, required this.unregister,
      Duration Function(int attempt)? backoff})
      : _backoff = backoff ?? _defaultBackoff;

  final PushRegistrar registrar;
  final Future<void> Function(String id, String platform) register;
  final Future<void> Function(String id) unregister;
  final Duration Function(int attempt) _backoff;

  /// 界面上看：没开（不支持、没配）/ 还没登记上（带原因）/ 登记上了。
  final ValueNotifier<PushState> state = ValueNotifier(PushState.off);
  String reason = '';

  bool _stopped = false;
  String? _registered;
  Future<void>? _run;
  Completer<void>? _wake;

  static Duration _defaultBackoff(int n) => Duration(seconds: n < 6 ? 2 << n : 60);

  void start() {
    if (!registrar.supported || _run != null) return;
    state.value = PushState.waiting;
    reason = tr('Registering for push notifications', '正在登记推送');
    _run = _loop();
  }

  Future<void> _loop() async {
    for (var attempt = 0; !_stopped; attempt++) {
      String? id;
      try {
        id = await registrar.registrationId();
      } catch (_) {}
      if (_stopped) return;
      if (id == null) {
        reason = tr(
            'No push ID yet (JPush not configured, or still registering). Will retry',
            '推送号还没拿到（没配极光、或者还在注册），稍后再试');
      } else {
        try {
          await register(id, registrar.platform);
          // 离开那一刻才登记上的也记下：stop() 等这一次落定以后注销它
          _registered = id;
          reason = '';
          state.value = PushState.ready;
          return;
        } catch (e) {
          reason = tr('Push registration failed: $e. Will retry', '推送登记没成：$e，稍后再试');
        }
      }
      state.value = PushState.waiting;
      final w = _wake = Completer<void>();
      await Future.any([Future<void>.delayed(_backoff(attempt)), w.future]);
    }
  }

  /// 离开站点（注销会话之前）调：不再登记；在路上的那一次落定；登记上了的注销。
  Future<void> stop() async {
    _stopped = true;
    final w = _wake;
    if (w != null && !w.isCompleted) w.complete();
    final r = _run;
    if (r != null) {
      try {
        await r.timeout(const Duration(seconds: 10));
      } catch (_) {}
    }
    final id = _registered;
    _registered = null;
    if (id != null) {
      try {
        await unregister(id);
      } catch (_) {}
    }
    state.value = PushState.off;
  }
}
