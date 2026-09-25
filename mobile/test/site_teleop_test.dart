// 站点模式的遥控页（W00c5c）：按住发帧、松手零速、限速按站点给的、没画面杆变灰、「停」走 halt、
// 结束原因说人话、退出放租。
import 'package:d1max_patrol/net/site_client.dart' show SiteError;
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:d1max_patrol/ui/widget/joystick.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

Future<void> _open(WidgetTester t, FakeApi api) async {
  // 每次一个新 key:同一个位置上的页面会复用旧的 State,新的假站点就不会被叫到。
  await t.pumpWidget(MaterialApp(home: SiteTeleopPage(key: UniqueKey(), api: api, robotId: 'A')));
  await t.pump();
  api.link!.ctl.add(<String, dynamic>{'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5,
    'max_wz': 0.75});
  await t.pump();
  await t.pump(); // 广播流晚一个微任务才送到:再画一帧
}

bool _stickEnabled(WidgetTester t, int i) => t.widget<Joystick>(find.byType(Joystick).at(i)).enabled;

void main() {
  testWidgets('按住前推每 100 ms 一帧、速度按站点给的限速；松手发零速', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    expect(find.textContaining('你在遥控'), findsOneWidget);
    expect(_stickEnabled(t, 0), isTrue);
    final g = await t.startGesture(t.getCenter(find.byType(Joystick).at(0)));
    await g.moveBy(const Offset(0, -200));                  // 推满
    await t.pump(const Duration(milliseconds: 350));
    final sent = List<List<double>>.of(api.link!.sent);
    expect(sent.length, inInclusiveRange(2, 5));
    expect(sent.last[0], closeTo(0.5, 1e-9), reason: '推满 = 站点给的限速');
    await g.up();
    await t.pump(const Duration(milliseconds: 150));
    expect(api.link!.sent.last, <double>[0, 0]);
    await t.pumpWidget(Container());
  });

  testWidgets('右杆右推是顺时针（wz 为负）', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    final g = await t.startGesture(t.getCenter(find.byType(Joystick).at(1)));
    await g.moveBy(const Offset(200, 0));
    await t.pump(const Duration(milliseconds: 150));
    expect(api.link!.sent.last[1], closeTo(-0.75, 1e-9));
    await g.up();
    await t.pumpWidget(Container());
  });

  testWidgets('没画面杆变灰、屏上说不许动；结束了说原因', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    api.link!.ctl.add(<String, dynamic>{'kind': 'video', 'ok': false});
    await t.pump();
    await t.pump();
    expect(_stickEnabled(t, 0), isFalse);
    expect(find.textContaining('没有画面'), findsOneWidget);
    api.link!.ctl.add(<String, dynamic>{'kind': 'video', 'ok': true});
    await t.pump();
    await t.pump();
    expect(_stickEnabled(t, 0), isTrue);
    api.link!.ctl.add(<String, dynamic>{'kind': 'ended', 'reason': 'taken_over'});
    await t.pump();
    await t.pump();
    expect(find.text('管理员接管了遥控'), findsOneWidget);
    expect(_stickEnabled(t, 0), isFalse);
    await t.pumpWidget(Container());
  });

  testWidgets('「停」走 halt 不走遥控连接，同时发零速', (t) async {
    final api = FakeApi('owner');
    await _open(t, api);
    await t.tap(find.byKey(SiteTeleopPage.stopKey));
    await t.pump();
    expect(api.calls, contains('halt A'));
    expect(api.link!.sent.last, <double>[0, 0]);
    await t.pumpWidget(Container());
  });

  testWidgets('被拒说站点给的原因；退出放租并关连接', (t) async {
    final refused = FakeApi('guard')..teleopError = const SiteError(409, 'gina 正在遥控 A');
    await t.pumpWidget(MaterialApp(home: SiteTeleopPage(api: refused, robotId: 'A')));
    await t.pump();
    await t.pump();
    expect(find.textContaining('gina 正在遥控 A'), findsOneWidget);
    final api = FakeApi('guard');
    await _open(t, api);
    await t.pumpWidget(Container());
    expect(api.link!.released && api.link!.isClosed, isTrue);
  });

  testWidgets('单狗页：保安有遥控按钮，业主没有', (t) async {
    for (final (role, has) in [('guard', true), ('owner', false)]) {
      final api = FakeApi(role);
      await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
      await t.pump();
      expect(find.byKey(const Key('btn-teleop')), has ? findsOneWidget : findsNothing, reason: role);
      await t.pumpWidget(Container());
    }
  });
}
