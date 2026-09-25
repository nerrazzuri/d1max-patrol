/// 跟**站点**说话（W00c4）。站点是庄园现场的一台主机（`packages/site-node`），手机登录站点、
/// 看狗、派巡检、叫停、看事件与排程；**不再连狗的热点**。遥控、视频这几屏在站点通路做出来
/// 之前仍走 `patrol_client.dart` 直连（「现场直连（过渡）」）。
///
/// **只认一张证书。** 站点用的是自己的 CA 签的证书，系统不认识它；这里不去装 CA，而是把站点
/// 服务证书的 SHA-256（`d1max-site fingerprint` 打出来）钉死：握手时拿到的证书指纹对得上才连，
/// 对不上一律断开（设计决定二 A）。`SecurityContext(withTrustedRoots: false)` 让**每一张**证书
/// 都走 `badCertificateCallback`，包括系统碰巧认识的——不然系统信任的证书会绕过钉扎。
///
/// **口令不落盘、不进日志**；令牌只在内存里。401 之后要人重新登录（站点的令牌有闲置 30 分钟、
/// 绝对 12 小时两道期限）。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

/// 跟站点说话时出的任何岔子。连不上、证书不对、超时是 0；站点回的 4xx/5xx 原样带状态码。
class SiteError implements Exception {
  final int status;
  final String message;
  const SiteError(this.status, this.message);

  @override
  String toString() => status == 0 ? message : '$message（$status）';
}

/// 证书的 SHA-256（DER），小写十六进制、不带冒号。跟 `d1max-site fingerprint` 同一个算法。
String certSha256(X509Certificate cert) => sha256.convert(cert.der).toString();

/// 把人照着抄的指纹规范化：去掉冒号、空格，转小写。
String normalizeFingerprint(String raw) {
  var t = raw.trim().toLowerCase();
  if (t.startsWith('sha256:')) t = t.substring(7); // 从站点输出里整段粘过来的
  return t.replaceAll(RegExp(r'[\s:]'), '');
}

/// 站点地址与指纹对不对。对 → null；不对 → 给人看的理由。添加站点时就查，别等到登录。
String? checkSiteEntry(String url, String fingerprint) {
  final Uri u;
  try {
    u = Uri.parse(url.trim());
  } on FormatException {
    return '地址写得不对';
  }
  if (u.scheme != 'https' || u.host.isEmpty) return '地址要是 https://主机:端口';
  if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(normalizeFingerprint(fingerprint))) {
    return '证书指纹要是 64 位十六进制（d1max-site fingerprint 打出来的那串）';
  }
  return null;
}

/// 登录之后的会话。
class SiteSession {
  final String token;
  final String name;

  /// admin / guard / owner。界面据此决定显示哪些按钮；**真正的权限在站点上**，这里只是不让
  /// 人按一个注定 403 的按钮。
  final String role;
  const SiteSession(this.token, this.name, this.role);

  bool get canDispatch => role == 'admin' || role == 'guard';
  bool get canAbort => role == 'admin' || role == 'guard' || role == 'owner';
}

/// 站点的接口面。界面只认它，测试可以换成假的。
abstract class SiteApi {
  SiteSession? get session;
  Future<SiteSession> login(String name, String password);
  Future<void> logout();
  Future<List<Map<String, dynamic>>> robots();
  Future<Map<String, dynamic>> robot(String id);
  Future<Map<String, dynamic>> patrol(String id, String missionId);
  Future<Map<String, dynamic>> abort(String id, String taskId);
  Future<Map<String, dynamic>> returnToStandby(String id);
  Future<Map<String, dynamic>> schedule();
  Future<List<Map<String, dynamic>>> incidents();
  Stream<Map<String, dynamic>> events();
  void close();
}

class SiteClient implements SiteApi {
  final Uri base;
  final String fingerprint;
  final Duration timeout;
  final HttpClient _io;
  @override
  SiteSession? session;

  /// 握手时见到的证书指纹（最近一次）。钉扎失败时拿它给人看「实际是哪一张」。
  String? lastSeenFingerprint;

  /// 站点等狗的回执最多 10 s（再加 5 s 余量）才回话；手机的超时要比它长，不然会把
  /// 「站点已经派出去了」报成超时，人再按一次就派了两趟。
  SiteClient(String baseUrl, String fingerprintRaw,
      {this.timeout = const Duration(seconds: 25)})
      : base = _parseBase(baseUrl),
        fingerprint = _checkedFingerprint(fingerprintRaw),
        _io = HttpClient(context: SecurityContext(withTrustedRoots: false)) {
    _io.connectionTimeout = const Duration(seconds: 5);
    _io.badCertificateCallback = (cert, host, port) {
      final got = certSha256(cert);
      lastSeenFingerprint = got;
      return got == fingerprint;
    };
  }

  static Uri _parseBase(String url) {
    final Uri u;
    try {
      u = Uri.parse(url.trim());
    } on FormatException {
      throw const SiteError(0, '站点地址写得不对');
    }
    if (u.scheme != 'https') {
      throw const SiteError(0, '站点地址要是 https://（口令不能明文过网）');
    }
    return u;
  }

