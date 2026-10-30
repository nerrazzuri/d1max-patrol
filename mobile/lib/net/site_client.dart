/// 跟**站点**说话（W00c4）。站点是现场的一台主机（`packages/site-node`），手机登录站点、
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

import '../l10n.dart';
import '../model/alert.dart';

/// 跟站点说话时出的任何岔子。连不上、证书不对、超时是 0；站点回的 4xx/5xx 原样带状态码。
class SiteError implements Exception {
  final int status;
  final String message;

  /// 站点回的整个错误体（除了 `error` 还可能带别的字段，比如标原点的 `name_taken`、`data`）。
  final Map<String, dynamic> body;
  const SiteError(this.status, this.message, {this.body = const <String, dynamic>{}});

  @override
  String toString() => status == 0 ? message : tr('$message ($status)', '$message（$status）');
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
    return tr('Invalid address', '地址写得不对');
  }
  if (u.scheme != 'https' || u.host.isEmpty) {
    return tr(
        'Address must be https://host:port',
        '地址要是 https://主机:端口');
  }
  if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(normalizeFingerprint(fingerprint))) {
    return tr(
        'Certificate fingerprint must be 64 hex characters (the string printed by d1max-site fingerprint)',
        '证书指纹要是 64 位十六进制（d1max-site fingerprint 打出来的那串）');
  }
  return null;
}

/// 登录之后的会话。
/// 版本兼容（商业化 A7，跟站点的 `d1max_contract.compat` 对着）：这个 App 的接口级别、它要站点至少几级。
/// 只在「两头都要会」的接口改了的时候加一。
const int appApiLevel = 1;

/// 这个 App 的版本号（跟 `pubspec.yaml` 的 `version` 加号前面那一段一致，测试核着）。登录时报给站点
/// （C-B4）：管理员在版本一览里看得到谁还在用老 App。
const String appVersion = '1.0.0';

/// 登录时报给站点的「我是谁」：哪一类程序、哪一版、接口级别、什么系统。只给人看，站点不拿它做判断。
Map<String, dynamic> clientInfo({String? platform}) => <String, dynamic>{
      'kind': 'app',
      'version': appVersion,
      'api_level': appApiLevel,
      'platform': platform ?? Platform.operatingSystem,
    };
const int minSiteApiLevel = 1;

/// 站点登录回复里的接口级别、站点要的 App 最低级别（老站点不带：都算 1）配不配这个 App：
/// 配就回 null，不配回给人看的原因。
String? compatProblem(Map<String, dynamic> login) {
  final siteLevel = login['api_level'] is int ? login['api_level'] as int : 1;
  final minApp = login['min_app_level'] is int ? login['min_app_level'] as int : 1;
  if (appApiLevel < minApp) {
    return tr(
        'App too old: this site needs app API level $minApp or higher (this app is $appApiLevel). Update the app',
        'App 太老了：这个站点要 App 接口级别 $minApp 以上（现在是 $appApiLevel），请升级 App');
  }
  if (siteLevel < minSiteApiLevel) {
    return tr(
        'Site too old: this app needs site API level $minSiteApiLevel or higher (the site is $siteLevel). Update the site first',
        '站点太老了：这个 App 要站点接口级别 $minSiteApiLevel 以上（站点是 $siteLevel），请先升级站点');
  }
  return null;
}

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

  /// 确认、解决告警（W00c5a）：值班的人（保安、管理员）；经理也行（W20：留守时自己看到了点「我知道了」）。
  bool get canHandleAlerts => role == 'admin' || role == 'guard' || role == 'owner';

  /// 切到布防（W20）：谁都能。
  bool get canArm => role == 'admin' || role == 'guard' || role == 'owner';

  /// 切到留守、临时撤防（撤防，W20）：经理、管理员；保安不能。
  bool get canSetMode => role == 'admin' || role == 'owner';

  /// 遥控（W00c5c，决策 7）：保安、管理员；经理没有（经理能按「停」）。
  bool get canTeleop => role == 'admin' || role == 'guard';

  /// 强制接管别人的遥控：只有管理员，而且要写理由。
  bool get canTakeover => role == 'admin';

  /// 判读、复核照片（W00c5d）：值班的人（保安、管理员）；经理只看。
  bool get canReview => role == 'admin' || role == 'guard';

  /// 下发地图、录包、重建（W00c5d 第二部分）：只有管理员。
  bool get canManageMaps => role == 'admin';

  /// 改巡检路线、排程（站点的 manage 权限）：管理员。看路线谁都能；派一趟要 [canDispatch]。
  bool get canManageRoutes => role == 'admin';

  /// 站点体检、诊断包（商业化 A1）：只有管理员。
  bool get canSeeHealth => role == 'admin';

  /// W42：批准机器人加入、确认机器人换 WiFi（站点的 manage 权限：管理员、工程师）。添加（扫贴纸配网）谁都能做，
  /// 批准要这个。
  bool get canManageRobots => role == 'admin' || role == 'engineer';
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
  final when = every is num && every > 0 ? tr(
      '${e['at']}–${e['until']} every $every min',
      '${e['at']}–${e['until']} 每 $every 分钟') : '${e['at']}';
  final stb = e['standby'];
  return stb is String && stb.isNotEmpty ? tr(
      '$when, then return to $stb',
      '$when，巡完回 $stb') : when;
}

