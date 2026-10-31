/// P1 推到手机（商业化 A6，决策 53：极光推送，只推标题）。
///
/// 手机进了站点（登录）就把这台手机的推送号报给站点，站点有没确认的 P1 就经极光推过来；
/// App 不在前台、被系统清掉也收得到（厂商通道要在极光控制台配好）。离开站点（注销）之前注销推送号。
/// 跟后台值守（W17）是两条路：值守是 App 自己连着站点，推送是站点经极光推；被省电杀掉时还有推送兜底。
///
/// AppKey 打包时给：Android 用 `-PjpushAppKey=…`（`android/app/build.gradle.kts`）。
/// iOS（H13）现在不推：要先配 APNs（苹果开发者账号、推送证书、aps-environment），之后用
/// `--dart-define=JPUSH_APPKEY=…` 给 AppKey，再放开 [JPushRegistrar.supported]。
///
/// **FCM**（RC 加固 H9，决策 56）：带 Google 服务的安卓手机（马来西亚的首个客户）走 FCM，跟极光并列。
/// Firebase 的四个参数打包时给（`--dart-define=FCM_APP_ID=… FCM_API_KEY=… FCM_PROJECT_ID=…
/// FCM_SENDER_ID=…`，都在 Firebase 控制台的 google-services.json 里）；没给就没有这一路。两个通道的
/// 推送号都报给站点，站点对同一部手机只走一个（先 FCM），不会推两遍。
library;

import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:jpush_flutter/jpush_flutter.dart';
import 'package:jpush_flutter/jpush_interface.dart';

import '../l10n.dart';

abstract class PushRegistrar {
  bool get supported;

  /// 报给站点的平台名（`android` / `ios` / `harmony`）。
  String get platform;

  /// 哪个通道的推送号（站点的 `provider`：`jpush` / `fcm`）。
  String get provider;

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
  String get provider => 'jpush';
  @override
  Future<String?> registrationId() async => null;
}

class JPushRegistrar implements PushRegistrar {
  JPushRegistrar();

  static const String _appKey = String.fromEnvironment('JPUSH_APPKEY');
  JPushFlutterInterface? _jpush;

  /// 只在安卓上开（H13）：iOS 工程还没配 APNs（没有推送权限、没有 aps-environment），极光 iOS 插件的
  /// `getRegistrationID` 在真机上没登录成功就**一直不回**（`JPushPlugin.m` 把回调挂起等登录），
  /// 推送登记会卡住、离开站点要白等 10 秒。配好 APNs 之后再放开 iOS。
  @override
  bool get supported => Platform.isAndroid;

  @override
  String get platform => Platform.isIOS ? 'ios' : 'android';

  @override
  String get provider => 'jpush';

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

/// FCM 的推送号：安卓原生那头（`MainActivity` 的 `d1max/push` 通道）用打包时给的四个参数起 Firebase、
/// 要令牌。手机上没有 Google 服务（国内的手机）、参数不对：拿不到，回 null，那台手机就只走极光。
class FcmRegistrar implements PushRegistrar {
  FcmRegistrar({Map<String, String>? options, bool? android, MethodChannel? channel})
      : _options = options ?? _built,
        _android = android ?? Platform.isAndroid,
        _channel = channel ?? const MethodChannel('d1max/push');

  static const Map<String, String> _built = <String, String>{
    'appId': String.fromEnvironment('FCM_APP_ID'),
    'apiKey': String.fromEnvironment('FCM_API_KEY'),
    'projectId': String.fromEnvironment('FCM_PROJECT_ID'),
    'senderId': String.fromEnvironment('FCM_SENDER_ID'),
  };

  final Map<String, String> _options;
  final bool _android;
  final MethodChannel _channel;

  /// 四个参数都给了才算配了（少一个 Firebase 起不来）。
  @override
  bool get supported => _android && _built.keys.every((k) => (_options[k] ?? '').isNotEmpty);

  @override
  String get platform => 'android';

  @override
  String get provider => 'fcm';

