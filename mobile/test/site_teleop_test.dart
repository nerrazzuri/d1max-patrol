// 站点模式的遥控页（W00c5c）：按住发帧、松手零速、限速按站点给的、没画面杆变灰、「停」走 halt、
// 结束原因说人话、退出放租。
import 'dart:async';

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

  testWidgets('切到后台：零速、放租、关连接，屏上说遥控结束了（W00c5e 内部评审）', (t) async {
    // 后台里定时器还在跑的话，手指按着的那个杆值会一直发出去，狗接着走 —— 而人已经不在看了。
    final api = FakeApi('guard');
    await _open(t, api);
    final g = await t.startGesture(t.getCenter(find.byType(Joystick).at(0)));
    await g.moveBy(const Offset(0, -200));
    await t.pump(const Duration(milliseconds: 150));
    expect(api.link!.sent.last[0], greaterThan(0));
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await t.pump(const Duration(milliseconds: 350));
    expect(api.link!.sent.last, <double>[0, 0]);
    final n = api.link!.sent.length;
    await t.pump(const Duration(milliseconds: 500));
    expect(api.link!.sent.length, n, reason: '后台里不再发帧');
    expect(api.link!.released, isTrue);
    expect(api.link!.isClosed, isTrue);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await t.pump();
    expect(find.textContaining('切到后台'), findsOneWidget);
    expect(_stickEnabled(t, 0), isFalse, reason: '回来也不接着开：要重新进这一页');
    await g.up();
    await t.pumpWidget(Container());
  });

  testWidgets('通知栏拉一下（inactive）：杆值归零、只发零速，不结束遥控', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    final g = await t.startGesture(t.getCenter(find.byType(Joystick).at(0)));
    await g.moveBy(const Offset(0, -200));
    await t.pump(const Duration(milliseconds: 150));
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await t.pump(const Duration(milliseconds: 250));
    expect(api.link!.sent.last, <double>[0, 0], reason: '手指多半被系统收走了:按零速发');
    await g.up();
    // 系统盖着的时候杆上又来了一下(新的一次按下):也只发零速。
    final g2 = await t.startGesture(t.getCenter(find.byType(Joystick).at(0)));
    await g2.moveBy(const Offset(0, -200));
    await t.pump(const Duration(milliseconds: 250));
    expect(api.link!.sent.last, <double>[0, 0]);
    expect(api.link!.released, isFalse);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await t.pump();
    expect(_stickEnabled(t, 0), isTrue);
    await g2.up();
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

  testWidgets('「停」走 halt 不走遥控连接，同时发零速、本机先收手（不再发杆值、放租、杆变灰）', (t) async {
    final api = FakeApi('owner');
    await _open(t, api);
    final g = await t.startGesture(t.getCenter(find.byType(Joystick).at(0)));
    await g.moveBy(const Offset(0, -200));
    await t.pump(const Duration(milliseconds: 150));
    await t.tap(find.byKey(SiteTeleopPage.stopKey));
    await t.pump();
    expect(api.calls, contains('halt A'));
    expect(api.link!.sent.last, <double>[0, 0]);
    expect(api.link!.released, isTrue);
    final n = api.link!.sent.length;
    await t.pump(const Duration(milliseconds: 500));
    expect(api.link!.sent.length, n, reason: '按了停之后手指还按在杆上也不许接着发');
    expect(_stickEnabled(t, 0), isFalse);
    await g.up();
    await t.pumpWidget(Container());
  });

  testWidgets('按着杆时画面断了：当场发零速；画面回来也不接着开（手指早就松了）', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    final g = await t.startGesture(t.getCenter(find.byType(Joystick).at(0)));
    await g.moveBy(const Offset(0, -200));
    await t.pump(const Duration(milliseconds: 250));
    expect(api.link!.sent.last[0], greaterThan(0));
    api.link!.ctl.add(<String, dynamic>{'kind': 'video', 'ok': false});
    await t.pump();
    await t.pump();
    expect(api.link!.sent.last, <double>[0, 0], reason: '画面一断当场零速');
    final n = api.link!.sent.length;
    await t.pump(const Duration(milliseconds: 300));
    expect(api.link!.sent.length, n, reason: '没画面不发杆值');
    await g.up();                                          // 手指抬起:被灰掉的杆吞了
    await t.pump();
    api.link!.ctl.add(<String, dynamic>{'kind': 'video', 'ok': true});
    await t.pump();
    await t.pump(const Duration(milliseconds: 350));
    final after = api.link!.sent.sublist(n);
    expect(after, isNotEmpty);
    expect(after.every((f) => f[0] == 0 && f[1] == 0), isTrue,
        reason: '画面回来之后杆值是零,不许按断之前的值开走: $after');
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

  testWidgets('连接还没回来就切后台：回来的那条连接当场零速、放租、关掉，不留租约（W00c5 外审阻断 2）',
      (t) async {
    // 以前 _open() 等到连接回来就照常存下、订阅 —— 手机上写着遥控结束了，站点上的租约却一直占着，
    // 自动巡检、事件派遣、别人接管都派不了。
    final api = FakeApi('guard')..teleopGate = Completer<void>();
    await t.pumpWidget(MaterialApp(home: SiteTeleopPage(key: UniqueKey(), api: api, robotId: 'A')));
    await t.pump();
    expect(api.link, isNull, reason: '前提：连接还在路上');
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await t.pump();
    api.teleopGate!.complete();
    await t.pump();
    await t.pump();
    final link = api.link!;
    expect(link.released, isTrue, reason: '回来的连接要放租');
    expect(link.isClosed, isTrue, reason: '回来的连接要关掉');
    expect(link.sent, <List<double>>[<double>[0, 0]], reason: '只发一帧零速');
    link.ctl.add(<String, dynamic>{'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5,
      'max_wz': 0.75});
    await t.pump();
    await t.pump(const Duration(milliseconds: 500));
    expect(link.sent.length, 1, reason: '晚到的 granted 不许把定时器点起来');
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await t.pump();
    expect(find.textContaining('切到后台'), findsOneWidget);
    expect(_stickEnabled(t, 0), isFalse);
    await t.pumpWidget(Container());
  });

  testWidgets('连接还没回来就按了「停」：halt 照发，回来的连接一样放租关掉', (t) async {
    final api = FakeApi('guard')..teleopGate = Completer<void>();
    await t.pumpWidget(MaterialApp(home: SiteTeleopPage(key: UniqueKey(), api: api, robotId: 'A')));
    await t.pump();
    await t.tap(find.byKey(SiteTeleopPage.stopKey));
    await t.pump();
    expect(api.calls, contains('halt A'));
    api.teleopGate!.complete();
    await t.pump();
    await t.pump();
    expect(api.link!.released, isTrue);
    expect(api.link!.isClosed, isTrue);
    expect(find.text('你按了停车'), findsOneWidget);
    await t.pumpWidget(Container());
  });

  testWidgets('连上了、还没授权就按了「停」：晚到的 granted 不许把页面翻回「你在遥控」', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteTeleopPage(key: UniqueKey(), api: api, robotId: 'A')));
    await t.pump();
    await t.tap(find.byKey(SiteTeleopPage.stopKey));
    await t.pump();
    expect(api.link!.released, isTrue);
    api.link!.ctl.add(<String, dynamic>{'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5,
      'max_wz': 0.75});
    await t.pump();
    await t.pump();
    expect(find.text('你按了停车'), findsOneWidget);
    expect(find.textContaining('你在遥控'), findsNothing);
    final n = api.link!.sent.length;
    await t.pump(const Duration(milliseconds: 500));
    expect(api.link!.sent.length, n, reason: '定时器不许起来');
    expect(_stickEnabled(t, 0), isFalse);
    await t.pumpWidget(Container());
  });

  testWidgets('连接还没回来就退出这一页：回来的那条连接放租、关掉（W00c5 修复内部评审）', (t) async {
    // 最常见的交错：「正在拿遥控……」的时候按了返回。页面已经销毁，回来的连接不许留着租约。
    final api = FakeApi('guard')..teleopGate = Completer<void>();
    await t.pumpWidget(MaterialApp(home: SiteTeleopPage(key: UniqueKey(), api: api, robotId: 'A')));
    await t.pump();
    await t.pumpWidget(Container());
    api.teleopGate!.complete();
    await t.pump();
    await t.pump();
    expect(api.link!.released, isTrue);
    expect(api.link!.isClosed, isTrue);
    expect(api.link!.sent, <List<double>>[<double>[0, 0]]);
  });
}
