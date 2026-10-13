// C-B4：手机上的站点体检页、保存诊断包、登录时报 App 版本。
import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_health.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/widget/alarm_widgets.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show MissingPluginException;
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
      theme: nwTheme(Brightness.dark),
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

  for (final (name, err) in <(String, Object)>[
    ('连不上', const SocketException('Connection reset by peer')),
    ('超时', TimeoutException('no reply')),
    ('握手失败', const HandshakeException('bad cert')),
    ('中途断了', const HttpException('Connection closed while receiving data')),
    ('别的什么错', StateError('boom')),
  ]) {
    testWidgets('外审 I1 取诊断包时$name（没包成 SiteError 的）：说没存上，按钮放开，再按一次能存', (t) async {
      final api = FakeApi('admin')..bundleError = err;
      final saved = await _open(t, api);
      await t.ensureVisible(find.byKey(SiteHealthPage.bundleKey));
      await t.tap(find.byKey(SiteHealthPage.bundleKey));
      await t.pumpAndSettle();
      expect(t.takeException(), isNull, reason: '错不许漏出页面');
      expect(saved, isEmpty);
      expect(t.widget<SelectableText>(find.byKey(SiteHealthPage.msgKey)).data, startsWith('诊断包没存上'));
      expect(find.text('正在存…'), findsNothing);
      expect(t.widget<OutlinedButton>(find.byKey(SiteHealthPage.bundleKey)).onPressed, isNotNull);
      api.bundleError = null;
      await t.tap(find.byKey(SiteHealthPage.bundleKey));
      await t.pumpAndSettle();
      expect(saved.length, 1);
      expect(t.widget<SelectableText>(find.byKey(SiteHealthPage.msgKey)).data, startsWith('存好了'));
    });
  }

  testWidgets('外审 I1 找存盘目录的插件坏了（不是文件错）：一样说没存上、按钮放开', (t) async {
    final api = FakeApi('admin');
    await _open(t, api, where: (_) => throw MissingPluginException('no path_provider'));
    await t.ensureVisible(find.byKey(SiteHealthPage.bundleKey));
    await t.tap(find.byKey(SiteHealthPage.bundleKey));
    await t.pumpAndSettle();
    expect(t.takeException(), isNull);
    expect(t.widget<SelectableText>(find.byKey(SiteHealthPage.msgKey)).data, startsWith('诊断包没存上'));
    expect(t.widget<OutlinedButton>(find.byKey(SiteHealthPage.bundleKey)).onPressed, isNotNull);
  });

  testWidgets('存的时候：按钮写正在存、按不了；存完放开', (t) async {
    final api = FakeApi('admin');
    final gate = Completer<String>();
    _portrait(t);
    await t.pumpWidget(MaterialApp(theme: nwTheme(Brightness.dark), home: SiteHealthPage(api: api, save: (n, b) => gate.future)));
    await t.pumpAndSettle();
    await t.ensureVisible(find.byKey(SiteHealthPage.bundleKey));
    await t.tap(find.byKey(SiteHealthPage.bundleKey));
    await t.pump();
    expect(find.text('正在存…'), findsOneWidget);
    expect(t.widget<OutlinedButton>(find.byKey(SiteHealthPage.bundleKey)).onPressed, isNull);
    gate.complete('/x/y.tar.gz');
    await t.pumpAndSettle();
    expect(find.text('正在存…'), findsNothing);
    expect(t.widget<OutlinedButton>(find.byKey(SiteHealthPage.bundleKey)).onPressed, isNotNull);
  });

  testWidgets('外审 M1 一刷新就撤掉上一次的结论、写正在读；体检坏了马上说，不等版本一览', (t) async {
    final api = FakeApi('admin');
    await _open(t, api);
    expect((t.widget<ListTile>(find.byKey(SiteHealthPage.overallKey)).title as Text).data, '各项都正常');
    api
      ..healthGate = Completer<void>()
      ..versionsGate = Completer<void>();
    await t.tap(find.byKey(SiteHealthPage.refreshKey));
    await t.pump();
    // 两个都还没回来：上一次的「各项都正常」已经不在了
    expect(find.byKey(SiteHealthPage.overallKey), findsNothing);
    expect(find.text('各项都正常'), findsNothing);
    expect(find.byKey(SiteHealthPage.checkKey('db')), findsNothing);
    expect(find.text('狗上的软件'), findsNothing);
    expect(find.byKey(SiteHealthPage.loadingKey), findsOneWidget);
    // 体检坏了、版本一览还卡着：错马上显示
    api.healthError = const SocketException('down');
    api.healthGate!.complete();
    await t.pump();
    await t.pump();
    expect(find.textContaining('站点体检没读到'), findsOneWidget);
    expect(find.byKey(SiteHealthPage.loadingKey), findsNothing);
    expect(find.byKey(SiteHealthPage.overallKey), findsNothing);
    expect(find.text('狗上的软件'), findsNothing, reason: '版本一览还没回来');
    api.versionsGate!.complete();
    await t.pump();
    await t.pump();
    expect(find.text('狗上的软件'), findsOneWidget);
    expect(find.textContaining('站点体检没读到'), findsOneWidget);
  });

  testWidgets('外审 M1 反过来：版本一览卡着，体检读到了就先显示', (t) async {
    final api = FakeApi('admin')..versionsGate = Completer<void>();
    _portrait(t);
    await t.pumpWidget(MaterialApp(theme: nwTheme(Brightness.dark), home: SiteHealthPage(api: api, save: (n, b) async => n)));
    await t.pump();
    await t.pump();
    expect(find.byKey(SiteHealthPage.overallKey), findsOneWidget);
    expect(find.byKey(SiteHealthPage.loadingKey), findsNothing);
    expect(find.text('狗上的软件'), findsNothing);
    api.versionsGate!.complete();
    await t.pumpAndSettle();
    expect(find.text('狗上的软件'), findsOneWidget);
  });

  testWidgets('正在读的时候刷新按钮按不了（不会同时问两次）', (t) async {
    final api = FakeApi('admin')..healthGate = Completer<void>();
    _portrait(t);
    await t.pumpWidget(MaterialApp(theme: nwTheme(Brightness.dark), home: SiteHealthPage(api: api, save: (n, b) async => n)));
    await t.pump();
    expect(t.widget<IconButton>(find.byKey(SiteHealthPage.refreshKey)).onPressed, isNull);
    api.healthGate!.complete();
    await t.pumpAndSettle();
    expect(t.widget<IconButton>(find.byKey(SiteHealthPage.refreshKey)).onPressed, isNotNull);
    expect(api.calls.where((c) => c == 'health').length, 1);
  });

  // 2026-10-14 重做：入口从狗列表顶栏的「⋮」菜单挪到了底部导航 —— 「站点体检」是管理栏（只有管理员
  // 有），「排程」是记录栏的一个分段（谁都有）。
  testWidgets('入口：管理员的底部导航有管理栏（站点体检），点了进去；保安、业主没有', (t) async {
    _portrait(t);
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(theme: nwTheme(Brightness.dark), home: NwShell(api: api)));
    await t.pumpAndSettle();
    expect(find.byType(SiteHealthPage), findsNothing);
    await t.tap(find.byKey(const Key('open-health')));
    await t.pumpAndSettle();
    expect(find.byType(SiteHealthPage), findsOneWidget);
    for (final role in ['guard', 'owner']) {
      await t.pumpWidget(Container());
      await t.pumpWidget(MaterialApp(theme: nwTheme(Brightness.dark), home: NwShell(api: FakeApi(role))));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('open-health')), findsNothing, reason: role);
      await t.tap(find.byKey(NwShell.moreKey));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('open-health')), findsNothing, reason: role);
      expect(find.byType(SiteHealthPage, skipOffstage: false), findsNothing, reason: role);
      await t.tap(find.byKey(NwShell.historyKey));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('open-schedule')), findsOneWidget);
    }
    await t.pumpWidget(Container());
  });

  // ---- RC 加固 H8：系统的「另存」和「分享」
  Future<void> page(WidgetTester t, FakeApi api,
      {BundleSaver? save, BundleSharer? share, bool? canShare = true}) async {
    _portrait(t);
    await t.pumpWidget(MaterialApp(
        theme: nwTheme(Brightness.dark),
        home: SiteHealthPage(
            api: api,
            now: () => DateTime(2026, 10, 11, 2, 4, 9),
            canShare: canShare,
            save: save ?? (n, b) async => '/d/$n',
            share: share ?? (n, b) async => true)));
    await t.pumpAndSettle();
  }

  Future<void> press(WidgetTester t, Key k) async {
    await t.ensureVisible(find.byKey(k));
    await t.tap(find.byKey(k));
    await t.pumpAndSettle();
  }

  String msg(WidgetTester t) => t.widget<SelectableText>(find.byKey(SiteHealthPage.msgKey)).data!;
  bool enabled(WidgetTester t, Key k) => t.widget<OutlinedButton>(find.byKey(k)).onPressed != null;

  testWidgets('分享：取回来交给分享面板，带着按时间起的名字；说交出去了', (t) async {
    final api = FakeApi('admin');
    final shared = <(String, int)>[];
    await page(t, api, share: (n, b) async {
      shared.add((n, b.length));
      return true;
    });
    await press(t, SiteHealthPage.shareKey);
    expect(api.calls, contains('support-bundle'));
    expect(shared, [('d1max-support-20261011-020409.tar.gz', 3000)]);
    expect(msg(t), '交给分享了（3 KB）：d1max-support-20261011-020409.tar.gz');
    expect(enabled(t, SiteHealthPage.shareKey) && enabled(t, SiteHealthPage.bundleKey), isTrue);
  });

  testWidgets('分享面板被关掉、另存被取消：照实说，不说成了，能再来', (t) async {
    final api = FakeApi('admin');
    await page(t, api, share: (n, b) async => false, save: (n, b) async => null);
    await press(t, SiteHealthPage.shareKey);
    expect(msg(t), '没分享：取消了');
    await press(t, SiteHealthPage.bundleKey);
    expect(msg(t), '没存：取消了');
    expect(enabled(t, SiteHealthPage.shareKey) && enabled(t, SiteHealthPage.bundleKey), isTrue);
  });

  testWidgets('分享出错（插件坏了、临时目录写不了、站点取不到）：说没分享出去和原因，两个按钮都放开', (t) async {
    for (final Object err in <Object>[
      StateError('no share plugin'),
      const FileSystemException('disk full'),
    ]) {
      final api = FakeApi('admin');
      await page(t, api, share: (n, b) async => throw err);
      await press(t, SiteHealthPage.shareKey);
      expect(msg(t), startsWith('诊断包没分享出去：'));
      expect(msg(t), contains(err is FileSystemException ? 'disk full' : 'no share plugin'));
      expect(enabled(t, SiteHealthPage.shareKey) && enabled(t, SiteHealthPage.bundleKey), isTrue);
      await t.pumpWidget(Container());
    }
    final api = FakeApi('admin')..bundleError = const SiteError(0, '连不上');
    var called = false;
    await page(t, api, share: (n, b) async => called = true);
    await press(t, SiteHealthPage.shareKey);
    expect(called, isFalse, reason: '没取到就没东西可分享');
    expect(msg(t), startsWith('诊断包没分享出去：'));
  });

  testWidgets('正在分享：两个按钮都按不了，只有分享那个写着正在取；完了都放开', (t) async {
    final api = FakeApi('admin');
    final gate = Completer<bool>();
    await page(t, api, share: (n, b) => gate.future);
    await t.ensureVisible(find.byKey(SiteHealthPage.shareKey));
    await t.tap(find.byKey(SiteHealthPage.shareKey));
    await t.pump();
    await t.pump();
    expect(find.text('正在取…'), findsOneWidget);
    expect(find.text('正在存…'), findsNothing);
    expect(enabled(t, SiteHealthPage.shareKey) || enabled(t, SiteHealthPage.bundleKey), isFalse);
    gate.complete(true);
    await t.pumpAndSettle();
    expect(enabled(t, SiteHealthPage.shareKey) && enabled(t, SiteHealthPage.bundleKey), isTrue);
  });

  testWidgets('桌面版（发不了文件）：没有分享按钮，另存还在', (t) async {
    await page(t, FakeApi('admin'), canShare: false);
    expect(find.byKey(SiteHealthPage.shareKey), findsNothing);
    expect(find.byKey(SiteHealthPage.bundleKey), findsOneWidget);
    await t.pumpWidget(Container());
    await page(t, FakeApi('admin'), canShare: null);
    expect(find.byKey(SiteHealthPage.shareKey), findsNothing, reason: '测试机是桌面系统');
  });
}