/// 布防模式说人话（W20）：`布防` / `留守` / `临时撤防（到 21:30）`。
String modeText(Map<String, dynamic> m) {
  // W44 第二期：站点的布防时间表管着时，写「按时间表：布防 / 部分布防 / 撤防」，不按 mode 写成「留守」
  if (m['source'] == 'schedule') {
    final st = switch (m['state']) {
      'armed' => tr('armed', '布防'),
      'disarmed' => tr('disarmed', '撤防'),
      _ => tr('partly armed', '部分布防'),
    };
    return tr('By schedule: $st', '按时间表：$st');
  }
  final label = switch (m['mode']) {
    'armed' => tr('Armed', '布防'),
    'home' => tr('Stay', '留守'),
    'visitor' => tr('Temp disarm', '临时撤防'),
    _ => '${m['mode']}',
  };
  final until = m['until_ms'];
  if (m['mode'] != 'visitor' || until is! num) return label;
  final t = DateTime.fromMillisecondsSinceEpoch(until.toInt());
  String two(int v) => v.toString().padLeft(2, '0');
  return tr(
      '$label (until ${two(t.hour)}:${two(t.minute)})',
      '$label（到 ${two(t.hour)}:${two(t.minute)}）');
}

/// 一条事件的去向说人话（W16）。
String incidentOutcomeText(String outcome) => switch (outcome) {
      'dispatched' => tr('Dispatched', '已出动'),
      'dispatching' => tr('Dispatching', '正在派'),
      'merged' => tr('Merged into an active dispatch', '并进已出动的'),
      'duplicate' => tr('Duplicate (same incident)', '重复（同一条事件）'),
      'unmapped' => tr('Zone has no intercept point, no robot sent', '防区没映射到拦截点，没机器人去'),
      'no_robot' => tr('No robot available', '没有能派的机器人'),
      'dispatch_failed' => tr('Dispatched, but the robot did not accept', '派了，机器人没收'),
      'ignored_type' => tr('Not an intrusion, logged only', '不是入侵，只记账'),
      'disarmed' => tr(
          'Disarmed, logged only (no robot sent, no alarm sounded)',
          '撤防中，只记录（没派机器人、没响铃）'),
      'unreachable' => tr('Intercept point unreachable, no robot sent', '拦截点走不到，没机器人去'),
      _ => outcome,
    };

/// 这台狗能不能「自动找位置」（W41）：代理报了 `relocalize.global`（配了避障和定位器）。
bool robotCanFindPose(Map<String, dynamic>? view) {
  final caps = view?['capabilities'];
  final tasks = caps is Map ? caps['tasks'] : null;
  final r = tasks is Map ? tasks['relocalize'] : null;
  return r is Map && r['global'] == true;
}

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
    return tr(
        'Localization not good enough${why.isEmpty ? '' : ': $why'}',
        '定位不够好${why.isEmpty ? '' : '：$why'}');
  }
  // 配了定位器的狗（W09a）：设位置要经定位器。
  if (reason.startsWith('localizer_unavailable')) {
    return tr(
        'Localizer not connected or not responding: ${reason.substring(reason.indexOf(':') + 1).trim()}',
        '定位器没连上或没回：${reason.substring(reason.indexOf(':') + 1).trim()}');
  }
  if (reason.startsWith('localizer_refused')) {
    return tr(
        'Localizer rejected this position: ${reason.substring(reason.indexOf(':') + 1).trim()}',
        '定位器没接这个位置：${reason.substring(reason.indexOf(':') + 1).trim()}');
  }
  // 狗说了在忙什么（W00c6f 标原点：在跑任务、在换图……）。
  if (reason.startsWith('busy:')) {
    return tr(
        'Robot is busy: ${reason.substring(5).trim()}',
        '它正忙着别的：${reason.substring(5).trim()}');
  }
  if (reason.startsWith('persist_failed')) {
    return tr(
        'Robot could not save it (disk full?): ${reason.substring(14).replaceFirst(':', '').trim()}',
        '机器人上记不下（盘满了？）：${reason.substring(14).replaceFirst(':', '').trim()}');
  }
  return _ackReasonText(reason);
}

String _ackReasonText(String reason) => switch (reason) {
      'unsupervised' => tr(
          "Robot needs on-site supervision: turn on \"I'm supervising on site\" first",
          '它要人现场监护：先打开「我在现场监护」再派'),
      'busy' => tr('Robot is busy', '它正忙着别的'),
      'expired' => tr(
          'Command had expired when it reached the robot (robot clock may be off, or the network is too slow)',
          '命令到机器人那儿已经过期了（机器人的钟可能不准，或者网络太慢）'),
      'stale_epoch' => tr(
          'Site and robot control epochs do not match. Refresh and try again',
          '站点和机器人的控制代次对不上，刷新一下再试'),
      'stale_seq' => tr('Late heartbeat, ignored by the robot', '这是一条迟到的旧心跳，机器人没认'),
      'halting' => tr('Robot is stopping. Try again shortly', '它正在叫停，稍后再派'),
      'moving' => tr('Robot is moving. Try again once it has stopped', '它在走：停稳了再试'),
      'no_home' => tr('This map has no origin marked. Enter coordinates', '这张图没标过原点：输坐标'),
      'map_mismatch' => tr('Robot just switched maps. Refresh and set again', '机器人刚换了图：刷新一下再设'),
      'no_map' => tr('Robot has no map loaded', '机器人没加载地图'),
      'odom_invalid' => tr(
          'Cannot read robot odometry (sidecar process down?)',
          '机器人的里程读不到（旁路进程断了？）'),
      _ => reason,
    };

/// 定位状态的人话（W00c6e，里程锚定）：单狗视图里的 `loc`（站点从狗的遥测取的）。没有就是空串。
String locText(Map<String, dynamic>? loc) {
  if (loc == null) return '';
  final reason = '${loc['reason'] ?? ''}';
  if (loc['localizer'] == 'bridge') return _bridgeLocText(loc, reason);
  if (loc['anchored'] != true) {
    return tr(
        'Localization: position not set — ${reason.isEmpty ? 'tap "Set position"' : reason}',
        '定位：没设位置 —— ${reason.isEmpty ? '点「设位置」' : reason}');
  }
  if (reason.isNotEmpty) return tr('Localization unreliable: $reason', '定位不可信：$reason');
  if (loc['source'] == 'odom_identity') {
    return tr(
        'Localization: simulated (odometry is the position)',
        '定位：仿真（里程就是位置）');
  }
  final s = loc['sigma_m'];
  return tr(
      'Localization: error about ${s is num ? s.toStringAsFixed(1) : '?'} m',
      '定位：偏差约 ${s is num ? s.toStringAsFixed(1) : '?'} m');
}

