/// 跟**站点**说话（W00c4）。站点是庄园现场的一台主机（`packages/site-node`），手机登录站点、
/// 看狗、派巡检、叫停、看事件与排程、画面、遥控、记录、地图、版本；**手机从不连狗**
/// （W00c5e 删掉了过渡用的「现场直连」）。
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

  /// 站点回的整个错误体（除了 `error` 还可能带别的字段，比如标原点的 `name_taken`、`data`）。
  final Map<String, dynamic> body;
  const SiteError(this.status, this.message, {this.body = const <String, dynamic>{}});

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

  /// 给人看的真名（W20 操作人实名）；站点没设就是账号名。
  final String displayName;
  const SiteSession(this.token, this.name, this.role, {this.displayName = ''});

  bool get canDispatch => role == 'admin' || role == 'guard';

  /// 确认、解决告警（W00c5a）：值班的人（保安、管理员）；业主也行（W20：在家时自己看到了点「我知道了」）。
  bool get canHandleAlerts => role == 'admin' || role == 'guard' || role == 'owner';

  /// 切到布防（W20）：谁都能。
  bool get canArm => role == 'admin' || role == 'guard' || role == 'owner';

  /// 切到在家、访客（撤防，W20）：业主、管理员；保安不能。
  bool get canSetMode => role == 'admin' || role == 'owner';

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

/// 这台狗报的能力里有没有这种任务（`robots()`、`robot()` 给的那一份）。没这个能力的按钮不显示：
/// 狗会回 `unsupported`，按了也白按（W00c5d 第二部分内部评审）。
bool robotCan(Map<String, dynamic>? view, String task) {
  final caps = view?['capabilities'];
  final tasks = caps is Map ? caps['tasks'] : null;
  return tasks is Map && tasks.containsKey(task);
}

/// 一条排程什么时候跑（W14）：一天一轮就是 `at`；重复的是「22:00–02:00 每 60 分钟」；写了回哪个待命点也说。
String scheduleWhen(Map<String, dynamic> e) {
  final every = e['every_min'];
  final when = every is num && every > 0 ? '${e['at']}–${e['until']} 每 $every 分钟' : '${e['at']}';
  final stb = e['standby'];
  return stb is String && stb.isNotEmpty ? '$when，巡完回 $stb' : when;
}

/// 布防模式说人话（W20）：`布防` / `在家` / `访客（到 21:30）`。
String modeText(Map<String, dynamic> m) {
  final label = switch (m['mode']) {
    'armed' => '布防',
    'home' => '在家',
    'visitor' => '访客',
    _ => '${m['mode']}',
  };
  final until = m['until_ms'];
  if (m['mode'] != 'visitor' || until is! num) return label;
  final t = DateTime.fromMillisecondsSinceEpoch(until.toInt());
  String two(int v) => v.toString().padLeft(2, '0');
  return '$label（到 ${two(t.hour)}:${two(t.minute)}）';
}

/// 一条事件的去向说人话（W16）。
String incidentOutcomeText(String outcome) => switch (outcome) {
      'dispatched' => '已出动',
      'dispatching' => '正在派',
      'merged' => '并进已出动的',
      'duplicate' => '重复（同一条事件）',
      'unmapped' => '防区没映射到拦截点，没狗去',
      'no_robot' => '没有能派的狗',
      'dispatch_failed' => '派了，狗没收',
      'ignored_type' => '不是入侵，只记账',
      'disarmed' => '撤防中，只记录（没派狗、没响铃）',
      'unreachable' => '拦截点走不到，没狗去',
      _ => outcome,
    };

/// 这台狗能不能「在这儿设待命点」（W13a）：代理报了 `mark_home.standby`。老代理不认、会当成标原点。
bool robotCanStandbyHere(Map<String, dynamic>? view) {
  final caps = view?['capabilities'];
  final tasks = caps is Map ? caps['tasks'] : null;
  final m = tasks is Map ? tasks['mark_home'] : null;
  return m is Map && m['standby'] == true;
}

