/// P1 推到手机（商业化 A6，决策 53：极光推送，只推标题）。
///
/// 手机进了站点（登录）就把这台手机的推送号报给站点，站点有没确认的 P1 就经极光推过来；
/// App 不在前台、被系统清掉也收得到（厂商通道要在极光控制台配好）。离开站点（注销）之前注销推送号。
/// 跟后台值守（W17）是两条路：值守是 App 自己连着站点，推送是站点经极光推；被省电杀掉时还有推送兜底。
///
/// AppKey 打包时给：Android 用 `-PjpushAppKey=…`（`android/app/build.gradle.kts`），
/// iOS 用 `--dart-define=JPUSH_APPKEY=…`。没给：[supported] 为假，什么都不做。
library;

import 'dart:io';

import 'package:jpush_flutter/jpush_flutter.dart';
import 'package:jpush_flutter/jpush_interface.dart';

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
      // 刚装好、第一次起来时极光还在注册：等几秒再要
      for (var i = 0; i < 5; i++) {
        final id = await j.getRegistrationID();
        if (id.isNotEmpty) return id;
        await Future<void>.delayed(const Duration(seconds: 2));
      }
    } catch (_) {
      // 没配 AppKey、插件起不来：不推（后台值守照旧）
    }
    return null;
  }
}

PushRegistrar defaultPush() =>
    Platform.isAndroid || Platform.isIOS ? JPushRegistrar() : const NoPush();
