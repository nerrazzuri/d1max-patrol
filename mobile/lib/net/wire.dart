/// 狗发过来的那几样东西，在手机这头的样子。
///
/// **全是只读的。** 真理源在狗身上，手机改了也不算数；能改的模型只会诱使人
/// 在界面上先改再发，而那两步之间的窗口里，屏幕上显示的是一件没发生的事。
///
/// 形状由 `test/fixtures/*.json` 钉着，那些文件是狗那头生成的（挂账 11）。
library;

class LeaseView {
  /// 还剩多久到期（毫秒）。
  ///
  /// **一律读这个相对量，不许拿绝对时刻去减手机自己的钟**：狗上没有 NTP，
  /// 两头的钟能差出几分钟（`engine/lease.py` 的 `LeaseState` 写着）。
  final int expiresInMs;
  final int? graceInMs;
  final String? holderRef;
  final String holderOperator;

  const LeaseView({
    required this.expiresInMs,
    this.graceInMs,
    this.holderRef,
    this.holderOperator = '',
  });

  factory LeaseView.fromJson(Map<String, dynamic> m) {
    final h = m['holder'] as Map<String, dynamic>?;
    return LeaseView(
      expiresInMs: (m['expires_in_ms'] as num?)?.toInt() ?? 0,
      graceInMs: (m['grace_in_ms'] as num?)?.toInt(),
      holderRef: h?['ref'] as String?,
      holderOperator: (h?['operator'] as String?) ?? '',
    );
  }

  bool get held => holderRef != null;
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
