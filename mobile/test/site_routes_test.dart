// 巡检路线（手机，2026-10-14）：路线列表、编辑页（点图加点、挪点、调序、删、动作、存）、逐点校验的错、
// 冲突、派给狗；地图栏点图「去这里 / 在这里设位置」。
// 点图的位置：测试用图自己的渲染框 `localToGlobal`（转角、缩放都算进去）算出屏上哪儿，点下去，
// 存下去 / 发出去的地图坐标要跟想点的那一处对上 —— 图不转、转 23° 都测。
import 'dart:async';
import 'dart:math' as math;

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/map_view.dart';
import 'package:d1max_patrol/ui/pose_marker.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_pose_pick.dart';
import 'package:d1max_patrol/ui/site_routes.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_map.dart' show Rot23Api, rot23Preview;

Map<String, dynamic> _route(String id, List<List<Object>> pts, {int rev = 3}) => <String, dynamic>{
      'id': id, 'map_id': 'estate-1', 'map_version': '8', 'revision': rev, 'bound': true,
      'policy': <String, dynamic>{},
      'waypoints': [
        for (var i = 0; i < pts.length; i++)
          <String, dynamic>{'name': 'P${i + 1}', 'x': pts[i][0], 'y': pts[i][1], 'yaw': 0.0},
      ],
    };

/// 转了 23° 的图，管理员，路线库里有一条 loop。
class RoutesApi extends Rot23Api {
  RoutesApi(super.role) {
    routesBody = <String, dynamic>{
      'revision': 2, 'timezone': 'UTC', 'schedules': <dynamic>[],
      'routes': [_route('loop', [[-3.0, 6.0], [-1.0, 8.0], [0.0, 9.0]])],
    };
  }
}

/// 图（画布）上地图坐标 ([x], [y]) 米在屏幕上的位置，和反过来：画布本地一点对应的地图米。
class _Canvas {
  final WidgetTester t;
  final Key key;
  final MapGeo geo;
  _Canvas(this.t, this.key, this.geo);

  RenderBox get box => t.renderObject<RenderBox>(find.byKey(key));

  /// 画布一个本地点（单位：图的像素 × 缩放比 [k]）→ 屏幕。
  double get k => box.size.width / geo.width;
  Offset global(double x, double y) => box.localToGlobal(geo.toPx(x, y) * k);
}

/// 面板是个 ListView，屏外的行没建出来：滚到看得见再点。
Future<void> _reveal(WidgetTester t, Key key) async {
  await t.scrollUntilVisible(find.byKey(key), 120, scrollable: find.descendant(of: find.byType(ListView), matching: find.byType(Scrollable)).first);
  await t.pumpAndSettle();
}

/// 面板里的按钮现在能不能按（滚到看得见再读）。
Future<bool> _enabled(WidgetTester t, Key key, Type kind) async {
  await _reveal(t, key);
  final b = t.widget(find.byKey(key));
  expect(b.runtimeType, kind);
  return (b is FilledButton ? b.onPressed : (b as OutlinedButton).onPressed) != null;
}

Future<void> _tapKey(WidgetTester t, Key key) async {
  await _reveal(t, key);
  await t.tap(find.byKey(key));
  await t.pumpAndSettle();
}

Future<void> _enter(WidgetTester t, Key key, String text) async {
  await _reveal(t, key);
  await t.enterText(find.byKey(key), text);
  await t.pumpAndSettle();
}

Future<void> _tapMap(WidgetTester t, _Canvas c, double x, double y) async {
  await t.tapAt(c.global(x, y));
  await t.pumpAndSettle();
}

Future<void> _open(WidgetTester t, SiteApi api, {Map<String, dynamic>? route, String? newName}) async {
  await t.binding.setSurfaceSize(const Size(900, 700));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(
      home: SiteRouteEditorPage(
          api: api, mapId: 'estate-1', version: '8', route: route, newName: newName,
          allRoutes: [?route])));
  await t.pumpAndSettle();
}