/// 这只狗有哪几路相机（W40）：单狗视图里狗报的 `capabilities.tasks.video.cameras`；老狗不报就是前后两路。
List<String> robotCameras(Map<String, dynamic>? view) {
  final caps = view?['capabilities'];
  final tasks = caps is Map ? caps['tasks'] : null;
  final video = tasks is Map ? tasks['video'] : null;
  final cams = video is Map ? video['cameras'] : null;
  if (cams is! List) return const <String>['front', 'back'];
  final got = <String>[for (final c in const ['front', 'back', 'thermal']) if (cams.contains(c)) c];
  return got.isEmpty ? const <String>['front', 'back'] : got;
}

/// 设备健康（W40）里设备名说人话：`relay` 继电器板、`camera:<名>`、`link:<名>`……
String deviceName(String id) {
  final i = id.indexOf(':');
  final kind = i < 0 ? id : id.substring(0, i);
  final name = i < 0 ? '' : id.substring(i + 1);
  return switch (kind) {
    'relay' => tr('Relay board', '继电器板'),
    'speaker' => tr('Speaker', '喇叭'),
    'siren' => tr('Siren (speaker)', '警笛（喇叭）'),
    'rtk' => 'RTK',
    'disk' => tr('Storage disk', '存储盘'),
    'board' => tr('Main board temperature', '主控板温度'),
    'fan' => tr('Fan $name', '风扇 $name'),
    'amp' => tr('Amplifier', '功放'),
    'lens' => tr('Thermal lens', '热像镜头'),
    'nav2' => tr('Autonomous navigation (Nav2)', '自主导航（Nav2）'),
    'camera' => tr('Camera ${cameraName(name)}', '相机 ${cameraName(name)}'),
    'link' => name == 'router'
        ? tr('Router', '路由器')
        : name == 'thermal'
            ? tr('Thermal camera (network)', '热像（网络）')
            : tr('Network device $name', '网络设备 $name'),
    _ => id,
  };
}

/// 设备健康的四种状态说人话。
String deviceStateText(String state) => switch (state) {
      'online' => tr('online', '在线'),
      'offline' => tr('offline', '离线'),
      'degraded' => tr('degraded', '降级'),
      'error' => tr('fault', '故障'),
      _ => state,
    };

/// 相机名说人话。
String cameraName(String c) => switch (c) {
      'front' => tr('Front', '前'),
      'back' => tr('Back', '后'),
      'thermal' => tr('Thermal', '热像'),
      _ => c,
    };

/// RTK 的人话（W09e）：单狗视图里的 `rtk`（站点从狗的遥测取的）。没配 RTK、老狗是空串。
String rtkText(Map<String, dynamic>? rtk) {
  if (rtk == null) return '';
  final fix = switch ('${rtk['fix']}') {
    'fixed' => tr('fixed', '固定解'),
    'float' => tr('float', '浮点解'),
    'dgps' => tr('DGPS', '差分'),
    'single' => tr('single', '单点'),
    _ => tr('no fix', '没有定位'),
  };
  final parts = <String>[tr('RTK: $fix', 'RTK：$fix')];
  if (rtk['sats'] is num) {
    parts.add(tr(
        '${(rtk['sats'] as num).toInt()} satellites',
        '卫星 ${(rtk['sats'] as num).toInt()}'));
  }
  final std = rtk['std_h_m'];
  if (std is num) {
    parts.add(std < 1
        ? tr('accuracy about ${(std * 100).toStringAsFixed(0)} cm',
            '精度约 ${(std * 100).toStringAsFixed(0)} cm')
        : tr('accuracy about ${std.toStringAsFixed(1)} m', '精度约 ${std.toStringAsFixed(1)} m'));
  }
  final hd = rtk['heading_deg'];
  if (hd is num) {
    // W40：双天线模组（上装 V1.2 的 UM982）才报朝向
    parts.add(tr('heading ${hd.toStringAsFixed(0)}°', '朝向 ${hd.toStringAsFixed(0)}°'));
  }
  final age = rtk['age_s'];
  if (age is num) {
    parts.add(tr(
        'corrections ${age.toStringAsFixed(0)} s ago',
        '改正 ${age.toStringAsFixed(0)} 秒前'));
  }
  if (rtk['stale'] == true) parts.add(tr('(not updated for a while)', '（好一会儿没更新了）'));
  return parts.join(tr(', ', '，'));
}

/// 头尾方向的人话（W11a、W09i）：不是狗头为前时说清楚、能不能自己走。狗头为前、老狗是空串。
String headText(Map<String, dynamic>? caps) {
  final tasks = caps?['tasks'];
  final h = tasks is Map ? tasks['head'] : null;
  if (h is! Map || h['direction'] == 'head') return '';
  final tail = h['direction'] == 'tail';
  final d = tail ? tr('tail first', '机器人尾为前') : tr('unknown', '不知道');
  if (h['autonomy'] == true) {
    return tr(
        'Heading: $d (rear lidar calibrated, autonomous as usual)',
        '头尾：$d（后雷达标过，照常自己走）');
  }
  final why = tail
      ? tr('rear lidar not calibrated, cannot move on its own', '后雷达没标定，不能自己走')
      : tr('cannot move on its own', '不能自己走');
  return tr('Heading: $d ($why, remote control still works)', '头尾：$d（$why，遥控照常）');
}

