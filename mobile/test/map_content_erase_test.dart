// 地图台账 M7、M9、44（2026-10-14）：
// - 全图按有内容的范围：站点给 `content_bbox`（预览像素），地图栏、看图页打开（和回正）时把它（四个角转过
//   以后的外框）放进画布、居中；没给按整张图（跟原来一模一样）。
// - 看到过、狗没走过：站点说 `seen: true`，图例加一行（淡淡的地面是站点画进底图的）。
// - 抹掉区（区域种类 `erase`）：看图页能画（点图换坐标在转了角度以后一点不变）；图上只描一道细边、图例加一行；
//   存完底图重新取（站点按新的抹掉区画好了）。
import 'dart:math' as math;

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/ui/map_view.dart';
import 'package:d1max_patrol/ui/nw/nw_map_raster.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

/// FakeApi 那张图（200 × 100 像素、一像素 0.1 m、左边缘 x = -10、上边缘 y = 5）；[extra] 并进预览 JSON。
class _Api extends FakeApi {
  _Api(super.role, {this.extra = const <String, dynamic>{}});
  Map<String, dynamic> extra;

  @override
  Future<Map<String, dynamic>> mapPreview(String mapId, String version) async =>
      <String, dynamic>{...await super.mapPreview(mapId, version), ...extra};
}

(double, double) _m(double px, double py) => (-10 + px * 0.1, 5 - py * 0.1);

const _bbox = <double>[40, 20, 160, 80];

