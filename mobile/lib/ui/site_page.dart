/// 站点模式的几屏（W00c4）：站点列表 → 登录 → 狗的列表（实时）→ 单狗页（派巡检、叫停、回待命点）；
/// 事件账、排程、值守（W00c5a）各一页。
///
/// **升到声音档、还没人确认的告警**（SSE `kind: alert`）：狗列表这一屏在栈底一直开着，由它响铃，
/// 不管人此刻在看哪一页。App 没开时的系统推送不在 W00c5a 里。
///
/// **按钮按角色显示**：经理（owner）只有「叫停」；保安（guard）与管理员（admin）能派单。真正的
/// 权限在站点上（站点回 403），这里只是不给人一个注定被拒的按钮。
///
/// 2026-10-14 起这几屏各在各的文件里；这个文件只把它们一起导出（老的导入照旧能用）。
library;

export 'nw/nw_shell.dart' show NwShell;
export 'site_incidents.dart';
export 'site_list.dart';
export 'site_robot.dart';
export 'site_robots.dart';
export 'site_schedule.dart';
