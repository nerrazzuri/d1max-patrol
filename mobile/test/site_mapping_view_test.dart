// 建图时看得见（W00c6h）：录包时手机每 2 秒取一次狗走过的轨迹画出来（哪儿走过了）；建好的图在地图页点开
// 看（能缩放），画上这张图上登记的待命点（建出来的图对不对）。
import 'dart:typed_data';

import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Map<String, dynamic> _view() {
  final v = Map<String, dynamic>.of(siteFixture('site_robot'));
  final caps = Map<String, dynamic>.of(v['capabilities'] as Map<String, dynamic>);
  final tasks = Map<String, dynamic>.of(caps['tasks'] as Map<String, dynamic>);
  tasks['mapping'] = <String, dynamic>{};
  tasks['mapping_trail'] = <String, dynamic>{};
  caps['tasks'] = tasks;
  v['capabilities'] = caps;
  return v;
}

Map<String, dynamic> _trail(List<List<double>> pts, int since, int total, {bool rec = true}) =>
    <String, dynamic>{'points': pts, 'since': since, 'total': total, 'full': false,
      'recording': rec};

void main() {
  testWidgets('录包时每 2 秒取新增的点、画出来；停了就不再取', (t) async {
    final api = FakeApi('admin', robotView: _view())
      ..trailReplies = [
        _trail([[0, 0], [0.5, 0]], 0, 2),
        _trail([[1.0, 0.2]], 2, 3),
        _trail([], 3, 3, rec: false),
      ];
    await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
    await t.pump();
    expect(api.trailSince, [0]);
    expect(find.textContaining('录包中'), findsOneWidget);
    expect(find.textContaining('2 个点'), findsOneWidget);
    await t.pump(const Duration(seconds: 2));
    expect(api.trailSince, [0, 2], reason: '只要新增的');
    expect(find.textContaining('3 个点'), findsOneWidget);
    expect(t.widget<CustomPaint>(find.byKey(SiteMappingTrailPage.canvasKey)).painter,
        isA<TrailPainter>().having((p) => p.points.length, 'points', 3));
    await t.pump(const Duration(seconds: 2));
    expect(find.textContaining('没在录包'), findsOneWidget);
    await t.pump(const Duration(seconds: 10));
    expect(api.trailSince, [0, 2, 3], reason: '停了不再取');
    await t.tap(find.byKey(SiteMappingTrailPage.refreshKey));
    await t.pump();
    expect(api.trailSince.length, 4, reason: '点刷新再看一次');
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('狗上重新开录了（点数变少）：从头取', (t) async {
    final api = FakeApi('admin', robotView: _view())
      ..trailReplies = [
        _trail([[0, 0], [1, 0], [2, 0]], 0, 3),
        _trail([], 3, 1),
        _trail([[0, 0]], 0, 1),
      ];
    await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
    await t.pump();
    await t.pump(const Duration(seconds: 2));
    await t.pump();
    expect(api.trailSince, [0, 3, 0]);
    expect(t.widget<CustomPaint>(find.byKey(SiteMappingTrailPage.canvasKey)).painter,
        isA<TrailPainter>().having((p) => p.points.length, 'points', 1));
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('退出页面就不再取', (t) async {
    final api = FakeApi('admin', robotView: _view())
      ..trailReplies = List.generate(20, (i) => _trail([], i, i));
    await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
    await t.pump();
    await t.pumpWidget(const SizedBox());
    final n = api.trailSince.length;
    await t.pump(const Duration(seconds: 10));
    expect(api.trailSince.length, n);
  });

  testWidgets('单狗页：管理员有「录包轨迹」；开始录包成功就打开轨迹页', (t) async {
    final api = FakeApi('admin', robotView: _view())..trailReplies = [_trail([], 0, 0)];
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-mapping-trail')), findsOneWidget);
    await t.tap(find.byKey(const Key('btn-record-start')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('record-go')));
    await t.pump();
    await t.pump(const Duration(milliseconds: 500));
    expect(find.byType(SiteMappingTrailPage), findsOneWidget);
    await t.pumpWidget(const SizedBox());
    final guard = FakeApi('guard', robotView: _view());
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: guard, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-mapping-trail')), findsNothing);
  });

  testWidgets('地图页：点「看图」→ 图 + 待命点（谁都能看）', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteMapsPage(api: api)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteMapsPage.previewKey('estate-1', '8')));
    await t.pumpAndSettle();
    expect(api.calls, containsAll(['mapPreview estate-1:8', 'mapPreviewPng estate-1:8']));
    expect(find.byType(Image), findsOneWidget);
    expect(find.textContaining('A · dock'), findsOneWidget);
    expect(find.textContaining('默认'), findsOneWidget);
    final painter = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.overlayKey)).painter!
        as StandbyPainter;
    // 左边缘 x = -10、上边缘 y = 5、一像素 0.1 m:(1.5, 2.0) 在 (115, 30)。
    expect(painter.pixels.single, const Offset(115, 30));
    expect(find.byType(InteractiveViewer), findsOneWidget, reason: '能缩放');
  });

  testWidgets('这张图没有栅格：说清楚', (t) async {
    final api = FakeApi('guard')..previewMissing = true;
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '9')));
    await t.pumpAndSettle();
    expect(find.textContaining('没有栅格'), findsOneWidget);
  });

  test('轨迹画布：点按比例缩进画布、起点在里面', () {
    final p = TrailPainter(const [Offset(0, 0), Offset(10, 0), Offset(10, 5)]);
    final m = p.layout(const Size(220, 120));
    for (final pt in p.points) {
      final q = m(pt);
      expect(q.dx, inInclusiveRange(0, 220));
      expect(q.dy, inInclusiveRange(0, 120));
    }
    expect(m(const Offset(10, 5)).dy, lessThan(m(const Offset(10, 0)).dy), reason: 'y 朝上');
  });

  test('解码用的 PNG 是真的（Image.memory 认得）', () {
    expect(fakePreviewPng.sublist(0, 8), Uint8List.fromList([137, 80, 78, 71, 13, 10, 26, 10]));
  });
}
