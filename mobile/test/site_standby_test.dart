// 待命点与原点（W13a，决策 16）：列出原点和待命点；能派单的人叫狗回某一个；管理员在这儿设（代理报了
// mark_home.standby 才有按钮）、设默认、删（先确认）。横屏 800×360。
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_standby.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Map<String, dynamic> _pt(String name, double x, {bool def = false}) => <String, dynamic>{
      'name': name, 'map_id': 'm', 'map_version': '1', 'x': x, 'y': 0.0, 'yaw': 0.0,
      'default': def,
    };

FakeApi _api(String role) {
  final api = FakeApi(role);
  api.standby = <String, dynamic>{
    'points': <Map<String, dynamic>>[_pt('gate', 1.0, def: true), _pt('yard', 2.0)],
    'homes': <Map<String, dynamic>>[
      <String, dynamic>{'name': 'home', 'map_id': 'm', 'map_version': '1', 'x': 0.5, 'y': 0.0,
        'yaw': 0.0},
    ],
  };
  return api;
}

Future<void> _open(WidgetTester t, FakeApi api, {bool canMarkHere = true}) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(
      home: SiteStandbyPage(api: api, robotId: 'A', canMarkHere: canMarkHere)));
  await t.pumpAndSettle();
}

void main() {
  testWidgets('列出原点和待命点，默认的标出来', (t) async {
    await _open(t, _api('admin'));
    expect(find.byKey(const Key('home-m-1')), findsOneWidget);
    expect(find.text('gate（默认）'), findsOneWidget);
    expect(find.text('yard'), findsOneWidget);
    expect(t.takeException(), isNull);
  });

  testWidgets('回指定的待命点', (t) async {
    final api = _api('guard');
    await _open(t, api);
    await t.tap(find.byKey(const Key('standby-go-yard')));
    await t.pumpAndSettle();
    expect(api.calls, contains('standby A yard'));
    expect(find.byKey(SiteStandbyPage.msgKey), findsOneWidget);
  });

  testWidgets('保安：能叫回，不能设、设默认、删', (t) async {
    await _open(t, _api('guard'));
    expect(find.byKey(SiteStandbyPage.hereKey), findsNothing);
    expect(find.byKey(const Key('standby-default-yard')), findsNothing);
    expect(find.byKey(const Key('standby-remove-yard')), findsNothing);
  });

  testWidgets('管理员：在这儿设待命点（名字、默认），列表刷新', (t) async {
    final api = _api('admin');
    await _open(t, api);
    await t.tap(find.byKey(SiteStandbyPage.hereKey));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(SiteStandbyPage.nameKey), 'dock2');
    await t.tap(find.byKey(SiteStandbyPage.defaultKey));
    await t.tap(find.byKey(SiteStandbyPage.goKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('standby-here A dock2 true'));
    expect(find.text('dock2（默认）'), findsOneWidget);
    expect(find.text('gate'), findsOneWidget);
  });

  testWidgets('狗不支持只标待命点（老代理）：没有「在这儿设」', (t) async {
    await _open(t, _api('admin'), canMarkHere: false);
    expect(find.byKey(SiteStandbyPage.hereKey), findsNothing);
  });

  testWidgets('狗拒了：说原因', (t) async {
    final api = _api('admin')..standbyError = SiteError(409, '狗没标:moving');
    await _open(t, api);
    await t.tap(find.byKey(SiteStandbyPage.hereKey));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(SiteStandbyPage.nameKey), 'x');
    await t.tap(find.byKey(SiteStandbyPage.goKey));
    await t.pumpAndSettle();
    expect(find.textContaining('没成'), findsOneWidget);
  });

  testWidgets('设默认、删（先确认）', (t) async {
    final api = _api('admin');
    await _open(t, api);
    await t.tap(find.byKey(const Key('standby-default-yard')));
    await t.pumpAndSettle();
    expect(api.calls, contains('standby-default A yard'));
    expect(find.text('yard（默认）'), findsOneWidget);
    await t.tap(find.byKey(const Key('standby-remove-gate')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('standby-remove')), isEmpty, reason: '先确认');
    await t.tap(find.byKey(const Key('standby-remove-go-gate')));
    await t.pumpAndSettle();
    expect(api.calls, contains('standby-remove A gate'));
    expect(find.text('gate'), findsNothing);
  });

  testWidgets('单狗页有「待命点…」，点进去是这一页', (t) async {
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    final api = _api('guard');
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    final btn = find.byKey(const Key('btn-standby-list'));
    await t.scrollUntilVisible(btn, 100);
    await t.tap(btn);
    await t.pumpAndSettle();
    expect(find.byType(SiteStandbyPage), findsOneWidget);
    expect(siteFixture('site_robot'), isNotNull);
  });
}
