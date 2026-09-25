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
import 'dart:math';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';

import '../model/alert.dart';

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

  /// 确认、解决告警（W00c5a）：值班的人（保安、管理员）；业主只看。
  bool get canHandleAlerts => role == 'admin' || role == 'guard';

  /// 遥控（W00c5c，决策 7）：保安、管理员；业主没有（业主能按「停」）。
  bool get canTeleop => role == 'admin' || role == 'guard';

  /// 强制接管别人的遥控：只有管理员，而且要写理由。
  bool get canTakeover => role == 'admin';

  /// 判读、复核照片（W00c5d）：值班的人（保安、管理员）；业主只看。
  bool get canReview => role == 'admin' || role == 'guard';

  /// 下发地图、录包、重建（W00c5d 第二部分）：只有管理员。
  bool get canManageMaps => role == 'admin';
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

  /// 告警（W00c5a）。默认只要未解决的，[all] 为真时连已解决的一起（最近的在前）。
  /// **读不懂就抛 `FormatException`，绝不退回一份空名单**（见 `alertsFromWire`）。
  Future<List<Alert>> alerts({bool all = false});
  Future<Map<String, dynamic>> ackAlert(String key);
  Future<Map<String, dynamic>> resolveAlert(String key);
  Future<Map<String, dynamic>> watchSummary();

  /// 视频经站点（W00c5b）：每路画面健康（`{cameras: {front: {online, …}}}`）。
  Future<Map<String, dynamic>> videoHealth(String robotId);

  /// 画面走的站点地址，与一个**钉住站点证书**的新 `HttpClient`（调用方用完关掉）。
  String get baseUrl;
  HttpClient pinnedClient();

  /// 遥控（W00c5c）：开一条遥控连接（连接就是租约的载体）。被拒抛 [SiteError]，带站点给的原因。
  /// [takeoverReason] 非空 = 管理员强制接管。
  Future<TeleopLink> teleop(String robotId, {String takeoverReason = ''});

  /// 停车（W00c5c）：走站点的 `halt`，**不走遥控连接**。业主也能按。
  Future<Map<String, dynamic>> halt(String robotId);

  /// 运行记录（W00c5d，决策 8：证据都在站点）：最近的在前；[robotId] 给了只要这台狗的。
  /// 读不懂就抛 `FormatException`，不当成「没有记录」。
  Future<List<Map<String, dynamic>>> runs({String? robotId});

  /// 一趟：`{run: {...}, photos: [{name, waypoint, camera, finding, review}]}`。
  Future<Map<String, dynamic>> run(int id);

  /// 一张照片的字节（钉证书、带令牌）。
  Future<Uint8List> runPhoto(int id, String name);

  /// 让站点重判这一趟（保安、管理员）。
  Future<Map<String, dynamic>> judgeRun(int id);

  /// 人工复核一张照片：[verdict] 是 normal / abnormal / unclear。
  Future<Map<String, dynamic>> reviewPhoto(int id, String photo, String verdict, String note);

  /// 站点的地图目录与建图录包（W00c5d 第二部分）：`{maps: [...], bags: [...]}`。
  Future<Map<String, dynamic>> maps();

  /// 给一台狗下发一张图（狗后台下载、核对、载入，完了发事件）。
  Future<Map<String, dynamic>> activateMap(String robotId, String mapId, String version);

  /// 录包：[action] 是 start（要 [name]）/ stop。
  Future<Map<String, dynamic>> mapping(String robotId, String action, {String name = ''});

  /// 拿一个录包在狗上重建一张图。
  Future<Map<String, dynamic>> buildMap(String robotId, String bag, String mapId, String version);

  /// 站点的发布目录与每台狗在跑哪一版（W00c5d 第三部分）：`{releases: [...], robots: {id: 版本}}`。
  Future<Map<String, dynamic>> releases();

  /// 给一台狗装（install）、切（activate）、退（rollback）一版。
  Future<Map<String, dynamic>> releaseAction(String robotId, String action, {String name = ''});
  Stream<Map<String, dynamic>> events();
  void close();
}

final Random _rand = Random.secure();

/// 一条遥控连接（W00c5c）。连着 = 握着租约；断了 = 放租，狗停。
abstract class TeleopLink {
  /// 站点发来的：`granted`（租约代次、限速）、`video`（画面有没有）、`ended`（为什么结束）。
  Stream<Map<String, dynamic>> get messages;
  bool get closed;

  /// 一帧摇杆（m/s、rad/s）。站点会再夹一次限速；狗那头也夹。
  void send(double vx, double wz);

  /// 我不遥控了（放租）。
  void release();
  Future<void> close();
}

class WsTeleopLink implements TeleopLink {
  WsTeleopLink(this._ws, this._io) {
    _ws.listen((dynamic m) {
      if (m is! String) return;
      try {
        final d = jsonDecode(m);
        if (d is Map<String, dynamic>) {
          if (d['kind'] == 'ended') _endedSeen = true;
          _ctl.add(d);
        }
      } on FormatException {
        return;
      }
    }, onDone: _done, onError: (Object _) => _done(), cancelOnError: true);
  }

