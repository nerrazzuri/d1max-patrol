// 建图时手机实时看图（W09f）：边走边建时录包页先问预览，有就画狗上攒的俯视图 + 走过的路 + 狗现在的位置；
// 没在边走边建（只录包）、老狗（409）退回录包轨迹。
import 'dart:convert';

import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Map<String, dynamic> _pv(int seq,
        {bool png = true, bool rec = true, String run = 'yard-1', double age = 1.0, int frames = 10}) =>
    <String, dynamic>{
      'live': true, 'seq': seq, 'map_id': 'estate-1', 'version': '9', 'run': run,
      'res': 0.1, 'origin': [-2.0, -1.0], 'width': 1, 'height': 1,
      'pose': [0.5, 0.5, 1.2], 'trail': [[0.0, 0.0], [0.5, 0.5]], 'frames': frames,
      'dropped': 3, 'age_s': age, 'recording': rec, 'starting': false, 'preview_error': '',
      if (png) 'png': base64Encode(fakePreviewPng),
    };

Future<void> _open(WidgetTester t, FakeApi api) async {
  await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
  await t.pump();
  await t.pump();
}

String _status(WidgetTester t) =>
    t.widget<Text>(find.byKey(SiteMappingTrailPage.statusKey)).data ?? '';

void main() {
  testWidgets('边走边建：画预览图，每 3 秒问一次、带上次的 seq；不问录包轨迹', (t) async {
    final api = FakeApi('admin')..previewReplies = [_pv(1), _pv(1, png: false), _pv(2, frames: 16)];
    await _open(t, api);
    expect(api.previewSince, [0]);
    expect(find.byKey(SiteMappingTrailPage.previewKey), findsOneWidget);
    expect(find.byKey(SiteMappingTrailPage.canvasKey), findsNothing);
    expect(_status(t), contains('边走边建 estate-1:9'));
    expect(_status(t), contains('10 帧'));
    await t.pump(const Duration(seconds: 2));
    expect(api.previewSince, [0], reason: '3 秒一次');
    await t.pump(const Duration(seconds: 1));
    expect(api.previewSince, [0, 1]);
    expect(find.byKey(SiteMappingTrailPage.previewKey), findsOneWidget, reason: '没新图：留着上一张');
    await t.pump(const Duration(seconds: 3));
    expect(api.previewSince, [0, 1, 1]);
    expect(_status(t), contains('16 帧'));
    expect(api.trailSince, isEmpty);
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('只录包（live: false）：退回录包轨迹，之后不再问预览', (t) async {
    final api = FakeApi('admin')
      ..previewReplies = [<String, dynamic>{'live': false}]
      ..trailReplies = [
        <String, dynamic>{'points': [[0, 0]], 'since': 0, 'total': 1, 'full': false, 'recording': true},
      ];
    await _open(t, api);
    expect(api.previewSince, [0]);
    expect(api.trailSince, [0]);
    expect(find.byKey(SiteMappingTrailPage.canvasKey), findsOneWidget);
    await t.pump(const Duration(seconds: 4));
    expect(api.previewSince, [0]);
    expect(api.trailSince.length, greaterThan(1));
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('老狗（409）：退回录包轨迹', (t) async {
    final api = FakeApi('admin')
      ..previewReplies = [const SiteError(409, '狗没给：unsupported')]
      ..trailReplies = [
        <String, dynamic>{'points': [[0, 0]], 'since': 0, 'total': 1, 'full': false, 'recording': true},
      ];
    await _open(t, api);
    expect(api.trailSince, [0]);
    expect(find.byKey(SiteMappingTrailPage.canvasKey), findsOneWidget);
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('开录还在起（starting、还没 live）：接着问预览，不退回轨迹', (t) async {
    final api = FakeApi('admin')
      ..previewReplies = [
        <String, dynamic>{'live': false, 'starting': true},
        _pv(1),
      ];
    await _open(t, api);
    expect(api.trailSince, isEmpty);
    expect(_status(t), contains('起'));
    await t.pump(const Duration(seconds: 3));
    expect(api.previewSince, [0, 0]);
    expect(find.byKey(SiteMappingTrailPage.previewKey), findsOneWidget);
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('还没第一张：说等；预览进程没起来：说原因、建图不受影响', (t) async {
    final api = FakeApi('admin')
      ..previewReplies = [
        <String, dynamic>{'live': true, 'seq': 0, 'recording': true, 'map_id': 'm', 'version': '1',
          'preview_error': ''},
        <String, dynamic>{'live': true, 'seq': 0, 'recording': true, 'map_id': 'm', 'version': '1',
          'preview_error': 'mapview 起不来'},
      ];
    await _open(t, api);
    expect(_status(t), contains('等第一张'));
    await t.pump(const Duration(seconds: 3));
    expect(_status(t), contains('mapview 起不来'));
    expect(_status(t), contains('建图不受影响'));
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('预览 15 秒以上没更新：说预览停了、建图不受影响', (t) async {
    final api = FakeApi('admin')..previewReplies = [_pv(1, age: 42)];
    await _open(t, api);
    expect(_status(t), contains('预览 42 秒没更新'));
    expect(_status(t), contains('建图不受影响'));
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('换了一趟（run 变了）：从头取', (t) async {
    final api = FakeApi('admin')
      ..previewReplies = [_pv(5), _pv(2, png: false, run: 'yard-2'), _pv(2, run: 'yard-2')];
    await _open(t, api);
    await t.pump(const Duration(seconds: 3));
    await t.pump();
    expect(api.previewSince, [0, 5, 0]);
    expect(find.byKey(SiteMappingTrailPage.previewKey), findsOneWidget);
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('连不上：留着上一张，说是多久前的', (t) async {
    final api = FakeApi('admin')..previewReplies = [_pv(1), const SiteError(0, '网络断了')];
    await _open(t, api);
    await t.pump(const Duration(seconds: 3));
    expect(find.byKey(SiteMappingTrailPage.previewKey), findsOneWidget);
    expect(_status(t), contains('连不上'));
    expect(_status(t), contains('3 秒前'));
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('停了在打包：说打包、接着问；打包完（live: false）：说建完了、不再问', (t) async {
    final api = FakeApi('admin')
      ..previewReplies = [_pv(1, rec: false), <String, dynamic>{'live': false}];
    await _open(t, api);
    expect(_status(t), contains('打包'));
    await t.pump(const Duration(seconds: 3));
    expect(_status(t), contains('建完了'));
    expect(find.byKey(SiteMappingTrailPage.previewKey), findsOneWidget, reason: '留着最后一张');
    await t.pump(const Duration(seconds: 9));
    expect(api.previewSince.length, 2, reason: '建完了不再问');
    expect(api.trailSince, isEmpty, reason: '看过预览的：不退回轨迹');
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('图太大狗没发：说', (t) async {
    final api = FakeApi('admin')..previewReplies = [{..._pv(1, png: false), 'too_big': true}];
    await _open(t, api);
    expect(_status(t), contains('太大'));
    await t.pumpWidget(const SizedBox());
  });

  test('预览上的坐标：左下角是 origin、y 朝上、按画出来的大小缩放', () {
    final p = PreviewPainter(res: 0.5, origin: const Offset(-2, -1), width: 8, height: 4,
        trail: const [Offset(0, 0)], pose: null);
    final m = p.layout(const Size(80, 40)); // 10 px 一格
    expect(m(const Offset(-2, -1)), const Offset(0, 40));
    expect(m(const Offset(2, 1)), const Offset(80, 0));
    expect(m(const Offset(0, 0)), const Offset(40, 20));
  });
}
