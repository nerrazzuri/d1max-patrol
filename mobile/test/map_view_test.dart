// 地图给客户看得懂（2026-10-14）：站点给的 `rotation_deg`（整张图顺时针转、摆正）、`areas`（片区和名字）、
// `plan`（平面图）；图例只列图上有的、比例尺跟着缩放走、打开就是整张转过的图放进画布、回正回到同一个样子。
// 只是显示：点图换算出来发给站点的坐标在转了角度之后一点不变（往返测）。站点没给这些字段（老站点）照旧。
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/map_view.dart';
import 'package:d1max_patrol/ui/nw/nw_map_raster.dart';
import 'package:d1max_patrol/ui/nw/nw_p2_duty.dart' show NwMapThumb;
import 'package:d1max_patrol/ui/nw/nw_shell.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_map.dart';
import 'support/fake_site.dart';
import 'support/zone_ui.dart';

/// FakeApi 那张图：200 × 100 像素、一像素 0.1 m、左边缘 x = -10、上边缘 y = 5；[extra] 并进预览 JSON。
class _GeoApi extends FakeApi {
  _GeoApi(super.role, {this.extra = const <String, dynamic>{}});
  Map<String, dynamic> extra;

  /// 真：预览 JSON 取不到（老站点、坏了）。
  bool metaError = false;

  @override
  Future<Map<String, dynamic>> mapPreview(String mapId, String version) async {
    final m = await super.mapPreview(mapId, version);
    if (metaError) throw const SiteError(404, 'no meta');
    return <String, dynamic>{...m, ...extra};
  }
}

/// 图的像素 → 地图米（FakeApi 那张图）。
(double, double) _m(double px, double py) => (-10 + px * 0.1, 5 - py * 0.1);

const _plan = <String, dynamic>{'url': '/api/maps/estate-1/8/plan.png', 'scale': 2};

void _size(WidgetTester t, [Size size = const Size(390, 844)]) {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

Future<void> _pump(WidgetTester t, Widget page, {ThemeData? theme}) async {
  await t.pumpWidget(MaterialApp(theme: theme, home: Scaffold(body: page)));
  await t.pumpAndSettle();
}

RenderBox _box(WidgetTester t, Finder f) => t.renderObject<RenderBox>(f);

/// 一个渲染框在屏上的朝向（弧度，顺时针为正）和比例：它自己的 +x 方向到了屏上哪儿。
(double, double) _turn(RenderBox b) {
  final d = b.localToGlobal(const Offset(1, 0)) - b.localToGlobal(Offset.zero);
  return (math.atan2(d.dy, d.dx), d.distance);
}

double _zoom(WidgetTester t) =>
    t.widget<InteractiveViewer>(find.byType(InteractiveViewer)).transformationController!.value.getMaxScaleOnAxis();

Uint8List _bytesOf(WidgetTester t, Finder image) => (t.widget<Image>(image).image as MemoryImage).bytes;

Alert _alarm({String version = '8'}) => Alert.fromWire(<String, dynamic>{
      'key': 'A/dog_sees_person#1', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'A',
      'title': '机器人看见人了', 'detail': '', 'first_ms': 1, 'last_ms': 1, 'count': 1, 'acked_by': '', 'acked_ms': null,
      'context': <String, dynamic>{
        'zone': 'East wall',
        'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': version, 'x': 0.0, 'y': 0.0},
      },
    });

