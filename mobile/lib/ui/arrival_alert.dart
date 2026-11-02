/// W69 巡检到点提醒（用户 2026-11-03）：现场测试时不用盯着地图看机器人到没到点。
///
/// 设置页里的开关，默认关、只存在内存里（App 重启就关）：平时巡检不会每个点都响。
/// 开着时机器人列表页收到代理的 `patrol_waypoint` / 任务结束事件就响一声、弹一行字；
/// 没到的点、任务失败或中止响两声。
library;

import 'package:flutter/foundation.dart';

import '../l10n.dart';

/// 到点提醒开没开。
final ValueNotifier<bool> arrivalAlerts = ValueNotifier<bool>(false);

class ArrivalNote {
  final String text;

  /// 没到 / 失败 / 中止：响两声。
  final bool urgent;
  const ArrivalNote(this.text, {this.urgent = false});
}

/// 站点事件流的一帧 → 要提醒的一行字；不是巡检到点、任务结束的就 null。
ArrivalNote? arrivalNote(Map<String, dynamic> frame) {
  if (frame['kind'] != 'event') return null;
  final ev = frame['event'];
  if (ev is! Map) return null;
  final d = ev['data'] is Map ? ev['data'] as Map : const <String, dynamic>{};
  final robot = '${frame['robot_id'] ?? ''}';
  String why(Object? r) => r == null || '$r'.isEmpty ? '' : tr(': $r', '：$r');
  switch (ev['kind']) {
    case 'patrol_waypoint':
      final i = d['index'];
      final n = d['total'];
      final name = '${d['name'] ?? ''}';
      final pos = i is int
          ? (n is int ? tr(' (${i + 1}/$n)', '（${i + 1}/$n）') : tr(' (#${i + 1})', '（第 ${i + 1} 个）'))
          : '';
      if (d['ok'] == true) {
        return ArrivalNote(tr('$robot reached $name$pos', '$robot 已到达 $name$pos'));
      }
      return ArrivalNote(tr('$robot missed $name$pos${why(d['note'])}', '$robot 未到达 $name$pos${why(d['note'])}'),
          urgent: true);
    case 'task_done':
      return ArrivalNote(tr('$robot task completed', '$robot 任务完成'));
    case 'task_failed':
      return ArrivalNote(tr('$robot task failed${why(d['reason'])}', '$robot 任务失败${why(d['reason'])}'),
          urgent: true);
    case 'task_aborted':
      return ArrivalNote(tr('$robot task stopped${why(d['reason'])}', '$robot 任务已中止${why(d['reason'])}'),
          urgent: true);
  }
  return null;
}
