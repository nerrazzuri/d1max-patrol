// App 重做外审(2026-10-14,`../reviews/2026-10-14-App-redesign-e0f829f-b20a8bc-review.md`)的三条。
import 'package:d1max_patrol/ui/nw/nw_p4_admin.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi, busyView;
import 'support/fake_site.dart' show siteFixture;

Map<String, dynamic> _idle() {
  final v = Map<String, dynamic>.of(siteFixture('site_robot'));
  v['status'] = Map<String, dynamic>.of(v['status'] as Map<String, dynamic>)..['task'] = null;
  return v;
}

Future<void> _robotPage(WidgetTester t, FakeApi api) async {
  await t.pumpWidget(MaterialApp(home: SiteRobotPage(key: UniqueKey(), api: api, robotId: 'A')));
  await t.pumpAndSettle();
}

void main() {
  group('I1 单机器人页的叫停不看打开页面时的旧状态', () {
    for (final role in ['owner', 'guard', 'admin']) {
      testWidgets('$role：进来时空闲，之后被别处派走，不刷新直接按叫停：撤的是现在这一趟', (t) async {
        final api = FakeApi(role, robotView: _idle());
        await _robotPage(t, api);
        final stop = t.widget<FilledButton>(find.byKey(const Key('btn-abort')));
        expect(stop.onPressed, isNotNull, reason: '有叫停权限就按得动，不拿进来时那一眼说了算');
        api.view = busyView();                               // 排程、入侵、另一台手机把它派走了
        await t.pump(const Duration(seconds: 60));           // 这一页没有人手动刷新
        await t.tap(find.byKey(const Key('btn-abort')));
        await t.pumpAndSettle();
        final tid = ((busyView()['status'] as Map)['task'] as Map)['task_id'];
        expect(api.calls, contains('abort A $tid'));
        await t.pumpWidget(const SizedBox());
      });
    }

    testWidgets('真的空闲：按了说它现在没在跑任务，不发撤任务', (t) async {
      final api = FakeApi('guard', robotView: _idle());
      await _robotPage(t, api);
      await t.tap(find.byKey(const Key('btn-abort')));
      await t.pumpAndSettle();
      expect(find.textContaining('它现在没在跑任务'), findsOneWidget);
      expect(api.calls.where((c) => c.startsWith('abort ')), isEmpty);
      await t.pumpWidget(const SizedBox());
    });
  });

  group('I2 地图上「在线」跟机器人列表一个口径', () {
    Map<String, dynamic> robot({bool? fresh = true, bool revoked = false}) {
      final r = Map<String, dynamic>.of(
          (siteFixture('site_robots')['robots'] as List).cast<Map<String, dynamic>>().first);
      r['robot_id'] = 'A';
      r['loc'] = <String, dynamic>{
        ...((r['loc'] as Map?) ?? const <String, dynamic>{}).cast<String, dynamic>(),
        'pose': <String, dynamic>{'x': 1.0, 'y': 2.0, 'yaw': 0.0},
      };
      final caps = Map<String, dynamic>.of(r['capabilities'] as Map<String, dynamic>);
      caps['tasks'] = <String, dynamic>{
        ...(caps['tasks'] as Map).cast<String, dynamic>(),
        'patrol': <String, dynamic>{'autonomy': 'autonomous', 'map_id': 'estate-1', 'map_version': '8'},
      };
      r['capabilities'] = caps;
      r['status'] = <String, dynamic>{...(r['status'] as Map).cast<String, dynamic>(), 'online': true};
      r['revoked'] = revoked;
      if (fresh == null) {
        r.remove('fresh');
      } else {
        r['fresh'] = fresh;
      }
      return r;
    }

    Future<void> open(WidgetTester t, Map<String, dynamic> r) async {
      t.view.physicalSize = const Size(390, 844);
      t.view.devicePixelRatio = 1;
      addTearDown(t.view.reset);
      final api = FakeApi('guard')..robotsOverride = [r];
      await t.pumpWidget(MaterialApp(home: Scaffold(body: SiteMapTab(api: api))));
      await t.pumpAndSettle();
    }

    NwTone? pillTone(WidgetTester t) => t
        .widget<NwStatusPill>(
            find.descendant(of: find.byKey(SiteMapTab.robotKey('A')), matching: find.byType(NwStatusPill)))
        .tone;

    testWidgets('站点说新鲜、没吊销：在线（绿）', (t) async {
      await open(t, robot());
      expect(find.descendant(of: find.byKey(SiteMapTab.robotKey('A')), matching: find.text('在线')), findsOneWidget);
      expect(pillTone(t), NwTone.success);
      await t.pumpWidget(Container());
    });

    for (final (name, r) in [
      ('状态不新鲜了', () => robot(fresh: false)),
      ('站点没给新不新鲜', () => robot(fresh: null)),
      ('已经吊销', () => robot(revoked: true)),
    ]) {
      testWidgets('$name：不写在线、不是绿的；图上的标记也不是绿的，写明是最后知道的位置', (t) async {
        await open(t, r());
        expect(find.descendant(of: find.byKey(SiteMapTab.robotKey('A')), matching: find.text('在线')), findsNothing);
        expect(pillTone(t), isNot(NwTone.success));
        final marker = find.byKey(SiteMapTab.markerKey('A'));
        if (marker.evaluate().isNotEmpty) {
          final m = t.widget<NwRobotMarker>(
              find.ancestor(of: marker, matching: find.byType(NwRobotMarker)).evaluate().isNotEmpty
                  ? find.ancestor(of: marker, matching: find.byType(NwRobotMarker))
                  : find.byType(NwRobotMarker));
          expect(m.tone, isNot(NwTone.success));
        }
        expect(find.descendant(of: find.byKey(SiteMapTab.robotKey('A')), matching: find.textContaining('最后')),
            findsOneWidget, reason: '位置是最后知道的，说出来');
        await t.pumpWidget(Container());
      });
    }
  });

  group('M1 遥控页不拿「拿到遥控」当「画面是实时的」', () {
    testWidgets('拿到遥控、一帧画面都还没到：不写 LIVE / 实时，写已获控制', (t) async {
      final api = FakeApi('guard');
      await t.pumpWidget(MaterialApp(home: SiteTeleopPage(key: UniqueKey(), api: api, robotId: 'A')));
      await t.pump();
      api.link!.ctl.add(<String, dynamic>{'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5, 'max_wz': 0.75});
      await t.pump();
      await t.pump();
      expect(find.text('实时'), findsNothing);
      expect(find.text('LIVE'), findsNothing);
      expect(find.text('已获控制'), findsOneWidget);
      await t.pumpWidget(const SizedBox());
    });
  });
}
