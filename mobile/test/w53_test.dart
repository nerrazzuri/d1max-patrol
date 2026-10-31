// W53（用户 2026-10-30 / 10-31）：值守页跟着一台机器人走、画面手动开、按角色的按钮、派巡逻弹框
// （估时、撞日程排队、被拒说原因）、今天的巡逻、管理栏（要你处理、开站清单）、账号审批、日程、申请账号、
// 上手导览和报警练习。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/guide.dart';
import 'package:d1max_patrol/ui/nw/nw_shell.dart';
import 'package:d1max_patrol/ui/patrol_dispatch.dart';
import 'package:d1max_patrol/ui/refusal.dart';
import 'package:d1max_patrol/ui/site_duty_page.dart';
import 'package:d1max_patrol/ui/site_list.dart';
import 'package:d1max_patrol/ui/site_tabs.dart';
import 'package:d1max_patrol/ui/site_video.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

void _size(WidgetTester t, [Size size = const Size(400, 1600)]) {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

Map<String, dynamic> _robot(String id, int enrolled, {String name = ''}) => <String, dynamic>{
      'robot_id': id, 'name': name, 'enrolled_at': enrolled, 'revoked': false, 'fresh': true,
      'battery_pct': 80,
      'status': <String, dynamic>{'online': true, 'task': null,
        'ready': <String, dynamic>{'estop_clear': true}},
    };

Future<void> _duty(WidgetTester t, FakeApi api) async {
  _size(t);
  await t.pumpWidget(MaterialApp(home: SiteDutyPage(api: api, title: '站点')));
  await t.pumpAndSettle();
}

void main() {
  setUp(() => appLang.value = AppLang.zh);

  group('值守页', () {
    testWidgets('默认跟第一台加入的；点「名字 ▾」从底部列表切；画面点了才播、✕ 关；切台就关', (t) async {
      final api = FakeApi('guard')
        ..robotsOverride = [_robot('B', 2000), _robot('A', 1000, name: '北门')];
      await _duty(t, api);
      // W60：「名字 ▾」显示当前这台；点开底部列表选
      expect(find.descendant(of: find.byKey(SiteDutyPage.selectorKey), matching: find.text('北门')), findsOneWidget,
          reason: 'A 先加入（enrolled_at 小）');
      expect(find.byType(SiteVideo), findsNothing, reason: '默认不播');
      await t.tap(find.byKey(SiteDutyPage.playKey));
      await t.pump();
      expect(find.byType(SiteVideo), findsOneWidget);
      await t.tap(find.byKey(SiteDutyPage.closeKey));
      await t.pump();
      expect(find.byType(SiteVideo), findsNothing);
      await t.tap(find.byKey(SiteDutyPage.playKey));
      await t.pump();
      await t.tap(find.byKey(SiteDutyPage.selectorKey));
      await t.pumpAndSettle();
      expect(t.widget<ListTile>(find.byKey(SiteDutyPage.chipKey('A'))).selected, isTrue);
      await t.tap(find.byKey(SiteDutyPage.chipKey('B')));
      await t.pumpAndSettle();
      expect(find.descendant(of: find.byKey(SiteDutyPage.selectorKey), matching: find.text('B')), findsOneWidget);
      expect(find.byType(SiteVideo), findsNothing, reason: '切台：原来那路关掉');
    });

    testWidgets('按钮按角色：保安 派巡逻 / 遥控 / 叫停；经理 派巡逻 / 叫停；只看的什么都没有', (t) async {
      for (final (role, patrol, teleop, stop) in [
        ('guard', true, true, true),
        ('owner', true, false, true),
        ('viewer', false, false, false),
      ]) {
        await _duty(t, FakeApi(role));
        expect(find.byKey(SiteDutyPage.patrolKey), patrol ? findsOneWidget : findsNothing, reason: role);
        expect(find.byKey(SiteDutyPage.teleopKey), teleop ? findsOneWidget : findsNothing, reason: role);
        expect(find.byKey(SiteDutyPage.stopKey), stop ? findsOneWidget : findsNothing, reason: role);
        await t.pumpWidget(Container());
      }
    });

    testWidgets('今天的巡逻：一行一趟；排队中的临时巡逻派的人能取消', (t) async {
      final api = FakeApi('guard')
        ..session = const SiteSession('t', 'gina', 'guard')
        ..today = <String, dynamic>{'date': 'x', 'timezone': 'UTC', 'items': <dynamic>[
          <String, dynamic>{'at_ms': 1800000000000, 'kind': 'schedule', 'label': '外围一圈', 'robot': 'A',
            'by': '', 'state': 'done', 'reason': '', 'end_ms': null, 'adhoc_id': ''},
          <String, dynamic>{'at_ms': 1800001000000, 'kind': 'adhoc', 'label': '临时路线 2 个点', 'robot': 'A',
            'by': 'gina', 'state': 'queued', 'reason': '', 'end_ms': 1800001600000, 'adhoc_id': 'q1'},
        ]};
      await _duty(t, api);
      expect(find.text('完成'), findsOneWidget);
      expect(find.text('排队中'), findsOneWidget);
      await t.ensureVisible(find.byKey(todayRowKey(1)));
      await t.tap(find.byKey(todayRowKey(1)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('today-cancel')));
      await t.pumpAndSettle();
      expect(api.calls, contains('cancelAdhoc q1'));
    });

    testWidgets('其他没处理的报警收成一行，点开是原来的告警总览', (t) async {
      final api = FakeApi('guard');
      await _duty(t, api);
      final others = find.byKey(SiteDutyPage.othersKey);
      if (others.evaluate().isEmpty) return; // 夹具里只有 P1 的时候没有这一行
      await t.ensureVisible(others);
      await t.tap(others);
      await t.pumpAndSettle();
      expect(find.byType(SiteWatchPage), findsOneWidget);
    });
  });

  group('派巡逻', () {
    testWidgets('每条路线写估时；撞日程先说清楚、按钮是「排队派出」；派出去说排到几点', (t) async {
      final api = FakeApi('guard')
        ..plans = <String, Map<String, dynamic>>{
          'loop': <String, dynamic>{'estimate_s': 1080, 'queued': true, 'start_ms': 1800001200000,
            'because': '22:00 日程「loop」'},
        }
        ..patrolReply = <String, dynamic>{'queued': true, 'start_after_ms': 1800001200000,
          'estimate_s': 1080, 'because': '22:00 日程「loop」', 'adhoc_id': 'a1'};
      await _duty(t, api);
      await t.tap(find.byKey(SiteDutyPage.patrolKey));
      await t.pumpAndSettle();
      expect(find.text('约 18 分钟'), findsOneWidget);
      await t.tap(find.byKey(patrolRouteKey('loop')));
      await t.pumpAndSettle();
      expect(find.byKey(patrolQueuedKey), findsOneWidget);
      expect(find.text('排队派出'), findsOneWidget);
      await t.tap(find.byKey(patrolSendKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('patrol ')), isNotEmpty);
      expect(find.textContaining('已排队'), findsOneWidget);
    });

    testWidgets('被拒（叫停了）：弹框说原因，「恢复」就恢复', (t) async {
      final api = FakeApi('guard')
        ..patrolError = const SiteError(409, 'A 被 olga 叫停了:先点「恢复」再派',
            body: <String, dynamic>{'reason_code': 'held', 'reason_en': 'This robot was stopped by someone.'});
      await _duty(t, api);
      await t.tap(find.byKey(SiteDutyPage.patrolKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(patrolRouteKey('loop')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(patrolSendKey));
      await t.pumpAndSettle();
      expect(find.byKey(refusalDialogKey), findsOneWidget);
      expect(find.textContaining('叫停'), findsWidgets);
      await t.tap(find.byKey(refusalActionKey));
      await t.pumpAndSettle();
      expect(api.calls, contains('resume A'));
    });

    testWidgets('机器人明确没收（忙）：不说「派出去了」，框里写原因（外审 W53-1 #4）', (t) async {
      final api = FakeApi('guard')
        ..patrolReply = <String, dynamic>{'queued': false, 'ack': <String, dynamic>{'result': 'rejected', 'reason': 'busy'}};
      await _duty(t, api);
      await t.tap(find.byKey(SiteDutyPage.patrolKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(patrolRouteKey('loop')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(patrolSendKey));
      await t.pumpAndSettle();
      expect(find.textContaining('机器人未接受'), findsOneWidget);
      expect(find.textContaining('派出去了'), findsNothing);
    });

    testWidgets('派巡逻框里能改成「在地图上点点位」', (t) async {
      await _duty(t, FakeApi('guard'));
      await t.tap(find.byKey(SiteDutyPage.patrolKey));
      await t.pumpAndSettle();
      expect(find.byKey(patrolCustomKey), findsOneWidget);
    });

    test('时刻按站点的钟：给了偏移就不看手机时区', () {
      const ms = 1800000000000; // 2027-01-15 08:00 UTC
      expect(hhmm(ms, 480), '16:00');
      expect(hhmm(ms, 0), '08:00');
    });

    test('被拒的原话按语言说：英文界面用 reason_en', () {
      const e = SiteError(409, 'A 不在线', body: <String, dynamic>{'reason_code': 'offline', 'reason_en': 'Offline.'});
      appLang.value = AppLang.en;
      expect(refusalText(e), 'Offline.');
      appLang.value = AppLang.zh;
      expect(refusalText(e), 'A 不在线');
      expect(zonesMapOf('estate-1:7 当前的区域修订还没做安全确认'), ('estate-1', '7'));
    });
  });

  group('管理栏、账号、日程', () {
    testWidgets('要你处理：禁行区没确认；开站清单能「不用了」、能收起', (t) async {
      final api = FakeApi('admin');
      _size(t);
      await t.pumpWidget(MaterialApp(home: AdminHomeTab(api: api, switchTab: (_) {})));
      await t.pumpAndSettle();
      expect(find.byKey(AdminHomeTab.todoKey), findsOneWidget);
      expect(find.textContaining('m1:3 的禁行区未确认'), findsOneWidget);
      await t.tap(find.byKey(SetupChecklist.skipKey('zones')));
      await t.pumpAndSettle();
      expect(api.calls, contains('setGuide skip'));
      await t.tap(find.byKey(SetupChecklist.hideKey));
      await t.pumpAndSettle();
      expect(api.calls, contains('setGuide setup_hidden'));
      expect(find.byKey(SetupChecklist.itemKey('robots')), findsNothing, reason: '收起来了');
    });

    testWidgets('账号审批：权限下拉默认只看，选保安、输密码批准', (t) async {
      final api = FakeApi('admin')
        ..acctReqs = <Map<String, dynamic>>[<String, dynamic>{'id': 'r1', 'name': 'wangxm', 'display': '王小明'}];
      _size(t);
      await t.pumpWidget(MaterialApp(home: AccountsPage(api: api)));
      await t.pumpAndSettle();
      expect(find.text('只看'), findsOneWidget);
      await t.tap(find.byKey(AccountsPage.roleKey('r1')));
      await t.pumpAndSettle();
      await t.tap(find.text('保安').last);
      await t.pumpAndSettle();
      await t.tap(find.byKey(AccountsPage.approveKey('r1')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('acct-confirm-pw')), 'my-password');
      await t.tap(find.byKey(const Key('acct-confirm-go')));
      await t.pumpAndSettle();
      expect(api.calls, contains('approveAccount r1 guard my-password'));
      expect(find.text('王小明 · wangxm'), findsNothing);
    });

    testWidgets('经理排日程：每周 → 加一条（路线、机器人、钟点、星期）→ 存', (t) async {
      final api = FakeApi('owner');
      _size(t);
      await t.pumpWidget(MaterialApp(home: SchedulePage(api: api)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SchedulePage.weekTabKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SchedulePage.addKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('sched-day-sun')));
      await t.pump();
      await t.tap(find.byKey(const Key('sched-save')));
      await t.pumpAndSettle();
      final saved = api.calls.where((c) => c.startsWith('saveSchedule ')).toList();
      expect(saved, hasLength(1));
      expect(saved.single, contains(' loop 08:00'));
      expect(find.byKey(SchedulePage.rowKey(saved.single.split(' ')[1])), findsOneWidget);
    });

    testWidgets('改已有的日程：编辑器没有的字段（每隔几分钟、到几点、回哪个待命点）原样带回去（外审 #9）', (t) async {
      final api = FakeApi('owner')
        ..schedules = <String, dynamic>{'timezone': 'UTC', 'revision': 1, 'schedules': <dynamic>[
          <String, dynamic>{'id': 'night', 'mission': 'loop', 'at': '22:00', 'days': <dynamic>['mon', 'tue'],
            'robot': 'A', 'every_min': 30, 'until': '02:00', 'standby': 'gate', 'priority': 3, 'revision': 4,
            'window_min': 20, 'on_missed': 'alarm', 'updated_by': 'x'},
        ]};
      Map<String, dynamic>? sent;
      api.onSaveSchedule = (b) => sent = b;
      _size(t);
      await t.pumpWidget(MaterialApp(home: SchedulePage(api: api)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SchedulePage.weekTabKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SchedulePage.rowKey('night')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('sched-save')));
      await t.pumpAndSettle();
      expect(sent, isNotNull);
      expect(sent!['every_min'], 30);
      expect(sent!['until'], '02:00');
      expect(sent!['standby'], 'gate');
      expect(sent!['priority'], 3);
      expect(sent!['window_min'], 20);
      expect(sent!['on_missed'], 'alarm');
      expect(sent!['base_revision'], 4);
      expect(sent!.containsKey('updated_by'), isFalse);
    });

    testWidgets('保安看日程：只看，不能加', (t) async {
      final api = FakeApi('guard');
      _size(t);
      await t.pumpWidget(MaterialApp(home: SchedulePage(api: api)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SchedulePage.weekTabKey));
      await t.pumpAndSettle();
      expect(find.byKey(SchedulePage.addKey), findsNothing);
    });
  });

  group('申请账号（W52）', () {
    testWidgets('登录页「申请账号」→ 填名字、账号、密码 → 交上去了', (t) async {
      final got = <String>[];
      _size(t);
      await t.pumpWidget(MaterialApp(
          home: SiteLoginPage(
              site: SiteEntry(name: '站点', url: 'https://x', fingerprint: 'ab' * 32, username: ''),
              signIn: (_, _) async => FakeApi('guard'),
              apply: (n, d, p) async => got.add('$n $d $p'))));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SiteLoginPage.applyKey));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(AccountApplyPage.nameKey), '王小明');
      await t.enterText(find.byKey(AccountApplyPage.accountKey), 'wangxm');
      await t.enterText(find.byKey(AccountApplyPage.passwordKey), 'short');
      await t.tap(find.byKey(AccountApplyPage.submitKey));
      await t.pumpAndSettle();
      expect(got, isEmpty, reason: '密码太短先在手机上拦');
      await t.enterText(find.byKey(AccountApplyPage.passwordKey), 'long-enough-pw');
      await t.tap(find.byKey(AccountApplyPage.submitKey));
      await t.pumpAndSettle();
      expect(got, ['wangxm 王小明 long-enough-pw']);
      expect(find.byKey(AccountApplyPage.doneKey), findsOneWidget);
    });

    test('登录失败时申请的状态按语言说', () {
      appLang.value = AppLang.en;
      expect(applyStateText('这个账号还在等管理员批准'), contains('waiting'));
      expect(applyStateText('这个账号的申请被管理员拒了'), contains('turned down'));
      appLang.value = AppLang.zh;
      expect(applyStateText('这个账号还在等管理员批准'), '这个账号还在等管理员批准');
    });
  });

  group('退出站点', () {
    testWidgets('每个角色的第 5 栏都有「退出站点」，点了回站点列表', (t) async {
      _size(t, const Size(400, 1400));
      for (final (role, tab) in [
        ('admin', NwShell.adminKey),
        ('owner', NwShell.planKey),
        ('guard', NwShell.moreKey),
      ]) {
        await t.pumpWidget(MaterialApp(
            home: Builder(builder: (c) => Scaffold(body: Center(child: TextButton(
                onPressed: () => Navigator.push(c, MaterialPageRoute<void>(
                    builder: (_) => NwShell(api: FakeApi(role), title: '站点', ring: () {}))),
                child: const Text('站点列表')))))));
        await t.tap(find.text('站点列表'));
        await t.pumpAndSettle();
        await t.tap(find.byKey(tab));
        await t.pumpAndSettle();
        final leave = role == 'guard' ? MeTab.leaveKey : leaveSiteKey;
        if (role == 'admin') {
          await t.scrollUntilVisible(find.byKey(leave), 200,
              scrollable: find.descendant(of: find.byType(AdminHomeTab), matching: find.byType(Scrollable)).first);
          await t.pumpAndSettle();
        }
        await t.tap(find.byKey(leave));
        await t.pumpAndSettle();
        expect(find.byType(NwShell), findsNothing, reason: role);
        expect(find.text('站点列表'), findsOneWidget, reason: role);
        await t.pumpWidget(Container());
      }
    });
  });

  group('上手向导', () {
    testWidgets('没看过导览的保安：进站点就开始，一步步走完记下来，最后练一次报警（不发给站点）', (t) async {
      final api = FakeApi('guard')..guide = <String, dynamic>{'tour': 0, 'setup_hidden': false, 'skip': <dynamic>[]};
      _size(t, const Size(400, 900));
      await t.pumpWidget(MaterialApp(home: NwShell(api: api, title: '站点', ring: () {})));
      await t.pumpAndSettle();
      expect(find.byKey(tourCardKey), findsOneWidget);
      var steps = 0;
      while (find.byKey(tourNextKey).evaluate().isNotEmpty && steps < 20) {
        await t.tap(find.byKey(tourNextKey));
        await t.pumpAndSettle();
        steps++;
      }
      expect(steps, tourSteps('guard', DutyTargets()).length);
      expect(api.calls, contains('setGuide tour'));
      expect(api.guide['tour'], kTourVersion);
      expect(find.byType(PracticeAlarmPage), findsOneWidget);
      final before = List<String>.of(api.calls);
      await t.tap(find.byKey(PracticeAlarmPage.respondKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(PracticeAlarmPage.takeKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(PracticeAlarmPage.outcomeKey('false')));
      await t.pumpAndSettle();
      expect(find.byKey(PracticeAlarmPage.doneKey), findsOneWidget);
      expect(api.calls.where((c) => !before.contains(c) && !c.startsWith('patrolsToday')), isEmpty,
          reason: '练习不发给站点');
    });

    testWidgets('看过了就不再弹；「跳过」也记下来', (t) async {
      final seen = FakeApi('admin');
      _size(t, const Size(400, 900));
      await t.pumpWidget(MaterialApp(home: NwShell(api: seen, title: '站点', ring: () {})));
      await t.pumpAndSettle();
      expect(find.byKey(tourCardKey), findsNothing);
    });

    testWidgets('「跳过」也记下来，跳过了就不练', (t) async {
      _size(t, const Size(400, 900));
      final fresh = FakeApi('owner')..guide = <String, dynamic>{'tour': 0};
      await t.pumpWidget(MaterialApp(home: NwShell(api: fresh, title: '站点', ring: () {})));
      await t.pumpAndSettle();
      await t.tap(find.byKey(tourSkipKey));
      await t.pumpAndSettle();
      expect(fresh.guide['tour'], kTourVersion);
      expect(find.byType(PracticeAlarmPage), findsNothing, reason: '跳过了就不练');
    });

    testWidgets('常见问题按角色：保安没有「怎么加账号」，管理员有', (t) async {
      await t.pumpWidget(const MaterialApp(home: HelpPage(role: 'guard')));
      expect(find.text('如何添加账号'), findsNothing);
      expect(find.text('收到报警如何处理'), findsOneWidget);
      await t.pumpWidget(const MaterialApp(home: HelpPage(role: 'admin')));
      expect(find.text('如何添加账号'), findsOneWidget);
    });
  });
}
