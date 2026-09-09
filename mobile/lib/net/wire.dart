/// 狗发过来的那几样东西，在手机这头的样子。
///
/// **全是只读的。** 真理源在狗身上，手机改了也不算数；能改的模型只会诱使人
/// 在界面上先改再发，而那两步之间的窗口里，屏幕上显示的是一件没发生的事。
///
/// 形状由 `test/fixtures/*.json` 钉着，那些文件是狗那头生成的（挂账 11）。
library;

class LeaseView {
  /// 还剩多久到期（毫秒）。**没人拿着的时候是 `null`，不是 0。**
  ///
  /// **一律读这个相对量，不许拿绝对时刻去减手机自己的钟**：狗上没有 NTP，
  /// 两头的钟能差出几分钟（`engine/lease.py` 的 `LeaseState` 写着）。
  ///
  /// **`null` 和 0 不许合并。** `engine/lease.py:126-128` 明写这两件事在界面
  /// 上是两种画法：`null` 是「这件事根本不存在」（没人拿着），0 是「这一刻
  /// 已经没了」（过期）。塌成 0 的话，一台谁也没在开的狗在屏上写着「已过期」，
  /// 人会去等一个不会来的时刻，而该做的只是按一下「取得控制权」。
  final int? expiresInMs;

  /// 宽限期还剩多久（毫秒）。没人在挑战就是 `null`。同样是相对量。
  final int? graceInMs;

  /// 狗要求多久续一次租约（毫秒）。**面板的校准/续期周期从这儿算，不硬编。**
  ///
  /// `engine/lease.py` 的 `LeaseState` docstring 明写「`ttl_ms`/`heartbeat_ms`
  /// 跟着一起发出去 —— 客户端不该把心跳间隔硬编在自己那头」。硬编的那头是
  /// 这样坏的：狗那边把 `LEASE_HEARTBEAT_MS` 调小（比如为了让接管更快生效），
  /// 手机还按老节奏续，人正开着狗，杆忽然灰了，而两头的代码各自看都是对的。
  ///
  /// 狗没发这一项（老版本的狗）就是 `null`，那时候回落到面板自己的缺省值。
  final int? heartbeatMs;
  final String? holderRef;
  final String holderOperator;

  /// 拿着的是不是我（`server.py` 的 `_control_wire` 按会话 ref 算好了发过来）。
  ///
  /// **手机自己拿 ref 比是不行的**：手机这头没有自己的会话 ref，狗那头才有。
  final bool mine;

  /// 正在要接管的那个人。没人在挑战就是 `null`。
  final String? challengerRef;
  final String challengerOperator;

  /// 现在几个远程会话 / 最多几个（§3.6，3 个封顶）。
  ///
  /// **叫 `remote_sessions` 不叫 `sessions`**：`GET /api/sessions` 里的
  /// `sessions` 是个数组，同一个名字两种类型最容易写出偶发崩溃的客户端。
  final int remoteSessions;
  final int maxSessions;

  /// 狗每次都跟着发的那句话（`auth.OPERATOR_NOTICE`）。**原样上屏。**
  ///
  /// 手机这头硬编一句「未核实」的话，狗那头改了措辞，手机上还是老话 ——
  /// 而这句话是现场判断「屏上这个名字能信到什么程度」的唯一依据。
  final String notice;

  const LeaseView({
    this.expiresInMs,
    this.graceInMs,
    this.heartbeatMs,
    this.holderRef,
    this.holderOperator = '',
    this.mine = false,
    this.challengerRef,
    this.challengerOperator = '',
    this.remoteSessions = 0,
    this.maxSessions = 0,
    this.notice = '',
  });

  factory LeaseView.fromJson(Map<String, dynamic> m) {
    final h = m['holder'] as Map<String, dynamic>?;
    final c = m['challenger'] as Map<String, dynamic>?;
    return LeaseView(
      expiresInMs: (m['expires_in_ms'] as num?)?.toInt(),
      graceInMs: (m['grace_in_ms'] as num?)?.toInt(),
      heartbeatMs: (m['heartbeat_ms'] as num?)?.toInt(),
      holderRef: h?['ref'] as String?,
      holderOperator: (h?['operator'] as String?) ?? '',
      mine: m['mine'] == true,
      challengerRef: c?['ref'] as String?,
      challengerOperator: (c?['operator'] as String?) ?? '',
      remoteSessions: (m['remote_sessions'] as num?)?.toInt() ?? 0,
      maxSessions: (m['max_sessions'] as num?)?.toInt() ?? 0,
      notice: (m['notice'] as String?) ?? '',
    );
  }

  bool get held => holderRef != null;

  /// 有人在要接管。
  bool get challenged => challengerRef != null;
}

