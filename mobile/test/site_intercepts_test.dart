// 拦截点与防区（W16）：列出；管理员在狗现在的位置设拦截点、把防区绑到拦截点、取消防区、删拦截点（都先确认）；
// 别的角色只看。事件页的去向说人话。横屏 800×360。
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_intercepts.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

FakeApi _api(String role) {
  final api = FakeApi(role);
  api.intercepted = <String, dynamic>{
    'intercepts': <Map<String, dynamic>>[
      <String, dynamic>{'name': 'gate', 'map_id': 'estate-1', 'map_version': '8', 'x': 3.0,
        'y': -1.0, 'yaw': 0.0},
    ],
    'zones': <Map<String, dynamic>>[
      <String, dynamic>{'zone': 'front-yard', 'intercept': 'gate'},
    ],
  };
  return api;
}

Future<void> _open(WidgetTester t, FakeApi api) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(home: SiteInterceptsPage(api: api)));
  await t.pumpAndSettle();
}

void main() {
  testWidgets('列出防区 → 拦截点、拦截点', (t) async {
    await _open(t, _api('guard'));
    expect(find.text('front-yard → gate'), findsOneWidget);
    expect(find.byKey(const Key('intercept-gate')), findsOneWidget);
    expect(find.byKey(SiteInterceptsPage.hereKey), findsNothing, reason: '保安只看');
    expect(find.byKey(const Key('zone-remove-front-yard')), findsNothing);
  });

  testWidgets('管理员：在狗这儿设拦截点（只有一只能报位置的狗就直接用它）', (t) async {
    final api = _api('admin');
    await _open(t, api);
    await t.tap(find.byKey(SiteInterceptsPage.hereKey));
    await t.pumpAndSettle();
    await t.enterText(find.byType(TextField).first, 'back');
    await t.tap(find.text('设'));
    await t.pumpAndSettle();
    expect(api.calls, contains('intercept-here A back'));
    expect(find.byKey(const Key('intercept-back')), findsOneWidget);
  });

  testWidgets('管理员：狗拒了说原因', (t) async {
    final api = _api('admin')..interceptError = const SiteError(409, '狗没标:moving');
    await _open(t, api);
    await t.tap(find.byKey(SiteInterceptsPage.hereKey));
    await t.pumpAndSettle();
    await t.enterText(find.byType(TextField).first, 'back');
    await t.tap(find.text('设'));
    await t.pumpAndSettle();
    expect(find.textContaining('没成'), findsOneWidget);
  });

  testWidgets('管理员：绑防区、取消防区（先确认）、删拦截点（先确认）', (t) async {
    final api = _api('admin');
    await _open(t, api);
    await t.tap(find.byKey(SiteInterceptsPage.zoneKey));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('zone-name')), 'pond');
    await t.tap(find.byKey(const Key('zone-go')));
    await t.pumpAndSettle();
    expect(api.calls, contains('zone pond gate'));
    expect(find.text('pond → gate'), findsOneWidget);
    await t.tap(find.byKey(const Key('zone-remove-front-yard')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('unzone')), isEmpty, reason: '先确认');
    await t.tap(find.byKey(const Key('zone-remove-go-front-yard')));
    await t.pumpAndSettle();
    expect(api.calls, contains('unzone front-yard'));
    await t.tap(find.byKey(const Key('intercept-remove-gate')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('intercept-remove-go-gate')));
    await t.pumpAndSettle();
    expect(api.calls, contains('rm-intercept gate'));
  });

  testWidgets('事件页：去向说人话，有「拦截点…」入口', (t) async {
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    final api = _api('guard');
    await t.pumpWidget(MaterialApp(home: SiteIncidentsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.textContaining('dispatched'), findsNothing);
    await t.tap(find.byKey(const Key('btn-intercepts')));
    await t.pumpAndSettle();
    expect(find.byType(SiteInterceptsPage), findsOneWidget);
  });

  testWidgets('W23：走不到的标红、没查成的写明、图有新版本要重设', (t) async {
    final api = _api('guard');
    (api.intercepted['intercepts'] as List).add(<String, dynamic>{'name': 'pond', 'map_id': 'estate-1',
      'map_version': '7', 'x': 1.0, 'y': 1.0, 'yaw': 0.0, 'reach': '从待命点走不到 pond（门太窄）',
      'reach_note': '', 'newer_version': '8'});
    await _open(t, api);
    expect(find.byKey(const Key('intercept-reach-pond')), findsOneWidget);
    expect(find.textContaining('门太窄'), findsOneWidget);
    expect(find.byKey(const Key('intercept-newer-pond')), findsOneWidget);
    expect(find.byKey(const Key('intercept-reach-gate')), findsNothing);
  });

  test('去向说人话', () {
    expect(incidentOutcomeText('unreachable'), '拦截点走不到，没狗去');
    expect(incidentOutcomeText('no_robot'), '没有能派的狗');
    expect(incidentOutcomeText('dispatched'), '已出动');
    expect(incidentOutcomeText('weird'), 'weird');
  });
}
