/// 机器人详情页「最近的事件」：把代理报上来的事件（`kind` + `data`）写成人看得懂的一行。
///
/// 2026-10-30 走查：原来直接把 `data` 整块原样印出来（`{task_id: …, distance_m: …}`）。现在：
/// 常见的种类查表给标题；告警词条里有的种类借用告警标题；都不认识的把下划线换成空格。
/// 说明只挑人读得懂的几个字段（原因、名字、距离），挑不出来就不写 —— 不再印原始数据。
library;

import '../design/alert_titles.g.dart';
import '../l10n.dart';

String? _title(String kind) => switch (kind) {
      'task_done' => tr('Task done', '任务完成'),
      'task_failed' => tr('Task failed', '任务失败'),
      'task_aborted' => tr('Task stopped', '任务中止'),
      'task_preempted' => tr('Task replaced', '任务被新任务顶掉'),
      'task_progress' => tr('On the way', '在路上'),
      'patrol_waypoint' => tr('Patrol point', '巡检点'),
      'relocalized' => tr('Position re-set', '重新定位'),
      'loc_wrong_solution' => tr('Position looked wrong', '定位结果不对'),
      'loc_conflict_resolved' => tr('Position conflict resolved', '定位冲突已解决'),
      'map_activated' => tr('Map switched', '换了地图'),
      'map_built' => tr('Map built', '建图完成'),
      'map_build_failed' => tr('Map build failed', '建图失败'),
      'map_activate_failed' => tr('Map switch failed', '换地图失败'),
      'map_load_failed' => tr('Map load failed', '地图没加载上'),
      'mapping' => tr('Mapping', '建图'),
      'mapping_failed' => tr('Mapping failed', '建图失败'),
      'recording_ok' => tr('Recording saved', '录像存好了'),
      'video_failed' => tr('Video failed', '画面出错'),
      'robot_fault' => tr('Robot fault', '机器人故障'),
      'dock_hazard' => tr('Dock blocked', '充电桩前有障碍'),
      'dock_hazard_cleared' => tr('Dock clear', '充电桩前障碍已清'),
      'head_changed' => tr('Front and back swapped', '前后方向换了'),
      'home_marked' => tr('Home point set', '标了原点'),
      'board_thermal' => tr('Board temperature', '主板温度'),
      'thermal_lens_blocked' => tr('Thermal lens blocked', '热像镜头被挡'),
      'thermal_lens_clear' => tr('Thermal lens clear', '热像镜头恢复'),
      'release_installed' => tr('Update installed', '更新已装好'),
      'release_activating' => tr('Update starting', '正在启用更新'),
      'release_committed' => tr('Update done', '更新完成'),
      'release_install_failed' => tr('Update install failed', '更新没装上'),
      'release_rolling_back' => tr('Rolling back update', '更新回退中'),
      'release_rolled_back' => tr('Update rolled back', '更新已回退'),
      'outbox_retry' => tr('Resending', '重发'),
      'archive_write_failed' => tr('Archive write failed', '存档写失败'),
      'map_integrity_failed' => tr('Map files damaged', '地图文件损坏'),
      _ => null,
    };

/// 事件标题：查表 → 告警词条 → 种类名（下划线换空格）。
String robotEventTitle(String kind) {
  final own = _title(kind);
  if (own != null) return own;
  final t = alertTitles[kind];
  if (t != null) return tr(t.$1, t.$2);
  return kind.replaceAll('_', ' ');
}

final RegExp _code = RegExp(r'^[a-z_]+$');

/// 事件说明：只挑人读得懂的字段，挑不出来是空串。
String robotEventDetail(String kind, Map<String, dynamic> data) {
  final parts = <String>[];
  final name = data['name'];
  if (name is String && name.isNotEmpty) parts.add(name);
  if (kind == 'patrol_waypoint' && data['ok'] is bool) {
    parts.add(data['ok'] == true ? tr('OK', '到了') : tr('missed', '没到'));
  }
  final dist = data['distance_m'];
  if (dist is num) parts.add(tr('${dist.toStringAsFixed(1)} m to go', '还有 ${dist.toStringAsFixed(1)} 米'));
  for (final k in const <String>['reason', 'why', 'note', 'message', 'error', 'detail']) {
    final v = data[k];
    // `preempted`、`halt` 这种是程序里的代号，人看不懂，跳过
    if (v is String && v.isNotEmpty && !_code.hasMatch(v)) {
      parts.add(v);
      break;
    }
  }
  return parts.join(' · ');
}

/// 事件时刻（站点给的 `stamp`，毫秒）→ 本机时间 `MM-dd HH:mm:ss`；没有就空串。
String robotEventTime(Object? stamp) {
  if (stamp is! int || stamp <= 0) return '';
  final t = DateTime.fromMillisecondsSinceEpoch(stamp);
  String two(int v) => v.toString().padLeft(2, '0');
  return '${two(t.month)}-${two(t.day)} ${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
}