  final WebSocket _ws;
  final HttpClient _io;
  final StreamController<Map<String, dynamic>> _ctl =
      StreamController<Map<String, dynamic>>.broadcast();
  bool _closed = false;
  bool _endedSeen = false;

  /// 帧的序号与发出时刻（本机单调钟，毫秒）：站点按它丢掉乱序、积压的帧（W00c5c 内部评审）。
  int _seq = 0;
  final Stopwatch _clock = Stopwatch()..start();

  void _done() {
    if (_closed) return;
    _closed = true;
    // 站点已经说了为什么结束（被接管、有人按了停……），就不再补一句笼统的「断了」盖掉它。
    if (!_endedSeen) _ctl.add(<String, dynamic>{'kind': 'ended', 'reason': 'disconnected'});
    unawaited(_ctl.close());
    _io.close(force: true);
  }

  @override
  Stream<Map<String, dynamic>> get messages => _ctl.stream;
  @override
  bool get closed => _closed;

  @override
  void send(double vx, double wz) {
    if (_closed) return;
    _seq++;
    _ws.add(jsonEncode(<String, dynamic>{
      'seq': _seq,
      't': _clock.elapsedMicroseconds / 1000.0,
      'vx': vx,
      'wz': wz,
    }));
  }

  @override
  void release() {
    if (_closed) return;
    _ws.add(jsonEncode(<String, dynamic>{'kind': 'release'}));
  }

  @override
  Future<void> close() async {
    await _ws.close(1000, 'bye');
    _done();
  }
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

  /// 事件流多久一行都没来就当断了（站点 15 s 一行心跳，默认给三倍余量）。
  final Duration sseIdleTimeout;

  /// 站点等狗的回执最多 10 s（再加 5 s 余量）才回话；手机的超时要比它长，不然会把
  /// 「站点已经派出去了」报成超时，人再按一次就派了两趟。
  SiteClient(String baseUrl, String fingerprintRaw,
      {this.timeout = const Duration(seconds: 25),
      this.sseIdleTimeout = const Duration(seconds: 45)})
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

  @override
  Future<List<Alert>> alerts({bool all = false}) async =>
      alertsFromWire(_map(await _send('GET', all ? '/api/alerts?all=1' : '/api/alerts')));

  /// 告警键 `robot/kind#seq` 里有 `/` 和 `#`：整个键编码成一段。确认人由站点取登录账号。
  @override
  Future<Map<String, dynamic>> ackAlert(String key) async => _map(await _send(
      'POST', '/api/alerts/${Uri.encodeComponent(key)}/ack', <String, dynamic>{}));

  @override
  Future<Map<String, dynamic>> resolveAlert(String key) async => _map(await _send(
      'POST', '/api/alerts/${Uri.encodeComponent(key)}/resolve', <String, dynamic>{}));

  @override
  Future<Map<String, dynamic>> watchSummary() async =>
      _map(await _send('GET', '/api/watch/summary'));

