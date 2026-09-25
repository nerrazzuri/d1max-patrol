/// 一个假站点（W00c4）：**真的 HTTPS**（`test/support/site_test.crt`，自签），回的报文取自
/// `test/fixtures/site_*.json` —— 那组文件是站点那头（Python）从真 API 取出来的，形状对不上
/// 那边先红。标识符用 ASCII（见 fake_dog.dart 的说明）。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

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

  /// 按路径覆盖 200 的回包（模拟站点回了一份读不懂的东西）。
  final Map<String, Map<String, dynamic>> bodies = <String, Map<String, dynamic>>{};
  final StreamController<Map<String, dynamic>> sse = StreamController.broadcast();
  String role = 'guard';

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
    if (code >= 300 && code < 400) {
      resp.statusCode = code;
      resp.headers.set(HttpHeaders.locationHeader, 'http://127.0.0.1:1/elsewhere');
      await resp.close();
      return;
    }
    if (code != 200) {
      resp.statusCode = code;
      resp.headers.contentType = ContentType.json;
      resp.write(jsonEncode(siteFixture('site_error')));
      await resp.close();
      return;
    }
    if (path == '/api/events') {
      resp.headers.contentType = ContentType('text', 'event-stream');
      resp.bufferOutput = false;
      resp.write('data: ${jsonEncode({"kind": "snapshot", "robots": []})}\n\n');
      await resp.flush();
      await for (final item in sse.stream) {
        resp.write('data: ${jsonEncode(item)}\n\n');
        await resp.flush();
      }
      await resp.close();
      return;
    }
    Map<String, dynamic> out;
    if (bodies.containsKey(path)) {
      out = bodies[path]!;
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
}
