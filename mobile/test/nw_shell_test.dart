// 登录之后的外壳（2026-10-14 重做）：底部五栏；管理栏只有管理员有；狗列表身上的站点级逻辑（事件流、
// 响铃、登录过期退回）切到哪一栏都照样活着。各栏里面还是老页面。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/nw/nw_shell.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/site_cameras.dart';
import 'package:d1max_patrol/ui/site_health.dart';
import 'package:d1max_patrol/ui/site_intercepts.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_releases.dart';
import 'package:d1max_patrol/ui/site_runs.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:d1max_patrol/ui/site_weather.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

void _size(WidgetTester t, [Size size = const Size(390, 844)]) {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

Future<void> _show(WidgetTester t, FakeApi api, {ThemeData? theme, void Function()? ring}) async {
  await t.pumpWidget(MaterialApp(
      theme: theme, home: NwShell(api: api, title: '庄园', ring: ring ?? () {})));
  await t.pumpAndSettle();
}

Future<void> _tab(WidgetTester t, Key key) async {
  await t.tap(find.byKey(key));
  await t.pumpAndSettle();
}

/// 狗列表的那个 State：切栏前后得是同一个（没被拆了重建）。
State _robotsState(WidgetTester t) => t.state(find.byType(SiteRobotsPage, skipOffstage: false));

const _navKeys = [NwShell.dutyKey, NwShell.dogsKey, NwShell.mapKey, NwShell.historyKey];

void main() {
  group('五栏', () {
    testWidgets('管理员：值守、机器人、地图、记录、管理；进来先看值守；原来顶栏那几个入口的 Key 在导航上', (t) async {
      _size(t);
      await _show(t, FakeApi('admin'));
      for (final (key, label) in [
        (const Key('open-watch'), '值守'),
        (const Key('open-dogs'), '机器人'),
        (const Key('open-maps'), '地图'),
        (const Key('open-runs'), '记录'),
        (const Key('open-health'), '管理'),
      ]) {
        expect(find.byKey(key), findsOneWidget, reason: '$key');
        expect(find.descendant(of: find.byKey(key), matching: find.text(label)), findsOneWidget, reason: label);
        expect(find.ancestor(of: find.byKey(key), matching: find.byType(NwBottomNav)), findsOneWidget);
      }
      expect(find.byKey(NwShell.moreKey), findsNothing);
      expect(find.byType(SiteWatchPage), findsOneWidget);
      expect(find.byType(SiteRobotsPage), findsNothing, reason: '机器人列表在背后，不显示');
      expect(find.byType(SiteRobotsPage, skipOffstage: false), findsOneWidget, reason: '但是活着');
      // 五栏等宽、够手指点、都在屏幕里
      final rects = [for (final k in [..._navKeys, NwShell.adminKey]) t.getRect(find.byKey(k))];
      expect(rects.map((r) => r.width.round()).toSet().length, 1);
      for (final r in rects) {
        expect(r.height, greaterThanOrEqualTo(48));
        expect(r.bottom, lessThanOrEqualTo(844));
      }
      expect(rects.first.left, greaterThanOrEqualTo(0));
      expect(rects.last.right, lessThanOrEqualTo(390));
      await t.pumpWidget(Container());
    });

    testWidgets('保安、业主：没有管理栏，那一栏是「更多」（版本、拦截点、摄像头、天气），没有站点体检', (t) async {
      _size(t);
      for (final role in ['guard', 'owner']) {
        await _show(t, FakeApi(role));
        expect(find.byKey(NwShell.adminKey), findsNothing, reason: role);
        expect(find.text('管理'), findsNothing, reason: role);
        for (final k in _navKeys) {
          expect(find.byKey(k), findsOneWidget, reason: '$role $k');
        }
        await _tab(t, NwShell.moreKey);
        expect(find.byType(SiteHealthPage, skipOffstage: false), findsNothing, reason: role);
        for (final k in [NwShell.releasesKey, NwShell.interceptsKey, NwShell.camerasKey, NwShell.weatherKey]) {
          expect(find.byKey(k), findsOneWidget, reason: '$role $k');
        }
        await _tab(t, NwShell.camerasKey);
        expect(find.byType(SiteCamerasPage), findsOneWidget);
        expect(find.byType(NwBottomNav), findsNothing, reason: '推进去的页面没有底部导航');
        await t.pageBack();
        await t.pumpAndSettle();
        await _tab(t, NwShell.weatherKey);
        expect(find.byType(SiteWeatherPage), findsOneWidget);
        await t.pumpWidget(Container());
      }
    });

    testWidgets('每一栏点了是哪一页；记录栏三个分段；管理栏有体检和几个入口', (t) async {
      _size(t);
      await _show(t, FakeApi('admin'));
      await _tab(t, NwShell.dogsKey);
      expect(find.byType(SiteRobotsPage), findsOneWidget);
      expect(find.byType(SiteWatchPage, skipOffstage: false), findsNothing, reason: '切走的那一栏拆掉，不在背后空跑');
      expect(find.byKey(const Key('robot-A')), findsOneWidget);
      await _tab(t, NwShell.mapKey);
      // 第四批：地图栏是当前这张图；地图列表从它右上角推进去
      expect(find.byType(SiteMapTab), findsOneWidget);
      expect(find.byKey(const Key('robot-A')), findsNothing);
      await _tab(t, SiteMapTab.listKey);
      expect(find.byType(SiteMapsPage), findsOneWidget);
      expect(find.byType(NwBottomNav), findsNothing);
      await t.pageBack();
      await t.pumpAndSettle();

      await _tab(t, NwShell.historyKey);
      expect(find.byType(SiteRunsPage), findsOneWidget);
      for (final k in [NwShell.runsKey, NwShell.incidentsKey, NwShell.scheduleKey]) {
        expect(find.byKey(k), findsOneWidget);
      }
      await _tab(t, const Key('open-incidents'));
      expect(find.byType(SiteIncidentsPage), findsOneWidget);
      expect(find.byType(SiteRunsPage), findsNothing);
      await _tab(t, const Key('open-schedule'));
      expect(find.byType(SiteSchedulePage), findsOneWidget);
      await _tab(t, NwShell.runsKey);
      expect(find.byType(SiteRunsPage), findsOneWidget);

      await _tab(t, NwShell.adminKey);
      expect(find.byType(SiteHealthPage), findsOneWidget);
      expect(t.widget(find.byKey(const Key('open-releases'))), isA<OutlinedButton>());
      // 第四批：入口在体检下面（同一页里往下滚）
      await t.ensureVisible(find.byKey(const Key('open-releases')));
      await _tab(t, const Key('open-releases'));
      expect(find.byType(SiteReleasesPage), findsOneWidget);
      expect(find.byType(NwBottomNav), findsNothing);
      await t.pageBack();
      await t.pumpAndSettle();
      expect(find.byType(SiteHealthPage), findsOneWidget, reason: '退回来还在管理栏');
      await t.ensureVisible(find.byKey(NwShell.interceptsKey));
      await _tab(t, NwShell.interceptsKey);
      expect(find.byType(SiteInterceptsPage), findsOneWidget);
      await t.pageBack();
      await t.pumpAndSettle();

      await _tab(t, NwShell.dutyKey);
      expect(find.byType(SiteWatchPage), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('机器人列表的顶栏上不再有那一排入口（挪到导航上了）；推送、后台值守的图标还在它那儿', (t) async {
      _size(t);
      await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: FakeApi('admin'), title: '庄园')));
      await t.pumpAndSettle();
      for (final k in ['open-watch', 'open-maps', 'open-runs', 'open-releases', 'open-incidents', 'open-schedule', 'open-health']) {
        expect(find.byKey(Key(k)), findsNothing, reason: k);
      }
      expect(find.byKey(const Key('robot-A')), findsOneWidget);
    });
  });

  group('机器人列表身上的站点级逻辑切栏不断', () {
    testWidgets('切到别的栏，事件来了照样刷新机器人列表；切回来看到的是新的；它一直是同一个', (t) async {
      _size(t);
      final api = FakeApi('guard');
      await _show(t, api);
      final state = _robotsState(t);
      await _tab(t, NwShell.mapKey);
      final before = api.robotsCalls;
      api.robotsOverride = [
        <String, dynamic>{'robot_id': 'Z9', 'fresh': true, 'status': <String, dynamic>{'online': true}}
      ];
      api.sse.add(<String, dynamic>{'kind': 'robot', 'robot_id': 'Z9'});
      await t.pump();
      await t.pump(const Duration(milliseconds: 600)); // 狗列表半秒内的事件合成一次刷新
      await t.pump();
      expect(api.robotsCalls, greaterThan(before), reason: '不在眼前也收到了事件、重新读了机器人列表');
      expect(find.byKey(const Key('robot-Z9')), findsNothing, reason: '人还在地图栏');
      await _tab(t, NwShell.historyKey);
      await _tab(t, NwShell.dogsKey);
      expect(find.byKey(const Key('robot-Z9')), findsOneWidget);
      expect(find.byKey(const Key('robot-A')), findsNothing);
      expect(_robotsState(t), same(state), reason: '没被拆了重建：订阅、响铃的计时器都还是原来那一份');
      expect(find.byKey(const Key('live-lost')), findsNothing, reason: '实时更新一直连着');
      await t.pumpWidget(Container());
    });

    testWidgets('人在地图栏：升到声音档的告警照样响铃、照样提示', (t) async {
      _size(t);
      final api = FakeApi('guard');
      var rang = 0;
      await _show(t, api, ring: () => rang++);
      await _tab(t, NwShell.mapKey);
      final f = siteFixture('site_alert_frame');
      api.sse.add(<String, dynamic>{
        ...f,
        'alert': <String, dynamic>{...(f['alert'] as Map<String, dynamic>), 'channel': 'sound'}
      });
      await t.pump();
      expect(rang, 1);
      await t.pump(const Duration(milliseconds: 100));
      expect(find.textContaining('没人确认'), findsOneWidget, reason: '提示条在外壳上，哪一栏都看得见');
      expect(find.byType(SiteMapTab), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('登录过期（401）：不管在哪一栏，整个外壳退回上一页并说明', (t) async {
      _size(t);
      final api = FakeApi('guard');
      String? why;
      await t.pumpWidget(MaterialApp(
          home: Builder(
              builder: (c) => TextButton(
                  onPressed: () async => why = await Navigator.push<String>(
                      c, MaterialPageRoute<String>(builder: (_) => NwShell(api: api, ring: () {}))),
                  child: const Text('进')))));
      await t.tap(find.text('进'));
      await t.pumpAndSettle();
      await _tab(t, NwShell.mapKey);
      expect(find.byType(NwShell), findsOneWidget);
      // 站点说令牌过期了：下一次读狗列表回 401
      api.robotsError = const SiteError(401, '没登录或登录已过期');
      api.sse.add(<String, dynamic>{'kind': 'robot'});
      await t.pump();
      await t.pump(const Duration(milliseconds: 600));
      await t.pumpAndSettle();
      expect(find.byType(NwShell), findsNothing);
      expect(why, contains('重新登录'));
      expect(find.text('进'), findsOneWidget);
    });

    testWidgets('从站点列表登录进来就是外壳；返回 = 离开站点（注销）', (t) async {
      _size(t);
      final api = FakeApi('admin');
      final store = MemorySiteStore([
        SiteEntry(name: '庄园', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'gina')
      ]);
      await t.pumpWidget(MaterialApp(home: SiteListPage(store: store, apiFactory: (_) => api)));
      await t.pumpAndSettle();
      await t.tap(find.text('庄园'));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('site-password')), 'pw');
      await t.tap(find.text('登录'));
      await t.pumpAndSettle();
      expect(find.byType(NwShell), findsOneWidget);
      expect(find.byType(NwBottomNav), findsOneWidget);
      await _tab(t, NwShell.dogsKey);
      expect(find.text('庄园（admin）'), findsOneWidget, reason: '站点名交给了机器人列表');
      await t.pageBack();
      await t.pumpAndSettle();
      expect(find.byType(NwShell), findsNothing);
      expect(api.pushLog, contains('logout'));
    });
  });

  group('版面', () {
    for (final th in <String, ThemeData?>{
      'light': nwTheme(Brightness.light),
      'dark': nwTheme(Brightness.dark),
      'no theme': null,
    }.entries) {
      for (final lang in AppLang.values) {
        for (final size in const [Size(390, 844), Size(360, 640), Size(800, 360)]) {
          for (final role in ['admin', 'owner']) {
            testWidgets(
                '不溢出 [${th.key}] [${lang.name}] ${size.width.toInt()}×${size.height.toInt()} $role：每一栏、每个分段',
                (t) async {
              appLang.value = lang;
              addTearDown(() => appLang.value = AppLang.zh);
              _size(t, size);
              await _show(t, FakeApi(role), theme: th.value);
              expect(t.takeException(), isNull);
              for (final k in [
                NwShell.dogsKey, NwShell.mapKey, NwShell.historyKey, NwShell.incidentsKey, NwShell.scheduleKey,
                role == 'admin' ? NwShell.adminKey : NwShell.moreKey, NwShell.dutyKey,
              ]) {
                await _tab(t, k);
                expect(t.takeException(), isNull, reason: '$k');
              }
              final nav = t.getRect(find.byType(NwBottomNav));
              expect(nav.bottom, size.height);
              expect(nav.height, lessThanOrEqualTo(72), reason: '横屏只有 360 高，导航不能占太多');
              await t.pumpWidget(Container());
            });
          }
        }
      }
    }
  });

  group('NwBottomNav', () {
    testWidgets('点哪栏回第几栏；Key 在能点的那一层；选中的是主色加粗，别的是次要色', (t) async {
      _size(t);
      final got = <int>[];
      const items = [
        NwNavItem(key: Key('a'), icon: Icons.shield_rounded, label: 'Duty'),
        NwNavItem(key: Key('b'), icon: Icons.pets_rounded, label: 'Dogs'),
        NwNavItem(key: Key('c'), icon: Icons.map_rounded, label: 'Map'),
      ];
      for (final b in Brightness.values) {
        final c = b == Brightness.dark ? NwColors.dark : NwColors.light;
        await t.pumpWidget(MaterialApp(
            theme: nwTheme(b),
            home: Scaffold(bottomNavigationBar: NwBottomNav(items: items, index: 1, onTap: got.add))));
        await t.pumpAndSettle();
        expect(t.widget(find.byKey(const Key('c'))), isA<InkWell>());
        expect(t.widget<Icon>(find.byIcon(Icons.pets_rounded)).color, c.primary);
        expect(t.widget<Icon>(find.byIcon(Icons.map_rounded)).color, c.textSecondary);
        expect(t.widget<Text>(find.text('Dogs')).style!.fontWeight, FontWeight.w600);
        expect(t.widget<Text>(find.text('Dogs')).style!.color, c.primary);
        expect(t.widget<Text>(find.text('Map')).style!.color, c.textSecondary);
        expect(t.widget<Text>(find.text('Map')).style!.fontSize, greaterThanOrEqualTo(12));
        final bar = t.widget<Material>(
            find.descendant(of: find.byType(NwBottomNav), matching: find.byType(Material)).first);
        expect(bar.color, c.surface);
        expect(bar.elevation, 0, reason: '没有阴影，上面一条线');
      }
      await t.tap(find.byKey(const Key('c')));
      await t.tap(find.text('Duty'));
      expect(got, [2, 0]);
    });
  });
}