class VideoHealth {
  /// 相机名 -> 这一刻有没有画面。
  final Map<String, bool> online;

  const VideoHealth(this.online);

  factory VideoHealth.fromJson(Map<String, dynamic> m) {
    final cams = (m['cameras'] as Map<String, dynamic>?) ?? <String, dynamic>{};
    return VideoHealth(<String, bool>{
      for (final e in cams.entries)
        e.key: (e.value as Map<String, dynamic>)['online'] == true,
    });
  }

  /// §5.9 那道闸在手机这头的样子：**一路都没有就不许开狗**。
  ///
  /// 判据跟狗那头的 `_video_gate` 一致 —— 两头不一致的话，摇杆是亮的而每一拍
  /// 都被拒，人只会以为 app 坏了。
  bool get anyLive => online.values.any((v) => v);
}

class RunView {
  final String state;
  final String waypointName;
  final int waypointIndex;
  final int total;

  /// 引擎让开腿时停在哪儿（§5.10）。没挂起就是 null。
  final String? suspendedAtWaypoint;

  const RunView({
    required this.state,
    required this.waypointName,
    required this.waypointIndex,
    required this.total,
    this.suspendedAtWaypoint,
  });

  factory RunView.fromJson(Map<String, dynamic> m) {
    final s = m['suspended_at'] as Map<String, dynamic>?;
    return RunView(
      state: (m['state'] as String?) ?? 'IDLE',
      waypointName: (m['waypoint_name'] as String?) ?? '',
      waypointIndex: (m['waypoint_index'] as num?)?.toInt() ?? 0,
      total: (m['total'] as num?)?.toInt() ?? 0,
      suspendedAtWaypoint: s?['waypoint_name'] as String?,
    );
  }

  /// 引擎让着腿吗。**遥控在这个状态下是放行的**（§5.10）。
  bool get suspended => state == 'SUSPENDED';
}

/// 盘况报文里的一趟归档（`forecast.runs[]`，`engine/retention.py` 的
/// `RunInfo.to_wire()`）。
///
/// **wire 上没有「还剩几天」。** `RunInfo.days_left()` 在狗那头是有的，可它
/// **没有进 `to_wire()`** —— 报文里只有 [retentionDays]，那是**保留期策略值**
/// （这一趟按 30 天存），不是「再过 30 天就删」。两者在一份标着「要过期的
/// 那几趟」的名单里长得一模一样，而它们能差出十几天：真正动手的时刻是
/// `delete_starts_at()` 算的 `max(到期日, 首次预告 + 预告期)`。
///
/// 所以**这一行上不许出现「还剩」「即将删除」这类话** —— 那句话只能来自
/// `forecast.detail`（狗那头拼好的原文）。见 `ui/storage_page.dart`。
class StorageRun {
  /// 归档目录的全路径。列表里拿它当身份，`mission` 会重名。
  final String path;
  final String mission;

  /// 开跑的时刻，**生戳**，形如 `20260830T041500Z`
  /// （`engine/archive.py` 的 `STAMP_FMT`，UTC）。
  ///
  /// **原样留着，不在这儿格式化。** 这一层是报文的样子；给人看的样子归
  /// `ui/storage_page.dart` 的 `formatStamp`。
  final String startedAt;

  /// 保留期策略值（天）。**不是「还剩几天」**，见类注释。
  final int retentionDays;
  final int sizeBytes;

  const StorageRun({
    required this.path,
    required this.mission,
    required this.startedAt,
    required this.retentionDays,
    required this.sizeBytes,
  });

  factory StorageRun.fromJson(Map<String, dynamic> m) => StorageRun(
        path: (m['path'] as String?) ?? '',
        mission: (m['mission'] as String?) ?? '',
        startedAt: (m['started_at'] as String?) ?? '',
        retentionDays: (m['retention_days'] as num?)?.toInt() ?? 0,
        sizeBytes: (m['size_bytes'] as num?)?.toInt() ?? 0,
      );
}

/// 备份盘那句话说到什么份上（`engine/backup.py` 的 `NoticeLevel`）。
///
/// **只有三档，没有 `warn`。** 狗那头从来不发 `warn` —— 拿它写断言的测试会
/// 绿，但什么也没钉住。
///
/// * `ok` —— 配了镜像盘、跟得上。
/// * `neutral` —— 未配备份盘。**中性的一句话，不是红的。**
/// * `push` —— 该顶到人脸上了。
///
/// **`neutral` 不许画成警示色。** `backup_notice` 的 docstring 写着：没配镜像
/// 盘不许常年报红（spec §7.6）—— 常年报警的东西等于没报警，现场的人会先学会
/// 忽略它，然后连真的那次也一起忽略。`NEUTRAL` 单独立一档就是为了这件事：
/// 手机端把它画黄画红，每一台没插镜像盘的狗从此常年顶着警示色。
const String backupLevelOk = 'ok';
const String backupLevelNeutral = 'neutral';
const String backupLevelPush = 'push';