/// 这台狗要不要人现场监护（W00c6i）：避障真机验收之前真狗是 `supervised` —— goto、巡检只在有人
/// 用「我在现场监护」时才收。**读不到按要人监护算**（跟站点一个口径）。没有 goto/巡检能力的狗谈不上。
bool robotNeedsSupervision(Map<String, dynamic>? view) {
  final caps = view?['capabilities'];
  final tasks = caps is Map ? caps['tasks'] : null;
  if (tasks is! Map || !(tasks.containsKey('goto') || tasks.containsKey('patrol'))) return false;
  for (final k in const ['patrol', 'goto']) {
    final t = tasks[k];
    if (t is Map && t['autonomy'] == 'autonomous') return false;
  }
  return true;
}

/// 狗拒收的原因给人看的话（回执里的 `reason`）。认不出的原样给。
String ackReasonText(String reason) {
  // 定位不够好（W00c6f 标原点）：狗在冒号后面说了为什么。
  if (reason.startsWith('loc_poor')) {
    final why = reason.contains(':') ? reason.substring(reason.indexOf(':') + 1).trim() : '';
    return '定位不够好${why.isEmpty ? '' : '：$why'}';
  }
  // 配了定位器的狗（W09a）：设位置要经定位器。
  if (reason.startsWith('localizer_unavailable')) {
    return '定位器没连上或没回：${reason.substring(reason.indexOf(':') + 1).trim()}';
  }
  if (reason.startsWith('localizer_refused')) {
    return '定位器没接这个位置：${reason.substring(reason.indexOf(':') + 1).trim()}';
  }
  // 狗说了在忙什么（W00c6f 标原点：在跑任务、在换图……）。
  if (reason.startsWith('busy:')) return '它正忙着别的：${reason.substring(5).trim()}';
  if (reason.startsWith('persist_failed')) return '狗上记不下（盘满了？）：${reason.substring(14).replaceFirst(':', '').trim()}';
  return _ackReasonText(reason);
}

String _ackReasonText(String reason) => switch (reason) {
      'unsupervised' => '它要人现场监护：先打开「我在现场监护」再派',
      'busy' => '它正忙着别的',
      'expired' => '命令到狗那儿已经过期了（狗的钟可能不准，或者网络太慢）',
      'stale_epoch' => '站点和狗的控制代次对不上，刷新一下再试',
      'stale_seq' => '这是一条迟到的旧心跳，狗没认',
      'halting' => '它正在叫停，稍后再派',
      'moving' => '它在走：停稳了再试',
      'no_home' => '这张图没标过原点：输坐标',
      'map_mismatch' => '狗刚换了图：刷新一下再设',
      'no_map' => '狗没加载地图',
      'odom_invalid' => '狗的里程读不到（旁路进程断了？）',
      _ => reason,
    };

/// 定位状态的人话（W00c6e，里程锚定）：单狗视图里的 `loc`（站点从狗的遥测取的）。没有就是空串。
String locText(Map<String, dynamic>? loc) {
  if (loc == null) return '';
  final reason = '${loc['reason'] ?? ''}';
  if (loc['localizer'] == 'bridge') return _bridgeLocText(loc, reason);
  if (loc['anchored'] != true) return '定位：没设位置 —— ${reason.isEmpty ? '点「设位置」' : reason}';
  if (reason.isNotEmpty) return '定位不可信：$reason';
  if (loc['source'] == 'odom_identity') return '定位：仿真（里程就是位置）';
  final s = loc['sigma_m'];
  return '定位：偏差约 ${s is num ? s.toStringAsFixed(1) : '?'} m';
}

/// RTK 的人话（W09e）：单狗视图里的 `rtk`（站点从狗的遥测取的）。没配 RTK、老狗是空串。
String rtkText(Map<String, dynamic>? rtk) {
  if (rtk == null) return '';
  final fix = switch ('${rtk['fix']}') {
    'fixed' => '固定解',
    'float' => '浮点解',
    'dgps' => '差分',
    'single' => '单点',
    _ => '没有定位',
  };
  final parts = <String>['RTK：$fix'];
  if (rtk['sats'] is num) parts.add('卫星 ${(rtk['sats'] as num).toInt()}');
  final std = rtk['std_h_m'];
  if (std is num) {
    parts.add(std < 1 ? '精度约 ${(std * 100).toStringAsFixed(0)} cm' : '精度约 ${std.toStringAsFixed(1)} m');
  }
  final age = rtk['age_s'];
  if (age is num) parts.add('改正 ${age.toStringAsFixed(0)} 秒前');
  if (rtk['stale'] == true) parts.add('（好一会儿没更新了）');
  return parts.join('，');
}

