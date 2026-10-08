// W17：告警带现场（狗在哪、拦截点、那一趟的照片）、响铃那一档一直响到有人确认、后台值守登录就开。
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/background_watch.dart';
import 'package:d1max_patrol/net/site_client.dart' show SiteError;
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_runs.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Alert _intrusion({Map<String, dynamic>? context}) => Alert.fromWire(<String, dynamic>{
      'key': 'A/intrusion#1', 'level': 'P1', 'kind': 'intrusion', 'robot': 'A',
      'title': '防区 front-yard 有入侵:A 已出动去 gate', 'detail': '事件源 nvr-1',
      'first_ms': 1, 'last_ms': 1, 'count': 1, 'acked_by': '', 'acked_ms': null,
      'resolved_by': '', 'resolved_ms': null, 'escalated': 2, 'channel': 'sound',
      'context': context ??
          <String, dynamic>{
            'zone': 'front-yard',
            'task_id': 'incident-abc',
            'intercept': <String, dynamic>{'name': 'gate', 'map_id': 'estate-1',
              'map_version': '7', 'x': 1.0, 'y': 0.0, 'yaw': 0.0},
            'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': '7', 'x': 3.0,
              'y': -1.0, 'yaw': 0.0, 'at_ms': 1000},
          },
    });

class _Watch implements BackgroundWatch {
  final List<String> calls = <String>[];
  @override
  bool get supported => true;
  @override
  Future<void> start(
      {required String site, required String url, required String fingerprint, required String token}) async =>
      calls.add('start $site $url $fingerprint $token');
  @override
  Future<void> stop() async => calls.add('stop');
  @override
  Future<bool> running() async => calls.isNotEmpty && calls.last.startsWith('start');
}

final _entry = SiteEntry(name: '庄园', url: 'https://10.0.0.2:8443', fingerprint: 'ab' * 32,
    username: 'gina');

Future<void> _land(WidgetTester t) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
}