/// 盘况（`GET /api/storage`，`app/server.py` 里那个 handler）。
///
/// **顶层只有这些键**：`used_bytes` / `total_bytes` / `used_ratio` /
/// `runs_total` / `runs_bytes` / `baselines_bytes` / `exports_bytes` /
/// `forecast` / `notice_written` / `notice_detail` / `backup`。
///
/// `runs` / `bytes_at_risk` / 预告那句话**都在 `forecast` 段里**，不在顶层；
/// `level` / `detail` 在 `backup` 段里。这里摊平了存，但解析要往里伸手 ——
/// 写 `m['runs']` 的话恒为 `null`，屏上那份名单永远是空的，而测试要是拿
/// 自己造的报文喂它，两头都是绿的。
class StorageView {
  final int usedBytes;
  final int totalBytes;
  final double usedRatio;
  final int runsTotal;
  final int runsBytes;
  final int baselinesBytes;
  final int exportsBytes;

  /// 预告到底记到盘上了没有。
  ///
  /// **为假的时候必须上屏。** 它的意思是盘满了或者被挂成只读，「再过几天
  /// 开始删除」那句话说了不算 —— 删除的钟根本没开始走。不显示的话，人看到的
  /// 是一块一切正常的屏，而归档会无声地堆到盘炸。
  final bool noticeWritten;

  /// 预告没落盘时狗那头给的那句话。**原样上屏，手机不重新组织。**
  final String noticeDetail;

  /// 见 [backupLevelNeutral]。认不出的值一律按中性处理，别崩。
  final String backupLevel;

  /// 备份盘那句话。**原样上屏。**
  final String backupDetail;

  /// 预告那句话（`Forecast.detail`）。**原样上屏。**
  ///
  /// `retention.py` 里 `Forecast` 的 docstring 明写「文案在这里拼，不在 app 里
  /// 拼 —— 免得手机端和网页端各说各的，而这是要给客户看的一句话」。
  /// **「还剩几天 / 即将删除」这类话只准出自这里。**
  final String forecastDetail;

  final int bytesAtRisk;

  /// 预告名单里的那几趟。夹具里是空数组（那台狗上一趟归档也没有）。
  final List<StorageRun> runs;

  const StorageView({
    this.usedBytes = 0,
    this.totalBytes = 0,
    this.usedRatio = 0,
    this.runsTotal = 0,
    this.runsBytes = 0,
    this.baselinesBytes = 0,
    this.exportsBytes = 0,
    this.noticeWritten = true,
    this.noticeDetail = '',
    this.backupLevel = backupLevelNeutral,
    this.backupDetail = '',
    this.forecastDetail = '',
    this.bytesAtRisk = 0,
    this.runs = const <StorageRun>[],
  });

  factory StorageView.fromJson(Map<String, dynamic> m) {
    final Map<String, dynamic> fc =
        (m['forecast'] as Map<String, dynamic>?) ?? <String, dynamic>{};
    final Map<String, dynamic> bk =
        (m['backup'] as Map<String, dynamic>?) ?? <String, dynamic>{};
    return StorageView(
      usedBytes: (m['used_bytes'] as num?)?.toInt() ?? 0,
      totalBytes: (m['total_bytes'] as num?)?.toInt() ?? 0,
      usedRatio: (m['used_ratio'] as num?)?.toDouble() ?? 0,
      runsTotal: (m['runs_total'] as num?)?.toInt() ?? 0,
      runsBytes: (m['runs_bytes'] as num?)?.toInt() ?? 0,
      baselinesBytes: (m['baselines_bytes'] as num?)?.toInt() ?? 0,
      exportsBytes: (m['exports_bytes'] as num?)?.toInt() ?? 0,
      // **缺了当真**：老版本的狗没有这一项时，按「预告落盘了」处理才不会
      // 凭空吓人；真出事的那台狗一定会把 false 发过来。
      noticeWritten: m['notice_written'] != false,
      noticeDetail: (m['notice_detail'] as String?) ?? '',
      backupLevel: (bk['level'] as String?) ?? backupLevelNeutral,
      backupDetail: (bk['detail'] as String?) ?? '',
      forecastDetail: (fc['detail'] as String?) ?? '',
      bytesAtRisk: (fc['bytes_at_risk'] as num?)?.toInt() ?? 0,
      runs: <StorageRun>[
        for (final Object? r in (fc['runs'] as List<dynamic>?) ?? const [])
          StorageRun.fromJson(r as Map<String, dynamic>),
      ],
    );
  }
}