/// 头尾方向的人话（W11a、W09i）：不是狗头为前时说清楚、能不能自己走。狗头为前、老狗是空串。
String headText(Map<String, dynamic>? caps) {
  final tasks = caps?['tasks'];
  final h = tasks is Map ? tasks['head'] : null;
  if (h is! Map || h['direction'] == 'head') return '';
  final tail = h['direction'] == 'tail';
  final d = tail ? '狗尾为前' : '不知道';
  if (h['autonomy'] == true) return '头尾：$d（后雷达标过，照常自己走）';
  final why = tail ? '后雷达没标定，不能自己走' : '不能自己走';
  return '头尾：$d（$why，遥控照常）';
}

/// 局部避障的人话（W11）：能力里的 `obstacles`（配了感知节点的狗才有）。不是「正常」的时候站点不派
/// 会自己走的任务。没配是空串。
String obstaclesText(Map<String, dynamic>? caps) {
  final tasks = caps?['tasks'];
  final o = tasks is Map ? tasks['obstacles'] : null;
  if (o is! Map) return '';
  final state = switch ('${o['state']}') {
    'ok' => '正常',
    'stale' => '感知不新鲜',
    'lost' => '感知断了（不派会自己走的任务）',
    'extrinsic_bad' => '外参自检没过（不派会自己走的任务）',
    'initializing' => '感知还在自检',
    _ => '${o['state']}',
  };
  final reason = o['reason'] is String && (o['reason'] as String).isNotEmpty ? '：${o['reason']}' : '';
  final rear = o['rear'] == true ? '' : '，后雷达没用上';
  return '避障：$state$reason${o['state'] == 'ok' ? rear : ''}';
}