void main() {
  group('几何（纯函数）', () {
    test('站点没给新字段：不转、没有片区、没有平面图；给了坏的也按没有', () {
      final g = MapGeo.fromPreview(<String, dynamic>{'width': 200, 'height': 100, 'm_per_px': 0.1, 'left_x': -10, 'top_y': 5});
      expect(g.rotationDeg, 0);
      expect(g.areas, isEmpty);
      expect(g.planUrl, isNull);
      final bad = MapGeo.fromPreview(<String, dynamic>{
        'width': 200, 'height': 100, 'rotation_deg': 'x', 'plan': <String, dynamic>{'url': ''}, 'areas': 'x'});
      expect(bad.rotationDeg, 0);
      expect(bad.planUrl, isNull);
      expect(bad.areas, isEmpty);
      final some = MapGeo.fromPreview(<String, dynamic>{
        'width': 200, 'height': 100, 'rotation_deg': -30, 'plan': _plan,
        'areas': <dynamic>[
          <String, dynamic>{'name': 'Lawn', 'points': [for (final p in [(0, 0), (1, 0), (1, 1)]) {'x': p.$1, 'y': p.$2}]},
          <String, dynamic>{'name': '  ', 'points': [for (final p in [(0, 0), (1, 0), (1, 1)]) {'x': p.$1, 'y': p.$2}]},
          <String, dynamic>{'name': 'Two', 'points': [for (final p in [(0, 0), (1, 0)]) {'x': p.$1, 'y': p.$2}]},
          <String, dynamic>{'name': 'Bad', 'points': [{'x': 0}, 'p', {'x': 1, 'y': 'y'}]},
          'not a map',
        ]});
      expect(some.rotationDeg, -30);
      expect(some.planUrl, '/api/maps/estate-1/8/plan.png');
      expect(some.areas.map((a) => a.name), ['Lawn'], reason: '名字空的、不到三个点的不要');
    });

    test('地图 ↔ 图的像素往返；转角不进这一步（转是屏上的事）', () {
      final g = MapGeo.fromPreview(<String, dynamic>{
        'width': 200, 'height': 100, 'm_per_px': 0.1, 'left_x': -10, 'top_y': 5, 'rotation_deg': 30});
      for (final p in const [Offset(-8, 4), Offset(0, 0), Offset(9.95, -4.95), Offset(3.21, 1.07)]) {
        final back = g.toMap(g.toPx(p.dx, p.dy));
        expect((back - p).distance, lessThan(1e-9));
      }
      expect(g.toPx(-10, 5), Offset.zero);
      expect(g.toPx(0, 0), const Offset(100, 50));
    });

    test('屏幕坐标 y 朝下：转正角是顺时针（右边转到下边）；外接框', () {
      final d = nwRotate(const Offset(1, 0), math.pi / 2);
      expect(d.dx, closeTo(0, 1e-12));
      expect(d.dy, closeTo(1, 1e-12), reason: '+90°：右 → 下 = 顺时针');
      final up = nwRotate(const Offset(0, -1), math.pi / 2);
      expect(up.dx, closeTo(1, 1e-12), reason: '上 → 右');
      expect(up.dy, closeTo(0, 1e-12));
      final b = nwRotatedBounds(const Size(200, 100), math.pi / 2);
      expect(b.width, closeTo(100, 1e-9));
      expect(b.height, closeTo(200, 1e-9));
      final b30 = nwRotatedBounds(const Size(200, 100), math.pi / 6);
      expect(b30.width, closeTo(200 * math.cos(math.pi / 6) + 100 * 0.5, 1e-9));
      expect(b30.height, closeTo(200 * 0.5 + 100 * math.cos(math.pi / 6), 1e-9));
      final g = MapGeo.fromPreview(<String, dynamic>{'width': 200, 'height': 100, 'rotation_deg': 30});
      expect(g.fitScale(const Size(400, 400)), closeTo(math.min(400 / b30.width, 400 / b30.height), 1e-9));
    });

    test('比例尺：1/2/5/10/20 m 里挑不超过上限的最长一段；1 m 都超了就 1 m', () {
      expect(MapScaleBar.pick(10), (5, 50.0));
      expect(MapScaleBar.pick(3), (20, 60.0));
      expect(MapScaleBar.pick(0.5), (20, 10.0));
      expect(MapScaleBar.pick(40), (2, 80.0));
      expect(MapScaleBar.pick(200), (1, 200.0));
      expect(MapScaleBar.pick(9.6), (10, 96.0));
    });

    test('片区的名字落在视觉中心：长方形正中，L 形在片区里头', () {
      final rect = [const Offset(0, 0), const Offset(40, 0), const Offset(40, 300), const Offset(0, 300)];
      final c = polygonLabelPoint(rect);
      expect((c - const Offset(20, 150)).distance, lessThan(2), reason: '长条的取中间，不是一头');
      final l = [
        const Offset(0, 0), const Offset(100, 0), const Offset(100, 20), const Offset(20, 20),
        const Offset(20, 100), const Offset(0, 100),
      ];
      final p = polygonLabelPoint(l);
      final inside = (p.dx >= 0 && p.dx <= 100 && p.dy >= 0 && p.dy <= 20) || (p.dx >= 0 && p.dx <= 20 && p.dy >= 0 && p.dy <= 100);
      expect(inside, isTrue, reason: 'L 形的形心在拐角外面，名字不能落在那儿：$p');
    });

    test('名字放不放得下：片区在屏上太小不写，放大了写', () {
      final sq = [const Offset(0, 0), const Offset(10, 0), const Offset(10, 10), const Offset(0, 10)];
      const at = Offset(5, 5);
      expect(MapAreaLabels.fits(sq, at, 0, 1, 40, 16), isFalse);
      expect(MapAreaLabels.fits(sq, at, 0, 4, 40, 16), isTrue);
      expect(MapAreaLabels.fits(sq, at, math.pi / 6, 4, 40, 16), isTrue, reason: '转了不改大小');
      expect(MapAreaLabels.fits(sq, const Offset(0.5, 5), 0, 4, 40, 16), isFalse, reason: '那一点贴着边');
    });
  });

  group('地图栏：摆正', () {
    testWidgets('转角的方向和中心：+90° 把图右边的机器人转到中心正下方；中心就是画布的中心', (t) async {
      _size(t);
      final (x, y) = _m(180, 50); // 图右边、中线上
      final api = _GeoApi('guard', extra: <String, dynamic>{'rotation_deg': 90})..robotsOverride = robotsOnMap('8', x, y);
      await _pump(t, SiteMapTab(api: api));
      final canvas = _box(t, find.byKey(SiteMapTab.canvasKey));
      final k = canvas.size.width / 200;
      final centre = canvas.localToGlobal(canvas.size.center(Offset.zero));
      final view = t.getRect(find.byType(InteractiveViewer));
      expect((centre - view.center).distance, lessThan(0.01), reason: '绕图的中心转，图在画布正中');
      final marker = t.getCenter(find.byKey(SiteMapTab.markerKey('A')));
      expect(marker.dx, closeTo(centre.dx, 0.01));
      expect(marker.dy, closeTo(centre.dy + 80 * k, 0.01), reason: '顺时针：右边 → 下边');
      expect(_turn(canvas).$1, closeTo(math.pi / 2, 1e-9));
      await t.pumpWidget(Container());
    });

    for (final deg in const [30.0, -30.0]) {
      testWidgets('$deg°：底图、区域、待命点、片区跟着图转；机器人的牌子、片区名字是正的、在转过的位置上', (t) async {
        _size(t);
        final (x, y) = _m(40, 20);
        final api = _GeoApi('guard', extra: <String, dynamic>{
          'rotation_deg': deg,
          'areas': <dynamic>[
            <String, dynamic>{'name': 'Lawn', 'points': [
              for (final (px, py) in const [(20.0, 20.0), (100.0, 20.0), (100.0, 80.0), (20.0, 80.0)])
                <String, dynamic>{'x': _m(px, py).$1, 'y': _m(px, py).$2},
            ]},
          ],
        })
          ..robotsOverride = robotsOnMap('8', x, y)
          ..zonesView = <String, dynamic>{...FakeApi('guard').zonesView, 'zones': <dynamic>[
            <String, dynamic>{'id': 'z', 'kind': 'nogo', 'label': 'x', 'polygon': [[0, 0], [1, 0], [1, 1]]},
          ]};
        await _pump(t, SiteMapTab(api: api));
        final rad = deg * math.pi / 180;
        final canvas = _box(t, find.byKey(SiteMapTab.canvasKey));
        final k = canvas.size.width / 200;
        final c = canvas.size.center(Offset.zero);
        final centre = canvas.localToGlobal(c);
        Offset onScreen(Offset local) => centre + nwRotate(local - c, rad);
        for (final key in [SiteMapTab.zonesKey, SiteMapTab.standbyKey, SiteMapTab.areasKey]) {
          expect(_turn(_box(t, find.byKey(key))).$1, closeTo(rad, 1e-9), reason: '$key 跟着图转');
        }
        final image = find.descendant(of: find.byKey(SiteMapTab.canvasKey), matching: find.byType(Image));
        expect(_turn(_box(t, image)).$1, closeTo(rad, 1e-9), reason: '底图跟着转');
        // 狗：图上 (40, 20) 像素
        final marker = find.byKey(SiteMapTab.markerKey('A'));
        expect((t.getCenter(marker) - onScreen(const Offset(40, 20) * k)).distance, lessThan(0.01));
        expect(_turn(_box(t, marker)).$1, closeTo(0, 1e-9), reason: '牌子正着读');
        // 片区名字：在片区的视觉中心（长方形的正中 (60, 50)），正着
        final label = find.byKey(SiteMapTab.areaLabelKey('Lawn'));
        expect(label, findsOneWidget);
        expect((t.getCenter(label) - onScreen(const Offset(60, 50) * k)).distance, lessThan(1));
        expect(_turn(_box(t, label)).$1, closeTo(0, 1e-9), reason: '名字正着读');
        expect(_turn(_box(t, label)).$2, closeTo(1, 1e-9), reason: '名字不跟着缩放变大变小');
        await t.pumpWidget(Container());
      });
    }

    testWidgets('站点没给转角：跟原来一样不转', (t) async {
      _size(t);
      final api = FakeApi('guard');
      await _pump(t, SiteMapTab(api: api));
      expect(_turn(_box(t, find.byKey(SiteMapTab.canvasKey))).$1, 0);
      expect(find.byKey(SiteMapTab.areasKey), findsNothing);
      await t.pumpWidget(Container());
    });

    for (final deg in const [0.0, 23.0, -40.0]) {
      testWidgets('打开就是整张转过的图放进画布（$deg°）；放大、拖了以后回正回到一模一样', (t) async {
        _size(t);
        final api = _GeoApi('guard', extra: <String, dynamic>{'rotation_deg': deg});
        await _pump(t, SiteMapTab(api: api));
        final view = t.getRect(find.byType(InteractiveViewer));
        List<Offset> corners() {
          final b = _box(t, find.byKey(SiteMapTab.canvasKey));
          return [for (final p in [Offset.zero, b.size.topRight(Offset.zero), b.size.bottomRight(Offset.zero), b.size.bottomLeft(Offset.zero)]) b.localToGlobal(p)];
        }

        final fitted = corners();
        for (final p in fitted) {
          expect(view.inflate(0.01).contains(p), isTrue, reason: '角 $p 在画布 $view 里');
        }
        final xs = fitted.map((p) => p.dx), ys = fitted.map((p) => p.dy);
        final w = xs.reduce(math.max) - xs.reduce(math.min), h = ys.reduce(math.max) - ys.reduce(math.min);
        expect(math.max(w / view.width, h / view.height), closeTo(1, 1e-6), reason: '放满一边（不是缩在中间一小块）');
        await t.tap(find.byKey(SiteMapTab.zoomInKey));
        await t.tap(find.byKey(SiteMapTab.zoomInKey));
        await t.pump();
        await t.drag(find.byType(InteractiveViewer), const Offset(-40, 30));
        await t.pumpAndSettle();
        expect((corners().first - fitted.first).distance, greaterThan(1));
        await t.tap(find.byKey(SiteMapTab.recenterKey));
        await t.pump();
        expect(_zoom(t), 1.0);
        final back = corners();
        for (var i = 0; i < 4; i++) {
          expect((back[i] - fitted[i]).distance, lessThan(1e-6), reason: '回正 = 打开时的样子');
        }
        await t.pumpWidget(Container());
      });
    }
  });

  group('地图栏：比例尺、片区、图例', () {
    for (final deg in const [0.0, 30.0]) {
      testWidgets('比例尺跟着缩放走（$deg°；转角不改长短）', (t) async {
        _size(t);
        final api = _GeoApi('guard', extra: <String, dynamic>{'rotation_deg': deg});
        await _pump(t, SiteMapTab(api: api));
        final k = _box(t, find.byKey(SiteMapTab.canvasKey)).size.width / 200;
        void check() {
          final pxPerM = k / 0.1 * _zoom(t);
          final (m, px) = MapScaleBar.pick(pxPerM);
          expect(t.getSize(find.byKey(SiteMapTab.scaleKey)).width, closeTo(px, 1e-6));
          expect(find.descendant(of: find.byType(MapScaleBar), matching: find.text('$m m')), findsOneWidget);
          // 屏上量：这一段的长度 = m 米在屏上的长度（图上两点隔 m 米）
          final b = _box(t, find.byKey(SiteMapTab.canvasKey));
          final onScreen = (b.localToGlobal(Offset(m / 0.1 * k, 0)) - b.localToGlobal(Offset.zero)).distance;
          expect(t.getSize(find.byKey(SiteMapTab.scaleKey)).width, closeTo(onScreen, 1e-6));
        }

        check();
        final first = find.descendant(of: find.byType(MapScaleBar), matching: find.byType(Text));
        final before = t.widget<Text>(first).data;
        for (var i = 0; i < 4; i++) {
          await t.tap(find.byKey(SiteMapTab.zoomInKey));
          await t.pump();
          check();
        }
        expect(t.widget<Text>(first).data, isNot(before), reason: '放大了换一段更短的');
        await t.tap(find.byKey(SiteMapTab.zoomOutKey));
        await t.pump();
        check();
        await t.pumpWidget(Container());
      });
    }

    testWidgets('片区的名字：大的写，太小的不写，放大了写；名字空的不画', (t) async {
      _size(t);
      List<Map<String, dynamic>> pts(List<(double, double)> px) =>
          [for (final (x, y) in px) <String, dynamic>{'x': _m(x, y).$1, 'y': _m(x, y).$2}];
      final api = _GeoApi('guard', extra: <String, dynamic>{
        'areas': <dynamic>[
          <String, dynamic>{'name': 'Lawn', 'points': pts([(20, 20), (100, 20), (100, 80), (20, 80)])},
          <String, dynamic>{'name': 'Shed', 'points': pts([(150, 40), (154, 40), (154, 44), (150, 44)])},
          <String, dynamic>{'name': '', 'points': pts([(160, 60), (190, 60), (190, 90)])},
        ],
      });
      await _pump(t, SiteMapTab(api: api));
      expect(find.byKey(SiteMapTab.areaLabelKey('Lawn')), findsOneWidget);
      expect(find.byKey(SiteMapTab.areaLabelKey('Shed')), findsNothing, reason: '屏上 7 个像素见方，写不下');
      final painter = t.widget<CustomPaint>(find.byKey(SiteMapTab.areasKey)).painter! as AreasPainter;
      expect(painter.shapes.length, 2, reason: '名字空的那片不画');
      final label = t.widget<Text>(find.byKey(SiteMapTab.areaLabelKey('Lawn')));
      expect(label.style!.fontSize, greaterThanOrEqualTo(12));
      for (var i = 0; i < 5; i++) {
        await t.tap(find.byKey(SiteMapTab.zoomInKey));
        await t.pump();
      }
      expect(find.byKey(SiteMapTab.areaLabelKey('Shed')), findsOneWidget, reason: '放大了写得下');
      await t.tap(find.byKey(SiteMapTab.recenterKey));
      await t.pump();
      expect(find.byKey(SiteMapTab.areaLabelKey('Shed')), findsNothing);
      await t.pumpWidget(Container());
    });

    Finder inLegend(String text) => find.descendant(of: find.byKey(SiteMapTab.legendKey), matching: find.text(text));

    testWidgets('图例只列图上有的：底图三样总有；没画的机器人、原点、区域、片区不列', (t) async {
      _size(t);
      await _pump(t, SiteMapTab(api: FakeApi('guard'))); // 狗不在这张图上、只有待命点
      for (final s in ['地面', '墙、障碍物', '未扫描', '待命点']) {
        expect(inLegend(s), findsOneWidget, reason: s);
      }
      for (final s in ['机器人', '原点', '禁行区', '限速区', '片区']) {
        expect(inLegend(s), findsNothing, reason: s);
      }
      await t.pumpWidget(Container());
    });

    testWidgets('图例：图上都有就都列；英文也有', (t) async {
      _size(t, const Size(390, 1400));
      final api = _GeoApi('guard', extra: <String, dynamic>{
        'homes': <dynamic>[<String, dynamic>{'robot_id': 'A', 'name': 'o', 'x': 0.0, 'y': 0.0}],
        'areas': <dynamic>[
          <String, dynamic>{'name': 'Lawn', 'points': [for (final p in [(-8, 3), (0, 3), (0, -3)]) {'x': p.$1, 'y': p.$2}]},
        ],
      })
        ..robotsOverride = robotsOnMap('8', 1, 2)
        ..zonesView = <String, dynamic>{...FakeApi('guard').zonesView, 'zones': <dynamic>[
          <String, dynamic>{'id': 'a', 'kind': 'nogo', 'label': 'x', 'polygon': [[0, 0], [1, 0], [1, 1]]},
          <String, dynamic>{'id': 'b', 'kind': 'slow', 'label': 'y', 'max_speed_mps': 0.3, 'polygon': [[2, 0], [3, 0], [3, 1]]},
        ]};
      await _pump(t, SiteMapTab(api: api));
      for (final s in ['地面', '墙、障碍物', '未扫描', '机器人', '待命点', '原点', '禁行区', '限速区', '片区']) {
        expect(inLegend(s), findsOneWidget, reason: s);
      }
      appLang.value = AppLang.en;
      addTearDown(() => appLang.value = AppLang.zh);
      await _pump(t, SiteMapTab(key: UniqueKey(), api: api));
      for (final s in ['Floor', 'Wall, obstacle', 'Not mapped', 'Robot', 'Standby point', 'Origin', 'No-go zone', 'Speed-limit zone', 'Named area']) {
        expect(inLegend(s), findsOneWidget, reason: s);
      }
      await t.pumpWidget(Container());
    });

    testWidgets('图例展开了会压着整张图（真图、竖屏）：先收着，点「图例」展开、再点收起', (t) async {
      _size(t);
      await t.pumpWidget(MaterialApp(home: NwShell(api: Rot23Api('guard'), ring: () {})));
      await t.pumpAndSettle();
      await t.tap(find.byKey(NwShell.mapKey));
      await t.pumpAndSettle();
      expect(find.byKey(SiteMapTab.legendKey), findsOneWidget);
      expect(inLegend('地面'), findsNothing);
      expect(inLegend('图例'), findsOneWidget);
      await t.tap(find.byKey(SiteMapTab.legendToggleKey));
      await t.pump();
      for (final s in ['地面', '墙、障碍物', '未扫描', '机器人', '待命点', '原点', '禁行区', '限速区', '片区']) {
        expect(inLegend(s), findsOneWidget, reason: s);
      }
      expect(t.takeException(), isNull);
      await t.tap(find.byKey(SiteMapTab.legendToggleKey));
      await t.pump();
      expect(inLegend('地面'), findsNothing);
      await t.pumpWidget(Container());
    });
  });

  group('平面图', () {
    for (final b in Brightness.values) {
      testWidgets('站点给了平面图：取它（不取栅格图）、画它；深色过同一个滤镜（${b.name}）', (t) async {
        _size(t);
        final api = _GeoApi('guard', extra: <String, dynamic>{'plan': _plan});
        await _pump(t, SiteMapTab(api: api), theme: nwTheme(b));
        expect(api.calls, contains('mapPlanPng /api/maps/estate-1/8/plan.png'));
        expect(api.calls.where((c) => c.startsWith('mapPreviewPng')), isEmpty);
        final image = find.descendant(of: find.byKey(SiteMapTab.canvasKey), matching: find.byType(Image));
        expect(image, findsOneWidget);
        expect(_bytesOf(t, image), fakePlanPng);
        final filter = find.byKey(SiteMapTab.darkRasterKey);
        if (b == Brightness.dark) {
          expect(t.widget<ColorFiltered>(filter).colorFilter, darkMapRaster);
          expect(find.descendant(of: filter, matching: image), findsOneWidget);
        } else {
          expect(filter, findsNothing);
        }
        expect(find.descendant(of: find.byKey(SiteMapTab.legendKey), matching: find.text('未扫描')), findsOneWidget);
        await t.pumpWidget(Container());
      });
    }

    testWidgets('平面图取不到：退回栅格图；站点没给平面图：只取栅格图', (t) async {
      _size(t);
      final api = _GeoApi('guard', extra: <String, dynamic>{'plan': _plan})..planError = const SiteError(500, 'x');
      await _pump(t, SiteMapTab(api: api));
      expect(api.calls, containsAll(['mapPlanPng /api/maps/estate-1/8/plan.png', 'mapPreviewPng estate-1:8']));
      final image = find.descendant(of: find.byKey(SiteMapTab.canvasKey), matching: find.byType(Image));
      expect(_bytesOf(t, image), fakePreviewPng);
      final old = FakeApi('guard');
      await _pump(t, SiteMapTab(key: UniqueKey(), api: old));
      expect(old.calls.where((c) => c.startsWith('mapPlanPng')), isEmpty);
      expect(_bytesOf(t, find.descendant(of: find.byKey(SiteMapTab.canvasKey), matching: find.byType(Image))),
          fakePreviewPng);
      await t.pumpWidget(Container());
    });

    testWidgets('看图页也用平面图、也转', (t) async {
      final api = _GeoApi('guard', extra: <String, dynamic>{'plan': _plan, 'rotation_deg': 23});
      await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1', version: '8')));
      await t.pumpAndSettle();
      expect(_bytesOf(t, find.byType(Image)), fakePlanPng);
      expect(api.calls.where((c) => c.startsWith('mapPreviewPng')), isEmpty);
      expect(_turn(_box(t, find.byKey(SiteMapPreviewPage.canvasKey))).$1, closeTo(23 * math.pi / 180, 1e-9));
    });
  });

  group('点图换坐标：转了角度，发给站点的坐标一点不变', () {
    for (final deg in const [0.0, 30.0, -30.0]) {
      testWidgets('看图页画禁行区（$deg°）：点屏上转过的位置，存下的是那几个地图点', (t) async {
        final api = _GeoApi('admin', extra: <String, dynamic>{'rotation_deg': deg});
        await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1', version: '8')));
        await t.pumpAndSettle();
        await tapZoneMenu(t, SiteMapPreviewPage.drawNogoKey);
        await t.pump();
        final b = _box(t, find.byKey(SiteMapPreviewPage.canvasKey));
        const c = Offset(100, 50);
        final centre = b.localToGlobal(c);
        final (turn, s) = _turn(b);
        expect(turn, closeTo(deg * math.pi / 180, 1e-9));
        // 自己算屏上的位置（绕图的中心顺时针转、乘缩放），跟 Flutter 画出来的对得上，再点它
        Offset screen(Offset px) => centre + nwRotate(px - c, deg * math.pi / 180) * s;
        const pxs = [Offset(20, 10), Offset(60, 10), Offset(60, 40)];
        for (final p in pxs) {
          expect((screen(p) - b.localToGlobal(p)).distance, lessThan(1e-6));
          await t.tapAt(screen(p));
          await t.pump();
        }
        await t.tap(find.byKey(SiteMapPreviewPage.finishKey));
        await t.pumpAndSettle();
        final poly = (api.savedZones.single.single['polygon'] as List).cast<List>();
        for (var i = 0; i < 3; i++) {
          final (x, y) = _m(pxs[i].dx, pxs[i].dy);
          expect((poly[i][0] as num).toDouble(), closeTo(x, 0.011), reason: '第 $i 点 x');
          expect((poly[i][1] as num).toDouble(), closeTo(y, 0.011), reason: '第 $i 点 y');
        }
      });
    }

    testWidgets('看图页：片区画出来，名字正着；告警现场的点跟着图转', (t) async {
      final api = _GeoApi('guard', extra: <String, dynamic>{
        'rotation_deg': -30,
        'areas': <dynamic>[
          <String, dynamic>{'name': 'Lawn', 'points': [
            for (final (px, py) in const [(20.0, 10.0), (180.0, 10.0), (180.0, 90.0), (20.0, 90.0)])
              <String, dynamic>{'x': _m(px, py).$1, 'y': _m(px, py).$2},
          ]},
        ],
      });
      await t.pumpWidget(MaterialApp(
          home: SiteMapPreviewPage(api: api, mapId: 'estate-1', version: '8', marks: const [MapMark('A', 0, 0, Colors.red)])));
      await t.pumpAndSettle();
      expect(find.byKey(SiteMapPreviewPage.areasKey), findsOneWidget);
      final label = find.byKey(SiteMapPreviewPage.areaLabelKey('Lawn'));
      expect(label, findsOneWidget);
      expect(_turn(_box(t, label)).$1, closeTo(0, 1e-9));
      expect(_turn(_box(t, label)).$2, closeTo(1, 1e-6), reason: '屏上 12 号字，不跟着图缩');
      expect(_turn(_box(t, find.byKey(SiteMapPreviewPage.marksKey))).$1, closeTo(-30 * math.pi / 180, 1e-9));
    });
  });

  group('告警页小地图', () {
    Future<void> scene(WidgetTester t, FakeApi api, Alert a) async {
      _size(t);
      api.alertRows = <Map<String, dynamic>>[];
      await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: a)));
      await t.pumpAndSettle();
      await t.ensureVisible(find.byKey(SiteAlertScenePage.mapKey));
      await t.pumpAndSettle();
    }

    Finder thumbImage() => find.descendant(of: find.byType(NwMapThumb), matching: find.byType(Image));

    testWidgets('站点给了转角、平面图：缩略也摆正、用平面图', (t) async {
      final api = _GeoApi('guard', extra: <String, dynamic>{'rotation_deg': 23, 'plan': _plan});
      await scene(t, api, _alarm());
      final rot = find.descendant(of: find.byType(NwMapThumb), matching: find.byType(NwMapRotation));
      expect(rot, findsOneWidget);
      expect(t.widget<NwMapRotation>(rot).rad, closeTo(23 * math.pi / 180, 1e-12));
      expect(_turn(_box(t, thumbImage())).$1, closeTo(23 * math.pi / 180, 1e-9));
      expect(_bytesOf(t, thumbImage()), fakePlanPng);
      await t.pumpWidget(Container());
    });

    testWidgets('老站点（没有新字段、或者元数据取不到）：缩略照旧不转、用栅格图', (t) async {
      for (final api in [_GeoApi('guard'), _GeoApi('guard')..metaError = true]) {
        await scene(t, api, _alarm());
        expect(find.descendant(of: find.byType(NwMapThumb), matching: find.byType(NwMapRotation)), findsNothing);
        expect(_bytesOf(t, thumbImage()), fakePreviewPng);
        expect(api.calls, contains('mapPreviewPng estate-1:8'));
        await t.pumpWidget(Container());
      }
    });
  });

  group('不溢出：转了的真图、平面图、片区', () {
    for (final b in Brightness.values) {
      for (final lang in AppLang.values) {
        for (final size in const [Size(390, 844), Size(800, 360), Size(1280, 720)]) {
          testWidgets('[${b.name}] [${lang.name}] ${size.width.toInt()}×${size.height.toInt()}：地图栏（图例收着、展开）、看图页',
              (t) async {
            appLang.value = lang;
            addTearDown(() => appLang.value = AppLang.zh);
            _size(t, size);
            await t.pumpWidget(MaterialApp(theme: nwTheme(b), home: NwShell(api: Rot23Api('admin'), ring: () {})));
            await t.pumpAndSettle();
            await t.tap(find.byKey(NwShell.mapKey));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
            await t.tap(find.byKey(SiteMapTab.legendToggleKey));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
            await t.tap(find.byKey(SiteMapTab.zoomInKey));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
            await t.pumpWidget(MaterialApp(
                theme: nwTheme(b), home: SiteMapPreviewPage(api: Rot23Api('admin'), mapId: 'estate-1', version: '8')));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
            await t.pumpWidget(Container());
          });
        }
      }
    }
  });
}
