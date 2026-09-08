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

  /// 狗跟着这次应答发过来的那句话（`auth.OPERATOR_NOTICE`）。**原样留着。**
  ///
  /// [operatorVerified] 只说得出「核没核」，说不出「为什么不核、要可信的
  /// 身份该怎么办」—— 那句话在狗那头，措辞也归狗那头。这里硬编一句的话，
  /// 狗改了措辞手机不跟。
  final String notice;

  const Session({
    required this.token,
    required this.operator,
    required this.operatorVerified,
    required this.readonly,
    this.notice = '',
  });
}

class PatrolClient {
  final String baseUrl;
  final HttpClient _io;

  /// 只有自己造的 `HttpClient` 才是 `true`。调用方传进来的那个不归我们
  /// 所有——不许改它的超时，`close()` 也不许替调用方把它强关掉（N-1/N-2）。
  final bool _ownsIo;

  /// 罩住「发请求到读完整个响应体」这一整段的超时（B-2）。
  ///
  /// **不是只罩住等响应头。** `dart:io` 的 `HttpClientRequest.close()` 在
  /// 响应头一到就完成——狗发完头就沉默时，读 body 那一步没有别的东西兜底，
  /// 会挂到天荒地老，界面上连「狗没回话」都弹不出来。默认 10 秒；测试可以
  /// 注入一个短值，不必真等 10 秒去证明超时生效。
  final Duration timeout;

  String? _token;

  PatrolClient(this.baseUrl,
      {HttpClient? io, this.timeout = const Duration(seconds: 10)})
      : _io = io ?? HttpClient(),
        _ownsIo = io == null {
    if (_ownsIo) {
      // 狗在热点上，慢一点是常态，但不该等到人以为死机。
      // 只在自己造的连接上设——注入进来的那个不归我们管（N-1）。
      _io.connectionTimeout = const Duration(seconds: 5);
    }
  }

  String? get token => _token;

  void close() {
    // 只关自己造的那个（N-2）。
    if (_ownsIo) _io.close(force: true);
  }

  /// PIN 换 token。**PIN 不出这个函数。**
  ///
  /// 狗回的质询/应答缺了该有的字段时（连错热点、撞上强制门户都会长这样），
  /// **直接抛 `PatrolError`——绝不退回 `{'pin': pin}` 那条老路**去凑一个
  /// token。那条路狗会静默降级成只读凭证：遥控全部 403，报错指向权限而不
  /// 是「PIN 明文发出去了」，比一个裸的类型错误更难查（B-1）。
  Future<Session> unlock(String pin, {required String operator}) async {
    final ch = await _send('GET', '/api/auth/challenge', null, auth: false);
    final nonce = ch['nonce'];
    if (nonce is! String) {
      throw const PatrolError(
          0, '狗回的质询看不懂', '没有 nonce 字段。热点连对了吗，还是撞上了强制门户');
    }
    final got = await _send('POST', '/api/auth', <String, dynamic>{
      'nonce': nonce,
      'proof': proofFor(pin, nonce),
      'operator': operator,
    }, auth: false);
    final token = got['token'];
    if (token is! String) {
      throw const PatrolError(0, '狗回的应答看不懂', '没有 token 字段');
    }
    _token = token;
    return Session(
      token: token,
      operator: (got['operator'] as String?) ?? '',
      operatorVerified: got['operator_verified'] == true,
      readonly: got['readonly'] == true,
      notice: (got['notice'] as String?) ?? '',
    );
  }

  Future<Map<String, dynamic>> get(String path) => _send('GET', path, null);

  Future<Map<String, dynamic>> post(String path, [Object? body]) =>
      _send('POST', path, body);

  Future<Map<String, dynamic>> _send(String method, String path, Object? body,
      {bool auth = true}) async {
    try {
      final res = await _sendAndRead(method, path, body, auth: auth)
          .timeout(timeout);
      Map<String, dynamic> m = <String, dynamic>{};
      if (res.raw.isNotEmpty) {
        try {
          final parsed = jsonDecode(res.raw);
          if (parsed is Map<String, dynamic>) m = parsed;
        } on FormatException {
          // 非 JSON 的响应体解析不出来时，状态码仍然要报对（S-2）——
          // 下面照样走 >=400 分支，不许把这种情况压成「连不上狗」、
          // 状态码归零。
        }
      }
      if (res.status >= 400) {
        throw PatrolError(res.status,
            (m['error'] as String?) ?? '狗回了 ${res.status}',
            (m['detail'] as String?) ?? '');
      }
      return m;
    } on PatrolError {
      rethrow;
    } on TimeoutException {
      throw PatrolError(
          0, '狗没回话', '等了 ${timeout.inMilliseconds} 毫秒。看看还连着它的热点吗');
    } catch (e) {
      // **这里原样带 `$e` 的前提是请求体里从来没有长期凭证（S-3）。**
      // `unlock` 走的是质询-应答，body 里只有 nonce/proof/operator，PIN
      // 本身从不出现在任何请求里；异常信息里能带的至多是 URL/nonce 这类
      // 不敏感的东西。哪天有人往某个请求体里塞了敏感字段，这一行要跟着
      // 改，不能让那类内容原样出现在错误信息里。
      throw PatrolError(0, '连不上狗', '$e');
    }
  }

  /// 发请求、等响应头、读完整个响应体——三步算一个超时窗口（B-2）。
  ///
  /// **只适用于一次性的 JSON 响应。** `/api/events`（SSE）和
  /// `/api/video/<name>`（MJPEG）是**永不结束**的响应：这里最后那句
  /// `.join()` 要等 `onDone`，而它永远不来，于是外面那个 `.timeout` 会把
  /// 一条正常工作的长连接整体掐掉，还报成「狗没回话」——文案指向的是狗，
  /// 真正的原因却是拿错了方法。这两条路自己开 `HttpClient` 增量读
  /// （MJPEG 的做法见 `ui/widget/live_video.dart` 的 `mjpegFrames`）。
  Future<({int status, String raw})> _sendAndRead(
      String method, String path, Object? body,
      {required bool auth}) async {
    final req = await _io.openUrl(method, Uri.parse('$baseUrl$path'));
    // **不跟重定向。** 跟的话 302 会把下面那个 `Authorization: Bearer` 原样
    // 带到重定向目标去 —— 那是一条 token 外泄的路。狗从不发 3xx，关掉它
    // 不影响任何正常路径;真收到 3xx 的话现在会当成一个非 200 的状态码报
    // 出来，那也正是该有的反应。
    req.followRedirects = false;
    // token 走请求头，不走 cookie 也不走查询串。狗那头的理由写在
    // `auth.py` 开头：cookie 是浏览器自动带的，等于替人开狗。
    if (auth && _token != null) {
      req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $_token');
    }
    if (body != null) {
      req.headers.contentType = ContentType.json;
      req.write(jsonEncode(body));
    }
    final resp = await req.close();
    final raw = await utf8.decoder.bind(resp).join();
    return (status: resp.statusCode, raw: raw);
  }
}
