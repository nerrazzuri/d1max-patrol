// 机器人详情页「最近的事件」写成人话（2026-10-30 走查：原来把事件数据原样印出来）。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/ui/robot_events.dart';
import 'package:d1max_patrol/ui/site_robot.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

void main() {
  tearDown(() => appLang.value = AppLang.en);

  test('标题：查表、借告警词条、都不认识就把下划线换空格', () {
    appLang.value = AppLang.zh;
    expect(robotEventTitle('task_failed'), '任务失败');
    expect(robotEventTitle('nav_blocked'), isNot(contains('_')), reason: '告警词条里有');
    expect(robotEventTitle('brand_new_kind'), 'brand new kind');
    appLang.value = AppLang.en;
    expect(robotEventTitle('task_done'), 'Task done');
  });

  test('说明：只挑人读得懂的字段；程序代号、任务号不写', () {
    appLang.value = AppLang.zh;
    expect(robotEventDetail('task_failed', {'reason': '急停按下', 'task_id': 'task-1'}), '急停按下');
    expect(robotEventDetail('task_preempted', {'reason': 'preempted', 'task_id': 'task-1'}), '');
    expect(robotEventDetail('task_progress', {'distance_m': 4.04, 'task_id': 't'}), '还有 4.0 米');
    expect(robotEventDetail('patrol_waypoint', {'name': '东门', 'ok': false, 'note': '被挡'}),
        '东门 · 没到 · 被挡');
    expect(robotEventDetail('robot_fault', {'faults': []}), '');
  });

  test('时刻：没有就空串', () {
    expect(robotEventTime(null), '');
    expect(robotEventTime(DateTime(2026, 10, 30, 9, 5, 7).millisecondsSinceEpoch), '10-30 09:05:07');
  });

  testWidgets('详情页：事件是标题 + 说明，不出现原始数据', (t) async {
    appLang.value = AppLang.zh;
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: FakeApi('owner'), robotId: 'A')));
    await t.pump();
    await t.pump(const Duration(milliseconds: 100));
    await t.scrollUntilVisible(find.text('最近的事件'), 300, scrollable: find.byType(Scrollable).last);
    await t.pump();
    expect(find.text('任务失败'), findsWidgets);
    expect(find.textContaining('task_id'), findsNothing);
    expect(find.textContaining('{'), findsNothing);
    await t.pumpWidget(Container());
  });
}