/// 配了定位器的狗（W09a，经本机定位桥）：来源说人话。
String _bridgeLocText(Map<String, dynamic> loc, String reason) {
  if (loc['anchored'] != true) {
    return '定位：定位器还没给出位置${reason.isEmpty ? '' : ' —— $reason'}';
  }
  if (reason.isNotEmpty) return '定位不可信：$reason';
  final src = switch ('${loc['source']}') {
    'scan_match' => '点云匹配',
    'rtk' => 'RTK',
    'fused' => '融合',
    'dead_reckoning' => '里程推算中（定位器一时没来）',
    final other => other,
  };
  final s = loc['sigma_m'];
  return '定位：$src，偏差约 ${s is num ? s.toStringAsFixed(1) : '?'} m';
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
  /// 回默认待命点；给了 [name] 就回那一个（W13a）。
  Future<Map<String, dynamic>> returnToStandby(String id, {String? name});
  /// 这台狗的待命点与原点（W13a）：`{points: [...], homes: [...]}`。
  Future<Map<String, dynamic>> standbyPoints(String id);
  /// 在当前位置设待命点（W13a，管理员）：不动原点。
  Future<Map<String, dynamic>> standbyHere(String id, String name, {bool? isDefault});
  /// 改一个待命点（设成默认）、删一个（管理员）。
  Future<Map<String, dynamic>> setStandbyDefault(String id, Map<String, dynamic> point);
  Future<Map<String, dynamic>> removeStandby(String id, String name);
  Future<Map<String, dynamic>> schedule();
  Future<List<Map<String, dynamic>>> incidents();
  /// 拦截点与防区（W16）：`{intercepts: [...], zones: [...]}`。
  Future<Map<String, dynamic>> intercepts();
  /// 连续录像（W18）：一分钟一段，最新的在前。[since]/[until] 毫秒，按「这一段盖住的时间」算。
  Future<List<Map<String, dynamic>>> recordings(
      {String? robotId, String? camera, int? since, int? until, int? limit});
  /// 一段录像的字节（钉证书、带令牌；一段一分钟，几十 MB）。
  Future<Uint8List> recordingBytes(int id);
  /// 标「留着」（过了 30 天也不删）：值班的人、管理员。
  Future<Map<String, dynamic>> setRecordingKeep(int id, bool keep);
  /// 这一趟运行记录标「留着」（W30，PDPA：过了留存期、按时间段删都不删；值班的人留证据）。
  Future<Map<String, dynamic>> setRunKeep(int id, bool keep);
  /// 固定摄像头（W19）：名字、防区、连没连上、最近一次报的什么。不带口令。
  Future<List<Map<String, dynamic>>> cameras();
  /// 领一个值守令牌（W17）：`{token, expires_at}`。只能看告警，给后台值守用。
  Future<Map<String, dynamic>> watchToken();
  /// 在狗现在的位置设拦截点（W16，管理员；狗上什么都不改）。回拦截点与防区。
  Future<Map<String, dynamic>> interceptHere(String robotId, String name);
  /// 防区绑到拦截点 / 防区不再派狗 / 删拦截点（W16，管理员）。都回拦截点与防区。
  Future<Map<String, dynamic>> mapZone(String zone, String intercept);
  Future<Map<String, dynamic>> unmapZone(String zone);
  Future<Map<String, dynamic>> removeIntercept(String name);

  /// 布防模式（W20）：当前模式、访客到几点、每个防区现在布不布防。站点没开回 404。
  Future<Map<String, dynamic>> mode();
  /// 切模式：`armed`（谁都能）、`home`、`visitor`（业主、管理员；访客带 [zones] 和 [minutes]）。
  Future<Map<String, dynamic>> setMode(String mode, {List<String>? zones, int? minutes});
  /// 这个防区在家时布不布防（管理员）。
  Future<Map<String, dynamic>> setZoneHome(String zone, bool homeArmed);

  /// 天气（W29）：现在按什么管（正常 / 下雨 / 雷暴 / 不知道）、手动切的、联网查的。
  Future<Map<String, dynamic>> weather();
  /// 手动切天气：`normal`、`rain`、`storm`（带 [hours]，默认 3 小时），或者 `auto` 回到联网查的。
  Future<Map<String, dynamic>> setWeather(String condition, {int? hours});

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

  /// 停车（W00c5c）：走站点的 `halt`，**不走遥控连接**。业主也能按。停下之后站点不再派它，
  /// 直到有人 [resume]（W00c5e）。
  Future<Map<String, dynamic>> halt(String robotId);

  /// 解除叫停（W00c5e）：之后站点才重新给这只狗派单。管理员、保安。
  Future<Map<String, dynamic>> resume(String robotId);

  /// 监护心跳（W00c6i）：[action] 是 `renew`（开着的时候每秒一次）或 `release`；[session] 每次打开
  /// 开关新起一个，[seq] 在会话里单调递增（狗据此不认迟到的旧心跳）。管理员、保安。**短超时**：
  /// 慢了就当这一次没成，下一次再来。
  Future<Map<String, dynamic>> supervise(String robotId, String action,
      {required String session, required int seq});

  /// 设位置（W00c6e，里程锚定）：[atHome] 狗在原点；否则 [x]、[y]（米）、[yaw]（弧度），地图坐标。保安、管理员。
  Future<Map<String, dynamic>> relocalize(String robotId,
      {bool atHome = false, double x = 0, double y = 0, double yaw = 0});

  /// 分级驱离（W22）：正在进行的几场（每台狗最多一场）。站点没开回 404。
  Future<List<Map<String, dynamic>>> deterrence();
  /// 跳级、往回退（保安、管理员）。人动过一次就不再自动升。
  Future<Map<String, dynamic>> deterLevel(String robotId, int level);
  /// 解除（保安、管理员、业主）：全关、狗回待命点。
  Future<void> deterRelease(String robotId);

  /// 上装（W21）：开（带 [maxS] 秒，到点狗上自己关）/ 关一路：`strobe` 警灯、`siren` 警笛、`spotlight` 聚光灯、
  /// `speaker` 喇叭（开要 [clip]：话术名或 `tts:<语言>:<文字>`）。返回 `{ack}`。
  Future<Map<String, dynamic>> deter(String robotId, String output, bool on, {double? maxS, String? clip});

  /// 在当前位置标原点（W00c6f，管理员）：狗用它此刻的位置当原点（定位不好就拒），站点登记成默认待命点（名字
  /// [name]，空就是 `home`）。返回 `{ack, standby}`。
  /// 在当前位置标原点（W00c6f）。同名的点登记在别的图上时站点回 409 带 `name_taken`，
  /// 人确认之后 [replace] 为真再发一次。
  Future<Map<String, dynamic>> markHome(String robotId, {String name = '', bool replace = false});

  /// 运行记录（W00c5d，决策 8：证据都在站点）：最近的在前；[robotId] 给了只要这台狗的；[mission]
  /// 给了只要这个任务的（W17：告警带的 `task_id`，到了拍的现场照片在那一趟里）。
  /// 读不懂就抛 `FormatException`，不当成「没有记录」。
  Future<List<Map<String, dynamic>>> runs({String? robotId, String? mission});

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

  /// 给一台狗下发一张图（狗后台下载、核对、载入，完了发事件）。[withoutHome]：这台狗在这张图上还没有
  /// 原点，管理员明说照样下发（W13a：下发之后狗不接 goto、巡检，要先「在这儿标原点」）。
  Future<Map<String, dynamic>> activateMap(String robotId, String mapId, String version,
      {bool withoutHome = false});

  /// 录包：[action] 是 start（要 [name]）/ stop。
  /// 录包开始、停止（W00c5d）；开始时带地图号与版本 = 录包的同时在线建这一版（W09c2 边走边建）。
  Future<Map<String, dynamic>> mapping(String robotId, String action,
      {String name = '', String mapId = '', String version = ''});

  /// 拿一个录包在狗上重建一张图。
  Future<Map<String, dynamic>> buildMap(String robotId, String bag, String mapId, String version);

  /// 站点的发布目录与每台狗在跑哪一版（W00c5d 第三部分）：`{releases: [...], robots: {id: 版本}}`。
  Future<Map<String, dynamic>> releases();

  /// 给一台狗装（install）、切（activate）、退（rollback）一版。
  Future<Map<String, dynamic>> releaseAction(String robotId, String action, {String name = ''});

  /// 建图进程日志（W00c6g，管理员）：狗上录包、重建子进程的日志列表 `{logs: [{name, size, mtime_ms}]}`。
  Future<Map<String, dynamic>> procLogs(String robotId);

  /// 某一个日志的尾巴 `{name, size, bytes, truncated, text}`。[bytes] 为 0 用狗的默认（64 KB）。
  Future<Map<String, dynamic>> procLog(String robotId, String name, {int bytes = 0});

  /// 录包时的轨迹（W00c6h，管理员）：第 [since] 个点起的 `{points: [[x, y]], since, total, full,
  /// recording}`（录包起点为原点、起点朝向为 +x，米）。
  Future<Map<String, dynamic>> mappingTrail(String robotId, {int since = 0});

  /// 建图预览（W09f，管理员）：边走边建时狗上攒的俯视图。`{live, seq, png?（base64）, res, origin: [x, y],
  /// width, height, pose: [x, y, yaw], trail: [[x, y]], frames, age_s, recording, starting, run, …}`；
  /// `png` 只在 `seq` 比 [since] 新时带。没在边走边建 `{live: false}`；老狗（没这项能力）409。
  Future<Map<String, dynamic>> mappingPreview(String robotId, {int since = 0});

  /// 建好的图的预览（W00c6h，谁都能看）：`{width, height, m_per_px, left_x, top_y, standby: [...]}`；
  /// 地图 (x, y) 在图上是 `((x − left_x) / m_per_px, (top_y − y) / m_per_px)`。
  Future<Map<String, dynamic>> mapPreview(String mapId, String version);

  /// 预览图（灰度 PNG）。
  Future<Uint8List> mapPreviewPng(String mapId, String version);

  /// 这张图这一版的禁行区、限速区（W10，谁都能看）：`{revision, zones: [{id, kind: nogo|slow, label,
  /// polygon: [[x, y]], max_speed_mps?}], confirmed: {revision, by, at_ms, current}?, confirm_text}`。
  Future<Map<String, dynamic>> mapZones(String mapId, String version);

  /// 整份改（管理员）：[baseRevision] 是改之前看到的修订号，别人先改了回 409。
  Future<Map<String, dynamic>> saveZones(
      String mapId, String version, List<Map<String, dynamic>> zones, int baseRevision);

  /// 发布前人工确认（管理员，W08 决定 5）：确认的是 [revision] 这一版；没确认的图下发不了。
  Future<Map<String, dynamic>> confirmZones(String mapId, String version, int revision);
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

  /// 本机已经开始关（``close()`` 调过了、握手还没完）。dart:io 调过 close 之后再 add 会抛 StateError，
  /// 而关握手最长要等站点几秒 —— 这段时间里的 send / release 一律不发（W00c5 修复内部评审）。
  bool _closing = false;

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
    if (_closed || _closing) return;
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
    if (_closed || _closing) return;
    _ws.add(jsonEncode(<String, dynamic>{'kind': 'release'}));
  }

  @override
  Future<void> close() async {
    _closing = true;
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

  Future<dynamic> _send(String method, String path, [Object? body, Duration? within]) async {
    try {
      return await _sendRaw(method, path, body).timeout(within ?? timeout);
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
      throw SiteError(resp.statusCode, msg,
          body: decoded is Map<String, dynamic> ? decoded : const <String, dynamic>{});
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
        d['role'] as String? ?? '', displayName: d['display_name'] as String? ?? '');
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
  Future<Map<String, dynamic>> returnToStandby(String id, {String? name}) async => _map(
      await _send('POST', '/api/robots/${Uri.encodeComponent(id)}/standby/return',
          <String, dynamic>{'name': ?name}));

  @override
  Future<Map<String, dynamic>> standbyPoints(String id) async =>
      _map(await _send('GET', '/api/robots/${Uri.encodeComponent(id)}/standby'));

  @override
  Future<Map<String, dynamic>> standbyHere(String id, String name, {bool? isDefault}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(id)}/standby/here',
          <String, dynamic>{'name': name, 'default': ?isDefault}));

  @override
  Future<Map<String, dynamic>> setStandbyDefault(String id, Map<String, dynamic> point) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(id)}/standby', <String, dynamic>{
        for (final k in const ['name', 'map_id', 'map_version', 'x', 'y', 'yaw']) k: point[k],
        'default': true,
      }));

  @override
  Future<Map<String, dynamic>> removeStandby(String id, String name) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(id)}/standby',
          <String, dynamic>{'name': name, 'remove': true}));

  @override
  Future<Map<String, dynamic>> schedule() async =>
      _map(await _send('GET', '/api/schedule'));

  @override
  Future<Map<String, dynamic>> watchToken() async =>
      _map(await _send('POST', '/api/watch/token', <String, dynamic>{}));

  @override
  Future<Map<String, dynamic>> intercepts() async => _map(await _send('GET', '/api/intercepts'));

  @override
  Future<Map<String, dynamic>> interceptHere(String robotId, String name) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/intercept/here',
          <String, dynamic>{'name': name}));

  @override
  Future<Map<String, dynamic>> mapZone(String zone, String intercept) async =>
      _map(await _send('POST', '/api/zones', <String, dynamic>{'zone': zone, 'intercept': intercept}));

  @override
  Future<Map<String, dynamic>> mode() async => _map(await _send('GET', '/api/mode'));

  @override
  Future<Map<String, dynamic>> weather() async => _map(await _send('GET', '/api/weather'));

  @override
  Future<Map<String, dynamic>> setWeather(String condition, {int? hours}) async =>
      _map(await _send('POST', '/api/weather', <String, dynamic>{'condition': condition, 'hours': ?hours}));

  @override
  Future<Map<String, dynamic>> setMode(String mode, {List<String>? zones, int? minutes}) async =>
      _map(await _send('POST', '/api/mode', <String, dynamic>{
        'mode': mode,
        'zones': ?zones,
        'minutes': ?minutes,
      }));

  @override
  Future<Map<String, dynamic>> setZoneHome(String zone, bool homeArmed) async =>
      _map(await _send('POST', '/api/mode/zones',
          <String, dynamic>{'zone': zone, 'home_armed': homeArmed}));

  @override
  Future<Map<String, dynamic>> unmapZone(String zone) async =>
      _map(await _send('POST', '/api/zones', <String, dynamic>{'zone': zone, 'remove': true}));

  @override
  Future<Map<String, dynamic>> removeIntercept(String name) async =>
      _map(await _send('POST', '/api/intercepts', <String, dynamic>{'name': name, 'remove': true}));

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
  Future<List<Map<String, dynamic>>> runs({String? robotId, String? mission}) async {
    final q = <String>[
      if (robotId != null) 'robot=${Uri.encodeQueryComponent(robotId)}',
      if (mission != null) 'mission=${Uri.encodeQueryComponent(mission)}',
    ];
    final d = _map(await _send('GET', '/api/runs${q.isEmpty ? '' : '?${q.join('&')}'}'));
    final rows = d['runs'];
    if (rows is! List) throw const FormatException('站点回的运行记录里没有 runs');
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  @override
  Future<Map<String, dynamic>> run(int id) async => _map(await _send('GET', '/api/runs/$id'));

  /// 照片最多这么大（字节）：站点回的东西不照单全收。
  static const int maxPhotoBytes = 20 * 1024 * 1024;

  @override
  Future<Uint8List> runPhoto(int id, String name) =>
      _bytes('/api/runs/$id/photos/${Uri.encodeComponent(name)}', maxPhotoBytes, '照片');

  String _zonesPath(String mapId, String version) =>
      '/api/maps/${Uri.encodeComponent(mapId)}/${Uri.encodeComponent(version)}/zones';

  @override
  Future<Map<String, dynamic>> mapZones(String mapId, String version) async =>
      _map(await _send('GET', _zonesPath(mapId, version)));

  @override
  Future<Map<String, dynamic>> saveZones(String mapId, String version,
          List<Map<String, dynamic>> zones, int baseRevision) async =>
      _map(await _send('POST', _zonesPath(mapId, version),
          <String, dynamic>{'zones': zones, 'base_revision': baseRevision}));

  @override
  Future<Map<String, dynamic>> confirmZones(String mapId, String version, int revision) async =>
      _map(await _send('POST', '${_zonesPath(mapId, version)}/confirm',
          <String, dynamic>{'revision': revision, 'confirm': true}));

  @override
  Future<Uint8List> mapPreviewPng(String mapId, String version) => _bytes(
      '/api/maps/${Uri.encodeComponent(mapId)}/${Uri.encodeComponent(version)}/preview.png',
      maxPhotoBytes, '地图预览');

  @override
  Future<List<Map<String, dynamic>>> recordings(
      {String? robotId, String? camera, int? since, int? until, int? limit}) async {
    final q = <String>[
      if (robotId != null) 'robot=${Uri.encodeQueryComponent(robotId)}',
      if (camera != null) 'camera=${Uri.encodeQueryComponent(camera)}',
      if (since != null) 'since=$since',
      if (until != null) 'until=$until',
      if (limit != null) 'limit=$limit',
    ];
    final d = _map(await _send('GET', '/api/recordings${q.isEmpty ? '' : '?${q.join('&')}'}'));
    final rows = d['recordings'];
    if (rows is! List) throw const FormatException('站点回的录像里没有 recordings');
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  @override
  Future<List<Map<String, dynamic>>> cameras() async {
    final d = _map(await _send('GET', '/api/cameras'));
    final rows = d['cameras'];
    if (rows is! List) throw const FormatException('站点回的摄像头里没有 cameras');
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  /// 一段录像最大多少字节（一分钟；真狗的码率没量过，给足余量）。
  static const int maxRecordingBytes = 200 * 1024 * 1024;

  @override
  Future<Uint8List> recordingBytes(int id) =>
      _bytes('/api/recordings/$id/video', maxRecordingBytes, '这段录像');

  @override
  Future<Map<String, dynamic>> setRecordingKeep(int id, bool keep) async =>
      _map(await _send('POST', '/api/recordings/$id/keep', <String, dynamic>{'keep': keep}));

  @override
  Future<Map<String, dynamic>> setRunKeep(int id, bool keep) async =>
      _map(await _send('POST', '/api/runs/$id/keep', <String, dynamic>{'keep': keep}));

  /// 取一份字节（照片、预览图）：钉证书、带令牌、不跟重定向、有上限。
  Future<Uint8List> _bytes(String path, int maxBytes, String what) async {
    final req = await _io.openUrl('GET', base.resolve(path)).timeout(timeout);
    req.followRedirects = false;
    final tok = session?.token;
    if (tok != null) req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $tok');
    final resp = await req.close().timeout(timeout);
    if (resp.statusCode != 200) {
      await resp.drain<void>();
      if (resp.statusCode == 401) session = null;
      throw SiteError(resp.statusCode, '$what拿不到');
    }
    final out = BytesBuilder(copy: false);
    await for (final chunk in resp.timeout(timeout)) {
      out.add(chunk);
      if (out.length > maxBytes) throw SiteError(0, '$what太大');
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
  Future<Map<String, dynamic>> activateMap(String robotId, String mapId, String version,
          {bool withoutHome = false}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/map', <String, dynamic>{
        'map_id': mapId,
        'version': version,
        if (withoutHome) 'without_home': true,
      }));

  @override
  Future<Map<String, dynamic>> mapping(String robotId, String action,
          {String name = '', String mapId = '', String version = ''}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/mapping',
          <String, dynamic>{
            'action': action,
            if (name.isNotEmpty) 'name': name,
            if (mapId.isNotEmpty) 'map_id': mapId,
            if (version.isNotEmpty) 'version': version,
          }));

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
  Future<Map<String, dynamic>> resume(String robotId) async => _map(await _send(
      'POST', '/api/robots/${Uri.encodeComponent(robotId)}/resume', <String, dynamic>{}));
  @override
  Future<Map<String, dynamic>> supervise(String robotId, String action,
          {required String session, required int seq}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/supervise',
          <String, dynamic>{'action': action, 'session': session, 'seq': seq},
          const Duration(seconds: 3)));

  @override
  Future<Map<String, dynamic>> mappingTrail(String robotId, {int since = 0}) async => _map(
      await _send('GET', '/api/robots/${Uri.encodeComponent(robotId)}/mapping/trail?since=$since'));

  @override
  Future<Map<String, dynamic>> mappingPreview(String robotId, {int since = 0}) async => _map(
      await _send('GET', '/api/robots/${Uri.encodeComponent(robotId)}/mapping/preview?since=$since'));

  @override
  Future<Map<String, dynamic>> mapPreview(String mapId, String version) async => _map(await _send(
      'GET', '/api/maps/${Uri.encodeComponent(mapId)}/${Uri.encodeComponent(version)}/preview'));

  @override
  Future<Map<String, dynamic>> procLogs(String robotId) async =>
      _map(await _send('GET', '/api/robots/${Uri.encodeComponent(robotId)}/logs'));

  @override
  Future<Map<String, dynamic>> procLog(String robotId, String name, {int bytes = 0}) async =>
      _map(await _send('GET', '/api/robots/${Uri.encodeComponent(robotId)}/logs/'
          '${Uri.encodeComponent(name)}${bytes > 0 ? '?bytes=$bytes' : ''}'));

  @override
  Future<Map<String, dynamic>> markHome(String robotId,
          {String name = '', bool replace = false}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/home/here',
          <String, dynamic>{if (name.isNotEmpty) 'name': name, if (replace) 'replace': true}));

  @override
  Future<List<Map<String, dynamic>>> deterrence() async =>
      ((_map(await _send('GET', '/api/deterrence'))['sessions'] as List?) ?? const [])
          .whereType<Map>()
          .map((m) => m.cast<String, dynamic>())
          .toList();

  @override
  Future<Map<String, dynamic>> deterLevel(String robotId, int level) async => _map(await _send(
      'POST', '/api/deterrence/${Uri.encodeComponent(robotId)}/level', <String, dynamic>{'level': level}));

  @override
  Future<void> deterRelease(String robotId) async =>
      _send('POST', '/api/deterrence/${Uri.encodeComponent(robotId)}/release', <String, dynamic>{});

  @override
  Future<Map<String, dynamic>> deter(String robotId, String output, bool on,
          {double? maxS, String? clip}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/deter', <String, dynamic>{
        'output': output,
        'on': on,
        if (on) 'max_s': ?maxS,
        if (on) 'clip': ?clip,
      }));

  @override
  Future<Map<String, dynamic>> relocalize(String robotId,
          {bool atHome = false, double x = 0, double y = 0, double yaw = 0}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/relocalize',
          atHome ? <String, dynamic>{'at_home': true} : <String, dynamic>{'x': x, 'y': y, 'yaw': yaw}));

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