  @override
  Future<List<Map<String, dynamic>>> runs({String? robotId}) async {
    final q = robotId == null ? '' : '?robot=${Uri.encodeQueryComponent(robotId)}';
    final d = _map(await _send('GET', '/api/runs$q'));
    final rows = d['runs'];
    if (rows is! List) throw const FormatException('站点回的运行记录里没有 runs');
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  @override
  Future<Map<String, dynamic>> run(int id) async => _map(await _send('GET', '/api/runs/$id'));

  /// 照片最多这么大（字节）：站点回的东西不照单全收。
  static const int maxPhotoBytes = 20 * 1024 * 1024;

  @override
  Future<Uint8List> runPhoto(int id, String name) async {
    final req = await _io
        .openUrl('GET', base.resolve('/api/runs/$id/photos/${Uri.encodeComponent(name)}'))
        .timeout(timeout);
    req.followRedirects = false;
    final tok = session?.token;
    if (tok != null) req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $tok');
    final resp = await req.close().timeout(timeout);
    if (resp.statusCode != 200) {
      await resp.drain<void>();
      if (resp.statusCode == 401) session = null;
      throw SiteError(resp.statusCode, '照片拿不到');
    }
    final out = BytesBuilder(copy: false);
    await for (final chunk in resp.timeout(timeout)) {
      out.add(chunk);
      if (out.length > maxPhotoBytes) throw const SiteError(0, '照片太大');
    }
    return out.takeBytes();
  }

  @override
  Future<Map<String, dynamic>> maps() async {
    final d = _map(await _send('GET', '/api/maps'));
    if (d['maps'] is! List || d['bags'] is! List) {
      throw const FormatException('站点回的地图目录里没有 maps / bags');
    }
    return d;
  }

  @override
  Future<Map<String, dynamic>> activateMap(String robotId, String mapId, String version) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/map',
          <String, dynamic>{'map_id': mapId, 'version': version}));

  @override
  Future<Map<String, dynamic>> mapping(String robotId, String action, {String name = ''}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/mapping',
          <String, dynamic>{'action': action, if (name.isNotEmpty) 'name': name}));

  @override
  Future<Map<String, dynamic>> buildMap(
          String robotId, String bag, String mapId, String version) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/map_build',
          <String, dynamic>{'bag': bag, 'map_id': mapId, 'version': version}));

  @override
  Future<Map<String, dynamic>> releases() async {
    final d = _map(await _send('GET', '/api/releases'));
    if (d['releases'] is! List || d['robots'] is! Map) {
      throw const FormatException('站点回的发布目录里没有 releases / robots');
    }
    return d;
  }

  @override
  Future<Map<String, dynamic>> releaseAction(String robotId, String action,
          {String name = ''}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/release',
          <String, dynamic>{'action': action, if (name.isNotEmpty) 'name': name}));

  @override
  Future<Map<String, dynamic>> judgeRun(int id) async =>
      _map(await _send('POST', '/api/runs/$id/judge', <String, dynamic>{}));

  @override
  Future<Map<String, dynamic>> reviewPhoto(
          int id, String photo, String verdict, String note) async =>
      _map(await _send('POST', '/api/runs/$id/review/${Uri.encodeComponent(photo)}',
          <String, dynamic>{'verdict': verdict, 'note': note}));

  @override
  Future<Map<String, dynamic>> videoHealth(String robotId) async => _map(
      await _send('GET', '/api/robots/${Uri.encodeComponent(robotId)}/video/health'));

  @override
  String get baseUrl => base.toString().replaceAll(RegExp(r'/$'), '');

  @override
  Future<TeleopLink> teleop(String robotId, {String takeoverReason = ''}) async {
    final uri = base.resolve('/api/robots/${Uri.encodeComponent(robotId)}/teleop').replace(
        queryParameters: takeoverReason.isEmpty ? null : {'takeover': takeoverReason});
    final io = pinnedClient()..connectionTimeout = const Duration(seconds: 5);
    // 自己握手（不用 WebSocket.connect）：被拒的时候要拿到站点给的状态码和原因（「gina 正在遥控」）。
    final key = base64.encode(List<int>.generate(16, (_) => _rand.nextInt(256)));
    try {
      final req = await io.openUrl('GET', uri).timeout(timeout);
      req.followRedirects = false;
      req.headers
        ..set(HttpHeaders.connectionHeader, 'Upgrade')
        ..set(HttpHeaders.upgradeHeader, 'websocket')
        ..set('Sec-WebSocket-Key', key)
        ..set('Sec-WebSocket-Version', '13');
      final tok = session?.token;
      if (tok != null) req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $tok');
      final resp = await req.close().timeout(timeout);
      if (resp.statusCode != 101) {
        var why = '遥控开不了';
        try {
          final d = jsonDecode(await utf8.decoder.bind(resp).join());
          if (d is Map && d['error'] is String) why = d['error'] as String;
        } on FormatException {
          // 没有原因就只报状态码
        }
        if (resp.statusCode == 401) session = null;
        io.close(force: true);
        throw SiteError(resp.statusCode, why);
      }
      final want = base64.encode(
          sha1.convert(utf8.encode('${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11')).bytes);
      if (resp.headers.value('Sec-WebSocket-Accept') != want) {
        io.close(force: true);
        throw const SiteError(0, '站点的遥控握手不对');
      }
      final sock = await resp.detachSocket();
      final ws = WebSocket.fromUpgradedSocket(sock, serverSide: false)
        // 3 s（跟站点判死一样长）：4G 上一次 pong 慢了 1 s 就断，遥控会三天两头掉。
        ..pingInterval = const Duration(seconds: 3);
      return WsTeleopLink(ws, io);
    } on SocketException catch (e) {
      io.close(force: true);
      throw SiteError(0, '连不上站点：${e.message}');
    } on HandshakeException {
      io.close(force: true);
      throw const SiteError(0, '站点的证书对不上');
    } on TimeoutException {
      io.close(force: true);
      throw const SiteError(0, '连站点超时');
    } on HttpException catch (e) {
      // 握手半路断了（站点重启、4G 切基站）：也要落成 SiteError，页面才会说出来、不卡在「正在拿遥控」。
      io.close(force: true);
      throw SiteError(0, '遥控握手没完成：${e.message}');
    } on WebSocketException catch (e) {
      io.close(force: true);
      throw SiteError(0, '遥控握手没完成：${e.message}');
    }
  }

  @override
  Future<Map<String, dynamic>> halt(String robotId) async => _map(await _send(
      'POST', '/api/robots/${Uri.encodeComponent(robotId)}/halt', <String, dynamic>{}));

  @override
  HttpClient pinnedClient() {
    final io = HttpClient(context: SecurityContext(withTrustedRoots: false));
    io.badCertificateCallback = (cert, host, port) => certSha256(cert) == fingerprint;
    return io;
  }

  /// SSE：第一帧是全量快照（`kind: snapshot`），之后是 status/event/ack/incident/alert……
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
      // **半开的连接要看得出来**：站点每 15 s 发一行心跳（注释行）；[sseIdleTimeout] 里一行都没来
      // 就当断了、结束流，调用方照常重连。不这样的话，换网之后连接半开，屏上长期停在旧数据上。
      await for (final line in resp
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .timeout(sseIdleTimeout, onTimeout: (sink) => sink.close())) {
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
