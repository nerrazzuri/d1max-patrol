// 连续录像（W18）：一只狗的一分钟一段，按时间翻；点一段下载下来放；值班的人能标留着；告警现场进来看那一刻前后。
import 'dart:typed_data';

import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/site_client.dart' show SiteError;
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_recordings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

const int t0 = 1791077400000; // 2026-10-04 01:30:00 UTC

class _Player implements ClipPlayer {
  final List<String> played = <String>[];
  @override
  Future<void> play(BuildContext context, Uint8List bytes, String title) async =>
      played.add('$title ${bytes.length}');
}

FakeApi _api(String role) => FakeApi(role)
  ..recordingRows = <Map<String, dynamic>>[
    for (var i = 0; i < 3; i++)
      <String, dynamic>{'id': 10 + i, 'robot_id': 'A', 'camera': i == 2 ? 'back' : 'front',
        'stamp': 's$i', 'start_ms': t0 - i * 60000, 'bytes': 30 * 1048576, 'keep': 0},
  ];

Future<void> _open(WidgetTester t, FakeApi api, {int? around, ClipPlayer? player}) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(
      home: SiteRecordingsPage(api: api, robotId: 'A', aroundMs: around, player: player ?? _Player())));
  await t.pumpAndSettle();
}

void main() {
  testWidgets('列出最近的；点一段下载下来放', (t) async {
    final api = _api('owner');
    final p = _Player();
    await _open(t, api, player: p);
    expect(api.calls.first, 'recordings A * - -');
    expect(find.byKey(SiteRecordingsPage.clipKey(10)), findsOneWidget);
    expect(find.textContaining('30.0 MB'), findsNWidgets(3));
    await t.tap(find.byKey(SiteRecordingsPage.clipKey(11)));
    await t.pumpAndSettle();
    expect(api.calls, contains('recording-bytes 11'));
    expect(p.played.single, endsWith(' 3'));
    expect(find.byKey(SiteRecordingsPage.keepKey(10)), findsNothing, reason: '业主不能标留着');
  });

  testWidgets('按相机筛；更早、更晚按 30 分钟翻', (t) async {
    final api = _api('guard');
    await _open(t, api);
    await t.tap(find.byKey(SiteRecordingsPage.cameraKey('back')));
    await t.pumpAndSettle();
    expect(find.byKey(SiteRecordingsPage.clipKey(12)), findsOneWidget);
    expect(find.byKey(SiteRecordingsPage.clipKey(10)), findsNothing);
    await t.tap(find.byKey(SiteRecordingsPage.olderKey));
    await t.pumpAndSettle();
    final last = api.calls.last.split(' ');
    final until = int.parse(last[4]);
    expect(int.parse(last[3]), until - 30 * 60000);
  });

  testWidgets('值班的人标留着、取消', (t) async {
    final api = _api('guard');
    await _open(t, api);
    await t.tap(find.byKey(SiteRecordingsPage.keepKey(10)));
    await t.pumpAndSettle();
    expect(api.calls, contains('keep 10 true'));
    expect(find.textContaining('标了留着'), findsOneWidget);
    await t.tap(find.byKey(SiteRecordingsPage.keepKey(10)));
    await t.pumpAndSettle();
    expect(api.calls, contains('keep 10 false'));
  });

  testWidgets('从告警进来：看那一刻前后 30 分钟，盖住那一刻的那段标出来', (t) async {
    final api = _api('guard');
    await _open(t, api, around: t0 - 30000);
    final q = api.calls.first.split(' ');
    expect(int.parse(q[4]), t0 - 30000 + 15 * 60000);
    expect(int.parse(q[3]), t0 - 30000 - 15 * 60000);
    final tile = t.widget<ListTile>(find.byKey(SiteRecordingsPage.clipKey(11)));
    expect(tile.selected, isTrue);
    expect(find.textContaining('告警那一刻'), findsOneWidget);
  });

  testWidgets('没开录像的站点、读不到：说清楚', (t) async {
    await _open(t, _api('guard')..recordingsError = const SiteError(404, 'x'));
    expect(find.text('这个站点没开录像'), findsOneWidget);
    await t.pumpWidget(Container());
    await _open(t, _api('guard')..recordingRows = <Map<String, dynamic>>[]);
    expect(find.textContaining('这段时间没有录像'), findsOneWidget);
  });

  testWidgets('告警现场页：狗的告警有「现场录像」，站点那一行的没有', (t) async {
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    final api = _api('guard');
    Alert alert(String robot) => Alert.fromWire(<String, dynamic>{
          'key': '$robot/x#1', 'level': 'P1', 'kind': 'stuck', 'robot': robot, 'title': 't',
          'first_ms': t0, 'context': <String, dynamic>{}});
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: alert('A'))));
    await t.pumpAndSettle();
    await t.ensureVisible(find.byKey(SiteAlertScenePage.videoKey));
    await t.tap(find.byKey(SiteAlertScenePage.videoKey));
    await t.pumpAndSettle();
    final page = t.widget<SiteRecordingsPage>(find.byType(SiteRecordingsPage));
    expect((page.robotId, page.aroundMs), ('A', t0));
    await t.pumpWidget(Container());
    await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: alert('site'))));
    await t.pumpAndSettle();
    expect(find.byKey(SiteAlertScenePage.videoKey), findsNothing);
  });
}