  static String _checkedFingerprint(String raw) {
    final fp = normalizeFingerprint(raw);
    if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(fp)) {
      throw const SiteError(0, '证书指纹要是 64 位十六进制（d1max-site fingerprint 打出来的那串）');
    }
    return fp;
  }

  @override
  void close() => _io.close(force: true);

  // ------------------------------------------------------------ 请求

  Future<dynamic> _send(String method, String path, [Object? body]) async {
    try {
      return await _sendRaw(method, path, body).timeout(timeout);
    } on SiteError {
      rethrow;
    } on HandshakeException catch (e) {
      final seen = lastSeenFingerprint;
      throw SiteError(
          0,
          seen != null && seen != fingerprint
              ? '站点证书指纹对不上：实际是 $seen'
              : '跟站点的加密握手失败：$e');
    } on TimeoutException {
      throw const SiteError(0, '站点没回话（超时）');
    } on SocketException catch (e) {
      throw SiteError(0, '连不上站点：${e.message}');
    } on HttpException catch (e) {
      throw SiteError(0, '跟站点的连接断了：${e.message}');
    }
  }

  Future<dynamic> _sendRaw(String method, String path, Object? body) async {
    final req = await _io.openUrl(method, base.resolve(path));
    req.followRedirects = false; // 重定向可能把人带到别处（甚至 http://）：一律不跟
    final tok = session?.token;
    if (tok != null) {
      req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $tok');
    }
    if (body != null) {
      final raw = utf8.encode(jsonEncode(body));
      req.headers.contentType = ContentType.json;
      req.contentLength = raw.length;
      req.add(raw);
    }
    final resp = await req.close();
    final text = await utf8.decodeStream(resp);
    dynamic decoded;
    try {
      decoded = text.isEmpty ? <String, dynamic>{} : jsonDecode(text);
    } on FormatException {
      decoded = <String, dynamic>{'error': text};
    }
    if (resp.statusCode >= 300) { // 3xx 也算错：重定向一律不跟（见 _sendRaw）
      final msg = decoded is Map && decoded['error'] is String
          ? decoded['error'] as String
          : '站点回了 ${resp.statusCode}';
      if (resp.statusCode == 401) {
        session = null; // 令牌死了：要人重新登录
      }
      throw SiteError(resp.statusCode, msg);
    }
    return decoded;
  }

  Map<String, dynamic> _map(dynamic d) =>
      d is Map<String, dynamic> ? d : <String, dynamic>{};

  // ------------------------------------------------------------ 接口

  @override
  Future<SiteSession> login(String name, String password) async {
    final d = _map(await _send('POST', '/api/login',
        <String, dynamic>{'name': name, 'password': password}));
    final s = SiteSession(d['token'] as String? ?? '', d['name'] as String? ?? name,
        d['role'] as String? ?? '');
    if (s.token.isEmpty) {
      throw const SiteError(0, '站点没给令牌');
    }
    session = s;
    return s;
  }

  @override
  Future<void> logout() async {
    try {
      await _send('POST', '/api/logout', <String, dynamic>{});
    } finally {
      session = null;
    }
  }

  @override
  Future<List<Map<String, dynamic>>> robots() async {
    final d = _map(await _send('GET', '/api/robots'));
    return (d['robots'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
  }

  @override
  Future<Map<String, dynamic>> robot(String id) async =>
      _map(await _send('GET', '/api/robots/${Uri.encodeComponent(id)}'));

  @override
  Future<Map<String, dynamic>> patrol(String id, String missionId) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(id)}/patrol',
          <String, dynamic>{'mission_id': missionId}));

  @override
  Future<Map<String, dynamic>> abort(String id, String taskId) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(id)}/abort',
          <String, dynamic>{'task_id': taskId}));

  @override
  Future<Map<String, dynamic>> returnToStandby(String id) async => _map(await _send(
      'POST', '/api/robots/${Uri.encodeComponent(id)}/standby/return',
      <String, dynamic>{}));

  @override
  Future<Map<String, dynamic>> schedule() async =>
      _map(await _send('GET', '/api/schedule'));

  @override
  Future<List<Map<String, dynamic>>> incidents() async {
    final d = _map(await _send('GET', '/api/incidents'));
    return (d['incidents'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
  }

  /// SSE：第一帧是全量快照（`kind: snapshot`），之后是 status/event/ack/incident……
  /// 流断了（站点重启、令牌过期）就结束；调用方决定要不要重连。
  @override
  Stream<Map<String, dynamic>> events() async* {
    final req = await _io.openUrl('GET', base.resolve('/api/events'));
    req.followRedirects = false;
    final tok = session?.token;
    if (tok != null) {
      req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $tok');
    }
    final resp = await req.close();
    if (resp.statusCode != 200) {
      await resp.drain<void>();
      if (resp.statusCode == 401) session = null;
      throw SiteError(resp.statusCode, '事件流开不了');
    }
    try {
      await for (final line
          in resp.transform(utf8.decoder).transform(const LineSplitter())) {
        if (!line.startsWith('data: ')) continue;
        try {
          final d = jsonDecode(line.substring(6));
          if (d is Map<String, dynamic>) yield d;
        } on FormatException {
          continue;
        }
      }
    } on HttpException {
      return; // 连接断了（站点重启、令牌过期、我们自己关了）：流就此结束
    } on SocketException {
      return;
    }
  }
}