/// 局部避障的人话（W11）：能力里的 `obstacles`（配了感知节点的狗才有）。不是「正常」的时候站点不派
/// 会自己走的任务。没配是空串。
String obstaclesText(Map<String, dynamic>? caps) {
  final tasks = caps?['tasks'];
  final o = tasks is Map ? tasks['obstacles'] : null;
  if (o is! Map) return '';
  final state = switch ('${o['state']}') {
    'ok' => tr('OK', '正常'),
    'stale' => tr('perception stale', '感知不新鲜'),
    'lost' => tr('perception lost (autonomous tasks not dispatched)', '感知断了（不派会自己走的任务）'),
    'extrinsic_bad' => tr(
        'extrinsic self-check failed (autonomous tasks not dispatched)',
        '外参自检没过（不派会自己走的任务）'),
    'initializing' => tr('perception still self-checking', '感知还在自检'),
    _ => '${o['state']}',
  };
  final reason = o['reason'] is String && (o['reason'] as String).isNotEmpty ? tr(
      ': ${o['reason']}',
      '：${o['reason']}') : '';
  final rear = o['rear'] == true ? '' : tr(', rear lidar not in use', '，后雷达没用上');
  return tr(
      'Obstacle avoidance: $state$reason${o['state'] == 'ok' ? rear : ''}',
      '避障：$state$reason${o['state'] == 'ok' ? rear : ''}');
}

/// 配了定位器的狗（W09a，经本机定位桥）：来源说人话。
String _bridgeLocText(Map<String, dynamic> loc, String reason) {
  if (loc['anchored'] != true) {
    return tr(
        'Localization: no position from the localizer yet${reason.isEmpty ? '' : ' — $reason'}',
        '定位：定位器还没给出位置${reason.isEmpty ? '' : ' —— $reason'}');
  }
  if (reason.isNotEmpty) return tr('Localization unreliable: $reason', '定位不可信：$reason');
  final src = switch ('${loc['source']}') {
    'scan_match' => tr('scan matching', '点云匹配'),
    'rtk' => 'RTK',
    'fused' => tr('fused', '融合'),
    'dead_reckoning' => tr('dead reckoning (localizer briefly silent)', '里程推算中（定位器一时没来）'),
    final other => other,
  };
  final s = loc['sigma_m'];
  final base = tr(
      'Localization: $src, error about ${s is num ? s.toStringAsFixed(1) : '?'} m',
      '定位：$src，偏差约 ${s is num ? s.toStringAsFixed(1) : '?'} m');
  // 可信、但变差了（狗在推算、σ 偏大，自动限速）：带上狗说的原因
  final degraded = '${loc['degraded'] ?? ''}';
  return degraded.isEmpty ? base : tr('$base · degraded: $degraded', '$base · 变差：$degraded');
}

/// 运行状态的人话（运营可视化）：单狗视图里的 `nav`（站点从狗的 `nav_blocked` / `nav_replan` 事件取的）。
/// 被挡写「被挡 N 秒：原因」，重规划写次数和原因；都没有是空串。
String navText(Map<String, dynamic>? nav) {
  if (nav == null) return '';
  final lines = <String>[];
  final b = nav['blocked'];
  if (b is Map) {
    final n = b['for_s'] is num ? (b['for_s'] as num).toInt() : 0;
    final why = '${b['reason'] ?? ''}';
    lines.add(tr('Blocked $n s: ${why.isEmpty ? 'no reason given' : why}',
        '被挡 $n 秒：${why.isEmpty ? '没说原因' : why}'));
  }
  final r = nav['replan'];
  if (r is Map) {
    final n = r['n'] is num ? (r['n'] as num).toInt() : 0;
    final why = switch ('${r['why']}') {
      'zones' => tr('zones changed', '区域换了'),
      'near_nogo' => tr('too close to a no-go zone', '离禁行区太近'),
      'detour' => tr('detouring around an obstacle', '绕障'),
      'preview' => tr('obstacle seen ahead, going around early', '前方有障碍，提前绕'),
      _ => tr('off the route', '偏离路径'),
    };
    lines.add(tr('Re-planned $n× ($why)', '重规划 $n 次（$why）'));
  }
  return lines.join('\n');
}

/// 站点的接口面。界面只认它，测试可以换成假的。
abstract class SiteApi {
  SiteSession? get session;
  Future<SiteSession> login(String name, String password);
  Future<void> logout();

  /// 商业化 A6：这台手机收 P1 推送（极光推送号），离开站点前注销。
  Future<void> pushRegister(String registrationId, String platform, {String provider = 'jpush'});
  Future<void> pushUnregister(String registrationId);
  Future<List<Map<String, dynamic>>> robots();

  /// 运营可视化：每台狗的规划路径、最近轨迹、被挡位置（`GET /api/runviz`，站点内存里的，不落库）。
  Future<Map<String, dynamic>> runViz();

  /// 运营可视化：定位丢了 / 变差的每一段（`GET /api/loc-episodes`），按图、狗、时间过滤。
  Future<List<Map<String, dynamic>>> locEpisodes(
      {String? mapId, String? mapVersion, String? robot, int? sinceMs, int limit = 200});
  Future<Map<String, dynamic>> robot(String id);
  Future<Map<String, dynamic>> patrol(String id, String missionId);

  /// 巡检路线库（2026-10-14，站点存的路线、排程是唯一真理源）：路线（按地图号、版本筛）、排程、时区、修订号。
  Future<Map<String, dynamic>> routes({String? mapId, String? version});

  /// 整条存（新建 `base_revision` 为 0）。站点逐点校验：400 带 `errors: [{index, name, problem}]`
  /// （在 [SiteError.body] 里），别人改过 409。管理员。点是平的：`{name, x, y, yaw, check?, actions?}`。
  Future<Map<String, dynamic>> saveRoute(String id, Map<String, dynamic> body);

  /// 删一条路线（还有排程在用的站点会拒）。管理员。
  Future<void> deleteRoute(String id, int baseRevision);

