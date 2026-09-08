/// 上线形状的契约夹具(挂账 11)。
///
/// **这些 JSON 不是手写的**，是 `tests/app/test_wire_fixtures.py` 从真的
/// 路由输出里生成的。狗那头改了形状，这里就该红 —— 而不是等到真机上点开
/// 那一屏才发现，那时候狗在客户现场。
///
/// 所以这个文件里**不许出现手写的 JSON 字面量**：那等于在这头又抄了一份
/// 形状，抄的那份跟狗那头不一样的那天，两边都是绿的。
library;

import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/net/wire.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> _load(String name) => jsonDecode(
        File('test/fixtures/$name.json').readAsStringSync())
    as Map<String, dynamic>;

void main() {
  test('租约：倒计时是相对量', () {
    final m = _load('lease_state');
    expect(m['expires_in_ms'], isA<int>());
    expect(m['ttl_ms'], isA<int>());
    expect(m['mine'], isA<bool>());
    expect(m['max_sessions'], isA<int>());
    expect(m['remote_sessions'], isA<int>());
    expect(m['notice'], isA<String>());
  });

  test('运行快照：挂起点位解析得出来', () {
    final m = _load('run_snapshot');
    expect(m['state'], 'SUSPENDED');
    final s = m['suspended_at'] as Map<String, dynamic>;
    expect(s['waypoint_name'], isA<String>());
    expect(s['at_ms'], isA<int>());
    // pose 这一份夹具必须非空 —— 否则这条断言测不出 position/orientation
    // 到底有哪些键，Dart 那头解析错了也不会红。
    final pose = s['pose'] as Map<String, dynamic>;
    final position = pose['position'] as Map<String, dynamic>;
    expect(position['x'], isA<num>());
    expect(position['y'], isA<num>());
    final orientation = pose['orientation'] as Map<String, dynamic>;
    expect(orientation['w'], isA<num>());
  });

  test('视频健康：每一路都有 online', () {
    final cams = _load('video_health')['cameras'] as Map<String, dynamic>;
    expect(cams.keys, containsAll(<String>['front', 'back']));
    for (final v in cams.values) {
      final cam = v as Map<String, dynamic>;
      expect(cam['online'], isA<bool>());
      expect(cam['viewers'], isA<int>());
    }
  });

  test('留痕：切远程那条认得出来', () {
    final audit = _load('control_audit')['audit'] as List<dynamic>;
    expect(audit.map((e) => (e as Map<String, dynamic>)['kind']),
        contains('mode_switched'));
    expect(_load('control_audit')['max'], isA<int>());
  });

  test('盘况和身份都解析得出来', () {
    final storage = _load('storage');
    expect(storage, isNotEmpty);
    expect(storage['used_bytes'], isA<int>());
    expect(storage['total_bytes'], isA<int>());
    final identity = _load('identity');
    expect(identity, isNotEmpty);
    expect(identity['sn'], isA<String>());
  });

  test('三个视图都吃得下狗发的那份', () {
    expect(LeaseView.fromJson(_load('lease_state')).expiresInMs, isPositive);
    expect(VideoHealth.fromJson(_load('video_health')).online, isNotEmpty);
    expect(RunView.fromJson(_load('run_snapshot')).suspended, isTrue);
  });
}