void main() {
  test('告警的现场：读得懂；老站点不发就是空的', () {
    expect(_intrusion().context['zone'], 'front-yard');
    final old = Alert.fromWire(<String, dynamic>{'key': 'k', 'level': 'P1'});
    expect(old.context, isEmpty);
    expect(Alert.fromWire(<String, dynamic>{'key': 'k', 'context': 'x'}).context, isEmpty);
  });

  test('图上标什么：狗（红）与拦截点（紫）；别的图上的不往这张图上画；都没有就不给看图', () {
    final (m, v, marks) = sceneMarks(_intrusion())!;
    expect((m, v), ('estate-1', '7'));
    expect(marks.map((x) => x.label), ['狗 A', '拦截点 gate']);
    final other = sceneMarks(_intrusion(context: <String, dynamic>{
      'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': '7', 'x': 1, 'y': 1},
      'intercept': <String, dynamic>{'name': 'g', 'map_id': 'estate-1', 'map_version': '9',
        'x': 0, 'y': 0},
    }))!;
    expect(other.$3.length, 1);
    expect(sceneMarks(_intrusion(context: <String, dynamic>{'zone': 'z'})), isNull);
  });

  testWidgets('现场页：防区、拦截点、狗在哪、那一趟；在图上看带着标记', (t) async {
    await _land(t);
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: _intrusion())));
    await t.pumpAndSettle();
    expect(find.textContaining('防区：front-yard'), findsOneWidget);
    expect(find.textContaining('拦截点：gate'), findsOneWidget);
    expect(find.textContaining('狗最后在：(3.0, -1.0)'), findsOneWidget);
    expect(find.textContaining('incident-abc'), findsOneWidget);
    await t.tap(find.byKey(SiteAlertScenePage.mapKey));
    await t.pumpAndSettle();
    final page = t.widget<SiteMapPreviewPage>(find.byType(SiteMapPreviewPage));
    expect(page.marks.length, 2);
    expect((page.mapId, page.version), ('estate-1', '7'));
  });

  testWidgets('现场照片：按那一趟找记录；还没传上来就说', (t) async {
    await _land(t);
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: _intrusion())));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteAlertScenePage.photoKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('runs mission=incident-abc'));
    expect(find.textContaining('还没传到站点'), findsOneWidget);
    final run = (siteFixture('site_runs')['runs'] as List).first as Map<String, dynamic>;
    final ctx = <String, dynamic>{..._intrusion().context, 'task_id': run['mission']};
    await t.pumpWidget(Container());
    await t.pumpWidget(
        MaterialApp(home: SiteAlertScenePage(api: api, alert: _intrusion(context: ctx))));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteAlertScenePage.photoKey));
    await t.pumpAndSettle();
    expect(find.byType(SiteRunPage), findsOneWidget);
  });

  testWidgets('值守屏：点告警进现场页', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteWatchPage(api: api)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteWatchPage.alertKey('A/estop_pressed#1')));
    await t.pumpAndSettle();
    expect(find.byType(SiteAlertScenePage), findsOneWidget);
  });

  testWidgets('响铃那一档没人确认：一直响；确认了就停', (t) async {
    final api = FakeApi('guard');
    var rang = 0;
    await t.pumpWidget(MaterialApp(
        home: SiteRobotsPage(api: api, ring: () => rang++, ringEvery: const Duration(seconds: 4))));
    await t.pumpAndSettle();
    final a = <String, dynamic>{
      'key': 'A/intrusion#1', 'level': 'P1', 'kind': 'intrusion', 'robot': 'A', 'title': 'x',
      'channel': 'sound', 'escalated': 2, 'acked_ms': null, 'resolved_ms': null,
    };
    api.sse.add(<String, dynamic>{'kind': 'alert', 'alert': a});
    await t.pump();
    expect(rang, 1);
    await t.pump(const Duration(seconds: 13));
    expect(rang, 4, reason: '每 4 秒再响一次');
    api.sse.add(<String, dynamic>{'kind': 'alert', 'alert': <String, dynamic>{...a, 'acked_ms': 5}});
    await t.pump();
    await t.pump(const Duration(seconds: 20));
    expect(rang, 4, reason: '确认了就停');
  });

  testWidgets('后台值守：登录进来就开（领值守令牌）；点了关；离开这一页就停', (t) async {
    await _land(t);
    final api = FakeApi('guard');
    final w = _Watch();
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, watch: w, entry: _entry)));
    await t.pumpAndSettle();
    expect(api.calls, contains('watch-token'));
    expect(w.calls, ['start 庄园 https://10.0.0.2:8443 ${'ab' * 32} watch-tok']);
    await t.tap(find.byKey(SiteRobotsPage.bgWatchKey));
    await t.pumpAndSettle();
    expect(w.calls.last, 'stop');
    expect(find.textContaining('后台值守关了'), findsOneWidget);
    await t.tap(find.byKey(SiteRobotsPage.bgWatchKey));
    await t.pumpAndSettle();
    expect(w.calls.last, startsWith('start'));
    await t.pumpWidget(Container());
    expect(w.calls.last, 'stop', reason: '离开（注销）就停');
  });

  testWidgets('后台值守：领不到令牌就说，不假装开着', (t) async {
    await _land(t);
    final api = FakeApi('guard')..watchTokenError = const SiteError(500, '坏了');
    final w = _Watch();
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, watch: w, entry: _entry)));
    await t.pumpAndSettle();
    expect(w.calls, isEmpty);
    expect(find.textContaining('后台值守开不了'), findsOneWidget);
    final icon = t.widget<IconButton>(find.byKey(SiteRobotsPage.bgWatchKey));
    expect((icon.icon as Icon).icon, Icons.shield_outlined);
  });

  testWidgets('不支持后台的（桌面版）：没有开关，也不领令牌', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, entry: _entry)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteRobotsPage.bgWatchKey), findsNothing);
    expect(api.calls, isNot(contains('watch-token')));
  });

  testWidgets('W33 狗看见人了：现场页有「就地驱离」，按了开一场；别的告警没有这个按钮', (t) async {
    await _land(t);
    final api = FakeApi('guard');
    final seen = Alert.fromWire(<String, dynamic>{
      'key': 'A/dog_sees_person#1', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'A',
      'title': '狗看见人了:2 个人,最近 3.5 m', 'detail': '布防中', 'first_ms': 1, 'last_ms': 1,
      'count': 1, 'acked_by': '', 'acked_ms': null, 'resolved_by': '', 'resolved_ms': null,
      'escalated': 0, 'channel': 'sound',
      'context': <String, dynamic>{'task_id': 'persons', 'persons': <String, dynamic>{'count': 2},
        'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': '7', 'x': 3.0, 'y': 4.0,
          'at_ms': 1000}},
    });
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: seen)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteAlertScenePage.photoKey), findsOneWidget, reason: '现场照片在 persons 那几趟');
    await t.tap(find.byKey(SiteAlertScenePage.deterKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('deter-start A'));
    expect(find.textContaining('开驱离（L1）'), findsOneWidget);
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: _intrusion())));
    await t.pumpAndSettle();
    expect(find.byKey(SiteAlertScenePage.deterKey), findsNothing);
  });
}
