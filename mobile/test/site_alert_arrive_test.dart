// W74 一期：看见人以后 —— 保安点「已到场」才能处置完成 / 标误报；去向不明、最后看见的位置。
import 'package:nexwar_patrol/model/alert.dart';
import 'package:nexwar_patrol/ui/site_alert_scene.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

Alert _person({Map<String, dynamic> extra = const <String, dynamic>{}}) =>
    Alert.fromWire(<String, dynamic>{
      'key': 'A/dog_sees_person#1', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'A',
      'title': '机器人看见人了:1 个人', 'detail': '', 'first_ms': 1, 'last_ms': 1, 'count': 1,
      'acked_by': '', 'acked_ms': null, 'resolved_by': '', 'resolved_ms': null, 'escalated': 0,
      'channel': 'screen',
      'context': <String, dynamic>{
        'task_id': 'persons',
        'pose': <String, dynamic>{'map_id': 'm', 'map_version': '1', 'x': 1.0, 'y': 1.0, 'at_ms': 1},
        'last_seen': <String, dynamic>{'map_id': 'm', 'map_version': '1', 'x': 5.0, 'y': 6.0,
          'yaw': 0.0, 'at_ms': 2000},
        ...extra,
      },
    });

Future<void> _tall(WidgetTester t) async {
  await t.binding.setSurfaceSize(const Size(420, 900));
  addTearDown(() => t.binding.setSurfaceSize(null));
}

void main() {
  testWidgets('我来处理、已到场都在；没到场误报点不了；点了发 arrive', (t) async {
    await _tall(t);
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: _person())));
    await t.pumpAndSettle();
    expect(find.byKey(SiteAlertScenePage.ackKey), findsOneWidget);
    final arrive = find.byKey(SiteAlertScenePage.arriveKey);
    expect(arrive, findsOneWidget);
    expect(find.textContaining('点「已到场」后才能关闭'), findsOneWidget);
    await t.tap(find.byKey(SiteAlertScenePage.falseKey));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('resolve')), isEmpty, reason: '误报要先到场');
    await t.tap(arrive);
    await t.pumpAndSettle();
    expect(api.calls, contains('arrive A/dog_sees_person#1'));
    expect(find.byKey(SiteAlertScenePage.doneKey), findsOneWidget, reason: '到场了：换成「处置完成」');
  });

  testWidgets('到场了：处置完成要确认，确认了才发 resolve', (t) async {
    await _tall(t);
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(
        home: SiteAlertScenePage(
            api: api, alert: _person(extra: <String, dynamic>{'arrived_by': '张三', 'arrived_ms': 3000}))));
    await t.pumpAndSettle();
    expect(find.textContaining('张三 已到场'), findsOneWidget);
    await t.tap(find.byKey(SiteAlertScenePage.doneKey));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('resolve')), isEmpty);
    await t.tap(find.byKey(SiteAlertScenePage.doneGoKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('resolve A/dog_sees_person#1'));
  });

  testWidgets('去向不明：写出几点起看不到；最后位置用最后看见的那一处', (t) async {
    await _tall(t);
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(
        home: SiteAlertScenePage(api: api, alert: _person(extra: <String, dynamic>{'lost_ms': 4000}))));
    await t.pumpAndSettle();
    expect(find.textContaining('看不到人了，去向不明'), findsWidgets);
    expect(find.textContaining('(5.0, 6.0)'), findsOneWidget);
    final (_, _, marks) = sceneMarks(_person())!;
    expect((marks.first.x, marks.first.y), (5.0, 6.0));
  });

  testWidgets('W74 二期：在跟写「正跟着」；跟丢了写最后在哪，图上标人最后的位置', (t) async {
    await _tall(t);
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(
        home: SiteAlertScenePage(api: api, alert: _person(extra: <String, dynamic>{
      'follow': 'follow', 'person': <String, dynamic>{'x': 7.0, 'y': 8.0, 'at_ms': 3000}}))));
    await t.pumpAndSettle();
    expect(find.textContaining('机器人正跟着这个人'), findsOneWidget);
    final lost = _person(extra: <String, dynamic>{
      'follow': 'lost', 'lost_ms': 4000,
      'person': <String, dynamic>{'x': 7.0, 'y': 8.0, 'at_ms': 3500, 'where': '东门附近'}});
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: lost)));
    await t.pumpAndSettle();
    expect(find.textContaining('机器人正跟着这个人'), findsNothing);
    expect(find.textContaining('跟丢了，人最后在 东门附近'), findsOneWidget);
    final (_, _, marks) = sceneMarks(lost)!;
    expect(marks.map((m) => (m.x, m.y)), contains((7.0, 8.0)));
  });

  testWidgets('别的告警照旧：我来处理、误报', (t) async {
    await _tall(t);
    final api = FakeApi('guard');
    final other = Alert.fromWire(<String, dynamic>{
      'key': 'A/stuck#1', 'level': 'P1', 'kind': 'stuck', 'robot': 'A', 'title': '卡住了',
      'first_ms': 1, 'last_ms': 1, 'count': 1, 'context': <String, dynamic>{}});
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: other)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteAlertScenePage.ackKey), findsOneWidget);
    expect(find.byKey(SiteAlertScenePage.arriveKey), findsNothing);
  });
}
