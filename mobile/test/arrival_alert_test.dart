// W69 巡检到点提醒（用户 2026-11-03：现场测试时不用盯着地图看到没到点）。
import 'package:nexwar_patrol/l10n.dart';
import 'package:nexwar_patrol/ui/arrival_alert.dart';
import 'package:nexwar_patrol/ui/site_robots.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

Map<String, dynamic> _ev(String kind, Map<String, dynamic> data) => <String, dynamic>{
      'kind': 'event',
      'robot_id': 'C40221',
      'event': <String, dynamic>{'event_id': 'e1', 'seq': 1, 'kind': kind, 'data': data},
    };

void main() {
  setUp(() => appLang.value = AppLang.zh);
  tearDown(() {
    appLang.value = AppLang.en;
    arrivalAlerts.value = false;
  });

  test('到点：第几个 / 共几个；没到：说原因、算急的', () {
    final ok = arrivalNote(_ev('patrol_waypoint',
        <String, dynamic>{'task_id': 't1', 'index': 1, 'total': 5, 'name': 'P2', 'ok': true}))!;
    expect(ok.text, 'C40221 已到达 P2（2/5）');
    expect(ok.urgent, isFalse);
    final bad = arrivalNote(_ev('patrol_waypoint', <String, dynamic>{
      'task_id': 't1', 'index': 2, 'total': 5, 'name': 'P3', 'ok': false, 'note': '路被挡住'
    }))!;
    expect(bad.text, 'C40221 未到达 P3（3/5）：路被挡住');
    expect(bad.urgent, isTrue);
    // 老代理没报 total：只说第几个
    expect(arrivalNote(_ev('patrol_waypoint',
            <String, dynamic>{'index': 0, 'name': 'P1', 'ok': true}))!.text,
        'C40221 已到达 P1（第 1 个）');
  });

  test('任务结束也提醒；别的事件、别的帧不管', () {
    expect(arrivalNote(_ev('task_done', <String, dynamic>{'task_id': 't1'}))!.text, 'C40221 任务完成');
    final f = arrivalNote(_ev('task_failed', <String, dynamic>{'task_id': 't1', 'reason': '定位丢失'}))!;
    expect(f.text, 'C40221 任务失败：定位丢失');
    expect(f.urgent, isTrue);
    expect(arrivalNote(_ev('task_aborted', <String, dynamic>{'task_id': 't1'}))!.urgent, isTrue);
    expect(arrivalNote(_ev('relocalized', <String, dynamic>{})), isNull);
    expect(arrivalNote(<String, dynamic>{'kind': 'status'}), isNull);
    expect(arrivalNote(<String, dynamic>{'kind': 'event', 'event': 'x'}), isNull);
  });

  testWidgets('开着：到点响一声、弹提示；没到响两声；关着不响', (t) async {
    final api = FakeApi('guard');
    var rang = 0;
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, ring: () => rang++)));
    await t.pumpAndSettle();
    api.sse.add(_ev('patrol_waypoint',
        <String, dynamic>{'index': 0, 'total': 3, 'name': 'P1', 'ok': true}));
    await t.pump();
    await t.pump(const Duration(seconds: 1));
    expect(rang, 0, reason: '默认关着');
    arrivalAlerts.value = true;
    api.sse.add(_ev('patrol_waypoint',
        <String, dynamic>{'index': 1, 'total': 3, 'name': 'P2', 'ok': true}));
    await t.pump();
    expect(rang, 1);
    await t.pump(const Duration(milliseconds: 300));
    expect(find.text('C40221 已到达 P2（2/3）'), findsOneWidget);
    await t.pump(const Duration(seconds: 1));
    expect(rang, 1);
    api.sse.add(_ev('patrol_waypoint',
        <String, dynamic>{'index': 2, 'total': 3, 'name': 'P3', 'ok': false, 'note': '超时'}));
    await t.pump();
    await t.pump(const Duration(seconds: 1));
    expect(rang, 3);
    await t.pump(const Duration(seconds: 5));
  });
}
