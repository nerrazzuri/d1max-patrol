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

  test('有一路真在出画面时：anyLive 为真，since_frame_s 是个数', () {
    // `video_health` 那一份两台相机都是 false、`since_frame_s` 都是 null，
    // 于是「有画面」这条路和 `since_frame_s` 的类型在这头从来没被夹具驱动
    // 过。Task 11 的遥控屏拿 `anyLive` 决定摇杆灰不灰 —— 它恒为假的话，
    // 那块判断在真机之前不会有人发现。
    final live = _load('video_health_live');
    final cams = live['cameras'] as Map<String, dynamic>;
    final front = cams['front'] as Map<String, dynamic>;
    expect(front['online'], isTrue);
    expect(front['since_frame_s'], isA<num>(),
        reason: 'null 不带类型信息，这头就学不到它是个数');
    expect((cams['back'] as Map<String, dynamic>)['since_frame_s'], isNull,
        reason: '没起过泵的相机没有「多久之前」可言，null 才是如实描述');
    expect(VideoHealth.fromJson(live).anyLive, isTrue);
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

  test('租约：面板要的那几样都解析得出来', () {
    // 面板上每一格都对着这里的一个字段：谁拿着、是不是我、几个席位、
    // 那句「不核实」。少解一个，屏上就少一格，而少的那一格在真机之前
    // 没人会发现。
    final v = LeaseView.fromJson(_load('lease_state'));
    expect(v.mine, isTrue);
    expect(v.holderOperator, isNotEmpty);
    expect(v.holderRef, isNotNull);
    expect(v.maxSessions, isPositive);
    expect(v.remoteSessions, isNotNull);
    expect(v.notice, isNotEmpty, reason: '§6.3 那句话要原样上屏，解不出来就没得上');
  });

  test('租约：没人拿着的时候 expires_in_ms 是 null,不是 0', () {
    // 夹具里是「张三拿着」，`holder: null` 这条路夹具驱动不到 —— 所以照着
    // 夹具改这两个字段（狗那头 `LeaseState.to_wire()` 就是这么发的：没有
    // 持有者时 `holder` 和 `expires_in_ms` 一起是 null）。
    //
    // **`null` 和 0 不许合并**（`engine/lease.py:126-128`）：合并掉的话，
    // 一台谁也没在开的狗在屏上写着「已过期」。
    final m = <String, dynamic>{
      ..._load('lease_state'),
      'holder': null,
      'expires_in_ms': null,
      'mine': false,
    };
    final v = LeaseView.fromJson(m);
    expect(v.expiresInMs, isNull,
        reason: '塌成 0 的话「没人拿着」会被画成「已过期」');
    expect(v.held, isFalse);
    expect(v.mine, isFalse);
  });

  test('租约：challenger 拆成 ref 和 operator,跟 holder 一个样子', () {
    // 夹具里没人在抢（`challenger: null`），所以这条也照着夹具改一个字段。
    // 形状抄的是 `Holder.to_wire()` —— holder 和 challenger 在狗那头是同一
    // 个类型，这头也该是同一种拆法。
    final m = <String, dynamic>{
      ..._load('lease_state'),
      'challenger': <String, dynamic>{'ref': 'cc00cc00', 'operator': '王五'},
      'grace_in_ms': 15000,
    };
    final v = LeaseView.fromJson(m);
    expect(v.challenged, isTrue);
    expect(v.challengerRef, 'cc00cc00');
    expect(v.challengerOperator, '王五');
    expect(v.graceInMs, 15000);
    expect(LeaseView.fromJson(_load('lease_state')).challenged, isFalse,
        reason: '没人在抢的时候不许凭空长出一个挑战者');
  });

  test('三个视图都吃得下狗发的那份', () {
    expect(LeaseView.fromJson(_load('lease_state')).expiresInMs, isPositive);
    expect(VideoHealth.fromJson(_load('video_health')).online, isNotEmpty);
    expect(RunView.fromJson(_load('run_snapshot')).suspended, isTrue);
  });
}
