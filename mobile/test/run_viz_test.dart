// 运营可视化（手机）：地图栏上的规划路径 / 轨迹 / 被挡 / 定位问题的点（转角下对得上）、单狗页的被挡和重规划、
// 定位变差的说法、两个新接口的请求。
import 'dart:math' as math;

import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/nw/nw_map_raster.dart';
import 'package:d1max_patrol/ui/run_viz.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart' show SiteRobotPage;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

void _size(WidgetTester t, [Size size = const Size(390, 844)]) {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

/// 报着 estate-1:8 这张图、站在 (1, 2) 的狗；预览可以带转角。
class _Api extends FakeApi {
  _Api(super.role, {this.rotation});
  final double? rotation;

  @override
  Future<Map<String, dynamic>> mapPreview(String mapId, String version) async {
    final m = await super.mapPreview(mapId, version);
    return rotation == null ? m : <String, dynamic>{...m, 'rotation_deg': rotation};
  }
}

List<Map<String, dynamic>> _robotsOn(String version) => [
      for (final r in (siteFixture('site_robots')['robots'] as List).cast<Map<String, dynamic>>())
        <String, dynamic>{
          ...r,
          'capabilities': <String, dynamic>{
            ...(r['capabilities'] as Map).cast<String, dynamic>(),
            'tasks': <String, dynamic>{
              ...((r['capabilities'] as Map)['tasks'] as Map).cast<String, dynamic>(),
              'patrol': <String, dynamic>{'autonomy': 'autonomous', 'map_id': 'estate-1', 'map_version': version},
            },
          },
        },
    ];

Map<String, dynamic> _viz({String version = '8'}) => <String, dynamic>{
      'robots': <String, dynamic>{
        'A': <String, dynamic>{
          'map': <String, dynamic>{'map_id': 'estate-1', 'version': version},
          'path': [
            [1.0, 2.0],
            [4.0, 2.0],
            [4.0, -1.0]
          ],
          'trail': [
            [-3.0, 2.0],
            [-1.0, 2.0],
            [1.0, 2.0]
          ],
          'blocked': {'x': 4.0, 'y': 0.5, 'for_s': 12, 'reason': '前方 0.4 m 有挡'},
        },
      },
    };

final List<Map<String, dynamic>> _episodes = [
  {'kind': 'lost', 'x': -5.0, 'y': 3.0, 'cause': '雷达没数据', 'robot_id': 'A'},
  {'kind': 'degraded', 'x': 2.0, 'y': -2.0, 'cause': '在推算', 'robot_id': 'A'},
  {'kind': 'lost', 'x': null, 'y': null, 'cause': '没位置的不画', 'robot_id': 'A'},
];

Future<void> _pump(WidgetTester t, Widget page) async {
  await t.pumpWidget(MaterialApp(home: Scaffold(body: page)));
  await t.pumpAndSettle();
}

void main() {
  group('纯算', () {
    test('runVizShapes：只取报着这张图的机器人；坐标换成画布像素；乱写的点丢掉', () {
      Offset px(double x, double y) => Offset(x * 10, y * 10);
      final shapes = runVizShapes(_viz(), 'estate-1', '8', px);
      expect(shapes.length, 1);
      expect(shapes[0].path, [const Offset(10, 20), const Offset(40, 20), const Offset(40, -10)]);
      expect(shapes[0].trail.length, 3);
      expect(shapes[0].blocked, const Offset(40, 5));
      expect(runVizShapes(_viz(), 'estate-1', '9', px), isEmpty, reason: '别的版本的路径不画');
      expect(runVizShapes(null, 'estate-1', '8', px), isEmpty);
      expect(runVizShapes(<String, dynamic>{'robots': 5}, 'estate-1', '8', px), isEmpty);
      final bad = <String, dynamic>{
        'robots': {
          'A': {
            'map': {'map_id': 'estate-1', 'version': '8'},
            'path': [
              [1, 2],
              ['a', 2],
              [double.nan, 1],
              [3]
            ],
            'trail': 'x',
            'blocked': {'x': 'q', 'y': 1},
          }
        }
      };
      final s = runVizShapes(bad, 'estate-1', '8', px).single;
      expect(s.path, [const Offset(10, 20)]);
      expect(s.trail, isEmpty);
      expect(s.blocked, isNull);
    });

    test('episodeDots：有位置的才画；丢了和变差分开', () {
      final dots = episodeDots(_episodes, (x, y) => Offset(x, y));
      expect(dots, [(const Offset(-5, 3), true), (const Offset(2, -2), false)]);
    });

    test('navText：被挡写多久和原因；重规划写次数和原因；没有是空串', () {
      expect(navText(null), '');
      expect(navText(<String, dynamic>{}), '');
      expect(navText(<String, dynamic>{'blocked': {'for_s': 12, 'reason': '前方 0.4 m 有挡'}}), '被挡 12 秒：前方 0.4 m 有挡');
      expect(navText(<String, dynamic>{'blocked': {'for_s': 5, 'reason': ''}}), '被挡 5 秒：没说原因');
      expect(navText(<String, dynamic>{'replan': {'n': 2, 'why': 'detour'}}), '重规划 2 次（绕障）');
      expect(navText(<String, dynamic>{'replan': {'n': 1, 'why': 'preview'}}), '重规划 1 次（前方有障碍，提前绕）');
      expect(navText(<String, dynamic>{'replan': {'n': 1, 'why': '???'}}), '重规划 1 次（偏离路径）');
      expect(
          navText(<String, dynamic>{
            'blocked': {'for_s': 1, 'reason': 'x'},
            'replan': {'n': 3, 'why': 'zones'}
          }),
          '被挡 1 秒：x\n重规划 3 次（区域换了）');
    });

    test('locText：可信但变差了，带机器人说的原因', () {
      final l = <String, dynamic>{'localizer': 'bridge', 'anchored': true, 'reason': '', 'source': 'scan_match', 'sigma_m': 0.6};
      expect(locText(l), '定位：点云匹配，偏差约 0.6 m');
      expect(locText(<String, dynamic>{...l, 'degraded': '定位偏差可能到 0.60 m'}), '定位：点云匹配，偏差约 0.6 m · 变差：定位偏差可能到 0.60 m');
    });
  });

  group('地图栏', () {
    testWidgets('路径、轨迹、被挡、定位问题画在转角里的同一层；坐标是画布像素，图例只列有的', (t) async {
      _size(t);
      final api = _Api('guard', rotation: 20)
        ..robotsOverride = _robotsOn('8')
        ..vizBody = _viz()
        ..episodesBody = _episodes;
      await _pump(t, SiteMapTab(api: api));
      expect(api.calls, containsAll(['runViz', 'locEpisodes estate-1:8']));
      final paint = t.widget<CustomPaint>(find.byKey(SiteMapTab.runVizKey));
      final painter = paint.painter! as RunVizPainter;
      // 跟地图栏上待命点同一个换算：左边缘 x = -10、上边缘 y = 5、一像素 0.1 m；转了 20° 的画布，外接框不是
      // 画布大小，所以比例 k 从画出来的路径长度反推（(1,2)→(4,-1) 在预览上是 hypot(30,30) 像素）
      final s = painter.shapes.single;
      final k = (s.path.first - s.path.last).distance / math.sqrt(30 * 30 + 30 * 30);
      expect(k, greaterThan(0));
      Offset px(double x, double y) => Offset((x + 10) / 0.1, (5 - y) / 0.1) * k;
      expect((s.path.first - px(1, 2)).distance, lessThan(1e-6));
      expect((s.path.last - px(4, -1)).distance, lessThan(1e-6));
      expect((s.blocked! - px(4, 0.5)).distance, lessThan(1e-6));
      expect(painter.episodes.length, 2);
      expect((painter.episodes[0].$1 - px(-5, 3)).distance, lessThan(1e-6));
      expect(painter.episodes[0].$2, isTrue);
      // 在转正的那一层里面（跟待命点、区域一起转），不在外面
      expect(find.ancestor(of: find.byKey(SiteMapTab.runVizKey), matching: find.byType(NwMapRotation)), findsOneWidget);
      expect(find.ancestor(of: find.byKey(SiteMapTab.standbyKey), matching: find.byType(NwMapRotation)), findsOneWidget);
      // 图例（加了几项以后人没点过时可能是收起的：点开）
      if (find.text('规划路径').evaluate().isEmpty) {
        await t.tap(find.byKey(SiteMapTab.legendToggleKey));
        await t.pumpAndSettle();
      }
      for (final label in ['规划路径', '最近走过', '被挡的地方', '定位出过问题']) {
        expect(find.descendant(of: find.byKey(SiteMapTab.legendKey), matching: find.text(label)), findsOneWidget, reason: label);
      }
      await t.pumpWidget(Container());
    });

    testWidgets('放大了线不变粗：线宽除以缩放', (t) async {
      _size(t);
      final api = _Api('guard')..robotsOverride = _robotsOn('8')..vizBody = _viz();
      await _pump(t, SiteMapTab(api: api));
      expect((t.widget<CustomPaint>(find.byKey(SiteMapTab.runVizKey)).painter! as RunVizPainter).zoom, 1.0);
      await t.tap(find.byKey(SiteMapTab.zoomInKey));
      await t.pumpAndSettle();
      expect((t.widget<CustomPaint>(find.byKey(SiteMapTab.runVizKey)).painter! as RunVizPainter).zoom, greaterThan(1.4));
      await t.pumpWidget(Container());
    });

    testWidgets('别的图版本上的路径不画；什么都没有就没有图例项；读不到也照样看图', (t) async {
      _size(t);
      final other = _Api('guard')..robotsOverride = _robotsOn('8')..vizBody = _viz(version: '9');
      await _pump(t, SiteMapTab(api: other));
      final painter = t.widget<CustomPaint>(find.byKey(SiteMapTab.runVizKey)).painter! as RunVizPainter;
      expect(painter.shapes, isEmpty);
      expect(find.descendant(of: find.byKey(SiteMapTab.legendKey), matching: find.text('规划路径')), findsNothing);
      await t.pumpWidget(Container());
      final broken = _Api('guard')
        ..robotsOverride = _robotsOn('8')
        ..vizError = const SiteError(500, '站点忙');
      await _pump(t, SiteMapTab(api: broken));
      expect(find.byKey(SiteMapTab.canvasKey), findsOneWidget, reason: '运行图层读不到不影响看图');
      expect(t.widget<CustomPaint>(find.byKey(SiteMapTab.runVizKey)).painter, isA<RunVizPainter>());
      await t.pumpWidget(Container());
    });
  });

  group('单机器人页', () {
    testWidgets('被挡、重规划写在定位下面；没有就不出现', (t) async {
      final view = <String, dynamic>{
        ...siteFixture('site_robot'),
        'nav': {
          'blocked': {'for_s': 12, 'reason': '前方 0.4 m 有挡'},
          'replan': {'n': 1, 'why': 'detour'}
        },
      };
      await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: FakeApi('guard', robotView: view), robotId: 'A')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('robot-nav')), findsOneWidget);
      expect(find.textContaining('被挡 12 秒：前方 0.4 m 有挡'), findsOneWidget);
      expect(find.textContaining('重规划 1 次（绕障）'), findsOneWidget);
      await t.pumpWidget(Container());
      await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: FakeApi('guard'), robotId: 'A')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('robot-nav')), findsNothing);
    });
  });
}
