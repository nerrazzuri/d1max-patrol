// macOS 桌面版的沙箱权限（W15 外审阻断）：开了 App Sandbox 的 app 不声明出站网络就连不上站点 ——
// 编得过、起得来、一登录就被系统拒。Release 包必须带 network.client；不监听端口，Release 不许带
// network.server（Debug/Profile 带着是 Flutter 调试连接要用）。
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

bool _on(String plist, String key) =>
    RegExp('<key>${RegExp.escape(key)}</key>\\s*<true/>').hasMatch(plist);

void main() {
  test('Release 包能出站联网、不监听端口', () {
    final rel = File('macos/Runner/Release.entitlements').readAsStringSync();
    expect(_on(rel, 'com.apple.security.app-sandbox'), isTrue);
    expect(_on(rel, 'com.apple.security.network.client'), isTrue);
    expect(rel.contains('com.apple.security.network.server'), isFalse);
  });

  test('Debug/Profile 也能出站联网', () {
    final dbg = File('macos/Runner/DebugProfile.entitlements').readAsStringSync();
    expect(_on(dbg, 'com.apple.security.network.client'), isTrue);
  });
}
