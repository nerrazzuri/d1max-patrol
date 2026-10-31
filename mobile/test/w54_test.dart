// W54 建图向导（用户 2026-10-31）：站点没图时管理员登录弹「开始建图」（稍后记下来）、别的角色只看到提示；
// 向导七步从头走到尾（遥控器、手机两种开法）、建图结论三档、退出能接着做；地图管理（版本、结论、下发、退回）。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/map_manage.dart';
import 'package:d1max_patrol/ui/map_wizard.dart';
import 'package:d1max_patrol/ui/nw/nw_shell.dart';
import 'package:d1max_patrol/ui/site_duty_page.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart' show siteFixture;

void _size(WidgetTester t, [Size size = const Size(400, 1000)]) {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

/// 机器人 A：在线、电量 [battery]、能边走边建；[map] 是它载着的图。
Map<String, dynamic> _robotA({int battery = 80, String map = 'estate-1:7', bool control = true}) {
  final r = Map<String, dynamic>.from(siteFixture('site_robot'));
  final caps = Map<String, dynamic>.from(r['capabilities'] as Map);
  final tasks = Map<String, dynamic>.from(caps['tasks'] as Map);
  final parts = map.split(':');
  tasks['patrol'] = <String, dynamic>{...(tasks['patrol'] as Map), 'map_id': parts[0], 'map_version': parts[1]};
  caps['tasks'] = tasks;
  return <String, dynamic>{
    ...r,
    'robot_id': 'A',
    'revoked': false,
    'fresh': true,
    'battery_pct': battery,
    'capabilities': caps,
    'status': <String, dynamic>{
      'online': true,
      'task': null,
      'ready': <String, dynamic>{'control': control, 'motion': true, 'estop_clear': true, 'loc_ok': true},
    },
  };
}

FakeApi _admin() {
  final a = _robotA();
  return FakeApi('admin', robotView: a)
    ..robotsOverride = [a]
    ..mapsCount = 0;
}

Map<String, dynamic> _pv({required List<List<double>> trail, bool recording = true}) => <String, dynamic>{
      'live': true, 'recording': recording, 'run': 'map-20261031-1', 'seq': 1, 'res': 0.1,
      'origin': <double>[-5, -5], 'width': 100, 'height': 100,
      'pose': <double>[...trail.last, 0], 'trail': trail,
    };

Future<void> _wizard(WidgetTester t, FakeApi api) async {
  _size(t);
  await t.pumpWidget(MaterialApp(
      home: Builder(
          builder: (ctx) => Scaffold(
              body: Center(
                  child: TextButton(onPressed: () => openMapWizard(ctx, api), child: const Text('open')))))));
  await t.tap(find.text('open'));
  await t.pumpAndSettle();
}

Future<void> _tap(WidgetTester t, Key k) async {
  await t.ensureVisible(find.byKey(k));
  await t.pumpAndSettle();
  await t.tap(find.byKey(k));
  await t.pumpAndSettle();
}

/// 点了以后页面上有一直转的（建图在等、遥控页）：不等静止，推几帧。
Future<void> _tapLive(WidgetTester t, Key k) async {
  await t.tap(find.byKey(k));
  for (var i = 0; i < 5; i++) {
    await t.pump(const Duration(milliseconds: 200));
  }
}

/// 等向导的定时器转一圈（建图那一步问站点、原点那一步问机器人）。
Future<void> _tick(WidgetTester t) async {
  await t.pump(const Duration(seconds: 3));
  await t.pumpAndSettle();
}

void main() {
  setUp(() => appLang.value = AppLang.zh);

  group('入口', () {
    testWidgets('管理员、站点没图：导览走完弹「开始建图」；点稍后记在账号上，下次不弹', (t) async {
      _size(t, const Size(400, 900));
      final api = _admin();
      await t.pumpWidget(MaterialApp(home: NwShell(api: api, title: '站点', ring: () {})));
      await t.pumpAndSettle();
      expect(find.byKey(mapOfferKey), findsOneWidget);
      await t.tap(find.byKey(mapOfferLaterKey));
      await t.pumpAndSettle();
      expect(api.guide['map_later'], isTrue);
      await t.pumpWidget(Container());
      await t.pumpWidget(MaterialApp(home: NwShell(api: api, title: '站点', ring: () {})));
      await t.pumpAndSettle();
      expect(find.byKey(mapOfferKey), findsNothing, reason: '点过稍后');
      expect(find.byKey(SiteDutyPage.mapWizardKey), findsOneWidget, reason: '值守页上那一行还在，能从这儿开');
    });

    testWidgets('有没做完的向导：改问「接着做」，稍后不记', (t) async {
      _size(t, const Size(400, 900));
      final api = _admin()
        ..guide = <String, dynamic>{'tour': 99, 'map_later': true}
        ..wizard = <String, dynamic>{'step': 'walk', 'robot_id': 'A', 'map_id': 'main', 'version': '20261031-1',
          'drive': 'remote', 'by': 'eng-1', 'label': ''};
      await t.pumpWidget(MaterialApp(home: NwShell(api: api, title: '站点', ring: () {})));
      await t.pumpAndSettle();
      expect(find.textContaining('第 3 步'), findsWidgets);
      await t.tap(find.byKey(mapOfferStartKey));
      await t.pumpAndSettle();
      expect(find.byType(MapWizardPage), findsOneWidget);
      expect(find.byKey(MapWizardPage.doneWalkKey), findsOneWidget, reason: '直接回到走一圈那一步');
    });

    testWidgets('保安：不弹窗，值守页一行「请管理员建图」，没有按钮；站点有图就什么都没有', (t) async {
      _size(t, const Size(400, 900));
      final guard = FakeApi('guard')..mapsCount = 0;
      await t.pumpWidget(MaterialApp(home: NwShell(api: guard, title: '站点', ring: () {})));
      await t.pumpAndSettle();
      expect(find.byKey(mapOfferKey), findsNothing);
      expect(find.byKey(SiteDutyPage.noMapKey), findsOneWidget);
      expect(find.textContaining('请管理员'), findsOneWidget);
      expect(find.byKey(SiteDutyPage.mapWizardKey), findsNothing);
      await t.pumpWidget(Container());
      await t.pumpWidget(MaterialApp(home: SiteDutyPage(api: FakeApi('guard'), title: '站点')));
      await t.pumpAndSettle();
      expect(find.byKey(SiteDutyPage.noMapKey), findsNothing);
    });
  });

  group('向导', () {
    testWidgets('遥控器开：七步走完 —— 起地图号版本、开录带版本、走完停录、能用、没有禁行区、下发、标原点', (t) async {
      final api = _admin();
      await _wizard(t, api);
      expect(find.text('1 / 7'), findsOneWidget);
      expect(find.byKey(MapWizardPage.checksKey), findsOneWidget);
      // 室外、玻璃多点不了
      await _tap(t, MapWizardPage.siteTypeKey('outdoor'));
      await t.enterText(find.byKey(MapWizardPage.labelKey), '一楼');
      await _tap(t, MapWizardPage.nextKey);
      expect(api.calls, containsAllInOrder(['wizard start remote', 'wizard step drive']));
      expect(api.wizard!['site_type'], 'indoor');
      expect(api.wizard!['label'], '一楼');
      expect(find.text('2 / 7'), findsOneWidget);
      expect(find.textContaining('低速'), findsWidgets, reason: '遥控器要放低速档');

      api.previewReplies.addAll([
        _pv(trail: [[0, 0]]),
        for (var i = 0; i < 6; i++) _pv(trail: [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0.5]]),
      ]);
      await _tap(t, MapWizardPage.nextKey);
      expect(api.calls, containsAllInOrder(['wizard step walk remote', 'mapping A start map-20261031-1 main:20261031-1']));
      expect(find.text('3 / 7'), findsOneWidget);
      await _tick(t);
      expect(find.byKey(MapWizardPage.controlKey), findsOneWidget);
      expect(find.textContaining('已回到起点'), findsOneWidget, reason: '走了 40 米、回到起点：提示可以点走完了');

      await _tap(t, MapWizardPage.doneWalkKey);
      await _tapLive(t, MapWizardPage.doneWalkOkKey);
      expect(api.calls, containsAllInOrder(['mapping A stop ', 'wizard step build']));
      expect(find.byKey(MapWizardPage.buildStatusKey), findsOneWidget, reason: '还在建');

      api.wizardBuild = <String, dynamic>{'state': 'ready', 'quality': <String, dynamic>{
        'verdict': 'usable', 'reasons': <dynamic>[], 'closures': 1, 'path_m': 40.0, 'max_shift_m': 0.05}};
      await _tick(t);
      expect(find.text('地图已建成：可用'), findsOneWidget);
      expect(api.calls, contains('mapPreviewPng main:20261031-1'));
      await _tap(t, MapWizardPage.useKey);
      expect(api.calls, contains('wizard step zones'));

      expect(find.text('5 / 7'), findsOneWidget);
      expect(t.widget<FilledButton>(find.byKey(MapWizardPage.nextKey)).onPressed, isNull, reason: '没确认不能往下');
      await _tap(t, MapWizardPage.zonesEmptyKey);
      expect(api.calls, contains('confirmZones main:20261031-1 rev=0'));
      expect(api.lastEmptyOk, isTrue);
      await _tap(t, MapWizardPage.nextKey);
      expect(api.calls, contains('wizard step origin'));

      expect(find.text('6 / 7'), findsOneWidget);
      await _tap(t, MapWizardPage.sendMapKey);
      expect(api.calls, contains('map A main:20261031-1 without_home'));
      api.view = _robotA(map: 'main:20261031-1');
      await _tick(t);
      await _tap(t, MapWizardPage.markHomeKey);
      expect(api.calls, containsAllInOrder(['markHome A', 'wizard finish']));
      expect(find.text('可以开始巡逻了'), findsOneWidget);
      expect(find.textContaining('一楼'), findsWidgets);
      await _tap(t, MapWizardPage.homeKey);
      expect(find.byType(MapWizardPage), findsNothing);
    });

    testWidgets('有问题：说原因，主按钮是重走（换新版本、旧的留着），也能先用', (t) async {
      final api = _admin()
        ..wizard = <String, dynamic>{'step': 'build', 'robot_id': 'A', 'map_id': 'main', 'version': '20261031-1',
          'drive': 'remote', 'by': 'u', 'label': ''}
        ..wizardBuild = <String, dynamic>{'state': 'ready', 'quality': <String, dynamic>{
          'verdict': 'problem', 'reasons': <dynamic>['no_loop', 'short_path'], 'path_m': 9.0}};
      await _wizard(t, api);
      expect(find.text('地图已建成：有问题'), findsOneWidget);
      expect(find.textContaining('未回到起点闭合'), findsOneWidget);
      expect(find.textContaining('行走距离过短'), findsOneWidget);
      expect(t.widget<FilledButton>(find.byKey(MapWizardPage.rewalkKey)), isNotNull, reason: '重走是实底主按钮');
      await _tap(t, MapWizardPage.rewalkKey);
      expect(api.calls, contains('wizard rewalk'));
      expect(api.wizard!['version'], '20261031-2');
      expect(find.text('3 / 7'), findsOneWidget);
      expect(api.calls, contains('mapping A start map-20261031-2 main:20261031-2'), reason: '重走：换了版本、接着就开录');
    });

    testWidgets('走到一半机器人重启、这一趟自己收尾建好了：不卡在走一圈，给「看建图结果」（演示站点 10-31 撞上的）', (t) async {
      final api = _admin()
        ..wizard = <String, dynamic>{'step': 'walk', 'robot_id': 'A', 'map_id': 'main', 'version': '20261031-1',
          'drive': 'phone', 'by': 'u', 'label': ''}
        ..wizardBuild = <String, dynamic>{'state': 'ready', 'quality': <String, dynamic>{
          'verdict': 'problem', 'reasons': <dynamic>['short_path'], 'path_m': 0.0}};
      await _wizard(t, api);
      expect(find.byKey(MapWizardPage.walkBuiltKey), findsOneWidget);
      expect(find.byKey(MapWizardPage.startRecKey), findsNothing, reason: '这一版已经有了，不能再录同一版');
      await _tap(t, MapWizardPage.walkBuiltKey);
      expect(api.calls, contains('wizard step build'));
      expect(find.text('地图已建成：有问题'), findsOneWidget);
    });

    testWidgets('没建成：写原因，只能重走', (t) async {
      final api = _admin()
        ..wizard = <String, dynamic>{'step': 'build', 'robot_id': 'A', 'map_id': 'main', 'version': '20261031-1',
          'drive': 'remote', 'by': 'u', 'label': ''}
        ..wizardBuild = <String, dynamic>{'state': 'failed', 'reason': 'MappingError: 录包是空的'};
      await _wizard(t, api);
      expect(find.text('建图失败'), findsOneWidget);
      expect(find.textContaining('录包是空的'), findsOneWidget);
      expect(find.byKey(MapWizardPage.useKey), findsNothing);
      expect(find.byKey(MapWizardPage.rewalkKey), findsOneWidget);
    });

    testWidgets('手机开：遥控页角上有小预览和「走完了」，点了回来停录建图', (t) async {
      final api = _admin()
        ..wizard = <String, dynamic>{'step': 'walk', 'robot_id': 'A', 'map_id': 'main', 'version': '20261031-1',
          'drive': 'phone', 'by': 'u', 'label': ''};
      api.previewReplies.addAll([for (var i = 0; i < 10; i++) _pv(trail: [[0, 0], [3, 0]])]);
      await _wizard(t, api);
      await _tick(t);
      await t.ensureVisible(find.byKey(MapWizardPage.teleopKey));
      await t.tap(find.byKey(MapWizardPage.teleopKey));
      for (var i = 0; i < 5; i++) {
        await t.pump(const Duration(milliseconds: 200)); // 遥控页一直有动画：不等静止
      }
      expect(find.byType(SiteTeleopPage), findsOneWidget);
      expect(find.byKey(SiteTeleopPage.overlayKey), findsOneWidget);
      expect(find.byKey(MappingPreviewView.canvasKey), findsOneWidget);
      await t.tap(find.byKey(const Key('teleop-done-walk')));
      for (var i = 0; i < 5; i++) {
        await t.pump(const Duration(milliseconds: 200));
      }
      expect(find.textContaining('距起点 3 米'), findsOneWidget, reason: '没走回起点：提醒一下');
      await _tapLive(t, MapWizardPage.doneWalkOkKey);
      expect(api.calls, containsAllInOrder(['mapping A stop ', 'wizard step build']));
    });

    testWidgets('站点说室外还不支持：写成人话；退出时问先停下还是不建了', (t) async {
      final api = _admin()
        ..wizard = <String, dynamic>{'step': 'drive', 'robot_id': 'A', 'map_id': 'main', 'version': '20261031-1',
          'drive': 'remote', 'by': 'u', 'label': '', 'site_type': 'indoor'};
      await _wizard(t, api);
      await _tap(t, MapWizardPage.exitKey);
      await _tap(t, MapWizardPage.exitPauseKey);
      expect(find.byType(MapWizardPage), findsNothing);
      expect(api.wizard, isNotNull, reason: '先停下：站点上的向导留着');
      await t.tap(find.text('open'));
      await t.pumpAndSettle();
      expect(find.text('2 / 7'), findsOneWidget, reason: '继续');
      await _tap(t, MapWizardPage.exitKey);
      await _tap(t, MapWizardPage.exitAbandonKey);
      expect(api.calls, contains('wizard abandon'));
      expect(api.wizard, isNull);
    });

    testWidgets('电量不够：检查那里写原因，下一步按不了', (t) async {
      final low = _robotA(battery: 30);
      final api = FakeApi('admin', robotView: low)
        ..robotsOverride = [low]
        ..mapsCount = 0;
      await _wizard(t, api);
      expect(find.textContaining('先充电'), findsOneWidget);
      expect(t.widget<FilledButton>(find.byKey(MapWizardPage.nextKey)).onPressed, isNull);
    });
  });

  group('外审 W54-1', () {
    Map<String, dynamic> at(String step) => <String, dynamic>{'id': 'w-9', 'step': step, 'robot_id': 'A',
          'map_id': 'main', 'version': '20261031-1', 'drive': 'remote', 'by': 'u', 'label': ''};

    testWidgets('每一步都带这份向导的编号（被别人换掉的旧页面改不了新的）', (t) async {
      final api = _admin()..wizard = at('drive');
      await _wizard(t, api);
      await _tap(t, MapWizardPage.backKey);
      expect(api.wizardBodies.last['wizard_id'], 'w-9');
    });

    testWidgets('换了图、机器人还不知道自己在哪：标原点按不了，给「设位置」（能自己找的让它找）', (t) async {
      final lost = _robotA(map: 'main:20261031-1');
      final caps = Map<String, dynamic>.from(lost['capabilities'] as Map);
      caps['tasks'] = <String, dynamic>{...(caps['tasks'] as Map), 'relocalize': <String, dynamic>{'global': true}};
      lost['capabilities'] = caps;
      (lost['status'] as Map)['ready'] = <String, dynamic>{'control': true, 'motion': true, 'estop_clear': true, 'loc_ok': false};
      final api = FakeApi('admin', robotView: lost)
        ..robotsOverride = [lost]
        ..mapsCount = 0
        ..wizard = at('origin');
      await _wizard(t, api);
      expect(t.widget<FilledButton>(find.byKey(MapWizardPage.markHomeKey)).onPressed, isNull);
      await _tap(t, MapWizardPage.relocKey);
      expect(api.calls, contains('relocalize A global'));
    });

    testWidgets('原点在旧版本上登记过：问一句，换就带 replace 再标', (t) async {
      final api = FakeApi('admin', robotView: _robotA(map: 'main:20261031-1'))
        ..robotsOverride = [_robotA(map: 'main:20261031-1')]
        ..mapsCount = 0
        ..wizard = at('origin')
        ..markHomeError = const SiteError(409, 'home 已经在 main:20261030-1 上登记了',
            body: <String, dynamic>{'name_taken': <String, dynamic>{'map_id': 'main', 'map_version': '20261030-1'}});
      await _wizard(t, api);
      await _tap(t, MapWizardPage.markHomeKey);
      await _tap(t, MapWizardPage.replaceHomeKey);
      expect(api.calls, containsAllInOrder(['markHome A', 'markHome A replace', 'wizard finish']));
    });

    testWidgets('「不建了」：在走一圈就先停录（不管预览回没回来）；机器人不停就不放弃', (t) async {
      final api = _admin()
        ..wizard = at('walk')
        ..mappingAck = <String, dynamic>{'result': 'rejected', 'reason': 'busy'};
      await _wizard(t, api);
      await _tap(t, MapWizardPage.exitKey);
      await _tap(t, MapWizardPage.exitAbandonKey);
      expect(api.calls, contains('mapping A stop '));
      expect(api.calls.where((c) => c == 'wizard abandon'), isEmpty, reason: '没停下来不放弃');
      expect(find.byType(MapWizardPage), findsOneWidget);
      api.mappingAck = <String, dynamic>{'result': 'rejected', 'reason': 'not_recording'};
      await _tap(t, MapWizardPage.exitKey);
      await _tap(t, MapWizardPage.exitAbandonKey);
      expect(api.calls, contains('wizard abandon'), reason: '本来就没在录：放弃');
    });

    testWidgets('说不清（没有建图记录）：不显示成绿色「能用」', (t) async {
      final api = _admin()
        ..wizard = at('build')
        ..wizardBuild = <String, dynamic>{'state': 'ready', 'quality': <String, dynamic>{'verdict': 'unknown', 'reasons': <dynamic>[]}};
      await _wizard(t, api);
      expect(find.text('地图已建成：可用'), findsNothing);
      expect(find.textContaining('无法评估'), findsWidgets);
      expect(find.byKey(MapWizardPage.useKey), findsOneWidget);
    });

    testWidgets('换图没成：说原因，能再下发', (t) async {
      final api = _admin()
        ..wizard = at('origin')
        ..overview = <String, dynamic>{'maps': <dynamic>[], 'robots': <dynamic>[
          <String, dynamic>{'robot_id': 'A', 'activation': <String, dynamic>{'map_id': 'main', 'version': '20261031-1',
            'state': 'failed', 'reason': '下载的 prior.mm 校验不对'}},
        ]};
      await _wizard(t, api);
      await _tap(t, MapWizardPage.sendMapKey);
      await _tick(t);
      expect(find.textContaining('prior.mm 校验不对'), findsOneWidget);
      expect(find.byKey(MapWizardPage.sendMapKey), findsOneWidget, reason: '能再下发');
    });
  });

  testWidgets('走的时候的提示按规则', (t) async {
    expect(walkTip(pathM: 5, fromStartM: 5, elapsed: Duration.zero, speed: 0.9), contains('请减速'));
    expect(walkTip(pathM: 5, fromStartM: 5, elapsed: Duration.zero, turn: 1.2), contains('放慢转向'));
    expect(walkTip(pathM: 30, fromStartM: 8, elapsed: Duration.zero), contains('回到起点'));
    expect(walkTip(pathM: 30, fromStartM: 0.5, elapsed: Duration.zero), contains('结束行走'));
    expect(walkTip(pathM: 80, fromStartM: 8, elapsed: const Duration(minutes: 45)), contains('较长时间'));
    expect(walkTip(pathM: 1, fromStartM: 0, elapsed: Duration.zero), contains('缓慢行走'));
  });

  testWidgets('地图管理：每一版写结论、谁在用；在用的、最新的不能删；能把机器人退回上一版', (t) async {
    _size(t);
    final api = FakeApi('admin')
      ..overview = <String, dynamic>{
        'maps': <dynamic>[
          <String, dynamic>{'map_id': 'main', 'versions': <dynamic>[
            <String, dynamic>{'version': '20261031-2', 'note': '一楼', 'created_ms': 0, 'latest': true,
              'loaded_on': <dynamic>['A'], 'quality': <String, dynamic>{'verdict': 'usable', 'reasons': <dynamic>[], 'path_m': 186.0}},
            <String, dynamic>{'version': '20261031-1', 'note': '', 'created_ms': 0, 'latest': false,
              'loaded_on': <dynamic>[], 'quality': <String, dynamic>{'verdict': 'problem', 'reasons': <dynamic>['no_loop'], 'path_m': 42.0}},
          ]},
        ],
        'robots': <dynamic>[
          <String, dynamic>{'robot_id': 'A', 'can_activate': true,
            'loaded': <String, dynamic>{'map_id': 'main', 'version': '20261031-2'},
            'rollback': <String, dynamic>{'map_id': 'main', 'version': '20261031-1'}},
        ],
      };
    await t.pumpWidget(MaterialApp(home: MapManagePage(api: api)));
    await t.pumpAndSettle();
    expect(find.text('能用'), findsOneWidget);
    expect(find.text('有问题'), findsOneWidget);
    expect(find.text('A 在用'), findsOneWidget);
    expect(find.textContaining('未回到起点闭合'), findsOneWidget);
    expect(find.byKey(MapManagePage.deleteKey('main', '20261031-2')), findsNothing);
    expect(find.byKey(MapManagePage.deleteKey('main', '20261031-1')), findsOneWidget);
    await _tap(t, MapManagePage.rollbackKey('A'));
    await t.tap(find.byKey(const Key('rollback-ok')));
    await t.pumpAndSettle();
    expect(api.calls, contains('rollback A'));
    await _tap(t, MapManagePage.sendKey('main', '20261031-1'));
    await t.tap(find.byKey(const Key('send-to-A')));
    await t.pumpAndSettle();
    expect(api.calls, contains('map A main:20261031-1'));
  });
}