  @override
  Future<String?> registrationId() async {
    if (!supported) return null;
    try {
      final t = await _channel.invokeMethod<String>('fcmToken', _options);
      if (t != null && t.isNotEmpty) return t;
    } catch (_) {
      // 没有 Google 服务、参数不对、老包没有这个通道：这台手机不走 FCM
    }
    return null;
  }
}

/// iOS 先不推（H13：没配 APNs，见 [JPushRegistrar.supported]）；iOS 上 P1 只在 App 开着时收得到。
PushRegistrar defaultPush() => Platform.isAndroid ? JPushRegistrar() : const NoPush();

/// 极光之外还要登记的通道（现在只有 FCM，配了才有）。
List<PushRegistrar> defaultAlsoPush() {
  final f = FcmRegistrar();
  return f.supported ? <PushRegistrar>[f] : const <PushRegistrar>[];
}


// ------------------------------------------------------------ 一次登录的推送登记（A6 外审 F2、F3）

enum PushState { off, waiting, ready }

/// 一次进站点（登录）期间的推送登记：拿不到推送号、登记请求失败都**退避重试**，直到登记上或者离开站点；
/// 离开时先不许再登记、等在路上的那一次落定，登记上了的（包括离开那一刻才登记上的）注销。
/// 站点那头推送号还跟登记它的那次登录绑着（退出了就不推），这里的注销是尽力而为。
///
/// 通道可以不止一个（[also]，H9）：各拿各的号、各登记各的；**有一个登记上就算登记上了**，剩下的再试
/// [extraTries] 轮就不试了（那台手机上没有那个通道）。离开时登记上的全注销。
class PushSession {
  PushSession(this.registrar, {required this.register, required this.unregister,
      this.also = const <PushRegistrar>[], this.extraTries = 4,
      Duration Function(int attempt)? backoff})
      : _backoff = backoff ?? _defaultBackoff;

  final PushRegistrar registrar;
  final List<PushRegistrar> also;
  final int extraTries;
  final Future<void> Function(String id, String platform, String provider) register;
  final Future<void> Function(String id) unregister;
  final Duration Function(int attempt) _backoff;

  /// 界面上看：没开（不支持、没配）/ 还没登记上（带原因）/ 登记上了。
  final ValueNotifier<PushState> state = ValueNotifier(PushState.off);
  String reason = '';

  bool _stopped = false;
  final List<String> _registered = <String>[];
  Future<void>? _run;
  Completer<void>? _wake;

  static Duration _defaultBackoff(int n) => Duration(seconds: n < 6 ? 2 << n : 60);

  List<PushRegistrar> get _all =>
      <PushRegistrar>[registrar, ...also].where((r) => r.supported).toList();

  void start() {
    if (_all.isEmpty || _run != null) return;
    state.value = PushState.waiting;
    reason = tr('Registering for push notifications', '正在登记推送');
    _run = _loop();
  }

  Future<void> _loop() async {
    final pending = _all;
    for (var attempt = 0; !_stopped && pending.isNotEmpty; attempt++) {
      var why = '';
      for (final r in List<PushRegistrar>.of(pending)) {
        String? id;
        try {
          id = await r.registrationId();
        } catch (_) {}
        if (_stopped) return;
        if (id == null) {
          if (why.isEmpty) {
            why = tr('Push not registered yet. Retrying later',
                '推送尚未注册，稍后自动重试');
          }
          continue;
        }
        try {
          await register(id, r.platform, r.provider);
          // 离开那一刻才登记上的也记下：stop() 等这一次落定以后注销它
          _registered.add(id);
          pending.remove(r);
        } catch (e) {
          why = tr('Push registration failed: $e. Retrying later', '推送注册失败：$e，稍后自动重试');
        }
        if (_stopped) return;
      }
      if (_registered.isNotEmpty) {
        reason = '';
        state.value = PushState.ready;
        if (attempt >= extraTries) return;                 // 剩下的通道这台手机上没有：不试了
      } else {
        reason = why;
        state.value = PushState.waiting;
      }
      if (pending.isEmpty) return;
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
    final ids = List<String>.of(_registered);
    _registered.clear();
    for (final id in ids) {
      try {
        await unregister(id);
      } catch (_) {}
    }
    state.value = PushState.off;
  }
}