  /// 去一个点（地图坐标，米；[yaw] 弧度）。站点拒了（要人监护、没就绪、点不可达……）原因在 [SiteError]。
  /// 保安、管理员。
  Future<Map<String, dynamic>> goto(String robotId,
      {required String mapId, required String version, required double x, required double y, required double yaw});
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

  /// 站点体检（商业化 A1，管理员）：`{ok, critical_ok, checks: {名字: {ok, detail, value?}}, version, now_ms}`。
  Future<Map<String, dynamic>> health();

  /// 版本一览（A7）：`{site: {version, schema, api_level, …}, robots: [{robot_id, online, agent, level,
  /// verdict}], clients?: [...]}`（`clients` 只给管理员）。
  Future<Map<String, dynamic>> versions();

  /// 诊断包（A1，管理员）：一个 `.tar.gz`，发给厂家查问题用。里面没有口令和私钥。
  Future<Uint8List> supportBundle();
  /// 标「留着」（过了 30 天也不删）：值班的人、管理员。
  Future<Map<String, dynamic>> setRecordingKeep(int id, bool keep);
  /// 把这台狗现在站的地方登记成充电桩对准点（W13：领到桩前约 1.5 米、正对桩）。管理员。
  Future<Map<String, dynamic>> setChargerHere(String robotId);
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

  /// 布防模式（W20）：当前模式、临时撤防到几点、每个防区现在布不布防。站点没开回 404。
  Future<Map<String, dynamic>> mode();
  /// 切模式：`armed`（谁都能）、`home`、`visitor`（经理、管理员；临时撤防带 [zones] 和 [minutes]）。
  Future<Map<String, dynamic>> setMode(String mode, {List<String>? zones, int? minutes});
  /// 这个防区留守时布不布防（管理员）。
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

  /// 停车（W00c5c）：走站点的 `halt`，**不走遥控连接**。经理也能按。停下之后站点不再派它，
  /// 直到有人 [resume]（W00c5e）。
  Future<Map<String, dynamic>> halt(String robotId);

  /// W42：等待批准加入的机器人（`[{id, serial, check, model, version, source, created_ms, expires_ms}]`，
  /// 管理员）。站点没开绑定回 404。
  Future<List<Map<String, dynamic>>> bindRequests();
  /// 批准一台加入（要再输一次密码；[name] 起个名字，空就用序列号）。
  Future<Map<String, dynamic>> approveBind(String id, String password, {String name = ''});
  Future<Map<String, dynamic>> rejectBind(String id);
  /// 机器人上有人提了换 WiFi：确认 / 不换（[changeId] 是机器人视图 `wifi.change.id`）。
  Future<Map<String, dynamic>> confirmRobotWifi(String robotId, String changeId, bool approve);

  /// 解除叫停（W00c5e）：之后站点才重新给这只狗派单。管理员、保安。
  Future<Map<String, dynamic>> resume(String robotId);

  /// 监护心跳（W00c6i）：[action] 是 `renew`（开着的时候每秒一次）或 `release`；[session] 每次打开
  /// 开关新起一个，[seq] 在会话里单调递增（狗据此不认迟到的旧心跳）。管理员、保安。**短超时**：
  /// 慢了就当这一次没成，下一次再来。
  Future<Map<String, dynamic>> supervise(String robotId, String action,
      {required String session, required int seq});

  /// 设位置（W00c6e，里程锚定）：[atHome] 狗在原点；否则 [x]、[y]（米）、[yaw]（弧度），地图坐标。保安、管理员。
  /// [mapId]、[version]：在哪张图上点的（外审 W39-I2；给了站点核对，跟狗加载的不一样 409）。
  /// [global]：让狗拿看到的墙在整张图上自己找（W41；狗报了 `relocalize.global` 才给）。
  Future<Map<String, dynamic>> relocalize(String robotId,
      {bool atHome = false,
      bool global = false,
      double x = 0,
      double y = 0,
      double yaw = 0,
      String? mapId,
      String? version});

  /// 分级驱离（W22）：正在进行的几场（每台狗最多一场）。站点没开回 404。
  Future<List<Map<String, dynamic>>> deterrence();
  /// 跳级、往回退（保安、管理员）。人动过一次就不再自动升。
  Future<Map<String, dynamic>> deterLevel(String robotId, int level);
  /// 解除（保安、管理员、经理）：全关、狗回待命点。
  Future<void> deterRelease(String robotId);
  /// 就地驱离（W33，决策 48；保安、管理员）：狗自己看见人报了 P1，看过现场决定驱离。狗在跑的任务先撤掉，
  /// 在它此刻的位置开一场、从 L1（灯光）起。回这一场。
  Future<Map<String, dynamic>> deterStart(String robotId);

  /// 上装（W21）：开（带 [maxS] 秒，到点狗上自己关）/ 关一路：`strobe` 警灯、`siren` 警笛、`spotlight` 聚光灯、
  /// `speaker` 喇叭（开要 [clip]：话术名或 `tts:<语言>:<文字>`）。返回 `{ack}`。
  /// W40：聚光灯可以只开、关一边（[side] `left`/`right`，狗报了 `spotlight_sides` 才给）；开警灯可以挑颜色
  /// （[color]，狗报的 `strobe_colors` 之一）。
  Future<Map<String, dynamic>> deter(String robotId, String output, bool on,
      {double? maxS, String? clip, String? side, String? color});

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

  /// 平面图（预览 JSON 里的 `plan.url`，灰度 + 透明的 PNG）：跟 [mapPreviewPng] 一样钉证书、带令牌取。
  /// 只取这个站点自己的地址（别的主机、端口、协议不去，令牌不外送）。
  Future<Uint8List> mapPlanPng(String url);

  /// 这张图这一版的禁行区、限速区（W10，谁都能看）：`{revision, zones: [{id, kind: nogo|slow, label,
  /// polygon: [[x, y]], max_speed_mps?, hard?, until_ms?}], confirmed: {revision, by, at_ms, current,
  /// how}?, confirm_text, proposal?, previous?}`。`hard`：硬禁区（决策 65，遥控也守）；`until_ms`：临时区
  /// 到期时刻（决策 66）；`proposal`：新版本从上一版带过来、等人核的（R2）。
  Future<Map<String, dynamic>> mapZones(String mapId, String version);

