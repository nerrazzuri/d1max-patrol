/// 一个假站点（W00c4）：**真的 HTTPS**（`test/support/site_test.crt`，自签），回的报文取自
/// `test/fixtures/site_*.json` —— 那组文件是站点那头（Python）从真 API 取出来的，形状对不上
/// 那边先红。标识符用 ASCII（见 fake_dog.dart 的说明）。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

Map<String, dynamic> siteFixture(String name) =>
    jsonDecode(File('test/fixtures/$name.json').readAsStringSync()) as Map<String, dynamic>;

String testCertFingerprint() => File('test/support/site_test.fp').readAsStringSync().trim();

class FakeSite {
  late HttpServer _s;
  final List<({String method, String path, String? auth, dynamic body})> received = [];

  /// 原样的请求地址（含百分号编码与查询串）：告警键里的 `/`、`#` 必须编码成一段。
  final List<String> rawPaths = <String>[];

  /// 按路径覆盖状态码（默认 200）。
  final Map<String, int> statusCodes = <String, int>{};

  /// 真：事件流发完快照之后一声不吭（模拟半开的连接）。
  bool sseSilent = false;

  /// 按路径覆盖 200 的回包（模拟站点回了一份读不懂的东西）。
  final Map<String, Map<String, dynamic>> bodies = <String, Map<String, dynamic>>{};
  final StreamController<Map<String, dynamic>> sse = StreamController.broadcast();
  String role = 'guard';

  /// 遥控连接（W00c5c）：拒绝时回的原因（配合 [statusCodes]）；握手时回一个错的
  /// `Sec-WebSocket-Accept`；连上之后收到的每条消息；站点这头的那条 WebSocket（测试拿来关）。
  String teleopRefusal = '';

  /// 运行记录的照片（W00c5d）。
  List<int> photoBytes = <int>[0xFF, 0xD8, 1, 2, 3, 0xFF, 0xD9];
  bool teleopBadAccept = false;
  final List<Map<String, dynamic>> teleopGot = <Map<String, dynamic>>[];
  WebSocket? teleopWs;

  int get port => _s.port;
  String get url => 'https://127.0.0.1:$port';

  Future<void> start() async {
    final ctx = SecurityContext()
      ..useCertificateChain('test/support/site_test.crt')
      ..usePrivateKey('test/support/site_test.key');
    _s = await HttpServer.bindSecure(InternetAddress.loopbackIPv4, 0, ctx);
    _s.listen(_handle);
  }

  Future<void> close() async {
    // 先断连接（事件流那条长连接会一直挂着），再关事件源。
    await _s.close(force: true);
    unawaited(sse.close());
  }

