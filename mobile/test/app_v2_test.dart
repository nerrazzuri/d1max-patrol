// App V2：英文为主中文可选、扫码 / 粘贴配对码添加站点、告警页（视觉规范 2.5）、竖屏顶栏、遥控锁横屏。
import 'dart:io';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/main.dart' show PatrolApp;
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_add.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_mapping.dart' show zoneLabelText;
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_standby.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:d1max_patrol/ui/theme.dart';
import 'package:d1max_patrol/ui/widget/alarm_widgets.dart';
import 'package:d1max_patrol/ui/widget/bar_actions.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

// 站点的 d1max_site.pairing.encode 真出的码（见 pairing_test.dart）。
const _code = 'D1MAXSITE1.eyJmIjoiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYiIsIm4iOiLluoTlm63kuIDlj7ciLCJ1IjoiaHR0cHM6Ly8xOTIuMTY4LjEuNTo4NDQzIn0';

void _portrait(WidgetTester t) {
  t.view.physicalSize = const Size(390 * 2, 844 * 2);
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

void _english() {
  appLang.value = AppLang.en;
  addTearDown(() => appLang.value = AppLang.zh);
}

final _t0 = DateTime(2026, 10, 11, 2, 14, 36).millisecondsSinceEpoch;

Alert _p1({String? ackedBy, int? ackedMs, int? resolvedMs, String level = 'P1'}) =>
    Alert.fromWire(<String, dynamic>{
      'key': 'A/dog_sees_person#1', 'level': level, 'kind': 'dog_sees_person', 'robot': 'A',
      'title': '狗看见人了:1 个人,最近 6.2 m', 'detail': '布防中', 'count': 1,
      'first_ms': _t0, 'last_ms': _t0,
      'acked_by': ackedBy ?? '', 'acked_ms': ackedMs, 'resolved_ms': resolvedMs,
      'context': <String, dynamic>{'zone': 'East wall',
        'persons': <String, dynamic>{'count': 1, 'nearest_m': 6.2}}});

Map<String, dynamic> _session({int level = 1, bool auto = true}) => <String, dynamic>{
      'robot_id': 'A', 'level': level, 'auto': auto, 'next_in_s': auto ? 30 : null,
      'started_ms': _t0 + 4000, 'level_ms': _t0 + 4000};

Future<void> _alarm(WidgetTester t, FakeApi api, Alert a, {int afterS = 42}) async {
  _portrait(t);
  api.alertRows = <Map<String, dynamic>>[];
  await t.pumpWidget(MaterialApp(
      theme: proTheme(),
      home: SiteAlertScenePage(
          api: api, alert: a, now: () => DateTime.fromMillisecondsSinceEpoch(_t0 + afterS * 1000))));
  await t.pumpAndSettle();
}

String _text(WidgetTester t, Key k) => t.widget<Text>(find.byKey(k)).data!;

double _frameOpacity(WidgetTester t) => t
    .widget<AnimatedOpacity>(
        find.ancestor(of: find.byKey(SiteAlertScenePage.headKey), matching: find.byType(AnimatedOpacity)))
    .opacity;

void main() {
  group('英文为主，中文可选', () {
    test('tr 按当前语言取；告警标题英文按种类查词条，中文用站点原文，不认识的种类用站点原文', () {
      _english();
      expect(tr('Stop all', '全部急停'), 'Stop all');
      expect(alertTitle('dog_sees_person', '狗看见人了:2 个人'), 'Person detected');
      expect(alertTitle('from_the_future', '新告警'), '新告警');
      appLang.value = AppLang.zh;
      expect(tr('Stop all', '全部急停'), '全部急停');
      expect(alertTitle('dog_sees_person', '狗看见人了:2 个人'), '狗看见人了:2 个人');
      expect(alertTitle('dog_sees_person', ''), isNotEmpty, reason: '站点没给标题：用词条里的中文');
    });

    testWidgets('第一次打开是英文；在站点列表换成中文，整个界面跟着换，选了什么存下来', (t) async {
      _english();
      final dir = Directory.systemTemp.createTempSync('d1max-lang');
      addTearDown(() => dir.deleteSync(recursive: true));
      final langs = LangStore(File('${dir.path}/lang.json'));
      expect(await t.runAsync(langs.load), AppLang.en, reason: '没存过：英文');
      await t.pumpWidget(PatrolApp(siteStore: MemorySiteStore(), langStore: langs));
      await t.pumpAndSettle();
      expect(find.text('Sites'), findsOneWidget);
      expect(find.text('No sites yet. Add one with the button at the bottom right.'), findsOneWidget);
      await t.tap(find.byKey(const Key('lang-switch')));
      await t.pumpAndSettle();
      await t.tap(find.text('中文'));
      await t.pumpAndSettle();
      expect(find.text('站点'), findsOneWidget);
      expect(find.text('Sites'), findsNothing);
      // 存盘是真的文件读写：得让真时钟走几步（见 support/pump.dart 的说明）
      for (var i = 0; i < 10 && !File('${dir.path}/lang.json').existsSync(); i++) {
        await t.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
        await t.pump();
      }
      expect(await t.runAsync(langs.load), AppLang.zh);
    });

    test('语言文件坏了、内容不认识：当没选过（英文），不崩', () async {
      final dir = Directory.systemTemp.createTempSync('d1max-lang');
      addTearDown(() => dir.deleteSync(recursive: true));
      final f = File('${dir.path}/lang.json');
      for (final bad in ['', '{', '[]', '{"lang":"fr"}', '{"lang":1}']) {
        f.writeAsStringSync(bad);
        expect(await LangStore(f).load(), AppLang.en, reason: bad);
      }
      await LangStore(f).save(AppLang.zh);
      expect(await LangStore(f).load(), AppLang.zh);
      await LangStore(f).save(AppLang.en);
      expect(await LangStore(f).load(), AppLang.en);
    });

    test('存在站点上的区域缺省名（中文）：英文界面翻过来，人自己起的名字原样', () {
      expect(zoneLabelText('禁行区'), '禁行区');
      _english();
      expect(zoneLabelText('禁行区'), 'No-go zone');
      expect(zoneLabelText('限速区'), 'Speed-limit zone');
      expect(zoneLabelText('泳池'), '泳池');
      expect(zoneLabelText(null), '');
    });

    testWidgets('专业版主题：深色、主色是中性灰白不是品牌色、字体是 Atkinson', (t) async {
      final th = proTheme();
      expect(th.brightness, Brightness.dark);
      expect(th.colorScheme.primary, const Color(0xFFE2E2E2));
      expect(th.scaffoldBackgroundColor, const Color(0xFF1C1D1F));
      expect(th.textTheme.bodyMedium!.fontFamily, 'Atkinson Hyperlegible Next');
      expect(th.textTheme.bodyMedium!.fontSize, 16);
      expect(D1Text.osd.fontFamily, 'Atkinson Hyperlegible Mono');
      for (final s in [th.textTheme.bodySmall!, th.textTheme.labelSmall!, D1Text.osd, D1Text.caption]) {
        expect(s.fontSize, greaterThanOrEqualTo(14), reason: '规范：最小 14');
      }
    });
  });

  group('扫码 / 粘贴配对码添加站点', () {
    Future<MemorySiteStore> open(WidgetTester t, {CodeScanner? scanner}) async {
      _portrait(t);
      final store = MemorySiteStore();
      await t.pumpWidget(MaterialApp(theme: proTheme(), home: SiteListPage(store: store, scanner: scanner)));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('site-add')));
      await t.pumpAndSettle();
      return store;
    }

    testWidgets('扫到站点的码：三样填进表单、提示核对；填了账号才存，口令不存', (t) async {
      final store = await open(t, scanner: (_) async => _code);
      expect(find.byKey(const Key('site-check-note')), findsNothing);
      await t.tap(find.byKey(const Key('site-scan')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('site-check-note')), findsOneWidget);
      expect(t.widget<TextField>(find.byKey(const Key('site-name'))).controller!.text, '庄园一号');
      expect(t.widget<TextField>(find.byKey(const Key('site-url'))).controller!.text, 'https://192.168.1.5:8443');
      expect(t.widget<TextField>(find.byKey(const Key('site-fp'))).controller!.text, 'ab' * 32);
      await t.ensureVisible(find.byKey(const Key('site-save')));
      await t.tap(find.byKey(const Key('site-save')));
      await t.pumpAndSettle();
      expect(store.sites, isEmpty, reason: '账号没填：配对码里没有账号');
      expect(find.textContaining('名字和账号要填'), findsOneWidget);
      await t.enterText(find.byKey(const Key('site-user')), 'gina');
      await t.tap(find.byKey(const Key('site-save')));
      await t.pumpAndSettle();
      final s = store.sites.single;
      expect((s.name, s.url, s.fingerprint, s.username), ('庄园一号', 'https://192.168.1.5:8443', 'ab' * 32, 'gina'));
      expect(s.toJson().keys, isNot(contains('password')));
      expect(find.text('庄园一号'), findsOneWidget, reason: '回到站点列表，新站点在');
    });

    testWidgets('粘贴配对码跟扫码一样；退出相机没扫到什么都不变', (t) async {
      await open(t, scanner: (_) async => null);
      await t.tap(find.byKey(const Key('site-scan')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('site-check-note')), findsNothing);
      expect(t.widget<TextField>(find.byKey(const Key('site-url'))).controller!.text, 'https://');
      await t.tap(find.byKey(const Key('site-paste')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('pair-code')), '  $_code \n');
      await t.tap(find.byKey(const Key('pair-code-ok')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('site-check-note')), findsOneWidget);
      expect(t.widget<TextField>(find.byKey(const Key('site-fp'))).controller!.text, 'ab' * 32);
    });

    testWidgets('扫到的不是站点的码（狗的开通码、别的二维码）：说出来，表单不动', (t) async {
      var next = 'D1MAX1.abc';
      await open(t, scanner: (_) async => next);
      await t.tap(find.byKey(const Key('site-scan')));
      await t.pumpAndSettle();
      expect(find.textContaining('狗的开通码'), findsOneWidget);
      expect(find.byKey(const Key('site-check-note')), findsNothing);
      expect(t.widget<TextField>(find.byKey(const Key('site-name'))).controller!.text, '');
      next = 'https://evil.example/login';
      ScaffoldMessenger.of(t.element(find.byType(SiteAddPage))).clearSnackBars();
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('site-scan')));
      await t.pumpAndSettle();
      expect(find.textContaining('不是 D1 Max 站点的配对码'), findsOneWidget);
      expect(t.widget<TextField>(find.byKey(const Key('site-url'))).controller!.text, 'https://');
    });

    testWidgets('桌面版没有相机：没有「扫码」按钮，粘贴照样在；手机上有', (t) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.linux;
      await open(t);
      expect(find.byKey(const Key('site-scan')), findsNothing);
      expect(find.byKey(const Key('site-paste')), findsOneWidget);
      debugDefaultTargetPlatformOverride = TargetPlatform.android;
      expect(canScanCodes, isTrue);
      debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
      expect(canScanCodes, isTrue);
      debugDefaultTargetPlatformOverride = TargetPlatform.windows;
      expect(canScanCodes, isFalse);
      debugDefaultTargetPlatformOverride = null;
    });
  });

  group('告警页（视觉规范 2.5）', () {
    testWidgets('没确认的 P1：头条说没确认多久、框在闪；「我来处理」之后头条换成谁确认的、不闪了、按钮灰掉', (t) async {
      final api = FakeApi('guard')..deterRows = [_session()];
      await _alarm(t, api, _p1());
      expect(_text(t, SiteAlertScenePage.stateKey), '没人确认已经 0:42');
      final seen = <double>{};
      for (var i = 0; i < 4; i++) {
        await t.pump(const Duration(milliseconds: 800));
        seen.add(_frameOpacity(t));
      }
      expect(seen, {1.0, 0.4}, reason: '没确认的 P1 闪：1600 ms 一个来回');
      await t.tap(find.byKey(SiteAlertScenePage.ackKey));
      await t.pumpAndSettle();
      expect(api.calls, contains('ack A/dog_sees_person#1'));
      expect(_text(t, SiteAlertScenePage.stateKey), startsWith('你在 '));
      for (var i = 0; i < 4; i++) {
        await t.pump(const Duration(milliseconds: 800));
        expect(_frameOpacity(t), 1.0, reason: '确认了就不闪');
      }
      expect(t.widget<FilledButton>(find.byKey(SiteAlertScenePage.ackKey)).onPressed, isNull);
      await t.pumpWidget(Container());
    });

    testWidgets('别人确认的：写别人的名字；P2、已关闭的都不闪，已关闭的不能再处理', (t) async {
      final api = FakeApi('guard');
      await _alarm(t, api, _p1(ackedBy: 'alice', ackedMs: _t0 + 44000));
      expect(_text(t, SiteAlertScenePage.stateKey), 'alice 在 02:15:20 确认了');
      await _alarm(t, api, _p1(level: 'P2'));
      for (var i = 0; i < 3; i++) {
        await t.pump(const Duration(milliseconds: 800));
        expect(_frameOpacity(t), 1.0, reason: 'P2 不闪');
      }
      await _alarm(t, api, _p1(resolvedMs: _t0 + 60000));
      expect(_text(t, SiteAlertScenePage.stateKey), '02:15:36 已关闭');
      expect(t.widget<FilledButton>(find.byKey(SiteAlertScenePage.ackKey)).onPressed, isNull);
      expect(t.widget<OutlinedButton>(find.byKey(SiteAlertScenePage.falseKey)).onPressed, isNull);
      await t.pumpWidget(Container());
    });

    testWidgets('在驱离：说到第几级、接下来多久升级；「开警笛」升到 2 级，开了就灰掉', (t) async {
      final api = FakeApi('guard')..deterRows = [_session()];
      await _alarm(t, api, _p1());
      expect(find.textContaining('在 A 前方 6.2 米。 驱离第 1 级。'), findsOneWidget);
      expect(_text(t, SiteAlertScenePage.nextKey), contains('30 秒后升到第 2 级'));
      expect(find.byKey(SiteAlertScenePage.deterKey), findsNothing, reason: '已经在驱离：不是「就地驱离」');
      await t.tap(find.byKey(SiteAlertScenePage.sirenKey));
      await t.pumpAndSettle();
      expect(api.calls, contains('deter-level A 2'));
      expect(t.widget<OutlinedButton>(find.byKey(SiteAlertScenePage.sirenKey)).onPressed, isNull);
      expect(find.text('警笛已开'), findsOneWidget);
      expect(find.byKey(SiteAlertScenePage.nextKey), findsNothing, reason: '人接手了就不再自动升级');
      await t.pumpWidget(Container());
    });

    testWidgets('驱离接口拿不到：说「说不清」，不是当成没有', (t) async {
      final api = FakeApi('guard')..deterrenceError = const SiteError(502, 'x');
      await _alarm(t, api, _p1());
      expect(find.textContaining('说不清有没有在驱离'), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('业主能确认、记误报，但没有警笛和全部急停（没有派遣权限）；误报要再问一遍，记了就回去', (t) async {
      final api = FakeApi('owner')..deterRows = [_session()];
      _portrait(t);
      api.alertRows = <Map<String, dynamic>>[];
      await t.pumpWidget(MaterialApp(
          theme: proTheme(),
          home: Builder(
              builder: (c) => TextButton(
                  onPressed: () => Navigator.push(c,
                      MaterialPageRoute<void>(builder: (_) => SiteAlertScenePage(api: api, alert: _p1()))),
                  child: const Text('open')))));
      await t.tap(find.text('open'));
      await t.pumpAndSettle();
      expect(find.byKey(SiteAlertScenePage.sirenKey), findsNothing);
      expect(find.byKey(StopAllButton.buttonKey), findsNothing);
      expect(t.widget<FilledButton>(find.byKey(SiteAlertScenePage.ackKey)).onPressed, isNotNull);
      await t.tap(find.byKey(SiteAlertScenePage.falseKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('resolve')), isEmpty, reason: '先问');
      await t.tap(find.text('取消'));
      await t.pumpAndSettle();
      expect(find.byType(SiteAlertScenePage), findsOneWidget);
      await t.tap(find.byKey(SiteAlertScenePage.falseKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SiteAlertScenePage.falseGoKey));
      await t.pumpAndSettle();
      expect(api.calls, contains('resolve A/dog_sees_person#1'));
      expect(find.byType(SiteAlertScenePage), findsNothing, reason: '关了就回上一页');
    });

    testWidgets('别人在别处确认了：这一页过几秒自己跟上', (t) async {
      final api = FakeApi('guard');
      await _alarm(t, api, _p1());
      expect(_text(t, SiteAlertScenePage.stateKey), startsWith('没人确认'));
      api.alertRows = [
        <String, dynamic>{'key': 'A/dog_sees_person#1', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'A',
          'title': 't', 'first_ms': _t0, 'last_ms': _t0, 'acked_by': 'bob', 'acked_ms': _t0 + 50000,
          'context': <String, dynamic>{}}
      ];
      await t.pump(const Duration(milliseconds: 3300));
      await t.pump();
      expect(_text(t, SiteAlertScenePage.stateKey), startsWith('bob 在 '));
      await t.pumpWidget(Container());
    });

    testWidgets('英文界面：标题按种类是英文，站点给的中文原因照样摆出来', (t) async {
      _english();
      final api = FakeApi('guard')..deterRows = [_session()];
      await _alarm(t, api, _p1());
      expect(find.text('Person detected'), findsWidgets);
      expect(_text(t, SiteAlertScenePage.stateKey), 'Unacknowledged for 0:42');
      expect(find.text('6.2 m ahead of A. Deterrence level 1.'), findsOneWidget);
      expect(find.text("I'll handle it"), findsOneWidget);
      expect(find.text('Sound siren (level 2)'), findsOneWidget);
      expect(find.text('Talk: not available'), findsOneWidget);
      expect(find.text('布防中'), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('全部急停：先问；只对在线的狗发；哪台没停住说出来', (t) async {
      final api = FakeApi('guard');
      await _alarm(t, api, _p1());
      await t.tap(find.byKey(StopAllButton.buttonKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('halt')), isEmpty, reason: '先问');
      await t.tap(find.byKey(StopAllButton.confirmKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('halt')), ['halt A']);
      expect(find.text('1 台狗都停了。'), findsOneWidget);
      await t.pumpWidget(Container());
    });

    test('多久、几点：0:42、12:05、1:02:03；负数当 0', () {
      expect(SiteAlertScenePageSpan.of(42000), '0:42');
      expect(SiteAlertScenePageSpan.of(725000), '12:05');
      expect(SiteAlertScenePageSpan.of(3723000), '1:02:03');
      expect(SiteAlertScenePageSpan.of(-5), '0:00');
    });
  });

  group('竖屏', () {
    testWidgets('顶栏的文字按钮：竖屏收进「⋮」菜单，点得到；横屏一个个摆着', (t) async {
      _portrait(t);
      final api = FakeApi('admin');
      await t.pumpWidget(MaterialApp(
          theme: proTheme(), home: SiteStandbyPage(api: api, robotId: 'A', canMarkHere: true)));
      await t.pumpAndSettle();
      expect(find.byKey(SiteStandbyPage.hereKey), findsNothing);
      await t.tap(find.byKey(barMoreKey));
      await t.pumpAndSettle();
      expect(find.byKey(SiteStandbyPage.hereKey), findsOneWidget);
      expect(find.byKey(SiteStandbyPage.chargerKey), findsOneWidget);
      await t.tap(find.byKey(SiteStandbyPage.hereKey));
      await t.pumpAndSettle();
      expect(find.byKey(SiteStandbyPage.nameKey), findsOneWidget, reason: '菜单里点了跟按钮一样：弹出起名的框');
      t.view.physicalSize = const Size(800 * 2, 360 * 2);
      await t.pumpWidget(MaterialApp(
          theme: proTheme(), home: SiteStandbyPage(api: FakeApi('admin'), robotId: 'A', canMarkHere: true)));
      await t.pumpAndSettle();
      expect(find.byKey(barMoreKey), findsNothing);
      expect(find.byKey(SiteStandbyPage.hereKey), findsOneWidget);
    });

    testWidgets('狗列表：竖屏时去别的页面的入口在菜单里、带文字；值守的铃还在外面', (t) async {
      _portrait(t);
      await t.pumpWidget(MaterialApp(
          theme: proTheme(), home: SiteRobotsPage(api: FakeApi('admin'), title: 'East Gate Estate')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('open-watch')), findsOneWidget);
      expect(find.byKey(const Key('open-maps')), findsNothing);
      await t.tap(find.byKey(barMoreKey));
      await t.pumpAndSettle();
      for (final k in ['open-releases', 'open-maps', 'open-runs', 'open-incidents', 'open-schedule']) {
        expect(find.byKey(Key(k)), findsOneWidget, reason: k);
      }
      expect(find.text('地图'), findsOneWidget);
      await t.tap(find.byKey(const Key('open-schedule')));
      await t.pumpAndSettle();
      expect(find.byType(SiteSchedulePage), findsOneWidget);
    });

    testWidgets('值守屏的告警卡片：竖屏时按钮在下面一行，不挤标题；点得到', (t) async {
      _portrait(t);
      final api = FakeApi('guard');
      await t.pumpWidget(MaterialApp(theme: proTheme(), home: SiteWatchPage(api: api)));
      await t.pumpAndSettle();
      const key = 'A/estop_pressed#1';
      final title = t.getRect(find.textContaining('急停被按下'));
      final ack = t.getRect(find.byKey(SiteWatchPage.ackKey(key)));
      expect(ack.top, greaterThanOrEqualTo(title.bottom), reason: '按钮在标题下面');
      expect(title.width, greaterThan(200), reason: '标题没被挤窄');
      await t.tap(find.byKey(SiteWatchPage.ackKey(key)));
      await t.pumpAndSettle();
      expect(api.calls, contains('ack $key'));
      await t.pumpWidget(Container());
    });

    testWidgets('遥控：进来锁横屏，离开放开（别的页面竖屏横屏都行）', (t) async {
      final calls = <Object?>[];
      t.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (c) async {
        if (c.method == 'SystemChrome.setPreferredOrientations') calls.add(c.arguments);
        return null;
      });
      addTearDown(() => t.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, null));
      t.view.physicalSize = const Size(800 * 2, 360 * 2);
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      await t.pumpWidget(MaterialApp(home: SiteStandbyPage(api: FakeApi('admin'), robotId: 'A')));
      await t.pumpAndSettle();
      expect(calls, isEmpty, reason: '普通页面不锁方向');
      await t.pumpWidget(MaterialApp(home: SiteTeleopPage(api: FakeApi('guard'), robotId: 'A')));
      await t.pump();
      expect(calls, [
        ['DeviceOrientation.landscapeLeft', 'DeviceOrientation.landscapeRight']
      ]);
      await t.pumpWidget(Container());
      await t.pump();
      expect(calls.last, isEmpty, reason: '空表 = 交还给系统');
    });
  });
}
