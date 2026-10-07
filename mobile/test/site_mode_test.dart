// 布防模式（W20）：首页那一行；保安只能切到布防、业主能切在家和访客（挑防区、挑时长）、管理员改「在家时撤防」；
// 站点推「模式变了」当场换；事件页的「撤防中」说人话。横屏 800×360。
import 'dart:async';

import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_mode.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Future<void> _open(WidgetTester t, FakeApi api) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(const SizedBox()); // 同一个测试里开第二次：不复用上一次的页面状态
  await t.pumpWidget(MaterialApp(home: SiteModePage(api: api)));
  await t.pumpAndSettle();
}

void main() {
  test('站点夹具：访客模式带结束时间、防区带现在布不布防', () {
    final m = siteFixture('site_mode');
    expect(m['mode'], 'visitor');
    expect(modeText(m), startsWith('访客（到 '));
    expect(modeText(<String, dynamic>{'mode': 'armed'}), '布防');
    expect(modeText(<String, dynamic>{'mode': 'home'}), '在家');
    final z = (m['zones'] as List).first as Map;
    expect(z.keys, containsAll(<String>['zone', 'armed', 'home_armed']));
    expect(incidentOutcomeText('disarmed'), contains('撤防'));
  });

  testWidgets('保安：只有「布防」，没有在家、访客，也改不了防区', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    expect(find.byKey(SiteModePage.homeKey), findsNothing);
    expect(find.byKey(SiteModePage.visitorKey), findsNothing);
    expect(find.text('撤防（在家、访客）要业主切'), findsOneWidget);
    expect(find.byKey(const Key('mode-zone-home-yard')), findsNothing);
    await t.tap(find.byKey(SiteModePage.armKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('mode armed'));
    expect(find.text('现在：布防'), findsOneWidget);
  });

  testWidgets('业主：开访客，挑防区、挑时长', (t) async {
    final api = FakeApi('owner')..modeView = <String, dynamic>{
      ...siteFixture('site_mode'), 'mode': 'home', 'visitor_zones': <String>[], 'until_ms': null};
    await _open(t, api);
    expect(find.text('现在：在家'), findsOneWidget);
    await t.tap(find.byKey(SiteModePage.visitorKey));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('visitor-zone-yard')));
    await t.tap(find.byKey(const Key('visitor-hours')));
    await t.pumpAndSettle();
    await t.tap(find.text('2 小时').last);
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('visitor-go')));
    await t.pumpAndSettle();
    expect(api.calls, contains('mode visitor yard 120'));
    expect(find.textContaining('现在：访客（到 '), findsOneWidget);
  });

  testWidgets('业主：访客默认 4 小时；切到在家', (t) async {
    final api = FakeApi('owner')..modeView = <String, dynamic>{...siteFixture('site_mode'), 'mode': 'armed'};
    await _open(t, api);
    await t.tap(find.byKey(SiteModePage.visitorKey));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('visitor-go')));
    await t.pumpAndSettle();
    expect(api.calls, contains('mode visitor  240'));
    await t.tap(find.byKey(SiteModePage.homeKey));
    await t.pumpAndSettle();
    expect(api.calls.last, 'mode home');
  });

  testWidgets('业主改不了防区，管理员能改「在家时也布防」', (t) async {
    await _open(t, FakeApi('owner'));
    expect(find.byKey(const Key('mode-zone-home-yard')), findsNothing);
    final api = FakeApi('admin');
    await _open(t, api);
    expect(find.text('yard · 撤防中'), findsOneWidget);
    await t.tap(find.byKey(const Key('mode-zone-home-yard')));
    await t.pumpAndSettle();
    expect(api.calls, contains('zone-home yard true'));
    expect(find.descendant(of: find.byKey(const Key('mode-zone-yard')), matching: find.text('在家时也布防')),
        findsOneWidget);
  });

  testWidgets('切不成说原因；老站点没开说清楚', (t) async {
    final api = FakeApi('owner');
    await _open(t, api);
    api.modeError = const SiteError(403, 'owner 没有 set_mode 权限');
    await t.tap(find.byKey(SiteModePage.homeKey));
    await t.pumpAndSettle();
    expect(find.textContaining('切到在家没成'), findsOneWidget);
    await _open(t, FakeApi('owner')..modeError = const SiteError(404, '没开'));
    expect(find.text('这个站点没开布防模式'), findsOneWidget);
  });

  testWidgets('模式页：别的手机撤防了，站点推一帧，当场换、布防按钮能按了（W20 外审）', (t) async {
    final api = FakeApi('guard')..modeView = <String, dynamic>{...siteFixture('site_mode'), 'mode': 'armed'};
    await _open(t, api);
    expect(find.text('现在：布防'), findsOneWidget);
    expect(t.widget<ButtonStyleButton>(find.byKey(SiteModePage.armKey)).onPressed, isNull);
    api.sse.add(<String, dynamic>{'kind': 'mode', 'mode': <String, dynamic>{...api.modeView, 'mode': 'home'}});
    await t.pump(const Duration(milliseconds: 50));
    expect(find.text('现在：在家'), findsOneWidget);
    expect(t.widget<ButtonStyleButton>(find.byKey(SiteModePage.armKey)).onPressed, isNotNull);
  });

  testWidgets('模式页：断线重连后重新拉一次（断线期间错过的切换补上）', (t) async {
    final api = FakeApi('guard')..modeView = <String, dynamic>{...siteFixture('site_mode'), 'mode': 'armed'};
    await _open(t, api);
    final opened = api.eventsOpened;
    final old = api.sse;
    api.sse = StreamController<Map<String, dynamic>>.broadcast();
    api.modeView = <String, dynamic>{...api.modeView, 'mode': 'home'}; // 断线期间业主撤防了
    await old.close();
    await t.pump(const Duration(seconds: 6));
    expect(api.eventsOpened, greaterThan(opened));
    expect(find.text('现在：在家'), findsOneWidget);
  });

  testWidgets('首页：模式那一行；站点推「模式变了」当场换；点进去是模式页', (t) async {
    final api = FakeApi('guard');
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.textContaining('模式：访客（到 '), findsOneWidget);
    api.modeView = <String, dynamic>{...api.modeView, 'mode': 'armed', 'until_ms': null};
    api.sse.add(<String, dynamic>{'kind': 'mode', 'mode': api.modeView});
    await t.pump(const Duration(milliseconds: 100)); // 不等那半秒的合并刷新：是推的那一帧换的
    expect(find.text('模式：布防'), findsOneWidget);
    await t.tap(find.byKey(SiteRobotsPage.modeKey));
    await t.pumpAndSettle();
    expect(find.byType(SiteModePage), findsOneWidget);
  });

  testWidgets('首页：老站点没有布防模式就不显示那一行', (t) async {
    final api = FakeApi('guard')..modeError = const SiteError(404, '没开');
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteRobotsPage.modeKey), findsNothing);
  });

  test('权限：业主能确认告警、能切模式；保安只能布防；业主不能标录像留着', () {
    const owner = SiteSession('t', 'o', 'owner', displayName: '张三');
    const guard = SiteSession('t', 'g', 'guard');
    expect(owner.canHandleAlerts && owner.canSetMode && owner.canArm, isTrue);
    expect(owner.canReview, isFalse);
    expect(guard.canArm && !guard.canSetMode, isTrue);
    expect(owner.displayName, '张三');
  });
}