void main() {
  // 英文界面（测试默认是中文）：断言的文字用英文写，切完切回来
  setUp(() => appLang.value = AppLang.en);
  tearDown(() => appLang.value = AppLang.zh);

  group('路线编辑页：点图加点，存下去的是点的那一处的地图坐标', () {
    for (final rotated in [false, true]) {
      testWidgets('图${rotated ? '转 23°' : '不转'}', (t) async {
        final api = rotated ? RoutesApi('admin') : FakeApi('admin');
        await _open(t, api, newName: 'night');
        final geo = MapGeo.fromPreview(rotated ? rot23Preview() : await api.mapPreview('estate-1', '8'));
        final c = _Canvas(t, SiteRouteEditorPage.canvasKey, geo);
        // 图里头挑的点：旋转图上取已知的室内点；不转图 x∈[-10,10]、y∈[-5,5]
        final want = rotated
            ? [(-3.29, 6.1), (-1.5, 8.4), (0.4, 7.7)]
            : [(-4.25, 2.5), (1.75, -1.25), (6.5, 3.0)];
        for (final (x, y) in want) {
          await _tapMap(t, c, x, y);
        }
        expect(find.byKey(SiteRouteEditorPage.pointKey(2)), findsOneWidget);
        expect(find.byKey(SiteRouteEditorPage.pointKey(3)), findsNothing);
        // 点是跟网页一样的定位箭头：中间写序号，字跟着图的转角转回正，选中的是最后加的那个
        PoseMarkerPainter mk(int i) => t
            .widget<CustomPaint>(
                find.descendant(of: find.byKey(SiteRouteEditorPage.pointKey(i)), matching: find.byType(CustomPaint)))
            .painter as PoseMarkerPainter;
        expect([for (var i = 0; i < 3; i++) mk(i).label], ['1', '2', '3']);
        expect(mk(0).labelAngle, closeTo(-geo.radians, 1e-9));
        expect([for (var i = 0; i < 3; i++) mk(i).selected], [false, false, true]);
        await _tapKey(t, SiteRouteEditorPage.saveKey);
        expect(api.savedRoutes, hasLength(1));
        final s = api.savedRoutes.single;
        expect(s['id'], 'night');
        expect(s['base_revision'], 0);
        expect(s['map_id'], 'estate-1');
        expect(s['map_version'], '8');
        final wp = (s['waypoints'] as List).cast<Map<String, dynamic>>();
        expect([for (final w in wp) w['name']], ['P1', 'P2', 'P3']);
        for (var i = 0; i < want.length; i++) {
          expect((wp[i]['x'] as double), closeTo(want[i].$1, 0.011), reason: 'x$i');
          expect((wp[i]['y'] as double), closeTo(want[i].$2, 0.011), reason: 'y$i');
        }
        // 新点的朝向 = 从上一个点走过来的方向
        expect(wp[1]['yaw'] as double,
            closeTo(math.atan2(want[1].$2 - want[0].$2, want[1].$1 - want[0].$1), 0.02));
        expect(find.textContaining('Route saved'), findsOneWidget);
      });
    }
  });

  testWidgets('挪点：先选中一个点，再点它的新位置；调序、删点、动作都进存下去的内容', (t) async {
    final api = RoutesApi('admin');
    final route = _route('loop', [[-3.29, 6.1], [-1.5, 8.4], [0.4, 7.7]]);
    await _open(t, api, route: route);
    final c = _Canvas(t, SiteRouteEditorPage.canvasKey, MapGeo.fromPreview(rot23Preview()));
    // 没改的不能存
    expect(await _enabled(t, SiteRouteEditorPage.saveKey, FilledButton), isFalse);
    // 挪第 1 个点到 (-2.0, 7.0)
    await t.tap(find.byKey(SiteRouteEditorPage.rowKey(0)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteRouteEditorPage.moveModeKey));
    await t.pumpAndSettle();
    await _tapMap(t, c, -2.0, 7.0);
    expect(find.byKey(SiteRouteEditorPage.pointKey(3)), findsNothing, reason: '挪点不加点');
    // 第 3 个上移一位、删掉原来的第 1 个（挪过的）：剩 P3、P2
    await t.tap(find.byKey(SiteRouteEditorPage.upKey(2)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteRouteEditorPage.removeKey(0)));
    await t.pumpAndSettle();
    // 选 P3(现在第 1 个)：停留 4 秒、前后拍照、开灯、检查文字、朝向 90°
    await t.tap(find.byKey(SiteRouteEditorPage.rowKey(0)));
    await t.pumpAndSettle();
    await _enter(t, SiteRouteEditorPage.dwellKey, '4');
    await _tapKey(t, SiteRouteEditorPage.photoFrontKey);
    await _tapKey(t, SiteRouteEditorPage.photoBackKey);
    await _tapKey(t, SiteRouteEditorPage.lightKey);
    await _enter(t, SiteRouteEditorPage.checkKey, 'padlock');
    await _enter(t, SiteRouteEditorPage.yawKey, '90');
    await t.pumpAndSettle();
    await _tapKey(t, SiteRouteEditorPage.saveKey);
    final s = api.savedRoutes.single;
    expect(s['base_revision'], 3);
    final wp = (s['waypoints'] as List).cast<Map<String, dynamic>>();
    expect([for (final w in wp) w['name']], ['P3', 'P2']);
    expect(wp[0]['actions'], [
      {'type': 'dwell', 'seconds': 4.0},
      {'type': 'photo', 'camera': 'front'},
      {'type': 'photo', 'camera': 'back'},
      {'type': 'light', 'on': true},
    ]);
    expect(wp[0]['check'], 'padlock');
    expect(wp[0]['yaw'] as double, closeTo(math.pi / 2, 0.001));
    expect(wp[1]['x'] as double, closeTo(-1.5, 1e-9), reason: '没碰的点原样');
    // 存好以后修订号 +1：再改再存带新的基准
    await t.tap(find.byKey(SiteRouteEditorPage.removeKey(1)));
    await t.pumpAndSettle();
    await _tapKey(t, SiteRouteEditorPage.saveKey);
    expect(api.savedRoutes.last['base_revision'], 4);
  });

  testWidgets('也能挪到被点中的第 1 个点之前先选的那个：挪点模式下点在别的点上 = 选中它，不是挪', (t) async {
    final api = RoutesApi('admin');
    await _open(t, api, route: _route('loop', [[-3.29, 6.1], [-1.5, 8.4]]));
    final c = _Canvas(t, SiteRouteEditorPage.canvasKey, MapGeo.fromPreview(rot23Preview()));
    await t.tap(find.byKey(SiteRouteEditorPage.moveModeKey));
    await t.pumpAndSettle();
    await _tapMap(t, c, -3.29, 6.1);                      // 点在第 1 个点上：选中
    await _tapMap(t, c, -1.5, 8.4);                       // 点在第 2 个点上：改选，不把第 1 个挪过去
    expect(await _enabled(t, SiteRouteEditorPage.saveKey, FilledButton), isFalse,
        reason: '没挪过，没改');
  });

  testWidgets('站点逐点校验没过：错的点标出来、原因列出来，存不下；改好再存', (t) async {
    final api = RoutesApi('admin')
      ..routeError = const SiteError(400, '2 problems', body: {
        'errors': [
          {'index': 1, 'name': 'P2', 'problem': 'P2 is not a drivable spot'},
          {'index': 2, 'name': 'P3', 'problem': 'P3 is 9.0 m from where the map was recorded'},
        ]
      });
    await _open(t, api, route: _route('loop', [[-3.29, 6.1], [-1.5, 8.4], [0.4, 7.7]]));
    final c = _Canvas(t, SiteRouteEditorPage.canvasKey, MapGeo.fromPreview(rot23Preview()));
    await _tapMap(t, c, 8.0, -10.0);                       // 加一个点才算改过
    await _tapKey(t, SiteRouteEditorPage.saveKey);
    expect(find.textContaining('P2 is not a drivable spot'), findsOneWidget);
    expect(find.textContaining('P3 is 9.0 m from where'), findsOneWidget);
    expect(find.textContaining('Not saved'), findsOneWidget);
    expect(api.savedRoutes, isEmpty);
    Color? dot(int i) => (t
            .widget<CustomPaint>(
                find.descendant(of: find.byKey(SiteRouteEditorPage.pointKey(i)), matching: find.byType(CustomPaint)))
            .painter as PoseMarkerPainter)
        .accent;
    expect(dot(1), isNot(dot(0)), reason: '有问题的点换了颜色');
    expect(dot(2), dot(1));
    api.routeError = null;
    await _tapKey(t, SiteRouteEditorPage.saveKey);
    expect(api.savedRoutes, hasLength(1));
    expect(find.textContaining('P2 is not a drivable spot'), findsNothing);
  });

  testWidgets('别人改过（409）：提示、给重新拉取；拉取后是站点上的样子', (t) async {
    final api = RoutesApi('admin')..routeError = const SiteError(409, 'Route loop has been changed by someone else');
    final two = _route('loop', [[-3.29, 6.1], [-1.5, 8.4]]);
    api.routesBody = {...api.routesBody, 'routes': [two]};
    await _open(t, api, route: two);
    final c = _Canvas(t, SiteRouteEditorPage.canvasKey, MapGeo.fromPreview(rot23Preview()));
    await _tapMap(t, c, 8.0, -10.0);
    expect(find.byKey(SiteRouteEditorPage.pointKey(2)), findsOneWidget);
    await _tapKey(t, SiteRouteEditorPage.saveKey);
    expect(find.textContaining('changed by someone else'), findsOneWidget);
    expect(find.byKey(SiteRouteEditorPage.reloadKey), findsOneWidget);
    api.routeError = null;
    await _tapKey(t, SiteRouteEditorPage.reloadKey);
    expect(find.byKey(SiteRouteEditorPage.pointKey(2)), findsNothing, reason: '回到站点上的两个点');
    expect(find.byKey(SiteRouteEditorPage.pointKey(1)), findsOneWidget);
  });

  testWidgets('删路线先确认，带当前修订号', (t) async {
    final api = RoutesApi('admin');
    await _open(t, api, route: _route('loop', [[-3.29, 6.1]]));
    await _reveal(t, SiteRouteEditorPage.deleteKey);
    await _tapKey(t, SiteRouteEditorPage.deleteKey);
    expect(api.calls.where((c) => c.startsWith('deleteRoute')), isEmpty);
    await _tapKey(t, SiteRouteEditorPage.deleteOkKey);
    expect(api.calls, contains('deleteRoute loop base=3'));
  });

  testWidgets('保安：能看、能派，不能改（没有加点 / 存 / 删）；派的是站点上存着的那一条', (t) async {
    final api = RoutesApi('guard');
    await _open(t, api, route: _route('loop', [[-3.29, 6.1], [-1.5, 8.4]]));
    expect(find.byKey(SiteRouteEditorPage.saveKey), findsNothing);
    expect(find.byKey(SiteRouteEditorPage.deleteKey), findsNothing);
    expect(find.byKey(SiteRouteEditorPage.addModeKey), findsNothing);
    final c = _Canvas(t, SiteRouteEditorPage.canvasKey, MapGeo.fromPreview(rot23Preview()));
    await _tapMap(t, c, 8.0, -10.0);
    expect(find.byKey(SiteRouteEditorPage.pointKey(2)), findsNothing, reason: '点图不加点');
    await _reveal(t, SiteRouteEditorPage.sendKey);
    await _tapKey(t, SiteRouteEditorPage.sendKey);
    await t.tap(find.byKey(const Key('route-send-A')));
    await t.pumpAndSettle();
    expect(api.calls, contains('patrol A loop'));
    expect(find.textContaining('Patrol sent to A'), findsOneWidget);
  });

  testWidgets('派之前有没存的修改：不让派', (t) async {
    final api = RoutesApi('admin');
    await _open(t, api, route: _route('loop', [[-3.29, 6.1], [-1.5, 8.4]]));
    final c = _Canvas(t, SiteRouteEditorPage.canvasKey, MapGeo.fromPreview(rot23Preview()));
    await _tapMap(t, c, 8.0, -10.0);
    await _reveal(t, SiteRouteEditorPage.sendKey);
    expect(await _enabled(t, SiteRouteEditorPage.sendKey, OutlinedButton), isFalse);
  });

  testWidgets('路线列表：按地图版本列，管理员有「新路线」，点一条进编辑页', (t) async {
    final api = RoutesApi('admin');
    await t.binding.setSurfaceSize(const Size(900, 700));
    addTearDown(() => t.binding.setSurfaceSize(null));
    await t.pumpWidget(MaterialApp(home: SiteRoutesPage(api: api)));
    await t.pumpAndSettle();
    expect(api.calls.any((c) => c.startsWith('routes estate-1:')), isTrue);
    expect(find.byKey(SiteRoutesPage.routeKey('loop')), findsOneWidget);
    expect(find.byKey(SiteRoutesPage.newKey), findsOneWidget);
    await t.tap(find.byKey(SiteRoutesPage.routeKey('loop')));
    await t.pumpAndSettle();
    expect(find.byType(SiteRouteEditorPage), findsOneWidget);
    expect(find.byKey(SiteRouteEditorPage.pointKey(2)), findsOneWidget);
  });

  testWidgets('路线列表：业主 / 保安没有「新路线」；没有路线时说没有', (t) async {
    final api = RoutesApi('guard')..routesBody = <String, dynamic>{'revision': 1, 'routes': <dynamic>[], 'schedules': <dynamic>[]};
    await t.pumpWidget(MaterialApp(home: SiteRoutesPage(api: api)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteRoutesPage.newKey), findsNothing);
    expect(find.byKey(SiteRoutesPage.emptyKey), findsOneWidget);
  });

  test('路线名的规矩跟站点一样', () {
    for (final ok in ['night', 'a.b-c_1', 'A' * 64]) {
      expect(routeNameRe.hasMatch(ok), isTrue, reason: ok);
    }
    for (final bad in ['', 'a b', '../x', '路线', 'x' * 65, '.hidden']) {
      expect(routeNameRe.hasMatch(bad), isFalse, reason: bad);
    }
    expect(nextPointName([RoutePt('P2', 0, 0, 0), RoutePt('P3', 0, 0, 0)]), 'P4');
  });

  // ------------------------------------------------------------------ 地图栏：去这里 / 设位置

  group('地图栏：点图选位置，发出去的就是点的坐标和朝向', () {
    Future<_Canvas> pumpTab(WidgetTester t, FakeApi api) async {
      await t.binding.setSurfaceSize(const Size(900, 800));
      addTearDown(() => t.binding.setSurfaceSize(null));
      await t.pumpWidget(MaterialApp(home: Scaffold(body: SiteMapTab(api: api))));
      await t.pumpAndSettle();
      return _Canvas(t, SiteMapTab.canvasKey, MapGeo.fromPreview(rot23Preview()));
    }

    testWidgets('保安点一处、拖把手朝正上方：去这里 = goto 那个坐标、90°', (t) async {
      final api = Rot23Api('guard');
      final c = await pumpTab(t, api);
      expect(find.byKey(PoseBar.barKey), findsOneWidget);
      await t.tapAt(c.global(-3.5, 4.25));
      await t.pumpAndSettle();
      final text = t.widget<Text>(find.byKey(PoseBar.textKey)).data!;
      expect(text, contains('x -3.5 m, y 4.25 m'));
      // 把手拖到图钉正上方（地图 +y 方向）
      final handle = t.getCenter(find.byKey(PoseLayer.handleKey));
      await t.timedDragFrom(handle, c.global(-3.5, 4.25 + 3) - handle, const Duration(milliseconds: 300));
      await t.pumpAndSettle();
      expect(t.widget<Text>(find.byKey(PoseBar.textKey)).data, contains('heading 90°'));
      // 图钉是跟网页一样的定位箭头，朝向跟着拖（这一层没转：地图 +y = 画布 -y）
      final pin = t.widget<CustomPaint>(find.byKey(PoseLayer.pinKey)).painter as PoseMarkerPainter;
      expect(pin.heading, closeTo(-math.pi / 2, 0.05));
      expect(pin.label, isNull);
      await t.tap(find.byKey(PoseBar.goKey));
      await t.pumpAndSettle();
      final g = api.gotos.single;
      expect(g['robot'], 'A');
      expect([g['map_id'], g['version']], ['estate-1', '8']);
      expect(g['x'] as double, closeTo(-3.5, 0.011));
      expect(g['y'] as double, closeTo(4.25, 0.011));
      expect(g['yaw'] as double, closeTo(math.pi / 2, 0.05));
      expect(find.textContaining('Go-to sent to A'), findsOneWidget);
    });

    testWidgets('站点拒了：原因原样显示', (t) async {
      final api = Rot23Api('admin')..routeError = const SiteError(409, 'A needs on-site supervision: turn it on in the app first');
      final c = await pumpTab(t, api);
      await t.tapAt(c.global(-3.5, 4.25));
      await t.pumpAndSettle();
      await t.tap(find.byKey(PoseBar.goKey));
      await t.pumpAndSettle();
      expect(find.textContaining('A needs on-site supervision: turn it on in the app first'), findsOneWidget);
    });

    testWidgets('±15° 按钮转朝向；设位置先弹确认，确认后 relocalize 的 x/y/yaw 就是选的', (t) async {
      final api = Rot23Api('admin');
      final c = await pumpTab(t, api);
      await t.tapAt(c.global(2.5, -3.75));
      await t.pumpAndSettle();
      await t.tap(find.byKey(PoseBar.turnLeftKey));
      await t.tap(find.byKey(PoseBar.turnLeftKey));
      await t.pumpAndSettle();
      expect(t.widget<Text>(find.byKey(PoseBar.textKey)).data, contains('heading 30°'));
      await t.tap(find.byKey(PoseBar.setKey));
      await t.pumpAndSettle();
      expect(find.descendant(of: find.byType(AlertDialog), matching: find.textContaining('x 2.5 m, y -3.75 m, heading 30°')), findsOneWidget,
          reason: '确认框写着坐标');
      expect(api.calls.where((x) => x.startsWith('relocalize')), isEmpty);
      await t.tap(find.text('Cancel'));
      await t.pumpAndSettle();
      expect(api.calls.where((x) => x.startsWith('relocalize')), isEmpty, reason: '取消不发');
      await t.tap(find.byKey(PoseBar.setKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(PoseBar.setOkKey));
      await t.pumpAndSettle();
      final call = api.calls.lastWhere((x) => x.startsWith('relocalize'));
      final p = call.split(' ');
      expect(p[1], 'A');
      expect(double.parse(p[2]), closeTo(2.5, 0.011));
      expect(double.parse(p[3]), closeTo(-3.75, 0.011));
      expect(double.parse(p[4]), closeTo(math.pi / 6, 0.001));
      expect(api.relocalizeMaps.last, 'estate-1/8', reason: '带上点的那张图，站点核对（外审 W39-I2）');
      expect(find.textContaining('Position set for A'), findsOneWidget);
    });

    for (final (ack, why) in [
      (<String, dynamic>{'result': 'rejected', 'reason': 'moving'}, 'rejected moving'),
      (<String, dynamic>{'result': 'expired'}, 'expired'),
      (<String, dynamic>{}, '没回执结果'),
    ]) {
      testWidgets('设位置机器人没收下（$why）：不说已设（外审 W39-I3）', (t) async {
        final api = Rot23Api('admin')..relocalizeAck = ack;
        final c = await pumpTab(t, api);
        await t.tapAt(c.global(2.5, -3.75));
        await t.pumpAndSettle();
        await t.tap(find.byKey(PoseBar.setKey));
        await t.pumpAndSettle();
        await t.tap(find.byKey(PoseBar.setOkKey));
        await t.pumpAndSettle();
        expect(api.calls.where((x) => x.startsWith('relocalize')), hasLength(1));
        expect(find.textContaining('Position set'), findsNothing);
        expect(find.byKey(PoseBar.messageKey), findsOneWidget);
        if (ack['reason'] != null) expect(find.textContaining('moving'), findsOneWidget);
      });
    }

    testWidgets('回包回来时已经换了图：不把结果说成新图的（外审 W39-I2）', (t) async {
      final api = Rot23Api('admin')..relocalizeGate = Completer<void>();
      final ctl = PosePickController()..pick(const Offset(1, 2));
      final robots = [
        <String, dynamic>{'robot_id': 'A', 'capabilities': {'tasks': {'goto': {}, 'relocalize': {}}}},
      ];
      Widget bar(String version) => MaterialApp(
          home: Scaffold(
              body: PoseBar(api: api, controller: ctl, mapId: 'estate-1', version: version, robots: robots)));
      await t.pumpWidget(bar('8'));
      await t.tap(find.byKey(PoseBar.setKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(PoseBar.setOkKey));
      await t.pump();
      expect(api.relocalizeMaps.single, 'estate-1/8');
      await t.pumpWidget(bar('9'));                          // 换了图
      api.relocalizeGate!.complete();
      await t.pumpAndSettle();
      expect(find.textContaining('Position set'), findsNothing);
      expect(find.byKey(PoseBar.messageKey), findsNothing);
    });

    testWidgets('确认框开着时图换了：不发', (t) async {
      final api = Rot23Api('admin');
      final ctl = PosePickController()..pick(const Offset(1, 2));
      final robots = [
        <String, dynamic>{'robot_id': 'A', 'capabilities': {'tasks': {'goto': {}, 'relocalize': {}}}},
      ];
      Widget bar(String version) => MaterialApp(
          home: Scaffold(
              body: PoseBar(api: api, controller: ctl, mapId: 'estate-1', version: version, robots: robots)));
      await t.pumpWidget(bar('8'));
      await t.tap(find.byKey(PoseBar.setKey));
      await t.pumpAndSettle();
      await t.pumpWidget(bar('9'));
      await t.pumpAndSettle();
      await t.tap(find.byKey(PoseBar.setOkKey));
      await t.pumpAndSettle();
      expect(api.calls.where((x) => x.startsWith('relocalize')), isEmpty);
      expect(find.textContaining('choose again'), findsOneWidget);
    });

    testWidgets('业主：没有这套工具，点图也不选位置', (t) async {
      final api = Rot23Api('owner');
      final c = await pumpTab(t, api);
      expect(find.byKey(PoseBar.barKey), findsNothing);
      await t.tapAt(c.global(-3.5, 4.25));
      await t.pumpAndSettle();
      expect(find.byKey(PoseLayer.pinKey), findsNothing);
    });
  });
}
