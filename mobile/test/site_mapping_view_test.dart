// 建图时看得见（W00c6h）：录包时手机每 2 秒取一次狗走过的轨迹画出来（哪儿走过了）；建好的图在地图页点开
// 看（能缩放），画上这张图上登记的待命点（建出来的图对不对）。
import 'dart:typed_data';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';
import 'support/zone_ui.dart';

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
    expect(find.textContaining('未在录包'), findsOneWidget);
    await t.pump(const Duration(seconds: 10));
    expect(api.trailSince, [0, 2, 3], reason: '停了不再取');
    await t.tap(find.byKey(SiteMappingTrailPage.refreshKey));
    await t.pump();
    expect(api.trailSince.length, 4, reason: '点刷新再看一次');
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('机器人上重新开录了（点数变少）：从头取', (t) async {
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

  testWidgets('开录还在起（starting）：接着问；起来了换一趟（epoch 变了）从头画', (t) async {
    final api = FakeApi('admin', robotView: _view())
      ..trailReplies = [
        {..._trail([[5, 5]], 0, 1, rec: false), 'starting': true, 'epoch': 3}, // 上一趟留下的
        {..._trail([[0, 0]], 1, 1), 'epoch': 4}, // 新的一趟碰巧也是 1 个点
        {..._trail([[0, 0]], 0, 1), 'epoch': 4},
      ];
    await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
    await t.pump();
    expect(find.textContaining('正在启动'), findsOneWidget);
    await t.pump(const Duration(seconds: 2));
    await t.pump();
    expect(api.trailSince, [0, 1, 0], reason: '换了一趟：不把新的一趟接在旧的后面');
    expect(t.widget<CustomPaint>(find.byKey(SiteMappingTrailPage.canvasKey)).painter,
        isA<TrailPainter>().having((p) => p.points, 'points', [Offset.zero]));
    expect(find.textContaining('录包中'), findsOneWidget);
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('切到后台就不问，回来接着问', (t) async {
    final api = FakeApi('admin', robotView: _view())
      ..trailReplies = List.generate(30, (i) => _trail([], i, 0));
    await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
    await t.pump();
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    final n = api.trailSince.length;
    await t.pump(const Duration(seconds: 10));
    expect(api.trailSince.length, n);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await t.pump(const Duration(seconds: 2));
    expect(api.trailSince.length, greaterThan(n));
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('点满了、里程跳过：说清楚', (t) async {
    final api = FakeApi('admin', robotView: _view())
      ..trailReplies = [{..._trail([[0, 0], [1, 0]], 0, 2), 'full': true, 'jumps': 2}];
    await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
    await t.pump();
    expect(find.textContaining('红点为最后记录点'), findsOneWidget);
    expect(find.textContaining('跳变 2 次'), findsOneWidget);
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

  testWidgets('单机器人页：管理员有「录包轨迹」；开始录包成功就打开轨迹页', (t) async {
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
    await openZonePanel(t);
    expect(find.textContaining('A · dock'), findsOneWidget);
    expect(find.textContaining('默认'), findsOneWidget);
    final painter = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.overlayKey)).painter!
        as StandbyPainter;
    // 左边缘 x = -10、上边缘 y = 5、一像素 0.1 m:(1.5, 2.0) 在 (115, 30)。
    expect(painter.pixels.single, const Offset(115, 30));
    expect(find.byType(InteractiveViewer), findsOneWidget, reason: '能缩放');
  });

  testWidgets('预览说用的是哪份 yaml；原点带转角有警告', (t) async {
    final api = FakeApi('guard')..previewWarning = '地图 origin 带转角 0.300 rad,预览没按转角转';
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.textContaining('yard.yaml'), findsOneWidget);
    expect(find.textContaining('转角'), findsOneWidget);
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
    expect(m(const Offset(10, 0)).dx, greaterThan(m(Offset.zero).dx), reason: 'x 朝右（不左右镜像）');
  });

  testWidgets('区域（W10）：谁都能看，画在图上；保安没有画、删、确认的按钮', (t) async {
    final api = FakeApi('guard')
      ..zonesView = <String, dynamic>{
        'revision': 2,
        'zones': <dynamic>[
          {'id': 'pond', 'kind': 'nogo', 'label': '池子',
            'polygon': [[0, 0], [1, 0], [1, 1]]},
          {'id': 'lawn', 'kind': 'slow', 'label': '草坪', 'max_speed_mps': 0.3,
            'polygon': [[2, 0], [3, 0], [3, 1]]},
        ],
        'confirmed': {'revision': 2, 'by': 'alice', 'at_ms': 1, 'current': true},
        'confirm_text': 'x'};
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.textContaining('第 2 版'), findsWidgets);
    expect(find.textContaining('已确认（alice）'), findsOneWidget);
    expect(find.textContaining('禁行 · 池子'), findsOneWidget);
    expect(find.textContaining('限速 0.3 m/s · 草坪'), findsOneWidget);
    final p = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.zonesKey)).painter!
        as ZonesPainter;
    expect(p.shapes.map((s) => s.nogo), [true, false]);
    // 左边缘 x = -10、上边缘 y = 5、一像素 0.1 m:(0, 0) 在 (100, 50)。
    expect(p.shapes.first.pixels.first, const Offset(100, 50));
    expect(find.byKey(SiteMapPreviewPage.drawNogoKey), findsNothing);
    await openZonePanel(t);
    expect(find.byKey(SiteMapPreviewPage.confirmKey), findsNothing);
    await openZonePanel(t);
    expect(find.byKey(SiteMapPreviewPage.deleteKey('pond')), findsNothing);
  });

  testWidgets('管理员画禁行区：点三下 → 完成 → 整份存（带看到的修订号）→ 确认这一版', (t) async {
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.textContaining('未确认：此地图无法下发'), findsOneWidget);
    await tapZoneMenu(t, SiteMapPreviewPage.drawNogoKey);
    await t.pump();
    final rect = t.getRect(find.byKey(SiteMapPreviewPage.canvasKey));
    final k = rect.width / 200;                      // 图 200 像素宽,画到屏上缩放了
    for (final o in const [Offset(20, 10), Offset(60, 10), Offset(60, 40)]) {
      await t.tapAt(rect.topLeft + o * k);
      await t.pump();
    }
    expect(find.textContaining('已 3 个'), findsOneWidget);
    await t.tap(find.byKey(SiteMapPreviewPage.undoKey));
    await t.pump();
    expect(find.textContaining('已 2 个'), findsOneWidget);
    expect(t.widget<FilledButton>(find.byKey(SiteMapPreviewPage.finishKey)).onPressed, isNull,
        reason: '不到三个点不能完成');
    await t.tapAt(rect.topLeft + const Offset(60, 40) * k);
    await t.pump();
    await t.tap(find.byKey(SiteMapPreviewPage.finishKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('saveZones estate-1:8 base=0'));
    final z = api.savedZones.single.single;
    expect(z['kind'], 'nogo');
    final poly = (z['polygon'] as List).cast<List>();
    expect(poly.length, 3);
    // 像素 (20, 10) → 地图 (-10 + 2, 5 - 1) = (-8, 4)
    expect((poly[0][0] as num).toDouble(), closeTo(-8.0, 0.02));
    expect((poly[0][1] as num).toDouble(), closeTo(4.0, 0.02));
    expect(find.textContaining('已保存'), findsOneWidget);
    await openZonePanel(t);
    await t.ensureVisible(find.byKey(SiteMapPreviewPage.confirmKey)); // 右栏多了「抹掉」那段说明,确认钮往下挪了
    await openZonePanel(t);
    await t.tap(find.byKey(SiteMapPreviewPage.confirmKey));
    await t.pumpAndSettle();
    expect(find.textContaining('确认之后这张图才能下发'), findsOneWidget, reason: '弹框念一遍那句话');
    await t.tap(find.text('返回检查'));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('confirmZones')), isEmpty);
    await openZonePanel(t);
    await t.tap(find.byKey(SiteMapPreviewPage.confirmKey));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteMapPreviewPage.confirmOkKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('confirmZones estate-1:8 rev=1'));
    expect(find.textContaining('已确认'), findsWidgets);
  });

  testWidgets('虚拟墙（2026-10-15）：两个点就能画完；只送墙线、标硬；图上画成线、列表写虚拟墙', (t) async {
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await tapZoneMenu(t, SiteMapPreviewPage.drawWallKey);
    await t.pump();
    final rect = t.getRect(find.byKey(SiteMapPreviewPage.canvasKey));
    final k = rect.width / 200;
    await t.tapAt(rect.topLeft + const Offset(20, 10) * k);
    await t.pump();
    expect(find.textContaining('虚拟墙：沿着墙点，已 1 个'), findsOneWidget);
    expect(t.widget<FilledButton>(find.byKey(SiteMapPreviewPage.finishKey)).onPressed, isNull,
        reason: '一个点画不成墙');
    await t.tapAt(rect.topLeft + const Offset(60, 10) * k);
    await t.pump();
    final draw = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.zonesKey)).painter! as ZonesPainter;
    expect(draw.draftOpen, isTrue, reason: '画墙是一条线，不闭合');
    await t.ensureVisible(find.byKey(SiteMapPreviewPage.hardKey));
    await t.tap(find.byKey(SiteMapPreviewPage.hardKey));
    await t.pump();
    await t.ensureVisible(find.byKey(SiteMapPreviewPage.finishKey));
    await t.tap(find.byKey(SiteMapPreviewPage.finishKey));
    await t.pumpAndSettle();
    final z = api.savedZones.single.single;
    expect(z['kind'], 'nogo');
    expect(z['hard'], isTrue);
    expect(z.containsKey('polygon'), isFalse, reason: '多边形站点按线算');
    final wall = (z['wall'] as List).cast<List>();
    expect(wall.length, 2);
    // 像素 (20, 10) → 地图 (-8, 4)；(60, 10) → (-4, 4)
    expect((wall[0][0] as num).toDouble(), closeTo(-8.0, 0.02));
    expect((wall[1][0] as num).toDouble(), closeTo(-4.0, 0.02));
    expect((wall[1][1] as num).toDouble(), closeTo(4.0, 0.02));
  });

  testWidgets('站点给的虚拟墙：画成墙线（不画带子）、列表写硬虚拟墙；带过来的墙也画成线', (t) async {
    final api = FakeApi('guard')
      ..zonesView = <String, dynamic>{
        'revision': 3,
        'zones': <dynamic>[
          {'id': 'w1', 'kind': 'nogo', 'label': '围栏', 'hard': true, 'wall': [[0, 0], [2, 0], [2, 1]],
            'polygon': [[-0.15, 0.15], [2.15, 0.15], [2.15, 1.15], [1.85, 1.15], [1.85, -0.15], [-0.15, -0.15]]},
        ],
        'confirmed': {'revision': 3, 'by': 'alice', 'at_ms': 1, 'current': true},
        'confirm_text': 'x'};
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.textContaining('硬虚拟墙 · 围栏'), findsOneWidget);
    final p = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.zonesKey)).painter! as ZonesPainter;
    final s = p.shapes.single;
    expect(s.hard, isTrue);
    expect(s.wall, isNotNull);
    // 左边缘 x = -10、上边缘 y = 5、一像素 0.1 m：(0, 0) 在 (100, 50)，(2, 0) 在 (120, 50)
    expect(s.wall!.first, const Offset(100, 50));
    expect(s.wall![1], const Offset(120, 50));
  });

  testWidgets('管理员画限速区带速度；删一个；别人先改了说清楚', (t) async {
    final api = FakeApi('admin')
      ..zonesView = <String, dynamic>{
        'revision': 1,
        'zones': <dynamic>[
          {'id': 'pond', 'kind': 'nogo', 'label': '池子', 'polygon': [[0, 0], [1, 0], [1, 1]]},
        ],
        'confirmed': null, 'confirm_text': 'x'};
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await tapZoneMenu(t, SiteMapPreviewPage.drawSlowKey);
    await t.pump();
    final rect = t.getRect(find.byKey(SiteMapPreviewPage.canvasKey));
    final k = rect.width / 200;
    for (final o in const [Offset(120, 10), Offset(160, 10), Offset(160, 40)]) {
      await t.tapAt(rect.topLeft + o * k);
      await t.pump();
    }
    await t.tap(find.byKey(SiteMapPreviewPage.finishKey));
    await t.pumpAndSettle();
    final saved = api.savedZones.single;
    expect(saved.map((z) => z['id']), ['pond', 'z2']);
    expect(saved.last['kind'], 'slow');
    expect(saved.last['max_speed_mps'], 0.3);
    await openZonePanel(t);
    await t.tap(find.byKey(SiteMapPreviewPage.deleteKey('pond')));
    await t.pumpAndSettle();
    expect(api.savedZones.last.map((z) => z['id']), ['z2']);
    api.zonesConflict = true;
    await openZonePanel(t);
    await t.tap(find.byKey(SiteMapPreviewPage.deleteKey('z2')));
    await t.pumpAndSettle();
    expect(find.textContaining('保存失败'), findsWidgets);
    expect(find.textContaining('重新拉一下'), findsWidgets);
    await tapZoneMenu(t, SiteMapPreviewPage.drawNogoKey);
    await t.pump();
    await t.tap(find.byKey(SiteMapPreviewPage.cancelKey));
    await t.pump();
    expect(find.byKey(SiteMapPreviewPage.menuKey), findsOneWidget, reason: '不画了回到菜单按钮');
  });

  // ------------------------------------------------------------ 决策 65、66，R2（2026-10-14）

  Future<void> drawTriangle(WidgetTester t) async {
    final rect = t.getRect(find.byKey(SiteMapPreviewPage.canvasKey));
    final k = rect.width / 200;
    for (final o in const [Offset(20, 10), Offset(60, 10), Offset(60, 40)]) {
      await t.tapAt(rect.topLeft + o * k);
      await t.pump();
    }
  }

  /// 说明栏能滚：先滚到看得见再点。不在眼前的先开说明栏找，还没有就是菜单里的。
  Future<void> tapVis(WidgetTester t, Finder f) async {
    if (f.evaluate().isEmpty) await openZonePanel(t);
    if (f.evaluate().isEmpty) {
      await closeZonePanel(t);
      await t.tap(find.byKey(SiteMapPreviewPage.menuKey));
      await t.pump();
    }
    await t.ensureVisible(f);
    await t.pumpAndSettle();
    await t.tap(f);
    await t.pumpAndSettle();
  }

  testWidgets('画硬禁区、临时的：存的时候带 hard 和 for_s；列表里看得出来；只收紧的说不用重新确认', (t) async {
    final api = FakeApi('admin')..autoConfirm = true;
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await tapZoneMenu(t, SiteMapPreviewPage.drawNogoKey);
    await t.pump();
    expect(find.byKey(SiteMapPreviewPage.hardKey), findsOneWidget, reason: '画禁行区时能标硬禁区');
    await tapVis(t, find.byKey(SiteMapPreviewPage.hardKey));
    await tapVis(t, find.byKey(SiteMapPreviewPage.untilKey));
    await t.tap(find.byKey(SiteMapPreviewPage.forKey(3600)).last);
    await t.pumpAndSettle();
    await drawTriangle(t);
    await tapVis(t, find.byKey(SiteMapPreviewPage.finishKey));
    final z = api.savedZones.single.single;
    expect(z['kind'], 'nogo');
    expect(z['hard'], isTrue);
    expect(z['for_s'], 3600);
    expect(find.textContaining('无需重新确认'), findsOneWidget, reason: '只收紧：站点当场算确认了');
    // 站点那一份里带了 until_ms、hard:列表、图上都看得出来
    api.zonesView = <String, dynamic>{...api.zonesView, 'zones': <dynamic>[
      {'id': 'z1', 'kind': 'nogo', 'label': '禁行区', 'hard': true,
        'until_ms': DateTime(2026, 10, 14, 18, 30).millisecondsSinceEpoch,
        'polygon': [[0, 0], [1, 0], [1, 1]]},
    ]};
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(key: UniqueKey(), api: api,
        mapId: 'estate-1', version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.textContaining('硬禁区'), findsWidgets);
    expect(find.textContaining('到 10-14 18:30'), findsOneWidget);
    final p = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.zonesKey)).painter!
        as ZonesPainter;
    expect(p.shapes.single.hard, isTrue);
    // 限速区没有「硬禁区」那个勾
    await tapVis(t, find.byKey(SiteMapPreviewPage.drawSlowKey));
    expect(find.byKey(SiteMapPreviewPage.hardKey), findsNothing);
  });

  testWidgets('放宽（删一个）存了说要重新确认', (t) async {
    final api = FakeApi('admin')
      ..zonesView = <String, dynamic>{
        'revision': 1,
        'zones': <dynamic>[
          {'id': 'pond', 'kind': 'nogo', 'label': '池子', 'polygon': [[0, 0], [1, 0], [1, 1]]},
        ],
        'confirmed': {'revision': 1, 'by': 'a', 'at_ms': 1, 'current': true}, 'confirm_text': 'x'};
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await tapVis(t, find.byKey(SiteMapPreviewPage.deleteKey('pond')));
    expect(find.textContaining('有改动，需重新确认'), findsOneWidget);
  });

  Map<String, dynamic> proposalView({bool? same = true}) => <String, dynamic>{
        'revision': 0, 'zones': <dynamic>[], 'confirmed': null, 'confirm_text': 'x',
        'previous': {'version': '7', 'revision': 3, 'zones': 2},
        'proposal': {
          'from_version': '7', 'from_revision': 3,
          'frame': {'same': same, 'note': 'n'},
          'zones': <dynamic>[
            {'id': 'pond', 'kind': 'nogo', 'label': '池子', 'hard': true,
              'polygon': [[0, 0], [1, 0], [1, 1]]},
            {'id': 'lawn', 'kind': 'slow', 'label': '草坪', 'max_speed_mps': 0.3,
              'polygon': [[2, 0], [3, 0], [3, 1]]},
          ],
          'standby': <dynamic>[
            {'robot_id': 'A', 'name': 'dock', 'x': 1.0, 'y': 2.0, 'yaw': 0.0, 'default': true},
          ],
          'intercepts': <dynamic>[{'name': 'gate', 'x': 3.0, 'y': 4.0, 'yaw': 0.0}],
        },
      };

  testWidgets('新版本：上一版带过来的给人核，勾上的才在确认时采纳；没勾的不要', (t) async {
    final api = FakeApi('admin')..zonesView = proposalView();
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.byKey(SiteMapPreviewPage.proposalKey), findsOneWidget);
    expect(find.textContaining('从第 7 版带入'), findsOneWidget);
    expect(find.textContaining('坐标可能有变'), findsOneWidget);
    final p = t.widget<CustomPaint>(find.byKey(SiteMapPreviewPage.zonesKey)).painter!
        as ZonesPainter;
    expect(p.shapes.where((s) => s.proposed).length, 2, reason: '带过来的画成虚的、跟生效的分开');
    await tapVis(t, find.byKey(SiteMapPreviewPage.proposedKey('zone', 'pond')));
    await tapVis(t, find.byKey(SiteMapPreviewPage.proposedKey('standby', 'A/dock')));
    await tapVis(t, find.byKey(SiteMapPreviewPage.confirmKey));
    expect(find.textContaining('采纳勾上的 2 个'), findsOneWidget, reason: '确认框说清楚采纳几个');
    await t.tap(find.byKey(SiteMapPreviewPage.confirmOkKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('confirmZones estate-1:8 rev=0'));
    expect(api.lastAdopt, <String, dynamic>{
      'zones': ['pond'], 'standby': [['A', 'dock']], 'intercepts': <String>[]});
    expect(api.lastEmptyOk, isFalse, reason: '采纳了一个区域:不是空的');
  });

  testWidgets('新版本：坐标系变了明说；丢掉带过来的', (t) async {
    final api = FakeApi('admin')..zonesView = proposalView(same: false);
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.textContaining('坐标系已变化'), findsOneWidget);
    await tapVis(t, find.byKey(SiteMapPreviewPage.discardProposalKey));
    expect(api.calls, contains('discardZoneProposal estate-1:8'));
    await openZonePanel(t);
    expect(find.byKey(SiteMapPreviewPage.proposalKey), findsNothing);
  });

  testWidgets('保安看得到带过来的，勾不了、丢不了', (t) async {
    final api = FakeApi('guard')..zonesView = proposalView();
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await openZonePanel(t);
    expect(find.byKey(SiteMapPreviewPage.proposalKey), findsOneWidget);
    await openZonePanel(t);
    expect(t.widget<CheckboxListTile>(find.byKey(SiteMapPreviewPage.proposedKey('zone', 'pond')))
        .onChanged, isNull);
    await openZonePanel(t);
    expect(find.byKey(SiteMapPreviewPage.discardProposalKey), findsNothing);
  });

  testWidgets('确认一个区域都没有的：多弹一道提示，明说了才带 empty_ok', (t) async {
    final api = FakeApi('admin')
      ..zonesView = <String, dynamic>{
        'revision': 0, 'zones': <dynamic>[], 'confirmed': null, 'confirm_text': 'x',
        'previous': {'version': '7', 'revision': 3, 'zones': 2}};
    await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
        version: '8')));
    await t.pumpAndSettle();
    await tapVis(t, find.byKey(SiteMapPreviewPage.confirmKey));
    await t.tap(find.byKey(SiteMapPreviewPage.confirmOkKey));
    await t.pumpAndSettle();
    expect(find.textContaining('没有任何禁行区和限速区'), findsOneWidget);
    expect(find.textContaining('第 7 版有 2 个'), findsOneWidget);
    await t.tap(find.text('返回检查'));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('confirmZones')), isEmpty, reason: '没明说就不确认');
    await tapVis(t, find.byKey(SiteMapPreviewPage.confirmKey));
    await t.tap(find.byKey(SiteMapPreviewPage.confirmOkKey));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteMapPreviewPage.confirmEmptyKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('confirmZones estate-1:8 rev=0'));
    expect(api.lastEmptyOk, isTrue);
  });

  for (final lang in AppLang.values) {
    testWidgets('竖屏手机（390×844，$lang）：带过来的建议、硬禁区、到期下拉都不溢出', (t) async {
      appLang.value = lang;
      addTearDown(() => appLang.value = AppLang.zh);
      t.view.physicalSize = const Size(390, 844);
      t.view.devicePixelRatio = 1.0;
      addTearDown(t.view.reset);
      final api = FakeApi('admin')..zonesView = proposalView(same: null);
      await t.pumpWidget(MaterialApp(home: SiteMapPreviewPage(api: api, mapId: 'estate-1',
          version: '8')));
      await t.pumpAndSettle();
      expect(t.takeException(), isNull);
      await tapVis(t, find.byKey(SiteMapPreviewPage.drawNogoKey));
      expect(find.byKey(SiteMapPreviewPage.untilKey), findsOneWidget);
      expect(t.takeException(), isNull);
    });
  }

  test('解码用的 PNG 是真的（Image.memory 认得）', () {
    expect(fakePreviewPng.sublist(0, 8), Uint8List.fromList([137, 80, 78, 71, 13, 10, 26, 10]));
  });
}
