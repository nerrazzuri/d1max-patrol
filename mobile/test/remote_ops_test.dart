// W55 远程运维（手机）：机器人运维页（诊断快照、日志按服务和时间段取、重启服务、重启整机再输密码、
// 机器人没报的能力不给按钮）；App 错误记录（最多 500 条、定时传、传不出去留着）；从站点更新 App
// （按构建号问、下载核对后交给系统装、名字不对不下载、登录后底部提示「更新」）。
import 'dart:io';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/app_update.dart';
import 'package:d1max_patrol/net/error_log.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/app_update_ui.dart';
import 'package:d1max_patrol/ui/robot_ops.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

Map<String, dynamic> _diag() => <String, dynamic>{
      'at_ms': 1,
      'release': <String, dynamic>{'current': 'v1.5.0'},
      'memory': <String, dynamic>{'MemTotal': 8 << 30, 'MemAvailable': 1 << 30},
      'disks': <String, dynamic>{'/': <String, dynamic>{'total': 100 << 30, 'used': 1, 'free': 40 << 30}},
      'time': <String, dynamic>{'synced': false},
      'services': <dynamic>[
        <String, dynamic>{'unit': 'd1max-agent.service', 'active': 'active', 'sub': 'running', 'restarts': 3},
        <String, dynamic>{'unit': 'd1max-nav2.service', 'active': '', 'sub': ''},
      ],
    };

Future<void> _open(WidgetTester t, FakeApi api, {Set<String> tasks = robotOpsAll}) async {
  appLang.value = AppLang.zh;
  await t.binding.setSurfaceSize(const Size(500, 1400));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(home: RobotOpsPage(api: api, robotId: 'A', tasks: tasks)));
}