  Future<void> _handle(HttpRequest req) async {
    final raw = await utf8.decodeStream(req);
    final body = raw.isEmpty ? null : jsonDecode(raw);
    final path = req.uri.path;
    rawPaths.add(req.requestedUri.toString());
    received.add((
      method: req.method,
      path: path,
      auth: req.headers.value(HttpHeaders.authorizationHeader),
      body: body
    ));
    final code = statusCodes[path] ?? 200;
    final resp = req.response;
    if (path.endsWith('/teleop')) {
      await _teleop(req, code);
      return;
    }
    if (path.endsWith('/preview.png') && code == 200) {
      resp.headers.contentType = ContentType('image', 'png');
      resp.add(fakePreviewPng);
      await resp.close();
      return;
    }
    if (path.startsWith('/api/runs/') && path.contains('/photos/') && code == 200) {
      resp.headers.contentType = ContentType('image', 'jpeg');
      resp.add(photoBytes);
      await resp.close();
      return;
    }
    if (code >= 300 && code < 400) {
      resp.statusCode = code;
      resp.headers.set(HttpHeaders.locationHeader, 'http://127.0.0.1:1/elsewhere');
      await resp.close();
      return;
    }
    if (code != 200) {
      resp.statusCode = code;
      resp.headers.contentType = ContentType.json;
      resp.write(jsonEncode(bodies[path] ?? siteFixture('site_error')));
      await resp.close();
      return;
    }
    if (path == '/api/events') {
      resp.headers.contentType = ContentType('text', 'event-stream');
      resp.bufferOutput = false;
      resp.write('data: ${jsonEncode({"kind": "snapshot", "robots": []})}\n\n');
      await resp.flush();
      if (sseSilent) {
        // 半开：快照之后什么都不发，连接也不关（心跳也没有）。
        await Future<void>.delayed(const Duration(seconds: 30));
      }
      await for (final item in sse.stream) {
        resp.write('data: ${jsonEncode(item)}\n\n');
        await resp.flush();
      }
      await resp.close();
      return;
    }
    if (path.contains('/video/') && !path.endsWith('/health')) {
      // 三帧假 JPEG（SOI … EOI），multipart 跟站点一个写法（最后一帧要等下一个边界才算完整）。
      resp.headers.set(HttpHeaders.contentTypeHeader, 'multipart/x-mixed-replace; boundary=frame');
      for (var i = 0; i < 3; i++) {
        final jpeg = <int>[0xFF, 0xD8, 0xFF, 0xE0, i, 1, 2, 3, 0xFF, 0xD9];
        resp.add(utf8.encode('--frame\r\nContent-Type: image/jpeg\r\n'
            'Content-Length: ${jpeg.length}\r\n\r\n'));
        resp.add(jpeg);
        resp.add(utf8.encode('\r\n'));
      }
      await resp.close();
      return;
    }
    Map<String, dynamic> out;
    if (path.endsWith('/halt')) {
      out = <String, dynamic>{
        'ack': <String, dynamic>{'result': 'accepted'}
      };
    } else if (bodies.containsKey(path)) {
      out = bodies[path]!;
    } else if (path.endsWith('/video/health')) {
      out = siteFixture('site_video_health');
    } else if (path == '/api/login') {
      out = Map.of(siteFixture('site_login'))..['role'] = role;
    } else if (path == '/api/robots') {
      out = siteFixture('site_robots');
    } else if (path.startsWith('/api/robots/') && req.method == 'GET') {
      out = siteFixture('site_robot');
    } else if (path == '/api/schedule') {
      out = siteFixture('site_schedule');
    } else if (path == '/api/incidents') {
      out = siteFixture('site_incidents');
    } else if (path == '/api/alerts') {
      out = siteFixture('site_alerts');
    } else if (path == '/api/watch/summary') {
      out = siteFixture('site_watch_summary');
    } else if (path == '/api/releases') {
      out = siteFixture('site_releases');
    } else if (path == '/api/maps') {
      out = siteFixture('site_maps');
    } else if (path == '/api/runs') {
      out = siteFixture('site_runs');
    } else if (RegExp(r'^/api/runs/\d+$').hasMatch(path)) {
      out = siteFixture('site_run');
    } else if (path.startsWith('/api/runs/') && path.contains('/review/')) {
      out = <String, dynamic>{'photo': path.split('/').last, 'verdict': 'x', 'reviewed': 1};
    } else if (path.startsWith('/api/runs/') && path.endsWith('/judge')) {
      out = <String, dynamic>{'findings': <dynamic>[]};
    } else if (path.startsWith('/api/alerts/')) {
      out = siteFixture('site_alert_ack');
    } else if (path == '/api/logout') {
      out = <String, dynamic>{'ok': true};
    } else {
      out = siteFixture('site_dispatch'); // 真站点派单的回执
    }
    resp.headers.contentType = ContentType.json;
    resp.write(jsonEncode(out));
    await resp.close();
  }

  Future<void> _teleop(HttpRequest req, int code) async {
    final resp = req.response;
    if (code != 200) {
      resp.statusCode = code;
      resp.headers.contentType = ContentType.json;
      resp.write(jsonEncode(<String, dynamic>{'error': teleopRefusal}));
      await resp.close();
      return;
    }
    if (teleopBadAccept) {
      resp.statusCode = HttpStatus.switchingProtocols;
      resp.headers
        ..set(HttpHeaders.connectionHeader, 'Upgrade')
        ..set(HttpHeaders.upgradeHeader, 'websocket')
        ..set('Sec-WebSocket-Accept', base64.encode(List<int>.filled(20, 0)));
      final sock = await resp.detachSocket();
      await sock.close();
      return;
    }
    final ws = await WebSocketTransformer.upgrade(req);
    teleopWs = ws;
    ws.add(jsonEncode(<String, dynamic>{
      'kind': 'granted',
      'lease_epoch': 1,
      'operator': 'gina',
      'max_vx': 0.5,
      'max_wz': 0.75,
      'frame_period_ms': 100,
    }));
    await for (final m in ws) {
      if (m is String) teleopGot.add(jsonDecode(m) as Map<String, dynamic>);
    }
  }
}

/// 一张真的 1×1 灰度 PNG（W00c6h 地图预览的假图：`Image.memory` 认得）。
final Uint8List fakePreviewPng = Uint8List.fromList(<int>[137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 0, 0, 0, 0, 58, 126, 155, 85, 0, 0, 0, 10, 73, 68, 65, 84, 120, 156, 99, 56, 11, 0, 0, 207, 0, 206, 160, 254, 41, 80, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130]);
