// 狗的那几页重做（2026-10-14，P3）新加的行为：狗的状态怎么算、药丸什么颜色，列表上的筛选，
// 单狗页的「叫停」在页头、没任务时置灰，电量读不到写「--」，诊断卡只放狗报的。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_p3_robots.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi, busyView;
import 'support/fake_site.dart' show siteFixture;

/// 一只狗的视图：在线、新鲜、急停没按、没任务；[over] 盖掉顶层的键，[status] 盖掉 status 里的。
Map<String, dynamic> _robot(String id,
    {Map<String, dynamic> over = const {}, Map<String, dynamic> status = const {}}) {
  return <String, dynamic>{
    'robot_id': id,
    'revoked': false,
    'fresh': true,
    'held': null,
    'manual_only': null,
    'capabilities': <String, dynamic>{'tasks': <String, dynamic>{}},
    'status': <String, dynamic>{
      'online': true,
      'ready': <String, dynamic>{'control': true, 'motion': true, 'estop_clear': true, 'loc_ok': true},
      'task': null,
      ...status,
    },
    ...over,
  };
}

const _patrol = <String, dynamic>{'kind': 'patrol', 'state': 'running', 'task_id': 'task-1'};
const _dock = <String, dynamic>{'kind': 'dock', 'state': 'running', 'task_id': 'dock-1'};
const _toCharge = <String, dynamic>{'kind': 'goto', 'state': 'running', 'task_id': 'charge-goto-1'};

