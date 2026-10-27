// 2026-10-14 重做的第四批：地图栏（当前这张图 + 图上的狗）、记录栏的数值格和巡检卡、管理栏（站点状态、
// 版本格子、诊断包、几个入口）。各页原来的行为测试还在各自的文件里；这里只测这一批新加的东西。
import 'dart:typed_data';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_shell.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:d1max_patrol/ui/site_cameras.dart';
import 'package:d1max_patrol/ui/site_health.dart';
import 'package:d1max_patrol/ui/site_intercepts.dart';
import 'package:d1max_patrol/ui/site_logs.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart' show SiteIncidentsPage, SiteRobotPage, SiteSchedulePage;
import 'package:d1max_patrol/ui/site_releases.dart';
import 'package:d1max_patrol/ui/site_runs.dart';
import 'package:d1max_patrol/ui/site_weather.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi, onePixelPng;
import 'support/fake_site.dart';

void _size(WidgetTester t, [Size size = const Size(390, 844)]) {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

/// 能换掉地图目录、巡检记录的假站点。
class _Api extends FakeApi {
  _Api(super.role);
  Map<String, dynamic>? mapsBody;
  SiteError? mapsError;
  List<Map<String, dynamic>>? runRows;

  @override
  Future<Map<String, dynamic>> maps() async {
    if (mapsError != null) throw mapsError!;
    return mapsBody ?? await super.maps();
  }

  @override
  Future<List<Map<String, dynamic>>> runs({String? robotId, String? mission}) async =>
      runRows ?? await super.runs(robotId: robotId, mission: mission);
}

/// 夹具里的那只狗，改成报着 [version] 这一版图、站在 ([x], [y])。
List<Map<String, dynamic>> _robotsOn(String version, {double x = 1.0, double y = 2.0, bool online = true}) => [
      for (final r in (siteFixture('site_robots')['robots'] as List).cast<Map<String, dynamic>>())
        <String, dynamic>{
          ...r,
          'loc': <String, dynamic>{
            ...(r['loc'] as Map).cast<String, dynamic>(),
            'pose': <String, dynamic>{'x': x, 'y': y, 'yaw': 0.0},
          },
          'status': <String, dynamic>{...(r['status'] as Map).cast<String, dynamic>(), 'online': online},
          'capabilities': <String, dynamic>{
            ...(r['capabilities'] as Map).cast<String, dynamic>(),
            'tasks': <String, dynamic>{
              ...((r['capabilities'] as Map)['tasks'] as Map).cast<String, dynamic>(),
              'patrol': <String, dynamic>{'autonomy': 'autonomous', 'map_id': 'estate-1', 'map_version': version},
            },
          },
        },
    ];

/// 当地时间 → 站点目录名那种 UTC 时刻（`20260925T010000Z`）。
String _stamp(DateTime local) {
  final u = local.toUtc();
  String two(int n) => n.toString().padLeft(2, '0');
  return '${u.year}${two(u.month)}${two(u.day)}T${two(u.hour)}${two(u.minute)}${two(u.second)}Z';
}

Map<String, dynamic> _run(int id, DateTime when, {int abnormal = 0, int normal = 1}) => <String, dynamic>{
      'id': id, 'mission': 'loop-$id', 'robot_id': 'A', 'stamp': _stamp(when), 'photos': abnormal + normal,
      'finished': true, 'judged_ms': 1, 'reviewed': 0,
      'verdicts': <String, dynamic>{if (abnormal > 0) 'abnormal': abnormal, if (normal > 0) 'normal': normal},
    };

Future<void> _pump(WidgetTester t, Widget page, {ThemeData? theme}) async {
  await t.pumpWidget(MaterialApp(theme: theme, home: Scaffold(body: page)));
  await t.pumpAndSettle();
}

void main() {
  group('地图栏', () {
    testWidgets('机器人报着的那张图当主体：画布、图上的机器人、图例、底下这张图上的机器人；右上进地图列表', (t) async {
      _size(t);
      final api = _Api('guard')..robotsOverride = _robotsOn('8');
      await _pump(t, SiteMapTab(api: api));
      expect(api.calls, containsAll(['mapPreview estate-1:8', 'mapPreviewPng estate-1:8']));
      expect(find.text('estate-1:8'), findsOneWidget);
      expect(find.byKey(SiteMapTab.canvasKey), findsOneWidget);
      expect(find.byType(InteractiveViewer), findsOneWidget, reason: '能缩放');
      // 待命点、区域还是原来那几个 painter 画的
      final standby = t.widget<CustomPaint>(find.byKey(SiteMapTab.standbyKey)).painter! as StandbyPainter;
      expect(standby.pixels.length, 1);
      expect(t.widget<CustomPaint>(find.byKey(SiteMapTab.zonesKey)).painter, isA<ZonesPainter>());
      // 狗：(1, 2) m；左边缘 x = -10、上边缘 y = 5、一像素 0.1 m → 图上 (110, 30)，按画出来的大小缩放
      final canvas = t.getRect(find.byKey(SiteMapTab.canvasKey));
      final k = canvas.width / 200;
      final marker = t.getRect(find.byKey(SiteMapTab.markerKey('A')));
      expect((marker.center - canvas.topLeft - const Offset(110, 30) * k).distance, lessThan(0.5));
      expect(find.byKey(SiteMapTab.legendKey), findsOneWidget);
      expect(find.descendant(of: find.byKey(SiteMapTab.legendKey), matching: find.text('待命点')), findsOneWidget);
      expect(find.descendant(of: find.byKey(SiteMapTab.legendKey), matching: find.text('禁行区')), findsNothing,
          reason: '这张图上没画的不列');
      expect(find.byKey(SiteMapTab.robotKey('A')), findsOneWidget);
      expect(find.descendant(of: find.byKey(SiteMapTab.robotKey('A')), matching: find.text('在线')), findsOneWidget);
      await t.tap(find.byKey(SiteMapTab.listKey));
      await t.pumpAndSettle();
      expect(find.byType(SiteMapsPage), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('横屏 800×360：画布高过 150；图例、缩放按钮都在画布那一块里', (t) async {
      _size(t, const Size(800, 360));
      final api = _Api('admin')..robotsOverride = _robotsOn('8');
      await t.pumpWidget(MaterialApp(home: NwShell(api: api, ring: () {})));
      await t.pumpAndSettle();
      await t.tap(find.byKey(NwShell.mapKey));
      await t.pumpAndSettle();
      expect(t.takeException(), isNull);
      expect(t.getRect(find.byKey(SiteMapTab.canvasKey)).height, greaterThan(150), reason: '图要占住大半屏');
      final nav = t.getRect(find.byType(NwBottomNav));
      for (final key in [SiteMapTab.legendKey, SiteMapTab.zoomInKey, SiteMapTab.zoomOutKey, SiteMapTab.recenterKey]) {
        final r = t.getRect(find.byKey(key));
        expect(r.bottom, lessThanOrEqualTo(nav.top), reason: '$key');
        expect(r.top, greaterThanOrEqualTo(0), reason: '$key');
      }
      await t.pumpWidget(Container());
    });

    testWidgets('缩放、回正：按钮动的是画布的变换', (t) async {
      _size(t);
      final api = _Api('guard')..robotsOverride = _robotsOn('8');
      await _pump(t, SiteMapTab(api: api));
      double scale() => t
          .widget<InteractiveViewer>(find.byType(InteractiveViewer))
          .transformationController!
          .value
          .getMaxScaleOnAxis();
      expect(scale(), 1.0);
      await t.tap(find.byKey(SiteMapTab.zoomInKey));
      await t.pump();
      expect(scale(), greaterThan(1.2));
      await t.tap(find.byKey(SiteMapTab.zoomOutKey));
      await t.pump();
      expect(scale(), closeTo(1.0, 1e-6));
      await t.tap(find.byKey(SiteMapTab.zoomOutKey));
      await t.pump();
      expect(scale(), closeTo(1.0, 1e-6), reason: '不缩到比整张图还小');
      await t.tap(find.byKey(SiteMapTab.zoomInKey));
      await t.tap(find.byKey(SiteMapTab.zoomInKey));
      await t.pump();
      await t.tap(find.byKey(SiteMapTab.recenterKey));
      await t.pump();
      expect(scale(), 1.0);
      await t.pumpWidget(Container());
    });

    testWidgets('机器人报的图站点上没有：摆站点上最新的那张，说清楚没有机器人在这张图上，不把机器人画上去', (t) async {
      _size(t);
      final api = _Api('guard'); // 夹具里的狗报的是 estate-1:7，站点上只有 estate-1:8
      await _pump(t, SiteMapTab(api: api));
      expect(find.text('estate-1:8'), findsOneWidget);
      expect(find.byKey(SiteMapTab.canvasKey), findsOneWidget);
      expect(find.byKey(SiteMapTab.markerKey('A')), findsNothing, reason: '坐标系不是这张图的');
      expect(find.textContaining('没有机器人在这张图上'), findsOneWidget);
      expect(find.descendant(of: find.byKey(SiteMapTab.robotKey('A')), matching: find.textContaining('estate-1:7')),
          findsOneWidget, reason: '它在哪张图上照实写');
      await t.pumpWidget(Container());
    });

    testWidgets('站点上一张图都没有：空状态说清楚，给去地图列表的路；不是一片空白', (t) async {
      _size(t);
      final api = _Api('admin')..mapsBody = <String, dynamic>{'maps': <dynamic>[], 'bags': <dynamic>[]};
      await _pump(t, SiteMapTab(api: api));
      expect(find.byKey(SiteMapTab.emptyKey), findsOneWidget);
      expect(find.text('站点上还没有图'), findsOneWidget);
      expect(find.byKey(SiteMapTab.canvasKey), findsNothing);
      expect(api.calls.where((c) => c.startsWith('mapPreview')), isEmpty);
      expect(find.byKey(SiteMapTab.listKey), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('地图目录拿不到、这张图没有栅格：照实说，不画成「还没有图」；刷新再问一次', (t) async {
      _size(t);
      final api = _Api('guard')..mapsError = const SiteError(502, '站点忙');
      await _pump(t, SiteMapTab(api: api));
      expect(find.textContaining('地图目录拿不到'), findsOneWidget);
      expect(find.byKey(SiteMapTab.emptyKey), findsNothing);
      expect(find.text('站点上还没有图'), findsNothing);
      api
        ..mapsError = null
        ..previewMissing = true;
      await t.tap(find.byKey(SiteMapTab.refreshKey));
      await t.pumpAndSettle();
      expect(find.textContaining('地图目录拿不到'), findsNothing);
      expect(find.textContaining('看不了'), findsOneWidget);
      expect(find.textContaining('没有栅格'), findsOneWidget);
      expect(find.byKey(SiteMapTab.canvasKey), findsNothing);
      expect(find.byKey(SiteMapTab.robotKey('A')), findsOneWidget, reason: '机器人的列表照样看');
      api.previewMissing = false;
      await t.tap(find.byKey(SiteMapTab.refreshKey));
      await t.pumpAndSettle();
      expect(find.byKey(SiteMapTab.canvasKey), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('底下的机器人点了进单机器人页', (t) async {
      _size(t);
      final api = _Api('guard')..robotsOverride = _robotsOn('8', online: false);
      await _pump(t, SiteMapTab(api: api));
      expect(find.descendant(of: find.byKey(SiteMapTab.robotKey('A')), matching: find.text('离线')), findsOneWidget);
      await t.tap(find.byKey(SiteMapTab.robotKey('A')));
      await t.pump();
      await t.pump(const Duration(milliseconds: 400));
      expect(find.byType(SiteRobotPage), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('图例、图上的机器人在浅色深色下都压在自己的底上（不靠栅格图的颜色）', (t) async {
      _size(t);
      for (final b in Brightness.values) {
        final c = b == Brightness.dark ? NwColors.dark : NwColors.light;
        final api = _Api('guard')..robotsOverride = _robotsOn('8');
        await _pump(t, SiteMapTab(key: UniqueKey(), api: api), theme: nwTheme(b));
        final legend = t.widget<Material>(find.byKey(SiteMapTab.legendKey));
        expect(legend.color, c.surface);
        final marker = t.widget<Container>(find.byKey(SiteMapTab.markerKey('A')));
        expect((marker.decoration! as BoxDecoration).color, c.surface);
        final label = t.widget<Text>(
            find.descendant(of: find.byKey(SiteMapTab.markerKey('A')), matching: find.byType(Text)));
        expect(label.style!.color, c.text);
        expect(label.style!.fontSize, greaterThanOrEqualTo(12));
      }
      await t.pumpWidget(Container());
    });
  });

  group('记录栏', () {
    test('数值格的数：今天几趟、其中几趟有异常（按当地日期）', () {
      final now = DateTime(2026, 9, 25, 12);
      final rows = [
        _run(1, DateTime(2026, 9, 25, 0, 5)),
        _run(2, DateTime(2026, 9, 25, 9), abnormal: 2),
        _run(3, DateTime(2026, 9, 25, 11, 59), abnormal: 1, normal: 0),
        _run(4, DateTime(2026, 9, 24, 23, 59), abnormal: 1),
        <String, dynamic>{'id': 5, 'stamp': 'not-a-stamp', 'verdicts': <String, dynamic>{'abnormal': 1}},
      ];
      expect(runStats(rows, now), (today: 3, anomalies: 2));
      expect(runStats(const [], now), (today: 0, anomalies: 0));
    });

    testWidgets('两个数值格 + 一趟一张卡；有异常的那一格、那张卡标出来；点卡进这一趟', (t) async {
      _size(t);
      final api = _Api('guard')
        ..runRows = [
          _run(1, DateTime(2026, 9, 25, 8)),
          _run(2, DateTime(2026, 9, 25, 9), abnormal: 1),
          _run(3, DateTime(2026, 9, 24, 9), abnormal: 1),
        ];
      await _pump(t, SiteRunsPage(api: api, embedded: true, now: () => DateTime(2026, 9, 25, 12)));
      expect(find.byType(AppBar), findsNothing, reason: '嵌在记录栏里：页头是记录栏的');
      expect(find.descendant(of: find.byKey(SiteRunsPage.todayKey), matching: find.text('2')), findsOneWidget);
      expect(find.descendant(of: find.byKey(SiteRunsPage.anomalyKey), matching: find.text('1')), findsOneWidget);
      expect(t.widget<NwStatTile>(find.byKey(SiteRunsPage.anomalyKey)).tone, NwTone.error);
      expect(find.byType(NwPatrolCard), findsNWidgets(3));
      final cards = t.widgetList<NwPatrolCard>(find.byType(NwPatrolCard)).toList();
      expect(cards.map((c) => c.anomaly), [false, true, true]);
      expect(cards.map((c) => c.thumbs.length).toSet(), {0}, reason: '列表里站点没给照片：不摆缩略图那一行');
      expect(find.byIcon(Icons.report), findsNWidgets(2));
      expect(api.calls.where((c) => c.startsWith('photo ') || c.startsWith('run ')), isEmpty,
          reason: '不为了缩略图多问站点');
      await t.tap(find.byKey(SiteRunsPage.runKey(2)));
      await t.pumpAndSettle();
      expect(find.byType(SiteRunPage), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('今天没有异常：那一格是 0、不上色', (t) async {
      _size(t);
      final api = _Api('owner')..runRows = [_run(1, DateTime(2026, 9, 25, 8))];
      await _pump(t, SiteRunsPage(api: api, embedded: true, now: () => DateTime(2026, 9, 25, 12)));
      expect(find.descendant(of: find.byKey(SiteRunsPage.anomalyKey), matching: find.text('0')), findsOneWidget);
      expect(t.widget<NwStatTile>(find.byKey(SiteRunsPage.anomalyKey)).tone, NwTone.neutral);
      expect(find.byIcon(Icons.report), findsNothing);
      await t.pumpWidget(Container());
    });

    testWidgets('外壳里的记录栏：三个分段下面都没有第二个顶栏；事件那一段还有摄像头、拦截点的入口', (t) async {
      _size(t);
      await t.pumpWidget(MaterialApp(home: NwShell(api: FakeApi('guard'), ring: () {})));
      await t.pumpAndSettle();
      await t.tap(find.byKey(NwShell.historyKey));
      await t.pumpAndSettle();
      expect(find.byType(AppBar), findsNothing);
      expect(find.byKey(SiteRunsPage.todayKey), findsOneWidget);
      await t.tap(find.byKey(NwShell.incidentsKey));
      await t.pumpAndSettle();
      expect(find.byType(AppBar), findsNothing);
      expect(find.byType(SiteIncidentsPage), findsOneWidget);
      expect(find.byKey(const Key('btn-cameras')), findsOneWidget);
      expect(find.byKey(const Key('btn-intercepts')), findsOneWidget);
      await t.tap(find.byKey(NwShell.scheduleKey));
      await t.pumpAndSettle();
      expect(find.byType(AppBar), findsNothing);
      expect(find.byType(SiteSchedulePage), findsOneWidget);
      expect(find.textContaining('nightly'), findsOneWidget);
      await t.pumpWidget(Container());
    });
  });

  group('管理栏', () {
    Future<void> health(WidgetTester t, FakeApi api, {ThemeData? theme}) async {
      _size(t);
      await t.pumpWidget(MaterialApp(
          theme: theme, home: SiteHealthPage(api: api, canShare: true, save: (n, b) async => '/x/$n')));
      await t.pumpAndSettle();
    }

    testWidgets('版本格子：站点、每台机器人、每台登录着的客户端各一格', (t) async {
      final api = FakeApi('admin');
      await health(t, api);
      final cards = t.widgetList<NwVersionCard>(find.byType(NwVersionCard)).toList();
      expect(cards.map((c) => c.entity), ['站点', 'A', 'gina']);
      expect(cards.map((c) => c.version), ['1.4.0', '代理 1.4.0 · 级别 2 · 在线', '手机 App 1.0.0 · android']);
      // 配不配的小标压在格子右上角，不跟版本那一句抢地方
      final pills = t.widgetList<NwStatusPill>(find.byType(NwStatusPill)).toList();
      expect(pills.map((p) => (p.label, p.tone)), [('正常', NwTone.success)]);
      final card = t.getRect(find.widgetWithText(NwVersionCard, 'A'));
      final pill = t.getRect(find.byType(NwStatusPill));
      expect(card.contains(pill.topLeft) && card.contains(pill.bottomRight), isTrue);
      expect(pill.bottom, lessThanOrEqualTo(t.getRect(find.text('代理 1.4.0 · 级别 2 · 在线')).top));
      expect(t.getSize(find.text('代理 1.4.0 · 级别 2 · 在线')).width, greaterThan(card.width * 0.8),
          reason: '版本那一句有整行的宽');
      await t.pumpWidget(Container());
    });

    testWidgets('版本格子：机器人太老、App 太老是橙色的小标；不清楚的不上色', (t) async {
      final api = FakeApi('admin')
        ..versionsBody = <String, dynamic>{
          'site': <String, dynamic>{'version': '2', 'min_app_level': 2},
          'robots': <dynamic>[
            <String, dynamic>{'robot_id': 'A', 'online': false, 'agent': '0.9', 'level': 1, 'verdict': 'agent_too_old'},
            <String, dynamic>{'robot_id': 'B', 'online': true, 'verdict': 'unknown'},
          ],
          'clients': <dynamic>[
            <String, dynamic>{'name': 'gina', 'kind': 'app', 'version': '1.0.0', 'api_level': 1},
            <String, dynamic>{'name': 'alice', 'kind': 'web', 'api_level': 0},
          ],
        };
      await health(t, api);
      final cards = t.widgetList<NwVersionCard>(find.byType(NwVersionCard)).toList();
      expect(cards.map((c) => c.entity), ['站点', 'A', 'B', 'gina', 'alice']);
      final pills = t.widgetList<NwStatusPill>(find.byType(NwStatusPill)).toList();
      expect(pills.map((p) => (p.label, p.tone)), [
        ('机器人太老', NwTone.warning),
        ('不清楚', NwTone.neutral),
        ('太老', NwTone.warning),
      ]);
      // 小标各在各的格子里
      for (final (entity, label) in [('A', '机器人太老'), ('B', '不清楚'), ('GINA', '太老')]) {
        final card = t.getRect(find.widgetWithText(NwVersionCard, entity));
        expect(card.contains(t.getCenter(find.text(label))), isTrue, reason: '$entity $label');
      }
      await t.pumpWidget(Container());
    });

    testWidgets('三个小节；没有「重启站点」（站点没有这个功能）；诊断包的说明和按钮在维护那一节', (t) async {
      for (final lang in AppLang.values) {
        appLang.value = lang;
        addTearDown(() => appLang.value = AppLang.zh);
        await health(t, FakeApi('admin'));
        final labels = t.widgetList<NwSectionLabel>(find.byType(NwSectionLabel)).map((l) => l.text).toList();
        expect(labels, lang == AppLang.zh ? ['站点状态', '固件与软件', '维护'] : ['Station status', 'Firmware & software', 'Maintenance']);
        expect(find.textContaining('重启'), findsNothing);
        expect(find.textContaining(RegExp('reboot', caseSensitive: false)), findsNothing);
        expect(find.byKey(SiteHealthPage.bundleKey), findsOneWidget);
        expect(find.byKey(SiteHealthPage.shareKey), findsOneWidget);
        await t.pumpWidget(Container());
      }
    });

    testWidgets('过了的是绿色的勾，没过的要紧项红、别的橙；浅色深色都从主题取', (t) async {
      for (final b in Brightness.values) {
        final c = b == Brightness.dark ? NwColors.dark : NwColors.light;
        final api = FakeApi('admin')
          ..healthBody = <String, dynamic>{
            'ok': false, 'critical_ok': false, 'version': '1',
            'checks': <String, dynamic>{
              'db': <String, dynamic>{'ok': false, 'detail': '库写不了'},
              'push': <String, dynamic>{'ok': false, 'detail': '没配'},
              'certs': <String, dynamic>{'ok': true, 'detail': 'x'},
            },
          };
        await health(t, api, theme: nwTheme(b));
        Color? trailing(String k) =>
            (t.widget<ListTile>(find.byKey(SiteHealthPage.checkKey(k))).trailing! as Text).style!.color;
        expect(trailing('db'), c.error);
        expect(trailing('push'), c.warning);
        expect(trailing('certs'), c.success);
        expect((t.widget<ListTile>(find.byKey(SiteHealthPage.checkKey('certs'))).leading! as Icon).color, c.success);
        await t.pumpWidget(Container());
      }
    });

    testWidgets('外壳里的管理栏：体检在上，下面是版本、拦截点、摄像头、天气的入口；刷新在页头', (t) async {
      _size(t);
      await t.pumpWidget(MaterialApp(home: NwShell(api: FakeApi('admin'), ring: () {})));
      await t.pumpAndSettle();
      await t.tap(find.byKey(NwShell.adminKey));
      await t.pumpAndSettle();
      expect(find.byType(AppBar), findsNothing);
      expect(t.widget(find.byKey(SiteHealthPage.refreshKey)), isA<IconButton>());
      final overall = t.getRect(find.byKey(SiteHealthPage.overallKey));
      for (final k in [NwShell.releasesKey, NwShell.interceptsKey, NwShell.camerasKey, NwShell.weatherKey]) {
        expect(t.widget(find.byKey(k)), isA<OutlinedButton>(), reason: '$k');
        expect(t.getRect(find.byKey(k)).top, greaterThan(overall.bottom), reason: '$k 在体检下面');
        expect(find.ancestor(of: find.byKey(k), matching: find.byType(SiteHealthPage)), findsOneWidget,
            reason: '跟体检一起滚');
      }
      await t.pumpWidget(Container());
    });
  });

  group('版面：桌面的窗口', () {
    for (final th in <String, ThemeData?>{
      'light': nwTheme(Brightness.light),
      'dark': nwTheme(Brightness.dark),
      'no theme': null,
    }.entries) {
      for (final lang in AppLang.values) {
        for (final size in const [Size(1280, 720), Size(1920, 1080)]) {
          testWidgets('不溢出 [${th.key}] [${lang.name}] ${size.width.toInt()}×${size.height.toInt()}：地图、记录、管理',
              (t) async {
            appLang.value = lang;
            addTearDown(() => appLang.value = AppLang.zh);
            t.view.physicalSize = size;
            t.view.devicePixelRatio = 1.0;
            addTearDown(t.view.reset);
            final api = _Api('admin')..robotsOverride = _robotsOn('8');
            await t.pumpWidget(MaterialApp(theme: th.value, home: NwShell(api: api, ring: () {})));
            await t.pumpAndSettle();
            for (final k in [
              NwShell.mapKey, NwShell.historyKey, NwShell.incidentsKey, NwShell.scheduleKey, NwShell.adminKey, //
            ]) {
              await t.tap(find.byKey(k));
              await t.pumpAndSettle();
              expect(t.takeException(), isNull, reason: '$k');
            }
            await t.pumpWidget(Container());
          });
        }
      }
    }

    testWidgets('推进去的那几页（地图列表、看图、一趟、照片、版本、拦截点、摄像头、天气、日志）桌面窗口下不溢出', (t) async {
      final run = siteFixture('site_run');
      final photo = ((run['photos'] as List).first as Map).cast<String, dynamic>();
      final pages = <Widget Function()>[
        () => SiteMapsPage(api: FakeApi('admin')),
        () => SiteMapPreviewPage(api: FakeApi('admin'), mapId: 'estate-1', version: '8'),
        () => SiteRunsPage(api: FakeApi('admin'), robotId: 'A'),
        () => SiteRunPage(api: FakeApi('admin'), runId: 1),
        () => SitePhotoPage(api: FakeApi('admin'), runId: 1, photo: photo, bytes: Future.value(onePixelPng)),
        () => SiteIncidentsPage(api: FakeApi('admin')),
        () => SiteSchedulePage(api: FakeApi('admin')),
        () => SiteReleasesPage(api: FakeApi('admin')),
        () => SiteInterceptsPage(api: FakeApi('admin')),
        () => SiteCamerasPage(api: FakeApi('admin')),
        () => SiteWeatherPage(api: FakeApi('admin')),
        () => SiteProcLogsPage(api: FakeApi('admin'), robotId: 'A'),
        () => SiteProcLogPage(api: FakeApi('admin'), robotId: 'A', name: 'slam'),
        () => SiteHealthPage(api: FakeApi('admin'), canShare: true, save: (n, b) async => n),
      ];
      for (final lang in AppLang.values) {
        appLang.value = lang;
        addTearDown(() => appLang.value = AppLang.zh);
        for (final size in const [Size(1280, 720), Size(1920, 1080)]) {
          t.view.physicalSize = size;
          t.view.devicePixelRatio = 1.0;
          addTearDown(t.view.reset);
          for (final b in Brightness.values) {
            for (var i = 0; i < pages.length; i++) {
              await t.pumpWidget(MaterialApp(theme: nwTheme(b), home: KeyedSubtree(key: UniqueKey(), child: pages[i]())));
              await t.pumpAndSettle();
              expect(t.takeException(), isNull, reason: '第 $i 页 ${lang.name} $size ${b.name}');
            }
          }
        }
      }
      await t.pumpWidget(Container());
    });

    testWidgets('这张图上没有栅格、没有图、读不到：三种样子在竖屏横屏、中英文下都不溢出', (t) async {
      for (final lang in AppLang.values) {
        appLang.value = lang;
        addTearDown(() => appLang.value = AppLang.zh);
        for (final size in const [Size(390, 844), Size(800, 360)]) {
          _size(t, size);
          for (final api in [
            _Api('admin')..previewMissing = true,
            _Api('admin')..mapsBody = <String, dynamic>{'maps': <dynamic>[], 'bags': <dynamic>[]},
            _Api('admin')..mapsError = const SiteError(502, '站点忙，一段很长很长的原因很长很长的原因很长很长的原因'),
            _Api('admin'),
          ]) {
            await _pump(t, SiteMapTab(key: UniqueKey(), api: api));
            expect(t.takeException(), isNull, reason: '${lang.name} $size');
          }
        }
      }
      await t.pumpWidget(Container());
    });
  });

  test('假站点的预览图是一张真的 PNG（画布测试靠它）', () {
    expect(fakePreviewPng, isA<Uint8List>());
    expect(fakePreviewPng.sublist(1, 4), [80, 78, 71]);
  });
}