  /// 整份改（管理员）：[baseRevision] 是改之前看到的修订号，别人先改了回 409。区域可以带 `for_s`
  /// （临时区的时长，站点按自己的钟换成 `until_ms`）。回的里面 `auto_confirmed`：只收紧，当场算确认了。
  Future<Map<String, dynamic>> saveZones(
      String mapId, String version, List<Map<String, dynamic>> zones, int baseRevision);

  /// 发布前人工确认（管理员，W08 决定 5）：确认的是 [revision] 这一版；没确认的图下发不了。
  /// 一个区域都没有的要 [emptyOk]（R2）；[adopt]：采纳从上一版带过来的
  /// `{zones: [id], standby: [[狗, 名字]], intercepts: [名字]}`。
  Future<Map<String, dynamic>> confirmZones(String mapId, String version, int revision,
      {bool emptyOk = false, Map<String, dynamic>? adopt});

  /// 不要新版本从上一版带过来的那些（R2，管理员）。
  Future<Map<String, dynamic>> discardZoneProposal(String mapId, String version);
  Stream<Map<String, dynamic>> events();
  void close();
}

final Random _rand = Random.secure();

/// 一条遥控连接（W00c5c）。连着 = 握着租约；断了 = 放租，狗停。
abstract class TeleopLink {
  /// 站点发来的：`granted`（租约代次、限速）、`video`（画面有没有）、`ended`（为什么结束）。
  Stream<Map<String, dynamic>> get messages;
  bool get closed;

  /// 一帧摇杆（m/s、rad/s）。站点会再夹一次限速；狗那头也夹。[vy]：横移（机身系左为正，2026-10-30；
  /// 站点只对报了能横移的狗转，别的当零）。
  void send(double vx, double wz, {double vy = 0});

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
  void send(double vx, double wz, {double vy = 0}) {
    if (_closed || _closing) return;
    _seq++;
    _ws.add(jsonEncode(<String, dynamic>{
      'seq': _seq,
      't': _clock.elapsedMicroseconds / 1000.0,
      'vx': vx,
      'wz': wz,
      if (vy != 0) 'vy': vy,
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
      throw SiteError(0, tr('Invalid site address', '站点地址写得不对'));
    }
    if (u.scheme != 'https') {
      throw SiteError(0, tr(
          'Site address must be https:// (the password must not travel in clear text)',
          '站点地址要是 https://（口令不能明文过网）'));
    }
    return u;
  }

  static String _checkedFingerprint(String raw) {
    final fp = normalizeFingerprint(raw);
    if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(fp)) {
      throw SiteError(0, tr(
          'Certificate fingerprint must be 64 hex characters (the string printed by d1max-site fingerprint)',
          '证书指纹要是 64 位十六进制（d1max-site fingerprint 打出来的那串）'));
    }
    return fp;
  }

  @override
  void close() => _io.close(force: true);

  // ------------------------------------------------------------ 请求

  Future<dynamic> _send(String method, String path, [Object? body, Duration? within]) =>
      _wrapped(() => _sendRaw(method, path, body).timeout(within ?? timeout));

  /// 连不上、握手失败、超时、连接中途断了：都收成页面认得的 [SiteError]。JSON 请求和取文件
  /// （照片、录像、诊断包）走同一套（C-B4 外审 I1：取文件的原来不包，页面接不住）。
  Future<T> _wrapped<T>(Future<T> Function() run) async {
    try {
      return await run();
    } on SiteError {
      rethrow;
    } on HandshakeException catch (e) {
      final seen = lastSeenFingerprint;
      throw SiteError(
          0,
          seen != null && seen != fingerprint
              ? tr('Site certificate fingerprint does not match: got $seen', '站点证书指纹对不上：实际是 $seen')
              : tr('TLS handshake with the site failed: $e', '跟站点的加密握手失败：$e'));
    } on TimeoutException {
      throw SiteError(0, tr('No reply from the site (timed out)', '站点没回话（超时）'));
    } on SocketException catch (e) {
      throw SiteError(0, tr("Can't reach the site: ${e.message}", '连不上站点：${e.message}'));
    } on HttpException catch (e) {
      throw SiteError(0, tr(
          'Connection to the site dropped: ${e.message}',
          '跟站点的连接断了：${e.message}'));
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
          : tr('Site returned ${resp.statusCode}', '站点回了 ${resp.statusCode}');
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
        <String, dynamic>{'name': name, 'password': password, 'client': clientInfo()}));
    final why = compatProblem(d);
    if (why != null) {
      // 配不上：不用这次登录（顺手注销掉，站点上不留一个没人用的令牌）
      final t = d['token'] as String? ?? '';
      if (t.isNotEmpty) {
        session = SiteSession(t, name, '');
        try {
          await _send('POST', '/api/logout', <String, dynamic>{});
        } catch (_) {}
        session = null;
      }
      throw SiteError(0, why);
    }
    final s = SiteSession(d['token'] as String? ?? '', d['name'] as String? ?? name,
        d['role'] as String? ?? '', displayName: d['display_name'] as String? ?? '');
    if (s.token.isEmpty) {
      throw SiteError(0, tr('Site returned no token', '站点没给令牌'));
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
  Future<void> pushRegister(String registrationId, String platform, {String provider = 'jpush'}) async {
    await _send('POST', '/api/push/devices', <String, dynamic>{
      'registration_id': registrationId,
      'platform': platform,
      'provider': provider,                              // 老站点不认识这个字段，照旧当极光
    });
  }

  @override
  Future<void> pushUnregister(String registrationId) async {
    await _send('POST', '/api/push/devices/remove',
        <String, dynamic>{'registration_id': registrationId});
  }

  @override
  Future<List<Map<String, dynamic>>> robots() async {
    final d = _map(await _send('GET', '/api/robots'));
    return (d['robots'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
  }

  @override
  Future<Map<String, dynamic>> runViz() async => _map(await _send('GET', '/api/runviz'));

  @override
  Future<List<Map<String, dynamic>>> locEpisodes(
      {String? mapId, String? mapVersion, String? robot, int? sinceMs, int limit = 200}) async {
    final q = <String, String>{
      'map_id': ?mapId,
      'map_version': ?mapVersion,
      'robot': ?robot,
      'since_ms': ?(sinceMs == null ? null : '$sinceMs'),
      'limit': '$limit',
    };
    final d = _map(await _send('GET', '/api/loc-episodes?${Uri(queryParameters: q).query}'));
    return (d['episodes'] as List? ?? const []).whereType<Map<String, dynamic>>().toList();
  }

  @override
  Future<Map<String, dynamic>> robot(String id) async =>
      _map(await _send('GET', '/api/robots/${Uri.encodeComponent(id)}'));

  @override
  Future<Map<String, dynamic>> patrol(String id, String missionId) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(id)}/patrol',
          <String, dynamic>{'mission_id': missionId}));

  @override
  Future<Map<String, dynamic>> routes({String? mapId, String? version}) async => _map(await _send(
      'GET',
      '/api/routes${mapId == null ? '' : '?map_id=${Uri.encodeQueryComponent(mapId)}'
          '${version == null ? '' : '&map_version=${Uri.encodeQueryComponent(version)}'}'}'));

  @override
  Future<Map<String, dynamic>> saveRoute(String id, Map<String, dynamic> body) async =>
      _map(await _send('POST', '/api/routes/${Uri.encodeComponent(id)}', body));

  @override
  Future<void> deleteRoute(String id, int baseRevision) async => _send(
      'POST', '/api/routes/${Uri.encodeComponent(id)}/delete', <String, dynamic>{'base_revision': baseRevision});

  @override
  Future<Map<String, dynamic>> goto(String robotId,
          {required String mapId,
          required String version,
          required double x,
          required double y,
          required double yaw}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/goto', <String, dynamic>{
        'target': <String, dynamic>{
          'map_id': mapId, 'map_version': version, 'frame_id': 'map', 'x': x, 'y': y, 'yaw': yaw,
        },
      }));

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
    if (rows is! List) {
      throw FormatException(tr(
          'Site reply for run records has no "runs" field',
          '站点回的运行记录里没有 runs'));
    }
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  @override
  Future<Map<String, dynamic>> run(int id) async => _map(await _send('GET', '/api/runs/$id'));