void main() {
  tearDown(() => appLang.value = AppLang.en);

  test('诊断快照摘成人话：版本、内存、磁盘、时间同步、服务重启次数；没装的服务不列', () {
    appLang.value = AppLang.zh;
    final lines = Map<String, String>.fromEntries(diagLines(_diag()).map((e) => MapEntry(e.$1, e.$2)));
    expect(lines['软件版本'], 'v1.5.0');
    expect(lines['可用内存'], '1.0 GB / 8.0 GB');
    expect(lines['磁盘剩余（/）'], '40.0 GB / 100.0 GB');
    expect(lines['时间同步'], '未同步');
    expect(lines['d1max-agent.service'], 'active/running · 重启 3 次');
    expect(lines.containsKey('d1max-nav2.service'), isFalse);
  });

  testWidgets('诊断快照、日志按服务和时间段取', (t) async {
    final api = FakeApi('admin')
      ..opsReply = (a, _) => a == 'diag'
          ? _diag()
          : <String, dynamic>{'unit': 'd1max-agent', 'text': 'hello journal', 'truncated': true};
    await _open(t, api);
    await t.tap(find.byKey(RobotOpsPage.diagKey));
    await t.pumpAndSettle();
    expect(find.text('v1.5.0'), findsOneWidget);
    await t.tap(find.byKey(RobotOpsPage.logKey));
    await t.pumpAndSettle();
    expect(find.text('hello journal'), findsOneWidget);
    expect(find.textContaining('仅显示最近一部分'), findsOneWidget);
    expect(api.opsCalls.last.$2, 'journal');
    expect(api.opsCalls.last.$4, <String, String>{'unit': 'd1max-agent', 'hours': '6'});
  });

  testWidgets('重启整机要再输密码，带上 confirm_password；取消就不发', (t) async {
    final api = FakeApi('admin');
    await _open(t, api);
    await t.tap(find.byKey(RobotOpsPage.rebootKey));
    await t.pumpAndSettle();
    await t.tap(find.text('取消'));
    await t.pumpAndSettle();
    expect(api.opsCalls, isEmpty);
    await t.tap(find.byKey(RobotOpsPage.rebootKey));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(RobotOpsPage.rebootPwKey), 'my-pw');
    await t.tap(find.byKey(RobotOpsPage.rebootGoKey));
    await t.pumpAndSettle();
    expect(api.opsCalls.single.$2, 'reboot');
    expect(api.opsCalls.single.$3, <String, dynamic>{'confirm_password': 'my-pw'});
    expect(find.textContaining('机器人正在重启'), findsOneWidget);
  });

  testWidgets('机器人忙、拒绝：把原因写出来', (t) async {
    final api = FakeApi('admin')..opsError = SiteError(409, '机器人未执行:busy');
    await _open(t, api);
    await t.tap(find.byKey(RobotOpsPage.timesyncKey));
    await t.pumpAndSettle();
    expect(find.textContaining('busy'), findsOneWidget);
  });

  testWidgets('重启服务带上选的服务名', (t) async {
    final api = FakeApi('admin');
    await _open(t, api);
    await t.tap(find.byKey(const Key('ops-unit')));
    await t.pumpAndSettle();
    await t.tap(find.text('d1max-localizer').last);
    await t.pumpAndSettle();
    await t.tap(find.byKey(RobotOpsPage.restartKey));
    await t.pumpAndSettle();
    await t.tap(find.text('重启'));
    await t.pumpAndSettle();
    expect(api.opsCalls.single.$3, <String, dynamic>{'unit': 'd1max-localizer'});
  });

  testWidgets('机器人只报了诊断：没有重启、日志这些按钮', (t) async {
    await _open(t, FakeApi('admin'), tasks: const <String>{'ops_diag'});
    expect(find.byKey(RobotOpsPage.diagKey), findsOneWidget);
    for (final k in <Key>[RobotOpsPage.rebootKey, RobotOpsPage.restartKey, RobotOpsPage.logKey, RobotOpsPage.certKey]) {
      expect(find.byKey(k), findsNothing);
    }
  });

  group('错误记录', () {
    test('最多留 max 条，丢最老的；传成功清掉，传不出去留着', () async {
      var now = 0;
      final log = ErrorLog(max: 3, now: () => ++now);
      for (var i = 0; i < 5; i++) {
        log.note('flutter', 'e$i');
      }
      expect(log.pending.map((e) => e['message']), <String>['e2', 'e3', 'e4']);
      expect(log.pending.first['app_version'], '$appVersion+$appBuild');
      final api = FakeApi('admin');
      await log.flush();                                     // 没接站点：不传
      expect(api.errorReports, isEmpty);
      log.attach(api);
      await log.flush();
      expect(api.errorReports.single.length, 3);
      expect(log.pending, isEmpty);
      log.note('api', 'x');
      api.session = null;                                    // 退出登录了：留着
      await log.flush();
      expect(log.pending.length, 1);
      log.detach(api);
    });
  });

  group('从站点更新 App', () {
    test('按本机构建号问；下载到目录后交给系统装；名字不对不下载', () async {
      final dir = await Directory.systemTemp.createTemp('upd');
      addTearDown(() => dir.delete(recursive: true));
      final api = FakeApi('guard')
        ..appUpdateBody = <String, dynamic>{'available': true, 'version': '1.1.0+5', 'build': 5, 'size': 3,
          'name': 'nw-1.1.0.apk', 'sha256': 'a' * 64}
        ..appBytes = <int>[1, 2, 3];
      File? installed;
      final u = AppUpdater(api: api, platform: 'android', dir: () async => dir, install: (f) async {
        installed = f;
        return 'ok';
      });
      final info = await u.check();
      expect(api.appUpdateAsks.single, ('android', appBuild));
      expect(await u.downloadAndInstall(info!), 'ok');
      expect(installed!.path, '${dir.path}${Platform.pathSeparator}nw-1.1.0.apk');
      expect(await installed!.readAsBytes(), <int>[1, 2, 3]);
      await expectLater(u.downloadAndInstall(<String, dynamic>{...info, 'name': '../evil.apk'}),
          throwsA(isA<SiteError>()));
    });

    testWidgets('有新版：底部提示，点「更新」装；没有就什么都不显示', (t) async {
      appLang.value = AppLang.zh;
      final api = FakeApi('guard');
      var asked = 0;
      final u = AppUpdater(api: api, platform: 'linux', dir: () async => Directory.systemTemp, install: (f) async {
        asked++;
        return '已保存';
      });
      await t.pumpWidget(MaterialApp(home: Scaffold(body: Builder(builder: (c) {
        return TextButton(onPressed: () => offerAppUpdate(c, u), child: const Text('go'));
      }))));
      await t.tap(find.text('go'));
      await t.pumpAndSettle();
      expect(find.byType(SnackBar), findsNothing);
      api.appUpdateBody = <String, dynamic>{'available': true, 'version': '1.1.0+5', 'build': 5, 'size': 1,
        'name': 'nw-linux.tar.gz', 'sha256': 'b' * 64};
      api.appBytes = <int>[9];
      await t.tap(find.text('go'));
      await t.pumpAndSettle();
      expect(find.text('有新版本 App：1.1.0+5'), findsOneWidget);
      await t.tap(find.byKey(appUpdateActionKey));
      for (var i = 0; i < 20 && asked == 0; i++) {
        await t.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
        await t.pump(const Duration(milliseconds: 50));
      }
      await t.pump(const Duration(milliseconds: 500));
      expect(asked, 1);
      expect(find.text('已保存'), findsOneWidget);
      await t.runAsync(() => File('${Directory.systemTemp.path}/nw-linux.tar.gz').delete());
    });
  });
}
