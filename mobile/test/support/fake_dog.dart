/// 一台假狗，给 `patrol_client_test.dart` 和 Task 11、12、13 的测试共用。
///
/// **起的是真的 `HttpServer`，不是 mock。** 这个任务要证的就是「请求真的
/// 长什么样」—— PIN 在不在请求体里、token 在不在请求头里 —— 把 `HttpClient`
/// mock 掉，恰好把要证的那件事一起 mock 掉了。
///
/// **标识符是 ASCII 的**：这台机器上的 Dart SDK（3.13.2）不接受非 ASCII
/// 字符出现在标识符里（`class 假狗 { ... }` 这类写法编译不过，
/// `dart analyze`/`dart run` 都报 `illegal_character`），中文只留在注释、
/// docstring 和字符串字面量里，跟仓库里其它 Dart 文件的写法一致。
library;

import 'dart:convert';
import 'dart:io';

/// 一台假狗。记下每个请求，好让测试事后翻。
class FakeDog {
  late HttpServer _s;
  final List<({String method, String path, String? auth, dynamic body})>
      received = <({String method, String path, String? auth, dynamic body})>[];
  final Map<String, dynamic> replies = <String, dynamic>{};

  /// 按路径分的状态码，默认 200。
  ///
  /// **不能是全局一个 int。** `unlock` 第一步是
  /// `GET /api/auth/challenge`，第二步才是 `POST /api/auth`——如果整台狗
  /// 只有一个状态，测「`/api/auth` 回 401」时会连带把 `/api/auth/challenge`
  /// 也打成 401，断言就对不上真正要测的那一步。
  final Map<String, int> statusCodes = <String, int>{};

  String get baseUrl => 'http://127.0.0.1:${_s.port}';

  Future<void> start() async {
    _s = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _s.listen((HttpRequest req) async {
      final raw = await utf8.decoder.bind(req).join();
      received.add((
        method: req.method,
        path: req.uri.path,
        auth: req.headers.value('authorization'),
        body: raw.isEmpty ? null : jsonDecode(raw),
      ));
      req.response.statusCode = statusCodes[req.uri.path] ?? 200;
      req.response.headers.contentType = ContentType.json;
      req.response
          .write(jsonEncode(replies[req.uri.path] ?? <String, dynamic>{}));
      await req.response.close();
    });
  }

  Future<void> stop() => _s.close(force: true);
}
