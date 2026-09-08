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