  /// 照片最多这么大（字节）：站点回的东西不照单全收。
  static const int maxPhotoBytes = 20 * 1024 * 1024;

  @override
  Future<Uint8List> runPhoto(int id, String name) =>
      _bytes('/api/runs/$id/photos/${Uri.encodeComponent(name)}', maxPhotoBytes, tr('Photo', '照片'));

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
  Future<Map<String, dynamic>> confirmZones(String mapId, String version, int revision,
          {bool emptyOk = false, Map<String, dynamic>? adopt}) async =>
      _map(await _send('POST', '${_zonesPath(mapId, version)}/confirm', <String, dynamic>{
        'revision': revision,
        'confirm': true,
        if (emptyOk) 'empty_ok': true,
        'adopt': ?adopt,
      }));

  @override
  Future<Map<String, dynamic>> discardZoneProposal(String mapId, String version) async =>
      _map(await _send('POST', '${_zonesPath(mapId, version)}/proposal',
          <String, dynamic>{'action': 'discard'}));

  @override
  Future<Uint8List> mapPreviewPng(String mapId, String version) => _bytes(
      '/api/maps/${Uri.encodeComponent(mapId)}/${Uri.encodeComponent(version)}/preview.png',
      maxPhotoBytes, tr('Map preview', '地图预览'));

