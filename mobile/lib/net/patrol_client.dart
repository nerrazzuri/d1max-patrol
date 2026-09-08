/// 跟狗说话。**PIN 从不上网。**
///
/// 狗那头是质询-应答（`src/d1max_patrol/app/auth.py`）：先取一个一次性
/// nonce，再交 `HMAC-SHA256(PIN, nonce)` 的十六进制。`auth.py` 开头写得很
/// 直白：热点上报文人人可解。
///
/// **不许走 `{"pin": ...}` 那条老路。** 它也换得到 token，但在狗的热点上换到
/// 的是**只读**凭证 —— 遥控会全部 403，而报错指向权限，不指向「你把 PIN 明文
/// 发出去了」。
///
/// 用 `dart:io` 的 `HttpClient` 而不是 `package:http`：这个 app 只做 Android，
/// 少一个依赖少一次升级。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

/// 这一轮质询的证明：`HMAC-SHA256(PIN, nonce)` 的十六进制。
///
/// 跟 `auth.proof_for` 逐字对应。**跨语言的向量在
/// `test/fixtures/proof_vectors.json` 里**，那份文件是狗那头生成的。
String proofFor(String pin, String nonce) =>
    Hmac(sha256, utf8.encode(pin)).convert(utf8.encode(nonce)).toString();

/// 跟狗说话时出的任何岔子。
///
/// **连不上、超时、狗回了 4xx —— 全归一到这一个类型。** 让每一处调用点各自
/// 去认 `SocketException` / `TimeoutException`，早晚漏一处，而漏的那一处在
/// 手机上是一个红屏。
class PatrolError implements Exception {
  /// HTTP 状态码。压根没连上是 0。
  final int status;
  final String error;
  final String detail;

  const PatrolError(this.status, this.error, [this.detail = '']);

  @override
  String toString() => detail.isEmpty ? error : '$error：$detail';
}

/// 换到的那次会话。
class Session {
  final String token;
  final String operator;

  /// **狗记下名字但不核实**（§6.3）。界面上不许说成「已登录：张三」。
  final bool operatorVerified;
  final bool readonly;

  const Session({
    required this.token,
    required this.operator,
    required this.operatorVerified,
    required this.readonly,
  });
}

class PatrolClient {
  final String baseUrl;
  final HttpClient _io;
  String? _token;

  PatrolClient(this.baseUrl, {HttpClient? io}) : _io = io ?? HttpClient() {
    // 狗在热点上，慢一点是常态，但不该等到人以为死机。
    _io.connectionTimeout = const Duration(seconds: 5);
  }

  String? get token => _token;

  void close() => _io.close(force: true);

  /// PIN 换 token。**PIN 不出这个函数。**
  Future<Session> unlock(String pin, {required String operator}) async {
    final ch = await _send('GET', '/api/auth/challenge', null, auth: false);
    final nonce = ch['nonce'] as String;
    final got = await _send('POST', '/api/auth', <String, dynamic>{
      'nonce': nonce,
      'proof': proofFor(pin, nonce),
      'operator': operator,
    }, auth: false);
    _token = got['token'] as String;
    return Session(
      token: _token!,
      operator: (got['operator'] as String?) ?? '',
      operatorVerified: got['operator_verified'] == true,
      readonly: got['readonly'] == true,
    );
  }

  Future<Map<String, dynamic>> get(String path) => _send('GET', path, null);

  Future<Map<String, dynamic>> post(String path, [Object? body]) =>
      _send('POST', path, body);

  Future<Map<String, dynamic>> _send(String method, String path, Object? body,
      {bool auth = true}) async {
    try {
      final req = await _io.openUrl(method, Uri.parse('$baseUrl$path'));
      // token 走请求头，不走 cookie 也不走查询串。狗那头的理由写在
      // `auth.py` 开头：cookie 是浏览器自动带的，等于替人开狗。
      if (auth && _token != null) {
        req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $_token');
      }
      if (body != null) {
        req.headers.contentType = ContentType.json;
        req.write(jsonEncode(body));
      }
      final resp = await req.close().timeout(const Duration(seconds: 10));
      final raw = await utf8.decoder.bind(resp).join();
      final parsed = raw.isEmpty ? <String, dynamic>{} : jsonDecode(raw);
      final m = parsed is Map<String, dynamic> ? parsed : <String, dynamic>{};
      if (resp.statusCode >= 400) {
        throw PatrolError(resp.statusCode,
            (m['error'] as String?) ?? '狗回了 ${resp.statusCode}',
            (m['detail'] as String?) ?? '');
      }
      return m;
    } on PatrolError {
      rethrow;
    } on TimeoutException {
      throw const PatrolError(0, '狗没回话', '等了 10 秒。看看还连着它的热点吗');
    } catch (e) {
      throw PatrolError(0, '连不上狗', '$e');
    }
  }
}