void main() {
  group('机器人的状态', () {
    test('按站点给的视图算，从重到轻', () {
      expect(robotPhase(_robot('A', over: {'revoked': true})), RobotPhase.revoked);
      expect(robotPhase(_robot('A', over: {'status': null})), RobotPhase.unknown);
      expect(robotPhase(_robot('A', status: {'online': false})), RobotPhase.offline);
      expect(robotPhase(_robot('A', over: {'fresh': false})), RobotPhase.offline, reason: '不新鲜也算离线');
      expect(robotPhase(_robot('A'), deterring: true), RobotPhase.deterring);
      expect(robotPhase(_robot('A', over: {'fresh': false}), deterring: true), RobotPhase.offline,
          reason: '离线盖过驱离：人先得知道它连不上');
      expect(
          robotPhase(_robot('A', status: {
            'ready': {'estop_clear': false}
          })),
          RobotPhase.estop);
      expect(robotPhase(_robot('A', over: {'held': {'by': 'olga'}})), RobotPhase.held);
      expect(robotPhase(_robot('A', status: {'task': _dock})), RobotPhase.charging);
      expect(robotPhase(_robot('A', status: {'task': _toCharge})), RobotPhase.charging);
      expect(robotPhase(_robot('A', over: {'battery_pct': 12})), RobotPhase.lowBattery);
      expect(robotPhase(_robot('A', over: {'battery_pct': 12}, status: {'task': _dock})), RobotPhase.charging,
          reason: '已经在充了就不再喊低电');
      expect(robotPhase(_robot('A', status: {'task': _patrol})), RobotPhase.working);
      expect(robotPhase(_robot('A')), RobotPhase.idle);
      expect(robotPhase(_robot('A', over: {'battery_pct': 80})), RobotPhase.idle);
    });

    test('颜色：红只给驱离、急停、离线；橙只给低电；绿给在干活、在充电；别的不上色', () {
      final tones = {for (final p in RobotPhase.values) p: robotPhaseTone(p)};
      expect(tones.entries.where((e) => e.value == NwTone.error).map((e) => e.key).toSet(),
          {RobotPhase.deterring, RobotPhase.estop, RobotPhase.offline});
      expect(tones.entries.where((e) => e.value == NwTone.warning).map((e) => e.key).toSet(),
          {RobotPhase.lowBattery});
      expect(tones.entries.where((e) => e.value == NwTone.success).map((e) => e.key).toSet(),
          {RobotPhase.working, RobotPhase.charging});
      for (final p in [RobotPhase.idle, RobotPhase.held, RobotPhase.revoked, RobotPhase.unknown]) {
        expect(tones[p], NwTone.neutral, reason: '$p');
      }
    });

    test('每种状态中英文都有字，英文里没有中文', () {
      addTearDown(() => appLang.value = AppLang.zh);
      for (final p in RobotPhase.values) {
        appLang.value = AppLang.zh;
        expect(robotPhaseLabel(p), isNotEmpty);
        appLang.value = AppLang.en;
        expect(robotPhaseLabel(p), matches(RegExp(r'^[A-Z -]+$')), reason: '$p');
      }
    });

    test('电量：视图里有才有（站点现在没放），不编', () {
      expect(robotBattery(_robot('A')), isNull);
      expect(robotBattery(_robot('A', over: {'battery_pct': 41.6})), 41.6);
      expect(robotBattery(siteFixture('site_robot')), isNull, reason: '真站点的机器人视图里没有电量');
    });
  });

  group('机器人列表', () {
    FakeApi fleet() => FakeApi('guard')
      ..robotsOverride = <Map<String, dynamic>>[
        _robot('A'),
        _robot('B', status: {'task': _patrol}),
        _robot('C', status: {'task': _dock}),
        _robot('D', status: {'online': false}),
      ];

    Finder card(String id) => find.byKey(Key('robot-$id'));
    NwDogCard dog(WidgetTester t, String id) => t.widget<NwDogCard>(card(id));

    testWidgets('一台机器人一张卡：药丸的字和颜色按真实状态，在做什么照原来那一行', (t) async {
      await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: fleet())));
      await t.pumpAndSettle();
      expect(dog(t, 'A').tone, NwTone.neutral);
      expect(dog(t, 'A').status, '空闲');
      expect(dog(t, 'A').activity, '在线 · 空闲');
      expect(dog(t, 'B').tone, NwTone.success);
      expect(dog(t, 'B').status, '执行中');
      expect(dog(t, 'C').tone, NwTone.success);
      expect(dog(t, 'C').status, '充电');
      expect(dog(t, 'D').tone, NwTone.error);
      expect(dog(t, 'D').status, '离线');
      expect(dog(t, 'D').battery, isNull, reason: '站点没给电量：写「--」，不编');
      expect(dog(t, 'A').p1, isFalse);
    });

    testWidgets('筛选：全部 / 执行中 / 充电，只筛已经读到的，不另外问站点', (t) async {
      final api = fleet();
      await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
      await t.pumpAndSettle();
      final asked = api.robotsCalls;
      expect(find.text('全部 4'), findsOneWidget);
      expect(find.text('执行中 1'), findsOneWidget);
      expect(find.text('充电 1'), findsOneWidget);
      for (final id in ['A', 'B', 'C', 'D']) {
        expect(card(id), findsOneWidget);
      }
      await t.tap(find.byKey(NwFleet.filterKey(FleetFilter.active)));
      await t.pumpAndSettle();
      expect(card('B'), findsOneWidget);
      for (final id in ['A', 'C', 'D']) {
        expect(card(id), findsNothing, reason: id);
      }
      await t.tap(find.byKey(NwFleet.filterKey(FleetFilter.charging)));
      await t.pumpAndSettle();
      expect(card('C'), findsOneWidget);
      expect(card('B'), findsNothing);
      await t.tap(find.byKey(NwFleet.filterKey(FleetFilter.all)));
      await t.pumpAndSettle();
      expect(card('A'), findsOneWidget);
      expect(card('D'), findsOneWidget);
      expect(api.robotsCalls, asked, reason: '筛选是本地的');
      await t.pumpWidget(const SizedBox());
    });

    testWidgets('这一格里一只都没有：说出来', (t) async {
      final api = FakeApi('guard')..robotsOverride = <Map<String, dynamic>>[_robot('A')];
      await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
      await t.pumpAndSettle();
      expect(find.byKey(NwFleet.emptyKey), findsNothing);
      await t.tap(find.byKey(NwFleet.filterKey(FleetFilter.charging)));
      await t.pumpAndSettle();
      expect(find.byKey(NwFleet.emptyKey), findsOneWidget);
      expect(card('A'), findsNothing);
      await t.pumpWidget(const SizedBox());
    });

    testWidgets('实时更新一来（列表换了一份），选的那一格还在', (t) async {
      final api = fleet();
      await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(NwFleet.filterKey(FleetFilter.active)));
      await t.pumpAndSettle();
      api.robotsOverride = <Map<String, dynamic>>[
        _robot('A', status: {'task': _patrol}),
        _robot('B'),
      ];
      api.sse.add(<String, dynamic>{'kind': 'robot'});
      await t.pump(const Duration(milliseconds: 600));
      await t.pumpAndSettle();
      expect(card('A'), findsOneWidget, reason: '还筛着「执行中」');
      expect(card('B'), findsNothing);
      await t.pumpWidget(const SizedBox());
    });

    testWidgets('正在驱离的机器人：红药丸、红框', (t) async {
      final api = FakeApi('guard')
        ..robotsOverride = <Map<String, dynamic>>[_robot('A', status: {'task': _patrol})]
        ..deterRows = <Map<String, dynamic>>[
          <String, dynamic>{
            ...((siteFixture('site_deterrence')['sessions'] as List).first as Map<String, dynamic>),
            'robot_id': 'A'
          }
        ];
      await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('deterrence-A')), findsOneWidget);
      expect(dog(t, 'A').tone, NwTone.error);
      expect(dog(t, 'A').p1, isTrue);
      await t.pumpWidget(const SizedBox());
    });
  });

  group('单机器人页', () {
    testWidgets('叫停在页头、红的；这一页看着没任务时也按得动（外审 I1）', (t) async {
      await t.pumpWidget(MaterialApp(
          home: SiteRobotPage(api: FakeApi('guard', robotView: busyView()), robotId: 'A')));
      await t.pumpAndSettle();
      final stop = t.widget<FilledButton>(find.byKey(const Key('btn-abort')));
      expect(stop.onPressed, isNotNull);
      expect(stop.style!.backgroundColor!.resolve(<WidgetState>{}), NwColors.light.error);
      expect(find.ancestor(of: find.byKey(const Key('btn-abort')), matching: find.byType(NwHeader)),
          findsOneWidget, reason: '在页头，不跟着滚');
      expect(find.ancestor(of: find.byKey(const Key('btn-abort')), matching: find.byType(Scrollable)),
          findsNothing);
      expect(t.widget<NwStatusPill>(find.byKey(const Key('robot-phase'))).tone, NwTone.success);

      final idle = Map<String, dynamic>.of(siteFixture('site_robot'));
      idle['status'] = Map<String, dynamic>.of(idle['status'] as Map<String, dynamic>)..['task'] = null;
      await t.pumpWidget(MaterialApp(
          home: SiteRobotPage(key: UniqueKey(), api: FakeApi('guard', robotView: idle), robotId: 'A')));
      await t.pumpAndSettle();
      expect(t.widget<FilledButton>(find.byKey(const Key('btn-abort'))).onPressed, isNotNull);
      expect(find.byKey(const Key('btn-abort-off')), findsNothing);
      await t.pumpWidget(const SizedBox());
    });

    testWidgets('电量读不到写「--」；诊断卡只放机器人报的，没有运行时长、延迟', (t) async {
      await t.pumpWidget(MaterialApp(
          home: SiteRobotPage(api: FakeApi('admin', robotView: busyView()), robotId: 'A')));
      await t.pumpAndSettle();
      expect(find.text('--'), findsOneWidget);
      final diag = find.byKey(const Key('robot-diagnostics'));
      expect(diag, findsOneWidget);
      Finder inDiag(String s) => find.descendant(of: diag, matching: find.text(s));
      expect(inDiag('0.1.0'), findsOneWidget, reason: '代理版本');
      expect(inDiag('sim/0.1.0'), findsOneWidget, reason: '适配器');
      expect(inDiag('2026-09-20-aaaaaa'), findsOneWidget, reason: '当前版本');
      expect(find.descendant(of: diag, matching: find.byType(NwDiagnosticRow)), findsNWidgets(5));
      await t.pumpWidget(const SizedBox());
    });

    testWidgets('管理员的那几个单独一组；保安没有这一组', (t) async {
      await t.pumpWidget(MaterialApp(
          home: SiteRobotPage(api: FakeApi('admin', robotView: busyView()), robotId: 'A')));
      await t.pumpAndSettle();
      expect(find.text('操作'), findsOneWidget);
      expect(find.text('管理'), findsOneWidget);
      for (final k in ['btn-record-start', 'btn-record-stop', 'btn-mark-home', 'btn-mapping-trail']) {
        expect(find.byKey(Key(k)), findsOneWidget, reason: k);
      }
      await t.pumpWidget(MaterialApp(
          home: SiteRobotPage(key: UniqueKey(), api: FakeApi('guard', robotView: busyView()), robotId: 'A')));
      await t.pumpAndSettle();
      expect(find.text('操作'), findsOneWidget);
      expect(find.text('管理'), findsNothing);
      expect(find.byKey(const Key('btn-record-start')), findsNothing);
      await t.pumpWidget(const SizedBox());
    });

    testWidgets('竖屏手机（390×844）：叫停一进来就在屏幕里', (t) async {
      t.view.physicalSize = const Size(390 * 2, 844 * 2);
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      await t.pumpWidget(MaterialApp(
          home: SiteRobotPage(api: FakeApi('admin', robotView: busyView()), robotId: 'A')));
      await t.pumpAndSettle();
      final r = t.getRect(find.byKey(const Key('btn-abort')));
      expect(r.top, greaterThanOrEqualTo(0));
      expect(r.right, lessThanOrEqualTo(390));
      expect(r.height, greaterThanOrEqualTo(Nw.touch));
      await t.pumpWidget(const SizedBox());
    });
  });

  group('遥控', () {
    testWidgets('不管系统浅色深色，这一页是深色的；没有顶栏，左上能关', (t) async {
      t.view.physicalSize = const Size(800 * 2, 360 * 2);
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      await t.pumpWidget(MaterialApp(
          theme: nwTheme(Brightness.light),
          home: Builder(
              builder: (c) => TextButton(
                  onPressed: () => Navigator.push(
                      c,
                      MaterialPageRoute<void>(
                          builder: (_) => SiteTeleopPage(api: FakeApi('guard'), robotId: 'A'))),
                  child: const Text('进')))));
      await t.tap(find.text('进'));
      await t.pumpAndSettle();
      expect(find.byType(AppBar), findsNothing);
      final ctx = t.element(find.byKey(SiteTeleopPage.statusKey));
      expect(Nw.of(ctx).bg, NwColors.dark.bg);
      final stop = t.widget<FilledButton>(find.byKey(SiteTeleopPage.stopKey));
      expect(stop.style!.backgroundColor!.resolve(<WidgetState>{}), NwColors.dark.error);
      expect(find.text('A'), findsOneWidget, reason: '哪台机器人');
      await t.tap(find.byKey(const Key('teleop-close')));
      await t.pumpAndSettle();
      expect(find.byType(SiteTeleopPage), findsNothing);
      expect(find.text('进'), findsOneWidget);
    });

    testWidgets('状态标签说的是遥控权（外审 M1：不写「实时」）：还没拿到、拿到了、没画面、结束了各说各的', (t) async {
      t.view.physicalSize = const Size(800 * 2, 360 * 2);
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      final api = FakeApi('guard');
      await t.pumpWidget(MaterialApp(home: SiteTeleopPage(api: api, robotId: 'A')));
      await t.pump();
      NwStatusPill pill() => t.widget<NwStatusPill>(find.byType(NwStatusPill));
      expect(pill().label, '等待');
      expect(pill().tone, NwTone.neutral);
      api.link!.ctl.add(<String, dynamic>{'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5, 'max_wz': 0.75});
      await t.pump();
      await t.pump();
      expect(pill().label, '已获控制');
      expect(pill().tone, NwTone.success);
      api.link!.ctl.add(<String, dynamic>{'kind': 'video', 'ok': false});
      await t.pump();
      await t.pump();
      expect(pill().label, '无画面');
      expect(pill().tone, NwTone.neutral);
      api.link!.ctl.add(<String, dynamic>{'kind': 'ended', 'reason': 'halt'});
      await t.pump();
      await t.pump();
      expect(pill().label, '已结束');
      expect(pill().tone, NwTone.neutral);
      await t.pumpWidget(const SizedBox());
    });
  });

  // 版面矩阵（layout_matrix_test）里没有的几种：遥控（568、800 宽，各个阶段）、各种状态的狗列表、
  // 被叫停又要监护的单狗页。中英文、浅色深色不带主题都画一遍，不溢出。
  group('不溢出', () {
    final themes = <String, ThemeData? Function()>{
      'light': () => nwTheme(Brightness.light),
      'dark': () => nwTheme(Brightness.dark),
      'no theme': () => null,
    };
    for (final th in themes.entries) {
      for (final lang in AppLang.values) {
        Future<void> show(WidgetTester t, Size size, Widget page) async {
          appLang.value = lang;
          addTearDown(() => appLang.value = AppLang.zh);
          t.view.physicalSize = size * 2;
          t.view.devicePixelRatio = 2.0;
          addTearDown(t.view.reset);
          await t.pumpWidget(MaterialApp(theme: th.value(), home: page));
          await t.pump();
          await t.pump(const Duration(milliseconds: 100));
          expect(t.takeException(), isNull);
        }

        for (final size in const [Size(568, 360), Size(800, 360)]) {
          testWidgets('[${th.key} ${lang.name} ${size.width.toInt()}×${size.height.toInt()}] 遥控的各个阶段', (t) async {
            final api = FakeApi('guard');
            await show(t, size, SiteTeleopPage(api: api, robotId: 'D1-02-LONG-NAME'));
            for (final m in <Map<String, dynamic>>[
              {'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5, 'max_wz': 0.75},
              {'kind': 'video', 'ok': false},
              {'kind': 'video', 'ok': true},
              {'kind': 'ended', 'reason': 'lease_lost'},
            ]) {
              api.link!.ctl.add(m);
              await t.pump();
              await t.pump();
              expect(t.takeException(), isNull, reason: '$m');
              expect(t.getRect(find.byKey(SiteTeleopPage.statusKey)).bottom, lessThanOrEqualTo(size.height));
            }
            await t.pumpWidget(Container());
          });
        }

        testWidgets('[${th.key} ${lang.name}] 各种状态的机器人列表（390×844、800×360）', (t) async {
          FakeApi api() => FakeApi('admin')
            ..robotsOverride = <Map<String, dynamic>>[
              _robot('D1-02', status: {'task': _patrol}),
              _robot('D1-05-WITH-A-VERY-LONG-NAME'),
              _robot('D1-08', status: {'task': _dock}, over: {'battery_pct': 18}),
              _robot('D2-01', status: {'online': false}),
              _robot('D2-03', status: {
                'ready': {'estop_clear': false}
              }),
              _robot('D2-04', over: {'held': {'by': 'olga'}, 'manual_only': {'by': 'enroll'}}),
              _robot('D2-05', over: {'revoked': true}),
              _robot('D2-06', over: {'status': null, 'battery_pct': 7}),
            ]
            ..deterRows = <Map<String, dynamic>>[
              <String, dynamic>{
                ...((siteFixture('site_deterrence')['sessions'] as List).first as Map<String, dynamic>),
                'robot_id': 'D1-02'
              }
            ];
          for (final size in const [Size(390, 844), Size(800, 360)]) {
            await show(t, size, SiteRobotsPage(key: UniqueKey(), api: api(), title: 'East Gate Estate'));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
          }
          await t.pumpWidget(Container());
        });

        testWidgets('[${th.key} ${lang.name}] 单机器人页：被叫停、要人监护、有定位和 RTK（390×844、800×360）', (t) async {
          final v = Map<String, dynamic>.of(busyView())
            ..['held'] = <String, dynamic>{'by': 'olga'}
            ..['loc'] = <String, dynamic>{'anchored': false, 'reason': '还没设位置：开机之后要人给一次', 'source': 'odom_anchor'}
            ..['rtk'] = <String, dynamic>{'fix': 'float', 'sats': 17, 'std_h_m': 0.4, 'age_s': 3, 'stale': true};
          final caps = Map<String, dynamic>.of(v['capabilities'] as Map<String, dynamic>);
          final tasks = Map<String, dynamic>.of(caps['tasks'] as Map<String, dynamic>);
          tasks['patrol'] = <String, dynamic>{...(tasks['patrol'] as Map<String, dynamic>), 'autonomy': 'supervised'};
          tasks['goto'] = <String, dynamic>{...(tasks['goto'] as Map<String, dynamic>), 'autonomy': 'supervised'};
          tasks['head'] = <String, dynamic>{'direction': 'tail', 'autonomy': false};
          tasks['obstacles'] = <String, dynamic>{'state': 'lost', 'reason': 'no points', 'rear': false};
          tasks['proc_log'] = <String, dynamic>{};
          caps['tasks'] = tasks;
          v['capabilities'] = caps;
          for (final size in const [Size(390, 844), Size(800, 360)]) {
            await show(t, size, SiteRobotPage(key: UniqueKey(), api: FakeApi('admin', robotView: v), robotId: 'D1-02'));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
            final stop = t.getRect(find.byKey(const Key('btn-abort')));
            expect(stop.bottom, lessThanOrEqualTo(size.height));
            expect(stop.right, lessThanOrEqualTo(size.width));
          }
          await t.pumpWidget(Container());
        });
      }
    }
  });
}