  @override
  Future<Uint8List> mapPlanPng(String url) async {
    final u = base.resolve(url);
    if (u.scheme != base.scheme || u.host != base.host || u.port != base.port) {
      throw SiteError(0, tr('Floor plan is not on this site', '平面图不在这个站点上'));
    }
    return _bytes(u.toString(), maxPhotoBytes, tr('Floor plan', '平面图'));
  }

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
    if (rows is! List) {
      throw FormatException(tr(
          'Site reply for recordings has no "recordings" field',
          '站点回的录像里没有 recordings'));
    }
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  @override
  Future<List<Map<String, dynamic>>> cameras() async {
    final d = _map(await _send('GET', '/api/cameras'));
    final rows = d['cameras'];
    if (rows is! List) {
      throw FormatException(tr(
          'Site reply for cameras has no "cameras" field',
          '站点回的摄像头里没有 cameras'));
    }
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  /// 一段录像最大多少字节（一分钟；真狗的码率没量过，给足余量）。
  static const int maxRecordingBytes = 200 * 1024 * 1024;

  @override
  Future<Map<String, dynamic>> health() async => _map(await _send('GET', '/api/health'));

  @override
  Future<Map<String, dynamic>> versions() async => _map(await _send('GET', '/api/versions'));

  /// 诊断包最多这么大（站点打的是日志尾巴和配置，正常几百 KB）。
  static const int maxBundleBytes = 64 * 1024 * 1024;

  @override
  Future<Uint8List> supportBundle() =>
      _bytes('/api/support-bundle', maxBundleBytes, tr('Diagnostics bundle', '诊断包'));

  @override
  Future<Uint8List> recordingBytes(int id) =>
      _bytes('/api/recordings/$id/video', maxRecordingBytes, tr('Recording', '这段录像'));

  @override
  Future<Map<String, dynamic>> setRecordingKeep(int id, bool keep) async =>
      _map(await _send('POST', '/api/recordings/$id/keep', <String, dynamic>{'keep': keep}));

  @override
  Future<Map<String, dynamic>> setChargerHere(String robotId) async =>
      _map(await _send('POST', '/api/chargers/${Uri.encodeComponent(robotId)}',
          <String, dynamic>{'here': true}));

  @override
  Future<Map<String, dynamic>> setRunKeep(int id, bool keep) async =>
      _map(await _send('POST', '/api/runs/$id/keep', <String, dynamic>{'keep': keep}));

  /// 取一份字节（照片、预览图）：钉证书、带令牌、不跟重定向、有上限。
  Future<Uint8List> _bytes(String path, int maxBytes, String what) =>
      _wrapped(() => _bytesRaw(path, maxBytes, what));

  Future<Uint8List> _bytesRaw(String path, int maxBytes, String what) async {
    final req = await _io.openUrl('GET', base.resolve(path)).timeout(timeout);
    req.followRedirects = false;
    final tok = session?.token;
    if (tok != null) req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $tok');
    final resp = await req.close().timeout(timeout);
    if (resp.statusCode != 200) {
      await resp.drain<void>();
      if (resp.statusCode == 401) session = null;
      throw SiteError(resp.statusCode, tr('$what unavailable', '$what拿不到'));
    }
    final out = BytesBuilder(copy: false);
    await for (final chunk in resp.timeout(timeout)) {
      out.add(chunk);
      if (out.length > maxBytes) throw SiteError(0, tr('$what too large', '$what太大'));
    }
    return out.takeBytes();
  }

  @override
  Future<Map<String, dynamic>> maps() async {
    final d = _map(await _send('GET', '/api/maps'));
    if (d['maps'] is! List || d['bags'] is! List) {
      throw FormatException(tr(
          'Site reply for the map catalog has no "maps" / "bags" field',
          '站点回的地图目录里没有 maps / bags'));
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
      throw FormatException(tr(
          'Site reply for the release catalog has no "releases" / "robots" field',
          '站点回的发布目录里没有 releases / robots'));
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
        var why = tr("Can't start remote control", '遥控开不了');
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
        throw SiteError(0, tr('Bad remote-control handshake from the site', '站点的遥控握手不对'));
      }
      final sock = await resp.detachSocket();
      final ws = WebSocket.fromUpgradedSocket(sock, serverSide: false)
        // 3 s（跟站点判死一样长）：4G 上一次 pong 慢了 1 s 就断，遥控会三天两头掉。
        ..pingInterval = const Duration(seconds: 3);
      return WsTeleopLink(ws, io);
    } on SocketException catch (e) {
      io.close(force: true);
      throw SiteError(0, tr("Can't reach the site: ${e.message}", '连不上站点：${e.message}'));
    } on HandshakeException {
      io.close(force: true);
      throw SiteError(0, tr('Site certificate does not match', '站点的证书对不上'));
    } on TimeoutException {
      io.close(force: true);
      throw SiteError(0, tr('Timed out connecting to the site', '连站点超时'));
    } on HttpException catch (e) {
      // 握手半路断了（站点重启、4G 切基站）：也要落成 SiteError，页面才会说出来、不卡在「正在拿遥控」。
      io.close(force: true);
      throw SiteError(0, tr(
          'Remote-control handshake did not complete: ${e.message}',
          '遥控握手没完成：${e.message}'));
    } on WebSocketException catch (e) {
      io.close(force: true);
      throw SiteError(0, tr(
          'Remote-control handshake did not complete: ${e.message}',
          '遥控握手没完成：${e.message}'));
    }
  }

  @override
  Future<List<Map<String, dynamic>>> bindRequests() async {
    final rows = _map(await _send('GET', '/api/admin/bind'))['requests'];
    if (rows is! List) {
      throw FormatException(tr('Site reply has no "requests" field', '站点回的东西里没有 requests'));
    }
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  @override
  Future<Map<String, dynamic>> approveBind(String id, String password, {String name = ''}) async =>
      _map(await _send('POST', '/api/admin/bind/${Uri.encodeComponent(id)}/approve',
          <String, dynamic>{'confirm_password': password, 'name': name}));

  @override
  Future<Map<String, dynamic>> rejectBind(String id) async =>
      _map(await _send('POST', '/api/admin/bind/${Uri.encodeComponent(id)}/reject', <String, dynamic>{}));

  @override
  Future<Map<String, dynamic>> confirmRobotWifi(String robotId, String changeId, bool approve) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/wifi',
          <String, dynamic>{'id': changeId, 'approve': approve}));

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
  Future<Map<String, dynamic>> deterStart(String robotId) async => _map(await _send(
      'POST', '/api/deterrence/${Uri.encodeComponent(robotId)}/start', <String, dynamic>{}));

  @override
  Future<Map<String, dynamic>> deter(String robotId, String output, bool on,
          {double? maxS, String? clip, String? side, String? color}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/deter', <String, dynamic>{
        'output': output,
        'on': on,
        if (on) 'max_s': ?maxS,
        if (on) 'clip': ?clip,
        'side': ?side,
        if (on) 'color': ?color,
      }));

  @override
  Future<Map<String, dynamic>> relocalize(String robotId,
          {bool atHome = false,
          bool global = false,
          double x = 0,
          double y = 0,
          double yaw = 0,
          String? mapId,
          String? version}) async =>
      _map(await _send('POST', '/api/robots/${Uri.encodeComponent(robotId)}/relocalize',
          global
              ? <String, dynamic>{'global': true}
              : atHome
              ? <String, dynamic>{'at_home': true}
              : <String, dynamic>{
                  'x': x,
                  'y': y,
                  'yaw': yaw,
                  if (mapId != null && version != null) ...{'map_id': mapId, 'map_version': version},
                }));

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
      throw SiteError(resp.statusCode, tr("Can't open the event stream", '事件流开不了'));
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
