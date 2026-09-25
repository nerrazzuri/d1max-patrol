// 站点客户端（W00c4）：钉证书、登录、带令牌、错误统一成 SiteError、SSE。
import 'dart:async';
import 'dart:convert';

import 'package:d1max_patrol/net/site_client.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:d1max_patrol/ui/widget/live_video.dart';

import 'support/fake_site.dart';

void main() {
  late FakeSite site;

  setUp(() async {
    site = FakeSite();
    await site.start();
  });

  tearDown(() async => site.close());

  test('指纹对得上才连：登录拿到令牌和角色，之后的请求带令牌', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    final s = await c.login('alice', 'correct-horse-battery');
    expect(s.token, isNotEmpty);
    expect(s.role, 'guard');
    final robots = await c.robots();
    expect(robots.first['robot_id'], 'A');
    expect(site.received.last.auth, 'Bearer ${s.token}');
    expect(site.received.first.body, {'name': 'alice', 'password': 'correct-horse-battery'});
    c.close();
  });

  test('指纹不对就断开，报出实际指纹', () async {
    final c = SiteClient(site.url, 'ab' * 32);
    await expectLater(
        c.login('alice', 'x'),
        throwsA(isA<SiteError>()
            .having((e) => e.status, 'status', 0)
            .having((e) => e.message, 'message', contains(testCertFingerprint()))));
    expect(site.received, isEmpty, reason: '口令一个字节都不许发出去');
    c.close();
  });

  test('指纹写法宽松：冒号、大写都认', () async {
    final fp = testCertFingerprint();
    final pretty = [for (var i = 0; i < 64; i += 2) fp.substring(i, i + 2)].join(':').toUpperCase();
    final c = SiteClient(site.url, pretty);
    await c.login('alice', 'x');
    c.close();
  });

  test('不是 https、指纹不是 64 位十六进制：当场拒绝', () {
    expect(() => SiteClient('http://127.0.0.1:1', testCertFingerprint()), throwsA(isA<SiteError>()));
    expect(() => SiteClient(site.url, 'not-a-fingerprint'), throwsA(isA<SiteError>()));
  });

  test('401 之后令牌作废，要重新登录；403 原样报', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('olga', 'x');
    site.statusCodes['/api/robots/A/patrol'] = 403;
    await expectLater(c.patrol('A', 'loop'),
        throwsA(isA<SiteError>().having((e) => e.status, 'status', 403)));
    expect(c.session, isNotNull);
    site.statusCodes['/api/robots'] = 401;
    await expectLater(c.robots(), throwsA(isA<SiteError>().having((e) => e.status, 'status', 401)));
    expect(c.session, isNull);
    c.close();
  });

  test('派巡检、叫停、回待命点的请求形状', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gina', 'x');
    await c.patrol('A', 'loop');
    await c.abort('A', 'task-9');
    await c.returnToStandby('A');
    final posts = site.received.where((r) => r.method == 'POST').skip(1).toList();
    expect(posts.map((r) => r.path), [
      '/api/robots/A/patrol',
      '/api/robots/A/abort',
      '/api/robots/A/standby/return',
    ]);
    expect(posts[0].body, {'mission_id': 'loop'});
    expect(posts[1].body, {'task_id': 'task-9'});
    c.close();
  });

  test('SSE：第一帧快照，之后照推', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gina', 'x');
    final frames = <Map<String, dynamic>>[];
    final sub = c.events().listen(frames.add);
    await Future<void>.delayed(const Duration(milliseconds: 300));
    site.sse.add({'kind': 'status', 'robot_id': 'A'});
    await Future<void>.delayed(const Duration(milliseconds: 300));
    expect(frames.map((f) => f['kind']), ['snapshot', 'status']);
    c.close(); // 先强关连接，事件流跟着结束
    unawaited(sub.cancel());
  });

  test('排程与事件账读得出来（夹具来自真站点）', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gina', 'x');
    final s = await c.schedule();
    expect(s['missions'], contains('loop'));
    final inc = await c.incidents();
    expect(inc.first['zone'], 'yard');
    c.close();
  });

  test('从站点输出整段粘过来的 sha256: 前缀也认；添加前就能查出坏地址与坏指纹', () async {
    final c = SiteClient(site.url, 'sha256:${testCertFingerprint()}');
    await c.login('alice', 'x');
    c.close();
    expect(checkSiteEntry(site.url, testCertFingerprint()), isNull);
    expect(checkSiteEntry('https://[x', testCertFingerprint()), isNotNull);
    expect(checkSiteEntry('http://h:1', testCertFingerprint()), isNotNull);
    expect(checkSiteEntry(site.url, 'abc'), isNotNull);
    expect(() => SiteClient('https://[x', testCertFingerprint()), throwsA(isA<SiteError>()));
  });

  test('重定向不跟，当错处理', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('alice', 'x');
    site.statusCodes['/api/robots'] = 302;
    await expectLater(c.robots(), throwsA(isA<SiteError>().having((e) => e.status, 's', 302)));
    expect(site.received.where((r) => r.path == '/elsewhere'), isEmpty);
    c.close();
  });

  test('超时比站点等回执的时间长；注销清掉会话', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    expect(c.timeout, greaterThan(const Duration(seconds: 15)));
    await c.login('alice', 'x');
    await c.logout();
    expect(c.session, isNull);
    expect(site.received.last.path, '/api/logout');
    c.close();
  });

  test('派单的回执用的是真站点的形状', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gina', 'x');
    final r = await c.patrol('A', 'loop');
    expect(r['ack']['result'], 'accepted');
    expect(r['task_id'], isA<String>());
    c.close();
  });

  test('真 HTTPS：告警键整个编码成一段，列全部带 all=1', () async {
    final site = FakeSite();
    await site.start();
    final c = SiteClient(site.url, testCertFingerprint());
    try {
      await c.login('gina', 'pw');
      final rows = await c.alerts();
      expect(rows.first.key, 'A/estop_pressed#1');
      expect(rows.first.level, 'P1');
      await c.ackAlert('A/estop_pressed#1');
      await c.alerts(all: true);
      final s = await c.watchSummary();
      expect((s['robots'] as List).length, 1);
      expect(site.rawPaths.any((u) => u.endsWith('/api/alerts/A%2Festop_pressed%231/ack')),
          isTrue,
          reason: site.rawPaths.join('\n'));
      expect(site.rawPaths.any((u) => u.endsWith('/api/alerts?all=1')), isTrue);
      expect(site.received.firstWhere((r) => r.path.endsWith('/ack')).body,
          isEmpty, reason: '确认人由站点取登录账号，手机不传名字');
    } finally {
      c.close();
      await site.close();
    }
  });

  test('站点回了一份没有 alerts 的东西：抛读不懂，绝不当成空名单', () async {
    final site = FakeSite()..bodies['/api/alerts'] = <String, dynamic>{'ok': true};
    await site.start();
    final c = SiteClient(site.url, testCertFingerprint());
    try {
      await c.login('gina', 'pw');
      await expectLater(c.alerts(), throwsA(isA<FormatException>()));
    } finally {
      c.close();
      await site.close();
    }
  });

  test('事件流半开：一段时间一行都没来就结束，调用方好重连', () async {
    final site = FakeSite()..sseSilent = true;
    await site.start();
    final c = SiteClient(site.url, testCertFingerprint(),
        sseIdleTimeout: const Duration(milliseconds: 800));
    try {
      await c.login('gina', 'pw');
      final got = <Map<String, dynamic>>[];
      final sw = Stopwatch()..start();
      await for (final f in c.events()) {
        got.add(f);
      }
      expect(sw.elapsed, lessThan(const Duration(seconds: 5)), reason: '要自己结束，不许一直挂着');
      expect(got.first['kind'], 'snapshot');
    } finally {
      c.close();
      await site.close();
    }
  });

  test('画面经站点：钉证书的客户端 + 站点路径 + 令牌拉得到 MJPEG；指纹不对连不上', () async {
    final site = FakeSite();
    await site.start();
    final c = SiteClient(site.url, testCertFingerprint());
    try {
      await c.login('gina', 'pw');
      final h = await c.videoHealth('A');
      expect((h['cameras'] as Map).keys, containsAll(<String>['front', 'back']));
      final io = c.pinnedClient();
      final frames = await mjpegFrames(Uri.parse('${c.baseUrl}/api/robots/A/video/front'),
              io: io, token: c.session!.token)
          .toList();
      io.close(force: true);
      expect(frames.length, greaterThanOrEqualTo(2));
      expect(site.received.last.auth, 'Bearer ${c.session!.token}');
      final bad = SiteClient(site.url, '00' * 32);
      final io2 = bad.pinnedClient();
      await expectLater(
          mjpegFrames(Uri.parse('${bad.baseUrl}/api/robots/A/video/front'), io: io2).toList(),
          throwsA(isA<Exception>()));
      io2.close(force: true);
      bad.close();
    } finally {
      c.close();
      await site.close();
    }
  });

  test('遥控经站点：真 WebSocket 握手带令牌，收 granted、发摇杆帧和放租；站点关了就报 ended', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gina', 'pw');
    final link = await c.teleop('A');
    final msgs = <Map<String, dynamic>>[];
    final sub = link.messages.listen(msgs.add);
    await _until(() => msgs.isNotEmpty);
    expect(msgs.first['kind'], 'granted');
    expect(site.received.last.path, '/api/robots/A/teleop');
    expect(site.received.last.auth, 'Bearer ${c.session!.token}');
    link.send(0.3, -0.2);
    link.release();
    link.send(0.1, 0);
    await _until(() => site.teleopGot.length >= 3);
    final f1 = site.teleopGot[0], f2 = site.teleopGot[2];
    expect([f1['vx'], f1['wz'], f1['seq']], [0.3, -0.2, 1]);
    expect(f2['seq'], 2, reason: '每帧带单调序号');
    expect(f1['t'], isA<num>());
    expect((f2['t'] as num) >= (f1['t'] as num), isTrue, reason: '发出时刻按本机单调钟');
    expect(site.teleopGot[1], {'kind': 'release'});
    site.teleopWs!.add(jsonEncode({'kind': 'ended', 'reason': 'taken_over'}));
    await site.teleopWs!.close();                    // 站点说了原因再关
    await _until(() => link.closed);
    expect(msgs.where((m) => m['kind'] == 'ended').toList(),
        [{'kind': 'ended', 'reason': 'taken_over'}],
        reason: '站点说过为什么结束了,就不再补一句「断了」盖掉它');
    link.send(0.3, 0); // 关了之后发帧：不抛、不发
    await sub.cancel();
    c.close();
  });

  test('本机关连接的握手还没完就再发帧、放租：不抛，也不往外发（W00c5 修复内部评审）', () async {
    // dart:io 调过 close() 之后再 add 会抛 StateError；关握手最长要等站点几秒。以前 _closed 要等握手
    // 完才置上，这段时间里页面销毁（dispose 里先 send 再 release 再 close）会在第一句就炸掉，放租、
    // 关连接都跳过。
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gina', 'pw');
    final link = await c.teleop('A');
    await _until(() => site.teleopWs != null);
    final closing = link.close();
    expect(() => link.send(0, 0), returnsNormally);
    expect(() => link.release(), returnsNormally);
    await closing;
    expect(link.closed, isTrue);
    expect(site.teleopGot, isEmpty, reason: '关了之后的帧、放租不往外发');
    c.close();
  });

  test('遥控被拒：状态码和站点给的原因原样抛出；401 作废令牌', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gus', 'pw');
    site.statusCodes['/api/robots/A/teleop'] = 409;
    site.teleopRefusal = 'gina 正在遥控 A';
    await expectLater(
        c.teleop('A'),
        throwsA(isA<SiteError>()
            .having((e) => e.status, 'status', 409)
            .having((e) => e.message, 'message', 'gina 正在遥控 A')));
    site.statusCodes['/api/robots/A/teleop'] = 401;
    await expectLater(c.teleop('A'), throwsA(isA<SiteError>()));
    expect(c.session, isNull);
    c.close();
  });

  test('管理员接管把理由放进查询串；握手回的 Accept 不对就不认', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('alice', 'pw');
    site.teleopBadAccept = true;
    await expectLater(c.teleop('A', takeoverReason: '手机没电了 #2'),
        throwsA(isA<SiteError>().having((e) => e.message, 'message', contains('握手'))));
    expect(site.rawPaths.last, endsWith('/api/robots/A/teleop?takeover=%E6%89%8B%E6%9C%BA%E6%B2%A1%E7%94%B5%E4%BA%86+%232'));
    c.close();
  });

  test('停车走站点的 halt，不走遥控连接', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('olga', 'pw');
    final r = await c.halt('A');
    expect(r['ack']['result'], 'accepted');
    expect(site.received.last.method, 'POST');
    expect(site.received.last.path, '/api/robots/A/halt');
    c.close();
  });

  test('运行记录：列表带狗号、一趟、照片（钉证书带令牌）、复核与重判的请求形状', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('gina', 'pw');
    final rows = await c.runs(robotId: 'A 1');
    expect(rows.single['mission'], '巡检一');
    expect(site.rawPaths.last, endsWith('/api/runs?robot=A+1'));
    final d = await c.run(1);
    expect((d['photos'] as List).length, 2);
    final name = (d['photos'] as List).first['name'] as String;
    final bytes = await c.runPhoto(1, name);
    expect(bytes, site.photoBytes);
    expect(site.received.last.auth, 'Bearer ${c.session!.token}');
    await c.reviewPhoto(1, 'P #1.jpg', 'abnormal', '有人翻墙');
    expect(site.rawPaths.last, endsWith('/api/runs/1/review/P%20%231.jpg'));
    expect(site.received.last.body, {'verdict': 'abnormal', 'note': '有人翻墙'});
    await c.judgeRun(1);
    expect(site.received.last.path, '/api/runs/1/judge');
    site.statusCodes['/api/runs/1/photos/$name'] = 404;
    await expectLater(c.runPhoto(1, name), throwsA(isA<SiteError>()));
    c.close();
  });

  test('站点回了一份没有 runs 的东西：抛读不懂，不当成「没有记录」', () async {
    final site = FakeSite()..bodies['/api/runs'] = <String, dynamic>{'ok': true};
    await site.start();
    final c = SiteClient(site.url, testCertFingerprint());
    try {
      await c.login('gina', 'pw');
      await expectLater(c.runs(), throwsA(isA<FormatException>()));
    } finally {
      c.close();
      await site.close();
    }
  });

  test('地图：目录、下发、录包、重建的请求形状', () async {
    final c = SiteClient(site.url, testCertFingerprint());
    await c.login('alice', 'pw');
    final d = await c.maps();
    expect((d['maps'] as List).first['map_id'], 'estate-1');
    await c.activateMap('A 1', 'estate-1', '8');
    expect(site.rawPaths.last, endsWith('/api/robots/A%201/map'));
    expect(site.received.last.body, {'map_id': 'estate-1', 'version': '8'});
    await c.mapping('A', 'start', name: 'yard');
    expect(site.received.last.body, {'action': 'start', 'name': 'yard'});
    await c.mapping('A', 'stop');
    expect(site.received.last.body, {'action': 'stop'});
    await c.buildMap('A', 'yard', 'estate-1', '9');
    expect(site.received.last.path, '/api/robots/A/map_build');
    expect(site.received.last.body, {'bag': 'yard', 'map_id': 'estate-1', 'version': '9'});
    c.close();
  });
}

Future<void> _until(bool Function() ok) async {
  final sw = Stopwatch()..start();
  while (!ok()) {
    if (sw.elapsed > const Duration(seconds: 5)) throw TimeoutException('等不到');
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}
