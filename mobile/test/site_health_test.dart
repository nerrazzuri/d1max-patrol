// C-B4：手机上的站点体检页、保存诊断包、登录时报 App 版本。
import 'dart:io';
import 'dart:typed_data';

import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_health.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/theme.dart';
import 'package:d1max_patrol/ui/widget/alarm_widgets.dart';
import 'package:d1max_patrol/ui/widget/bar_actions.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

void _portrait(WidgetTester t) {
  t.view.physicalSize = const Size(390 * 2, 844 * 2);
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

Future<List<(String, Uint8List)>> _open(WidgetTester t, FakeApi api, {String Function(String)? where}) async {
  _portrait(t);
  final saved = <(String, Uint8List)>[];
  await t.pumpWidget(MaterialApp(
      theme: proTheme(),
      home: SiteHealthPage(
          api: api,
          now: () => DateTime(2026, 10, 11, 2, 4, 9),
          save: (name, bytes) async {
            saved.add((name, bytes));
            return where == null ? '/downloads/$name' : where(name);
          })));
  await t.pumpAndSettle();
  return saved;
}

Map<String, dynamic> _check(bool ok, String detail) => <String, dynamic>{'ok': ok, 'detail': detail};

String _trailing(WidgetTester t, String check) =>
    (t.widget<ListTile>(find.byKey(SiteHealthPage.checkKey(check))).trailing as Text).data!;

void main() {
  test('版本号跟 pubspec 一致；登录时报的那一份：类别、版本、接口级别、系统', () {
    final line = File('pubspec.yaml').readAsLinesSync().firstWhere((l) => l.startsWith('version:'));
    expect(line.split(':')[1].trim().split('+').first, appVersion);
    expect(clientInfo(platform: 'android'),
        {'kind': 'app', 'version': appVersion, 'api_level': appApiLevel, 'platform': 'android'});
    expect(clientInfo()['platform'], Platform.operatingSystem);
  });

  test('诊断包的文件名带当地时间', () {
    expect(bundleName(DateTime(2026, 10, 11, 2, 4, 9)), 'd1max-support-20261011-020409.tar.gz');
    expect(bundleName(DateTime(2026, 1, 2, 13, 14, 15)), 'd1max-support-20260102-131415.tar.gz');
  });

  testWidgets('都正常：说各项都正常、站点版本；每一项写着正常，不上报警的形状；狗和登录着的 App 列出来', (t) async {
    final api = FakeApi('admin');
    await _open(t, api);
    final head = t.widget<ListTile>(find.byKey(SiteHealthPage.overallKey));
    expect((head.title as Text).data, '各项都正常');
    expect((head.subtitle as Text).data, '站点版本 1.4.0');
    expect(find.byType(PriorityShape), findsNothing);
    expect(_trailing(t, 'db'), '正常');
    expect(find.text('数据库 · 要紧'), findsOneWidget);
    expect(find.text('证书'), findsOneWidget);
    expect(find.text('站点盘还剩 62%'), findsOneWidget);
    expect(find.text('代理 1.4.0 · 级别 2 · 在线'), findsOneWidget);
    expect(find.text('手机 App 1.0.0 · android'), findsOneWidget);
    expect(find.text('太老'), findsNothing);
  });

  testWidgets('要紧的一项没过：整体说有要紧的问题；没过的排在前面（要紧的最前），写没过', (t) async {
    final api = FakeApi('admin')
      ..healthBody = <String, dynamic>{
        'ok': false, 'critical_ok': false, 'version': '1.4.0',
        'checks': <String, dynamic>{
          'backup': _check(false, '没配备份'),
          'broker': _check(false, 'MQTT 断了'),
          'certs': _check(true, 'x'),
          'new_thing': _check(true, 'y'),
        },
      };
    await _open(t, api);
    expect((t.widget<ListTile>(find.byKey(SiteHealthPage.overallKey)).title as Text).data, '站点有要紧的问题');
    final order = t.widgetList<ListTile>(find.byWidgetPredicate((w) => w is ListTile && '${w.key}'.contains('health-check-')))
        .map((w) => '${w.key}').toList();
    expect(order, [
      '${SiteHealthPage.checkKey('broker')}', '${SiteHealthPage.checkKey('backup')}',
      '${SiteHealthPage.checkKey('certs')}', '${SiteHealthPage.checkKey('new_thing')}',
    ]);
    expect(_trailing(t, 'broker'), '没过');
    expect(_trailing(t, 'backup'), '没过');
    expect(find.text('new_thing'), findsOneWidget, reason: '不认得的检查项原样摆');
    final shapes = t.widgetList<PriorityShape>(find.byType(PriorityShape)).map((s) => s.level).toList();
    expect(shapes, ['P1', 'P1', 'P2'], reason: '整体、要紧的没过是 P1 的形状；别的没过是 P2');
  });

  testWidgets('只有不要紧的没过：整体说有几项要看；是 P2 的形状', (t) async {
    final api = FakeApi('admin')
      ..healthBody = <String, dynamic>{
        'ok': false, 'critical_ok': true, 'version': '1',
        'checks': <String, dynamic>{'db': _check(true, 'ok'), 'push': _check(false, '没配')},
      };
    await _open(t, api);
    expect((t.widget<ListTile>(find.byKey(SiteHealthPage.overallKey)).title as Text).data, '有几项要看一下');
    expect(t.widgetList<PriorityShape>(find.byType(PriorityShape)).map((s) => s.level), ['P2', 'P2']);
  });

  testWidgets('狗的代理太老、App 的接口级别比站点要的低：各自标出来', (t) async {
    final api = FakeApi('admin')
      ..versionsBody = <String, dynamic>{
        'site': <String, dynamic>{'version': '2', 'min_app_level': 2},
        'robots': <dynamic>[
          <String, dynamic>{'robot_id': 'A', 'online': false, 'agent': '0.9', 'level': 1, 'verdict': 'agent_too_old'},
          <String, dynamic>{'robot_id': 'B', 'online': true, 'verdict': 'unknown'},
        ],
        'clients': <dynamic>[
          <String, dynamic>{'name': 'gina', 'kind': 'app', 'version': '1.0.0', 'api_level': 1},
          <String, dynamic>{'name': 'wang', 'kind': 'app', 'version': '2.0.0', 'api_level': 2},
          <String, dynamic>{'name': 'old', 'kind': 'app'},
          <String, dynamic>{'name': 'alice', 'kind': 'web', 'api_level': 0},
        ],
      };
    await _open(t, api);
    expect(find.text('狗太老'), findsOneWidget);
    expect(find.text('不清楚'), findsOneWidget);
    expect(find.text('代理 0.9 · 级别 1 · 离线'), findsOneWidget);
    expect(find.text('太老'), findsOneWidget, reason: '只有 gina 那一个：没报级别的、网页的都不算');
    expect(find.text('网页值班台'), findsOneWidget);
    expect(find.text('手机 App ?'), findsOneWidget);
  });

  testWidgets('体检读不到：说读不到，不显示各项；版本一览照样看。刷新后读到了就显示', (t) async {
    final api = FakeApi('admin')..healthError = const SiteError(502, '坏了');
    await _open(t, api);
    expect(find.textContaining('站点体检没读到'), findsOneWidget);
    expect(find.byKey(SiteHealthPage.overallKey), findsNothing);
    expect(find.text('狗上的软件'), findsOneWidget);
    api.healthError = null;
    await t.tap(find.byKey(SiteHealthPage.refreshKey));
    await t.pumpAndSettle();
    expect(find.textContaining('没读到'), findsNothing);
    expect(find.byKey(SiteHealthPage.overallKey), findsOneWidget);
  });

  testWidgets('读到过、后来读不到：不拿上一次的当现在的', (t) async {
    final api = FakeApi('admin');
    await _open(t, api);
    expect(find.byKey(SiteHealthPage.overallKey), findsOneWidget);
    api
      ..healthError = const SiteError(502, 'x')
      ..versionsError = const SiteError(502, 'x');
    await t.tap(find.byKey(SiteHealthPage.refreshKey));
    await t.pumpAndSettle();
    expect(find.byKey(SiteHealthPage.overallKey), findsNothing);
    expect(find.byKey(SiteHealthPage.checkKey('db')), findsNothing);
    expect(find.text('狗上的软件'), findsNothing);
    expect(find.textContaining('站点体检没读到'), findsOneWidget);
  });

  testWidgets('保存诊断包：取回来、按时间起名、存下去，说存到哪儿了、多大；存的时候按钮按不了', (t) async {
    final api = FakeApi('admin');
    final saved = await _open(t, api);
    await t.ensureVisible(find.byKey(SiteHealthPage.bundleKey));
    await t.tap(find.byKey(SiteHealthPage.bundleKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('support-bundle'));
    expect(saved.single.$1, 'd1max-support-20261011-020409.tar.gz');
    expect(saved.single.$2.length, 3000);
    expect(t.widget<SelectableText>(find.byKey(SiteHealthPage.msgKey)).data,
        '存好了（3 KB）：/downloads/d1max-support-20261011-020409.tar.gz');
    expect(t.widget<OutlinedButton>(find.byKey(SiteHealthPage.bundleKey)).onPressed, isNotNull);
  });

  testWidgets('诊断包取不到、盘上存不下：都说出来，不说存好了', (t) async {
    final api = FakeApi('admin')..bundleError = const SiteError(403, '只有管理员');
    final saved = await _open(t, api);
    await t.ensureVisible(find.byKey(SiteHealthPage.bundleKey));
    await t.tap(find.byKey(SiteHealthPage.bundleKey));
    await t.pumpAndSettle();
    expect(saved, isEmpty);
    expect(t.widget<SelectableText>(find.byKey(SiteHealthPage.msgKey)).data, contains('诊断包没存上'));
    await t.pumpWidget(Container());
    final api2 = FakeApi('admin');
    await _open(t, api2, where: (_) => throw const FileSystemException('盘满了'));
    await t.ensureVisible(find.byKey(SiteHealthPage.bundleKey));
    await t.tap(find.byKey(SiteHealthPage.bundleKey));
    await t.pumpAndSettle();
    final msg = t.widget<SelectableText>(find.byKey(SiteHealthPage.msgKey)).data!;
    expect(msg, contains('诊断包没存上：盘满了'));
    expect(msg, isNot(contains('存好了')));
  });

  testWidgets('入口：管理员的菜单里有「站点体检」，点了进去；保安、业主没有', (t) async {
    _portrait(t);
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(theme: proTheme(), home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(barMoreKey));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('open-health')));
    await t.pumpAndSettle();
    expect(find.byType(SiteHealthPage), findsOneWidget);
    for (final role in ['guard', 'owner']) {
      await t.pumpWidget(Container());
      await t.pumpWidget(MaterialApp(theme: proTheme(), home: SiteRobotsPage(api: FakeApi(role))));
      await t.pumpAndSettle();
      await t.tap(find.byKey(barMoreKey));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('open-health')), findsNothing, reason: role);
      expect(find.byKey(const Key('open-schedule')), findsOneWidget);
      await t.tapAt(const Offset(5, 5));
      await t.pumpAndSettle();
    }
  });
}