void _size(WidgetTester t, [Size size = const Size(390, 844)]) {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

RenderBox _box(WidgetTester t, Finder f) => t.renderObject<RenderBox>(f);

double _zoom(WidgetTester t) =>
    t.widget<InteractiveViewer>(find.byType(InteractiveViewer)).transformationController!.value.getMaxScaleOnAxis();

/// 有内容的范围四个角（图的像素）在屏上哪儿：[canvas] 是图那一层，[k] 是一像素画成多大（地图栏的画布按 k
/// 放大过；看图页的画布是图的像素、外面缩放）。
List<Offset> _corners(RenderBox canvas, double k) => [
      for (final (x, y) in [(_bbox[0], _bbox[1]), (_bbox[2], _bbox[1]), (_bbox[0], _bbox[3]), (_bbox[2], _bbox[3])])
        canvas.localToGlobal(Offset(x, y) * k),
    ];

void _fitted(List<Offset> pts, Rect view, {double pad = 8}) {
  for (final p in pts) {
    expect(view.deflate(pad - 0.01).contains(p), isTrue, reason: '角 $p 在画布 $view 里（离边 $pad）');
  }
  final xs = pts.map((p) => p.dx), ys = pts.map((p) => p.dy);
  final w = xs.reduce(math.max) - xs.reduce(math.min), h = ys.reduce(math.max) - ys.reduce(math.min);
  expect(math.max(w / (view.width - 2 * pad), h / (view.height - 2 * pad)), closeTo(1, 1e-6), reason: '放满一边');
  final c = Offset((xs.reduce(math.max) + xs.reduce(math.min)) / 2, (ys.reduce(math.max) + ys.reduce(math.min)) / 2);
  expect((c - view.center).distance, lessThan(1e-6), reason: '居中');
}

void main() {
  group('纯函数', () {
    test('content_bbox：读进来、夹到图上、反着给也认；坏的、没大小的不认；seen', () {
      MapGeo g(Object? bb, [Object? seen]) => MapGeo.fromPreview(<String, dynamic>{
            'width': 200, 'height': 100, 'content_bbox': bb, 'seen': seen});
      expect(g(_bbox).content, const Rect.fromLTRB(40, 20, 160, 80));
      expect(g([160, 80, 40, 20]).content, const Rect.fromLTRB(40, 20, 160, 80));
      expect(g([-5, -5, 500, 50]).content, const Rect.fromLTRB(0, 0, 200, 50));
      for (final bad in [null, 'x', [1, 2, 3], [0, 0, 0, 0], [5, 5, 5, 9], [0, 0, '9', 9], [0, 0, double.nan, 9]]) {
        expect(g(bad).content, isNull, reason: '$bad');
      }
      expect(g(_bbox, true).seen, isTrue);
      expect(g(_bbox, 'true').seen, isFalse);
      expect(g(_bbox).seen, isFalse);
    });

    for (final deg in const [0.0, 23.0, -40.0, 44.9]) {
      test('contentFit（$deg°）：范围的四个角转过、乘 k、挪 shift 以后正好放进框里（离边 8）、居中', () {
        final geo = MapGeo.fromPreview(<String, dynamic>{'width': 200, 'height': 100, 'rotation_deg': deg,
          'content_bbox': _bbox});
        for (final box in const [Size(390, 600), Size(800, 360)]) {
          final (k, shift) = geo.contentFit(box);
          final c = geo.size.center(Offset.zero);
          final centre = box.center(Offset.zero);
          final pts = [
            for (final (x, y) in [(40.0, 20.0), (160.0, 20.0), (40.0, 80.0), (160.0, 80.0)])
              centre - shift + nwRotate(Offset(x, y) - c, geo.radians) * k,
          ];
          _fitted(pts, Offset.zero & box);
          expect(k, greaterThan(geo.fitScale(box)), reason: '比整张图放得大');
        }
      });
    }

    test('没给范围：整张图放进来，不留边、不挪（跟原来一样）', () {
      final geo = MapGeo.fromPreview(<String, dynamic>{'width': 200, 'height': 100, 'rotation_deg': 23});
      final (k, shift) = geo.contentFit(const Size(390, 600));
      expect(k, geo.fitScale(const Size(390, 600)));
      expect(shift, Offset.zero);
      expect(geo.contentBounds().size, nwRotatedBounds(geo.size, geo.radians));
    });
  });

  group('地图栏', () {
    for (final deg in const [0.0, 23.0, -40.0]) {
      testWidgets('打开就按有内容的范围放（$deg°）；放大、拖了以后回正回到一模一样', (t) async {
        _size(t);
        final api = _Api('guard', extra: <String, dynamic>{'rotation_deg': deg, 'content_bbox': _bbox});
        await t.pumpWidget(MaterialApp(home: Scaffold(body: SiteMapTab(api: api))));
        await t.pumpAndSettle();
        final view = t.getRect(find.byType(InteractiveViewer));
        RenderBox canvas() => _box(t, find.byKey(SiteMapTab.canvasKey));
        final k = canvas().size.width / 200;
        final first = _corners(canvas(), k);
        _fitted(first, view);
        await t.tap(find.byKey(SiteMapTab.zoomInKey));
        await t.pump();
        await t.drag(find.byType(InteractiveViewer), const Offset(-40, 30));
        await t.pumpAndSettle();
        expect((_corners(canvas(), k).first - first.first).distance, greaterThan(1));
        await t.tap(find.byKey(SiteMapTab.recenterKey));
        await t.pump();
        expect(_zoom(t), 1.0);
        final back = _corners(canvas(), k);
        for (var i = 0; i < 4; i++) {
          expect((back[i] - first[i]).distance, lessThan(1e-6));
        }
        await t.pumpWidget(Container());
      });
    }

    testWidgets('图例：站点说有看到过、图上有抹掉区才列；抹掉区只描边、不算限速区', (t) async {
      _size(t, const Size(390, 1400));
      Finder inLegend(String s) => find.descendant(of: find.byKey(SiteMapTab.legendKey), matching: find.text(s));
      final plain = FakeApi('guard');
      await t.pumpWidget(MaterialApp(home: Scaffold(body: SiteMapTab(api: plain))));
      await t.pumpAndSettle();
      expect(inLegend('看到过、狗没走过'), findsNothing);
      expect(inLegend('已抹掉（狗不去）'), findsNothing);
      final api = _Api('guard', extra: <String, dynamic>{'seen': true})
        ..zonesView = <String, dynamic>{...FakeApi('guard').zonesView, 'zones': <dynamic>[
          <String, dynamic>{'id': 'e1', 'kind': 'erase', 'label': '抹掉区', 'polygon': [[0, 0], [1, 0], [1, 1]]},
        ]};
      await t.pumpWidget(MaterialApp(home: Scaffold(body: SiteMapTab(key: UniqueKey(), api: api))));
      await t.pumpAndSettle();
      expect(inLegend('看到过、狗没走过'), findsOneWidget);
      expect(inLegend('已抹掉（狗不去）'), findsOneWidget);
      expect(inLegend('限速区'), findsNothing, reason: '抹掉区不是限速区');
      expect(inLegend('禁行区'), findsNothing);
      final painter = t.widget<CustomPaint>(find.byKey(SiteMapTab.zonesKey)).painter! as ZonesPainter;
      expect(painter.shapes.single.erase, isTrue);
      appLang.value = AppLang.en;
      addTearDown(() => appLang.value = AppLang.zh);
      await t.pumpWidget(MaterialApp(home: Scaffold(body: SiteMapTab(key: UniqueKey(), api: api))));
      await t.pumpAndSettle();
      expect(inLegend('Seen, not mapped for the robot'), findsOneWidget);
      expect(inLegend('Erased (robots never go there)'), findsOneWidget);
      await t.pumpWidget(Container());
    });
  });

  group('看图页', () {
    testWidgets('打开就按有内容的范围放（转了 23°）', (t) async {
      _size(t, const Size(1280, 720));
      final api = _Api('guard', extra: <String, dynamic>{'rotation_deg': 23, 'content_bbox': _bbox});
      await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1', version: '8')));
      await t.pumpAndSettle();
      _fitted(_corners(_box(t, find.byKey(SiteMapPreviewPage.canvasKey)), 1), t.getRect(find.byType(InteractiveViewer)));
    });

    for (final deg in const [0.0, 30.0, -30.0]) {
      testWidgets('抹掉一块（$deg°，按范围放过）：点屏上转过的位置，存下的是那几个地图点；存完底图重新取', (t) async {
        final api = _Api('admin', extra: <String, dynamic>{'rotation_deg': deg, 'content_bbox': _bbox})
          ..autoConfirm = true;
        await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1', version: '8')));
        await t.pumpAndSettle();
        await t.tap(find.byKey(SiteMapPreviewPage.drawEraseKey));
        await t.pump();
        final b = _box(t, find.byKey(SiteMapPreviewPage.canvasKey));
        const pxs = [Offset(60, 30), Offset(120, 30), Offset(120, 70)];
        for (final p in pxs) {
          await t.tapAt(b.localToGlobal(p));
          await t.pump();
        }
        expect(find.textContaining('抹掉：点图加顶点，已 3 个'), findsOneWidget);
        final before = api.calls.where((c) => c.startsWith('mapPreview ')).length;
        await t.tap(find.byKey(SiteMapPreviewPage.finishKey));
        await t.pumpAndSettle();
        final z = api.savedZones.single.single;
        expect(z['kind'], 'erase');
        expect(z['label'], '抹掉区');
        expect(z.containsKey('max_speed_mps'), isFalse);
        final poly = (z['polygon'] as List).cast<List>();
        for (var i = 0; i < 3; i++) {
          final (x, y) = _m(pxs[i].dx, pxs[i].dy);
          expect((poly[i][0] as num).toDouble(), closeTo(x, 0.011), reason: '第 $i 点 x');
          expect((poly[i][1] as num).toDouble(), closeTo(y, 0.011), reason: '第 $i 点 y');
        }
        expect(api.calls.where((c) => c.startsWith('mapPreview ')).length, greaterThan(before),
            reason: '抹掉区变了：底图重新取');
        expect(find.text('抹掉 · 抹掉区（z1）'), findsOneWidget);
        final painter = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.zonesKey)).painter! as ZonesPainter;
        expect(painter.shapes.single.erase, isTrue);
      });
    }

    testWidgets('抹掉区也能标硬的（遥控也守）；画别的、删别的区域不重新取底图', (t) async {
      final api = _Api('admin')..autoConfirm = true;
      await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1', version: '8')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SiteMapPreviewPage.drawEraseKey));
      await t.pump();
      expect(find.byKey(SiteMapPreviewPage.hardKey), findsOneWidget);
      await t.tap(find.byKey(SiteMapPreviewPage.hardKey));
      await t.pump();
      final b = _box(t, find.byKey(SiteMapPreviewPage.canvasKey));
      for (final p in const [Offset(20, 20), Offset(60, 20), Offset(60, 60)]) {
        await t.tapAt(b.localToGlobal(p));
        await t.pump();
      }
      await t.tap(find.byKey(SiteMapPreviewPage.finishKey));
      await t.pumpAndSettle();
      expect(api.savedZones.last.single['hard'], isTrue);
      expect(find.text('抹掉（硬） · 抹掉区（z1）'), findsOneWidget);
      final previews = api.calls.where((c) => c.startsWith('mapPreview ')).length;
      await t.tap(find.byKey(SiteMapPreviewPage.drawNogoKey));
      await t.pump();
      for (final p in const [Offset(120, 20), Offset(160, 20), Offset(160, 60)]) {
        await t.tapAt(b.localToGlobal(p));
        await t.pump();
      }
      await t.tap(find.byKey(SiteMapPreviewPage.finishKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('mapPreview ')).length, previews, reason: '抹掉区没变：不重新取');
      await t.tap(find.byKey(SiteMapPreviewPage.deleteKey('z1')));
      await t.pumpAndSettle();
      expect(api.savedZones.last.map((z) => z['kind']), ['nogo']);
      expect(api.calls.where((c) => c.startsWith('mapPreview ')).length, greaterThan(previews), reason: '删了抹掉区');
    });
  });
}
