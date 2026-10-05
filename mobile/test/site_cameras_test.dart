// 固定摄像头（W19）：状态一眼看清（连不上那一路的入侵收不到），有画面地址的点进去看实时画面。
import 'package:d1max_patrol/net/site_client.dart' show SiteError;
import 'package:d1max_patrol/ui/site_cameras.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/widget/live_video.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

FakeApi _api() => FakeApi('owner')
  ..cameraRows = <Map<String, dynamic>>[
    <String, dynamic>{'name': 'gate-cam', 'zone': 'front-yard', 'motion': false, 'live': true,
      'connected': true, 'since_ms': 1791077400000, 'last_event_ms': 1791077460000,
      'last_topic': 'x/FieldDetector', 'error': ''},
    <String, dynamic>{'name': 'pond-cam', 'zone': 'pond', 'motion': true, 'live': false,
      'connected': false, 'since_ms': 1791077400000, 'last_event_ms': null, 'last_topic': '',
      'error': '连不上'},
  ];

Future<void> _open(WidgetTester t, FakeApi api) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(home: SiteCamerasPage(api: api)));
  await t.pumpAndSettle();
}

void main() {
  testWidgets('连着的、连不上的一眼看清；连不上说这一路收不到', (t) async {
    await _open(t, _api());
    expect(find.textContaining('gate-cam · 防区 front-yard'), findsOneWidget);
    expect(find.textContaining('最近一次报入侵'), findsOneWidget);
    expect(find.textContaining('这一路的入侵收不到'), findsOneWidget);
    expect(find.textContaining('画面动了也算入侵'), findsOneWidget);
    await t.pumpWidget(Container());
  });

  testWidgets('有画面地址的点进去看实时画面；没有的点不进', (t) async {
    final api = _api();
    await _open(t, api);
    await t.tap(find.byKey(SiteCamerasPage.camKey('pond-cam')));
    await t.pumpAndSettle();
    expect(find.byType(SiteCameraLivePage), findsNothing);
    await t.tap(find.byKey(SiteCamerasPage.camKey('gate-cam')));
    await t.pump();
    await t.pump(const Duration(milliseconds: 100));
    final live = t.widget<LiveVideo>(find.byType(LiveVideo));
    expect(live.streamPath, '/api/cameras/gate-cam/live');
    await t.pumpWidget(Container());
  });

  testWidgets('没开摄像头、还没有摄像头：说清楚', (t) async {
    await _open(t, FakeApi('owner')..camerasError = const SiteError(404, 'x'));
    expect(find.text('这个站点没开摄像头'), findsOneWidget);
    await t.pumpWidget(Container());
    await _open(t, FakeApi('owner'));
    expect(find.textContaining('camera-add'), findsOneWidget);
    await t.pumpWidget(Container());
  });

  testWidgets('事件页有「摄像头…」入口', (t) async {
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    await t.pumpWidget(MaterialApp(home: SiteIncidentsPage(api: _api())));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-cameras')));
    await t.pumpAndSettle();
    expect(find.byType(SiteCamerasPage), findsOneWidget);
    await t.pumpWidget(Container());
  });
}
