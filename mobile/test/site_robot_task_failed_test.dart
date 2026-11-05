// POC 11-05：派了巡检，机器人起飞前检查没过（比如地图未标原点）—— 站点已经收下了，原因是机器人后来报的
// `task_failed`。单机器人页要说原因、说怎么办，不能只是「没动」。
import 'package:nexwar_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

void main() {
  testWidgets('任务失败：单机器人页说原因；未标原点说怎么办；别的机器人的不管', (t) async {
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    api.sse.add(<String, dynamic>{'kind': 'event', 'robot_id': 'B', 'event': <String, dynamic>{
      'kind': 'task_failed', 'data': <String, dynamic>{'task_id': 'p0', 'reason': '别的机器人'}}});
    await t.pump();
    await t.pump();
    expect(find.textContaining('别的机器人'), findsNothing);
    api.sse.add(<String, dynamic>{'kind': 'event', 'robot_id': 'A', 'event': <String, dynamic>{
      'kind': 'task_failed',
      'data': <String, dynamic>{'task_id': 'patrol-1', 'reason': "起飞前检查没过:地图 'mall' 未标原点,先去标一个换电位"}}});
    await t.pump();
    await t.pump();
    expect(find.textContaining('未标原点'), findsOneWidget);
    expect(find.textContaining('设好位置后点「在这儿标原点」'), findsOneWidget, reason: '说怎么办');
    await t.pumpWidget(Container());
  });
}
