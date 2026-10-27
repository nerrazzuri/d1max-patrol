import 'dart:async';

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
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
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
      'title': '机器人看见人了:1 个人,最近 6.2 m', 'detail': '布防中', 'count': 1,
      'first_ms': _t0, 'last_ms': _t0,
      'acked_by': ackedBy ?? '', 'acked_ms': ackedMs, 'resolved_ms': resolvedMs,
      'context': <String, dynamic>{'zone': 'East wall',
        'persons': <String, dynamic>{'count': 1, 'nearest_m': 6.2}}});

Map<String, dynamic> _session(
        {int level = 1,
        bool auto = true,
        List<String>? active = const ['strobe'],
        List<String>? outputs = const ['siren', 'speaker', 'spotlight', 'strobe']}) =>
    <String, dynamic>{
      'robot_id': 'A', 'level': level, 'auto': auto, 'next_in_s': auto ? 30 : null,
      'started_ms': _t0 + 4000, 'level_ms': _t0 + 4000,
      // 老站点不发这两样
      if (active != null) ...<String, dynamic>{'active': active, 'outputs': outputs}};

Alert _p1b() => Alert.fromWire(<String, dynamic>{
      'key': 'B/dog_sees_person#7', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'B', 'title': 't',
      'first_ms': _t0, 'last_ms': _t0, 'acked_by': '', 'acked_ms': null, 'resolved_ms': null,
      'context': <String, dynamic>{'zone': 'North gate'}});

/// 确认、跳级的回包扣在手里，测试自己放（或者让它失败）。
class _Held extends FakeApi {
  _Held(super.role);
  final ack = <Completer<Map<String, dynamic>>>[];
  final level = <Completer<Map<String, dynamic>>>[];
  @override
  Future<Map<String, dynamic>> ackAlert(String key) {
    calls.add('ack $key');
    ack.add(Completer<Map<String, dynamic>>());
    return ack.last.future;
  }

  @override
  Future<Map<String, dynamic>> deterLevel(String robotId, int level) {
    calls.add('deter-level $robotId $level');
    this.level.add(Completer<Map<String, dynamic>>());
    return this.level.last.future;
  }
}

Map<String, dynamic> _ackedWire(String key, {String by = 'alice'}) => <String, dynamic>{
      'alert': <String, dynamic>{'key': key, 'level': 'P1', 'kind': 'dog_sees_person', 'robot': key.split('/').first,
        'title': 't', 'first_ms': _t0, 'last_ms': _t0, 'acked_by': by, 'acked_ms': _t0 + 50000}};

String _btn(WidgetTester t, Key k) =>
    t.widget<Text>(find.descendant(of: find.byKey(k), matching: find.byType(Text))).data!;

bool _enabled(WidgetTester t, Key k) => t.widget<ButtonStyleButton>(find.byKey(k)).onPressed != null;

Future<void> _alarm(WidgetTester t, FakeApi api, Alert a, {int afterS = 42}) async {
  _portrait(t);
  api.alertRows = <Map<String, dynamic>>[];
  await t.pumpWidget(MaterialApp(
      theme: nwTheme(Brightness.dark),
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
      expect(alertTitle('dog_sees_person', '机器人看见人了:2 个人'), 'Person detected');
      expect(alertTitle('from_the_future', '新告警'), '新告警');
      appLang.value = AppLang.zh;
      expect(tr('Stop all', '全部急停'), '全部急停');
      expect(alertTitle('dog_sees_person', '机器人看见人了:2 个人'), '机器人看见人了:2 个人');
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
      // 2026-10-14 重做：这一页的标题按设计稿叫 Site Selection（原来是 Sites / 站点）
      expect(find.text('Site Selection'), findsOneWidget);
      expect(find.text('No sites yet. Add one with the button at the bottom right.'), findsOneWidget);
      await t.tap(find.byKey(const Key('lang-switch')));
      await t.pumpAndSettle();
      await t.tap(find.text('中文'));
      await t.pumpAndSettle();
      expect(find.text('选择站点'), findsOneWidget);
      expect(find.text('Site Selection'), findsNothing);
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

    testWidgets('主题（2026-10-14 重做）：浅色深色两套、主色和底色是设计稿的、字体是 Inter / JetBrains Mono', (t) async {
      final th = nwTheme(Brightness.dark);
      expect(th.brightness, Brightness.dark);
      expect(th.colorScheme.primary, const Color(0xFFD1E4FF));
      expect(th.scaffoldBackgroundColor, const Color(0xFF0A0C10));
      expect(th.textTheme.bodyMedium!.fontFamily, 'Inter');
      expect(th.textTheme.bodyMedium!.fontSize, 14);
      expect(NwText.labelSmall.fontFamily, 'JetBrains Mono');
      final light = nwTheme(Brightness.light);
      expect(light.brightness, Brightness.light);
      expect(light.colorScheme.primary, const Color(0xFF1A1C1E));
      expect(light.scaffoldBackgroundColor, const Color(0xFFFDFDFD));
      for (final s in [th.textTheme.bodySmall!, th.textTheme.labelSmall!, light.textTheme.bodySmall!, light.textTheme.labelSmall!, ...NwText.all]) {
        expect(s.fontSize, greaterThanOrEqualTo(12), reason: '用户定的：最小 12');
      }
    });
  });

  group('扫码 / 粘贴配对码添加站点', () {
    Future<MemorySiteStore> open(WidgetTester t, {CodeScanner? scanner}) async {
      _portrait(t);
      final store = MemorySiteStore();
      await t.pumpWidget(MaterialApp(theme: nwTheme(Brightness.dark), home: SiteListPage(store: store, scanner: scanner)));
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

    testWidgets('扫到的不是站点的码（机器人的开通码、别的二维码）：说出来，表单不动', (t) async {
      var next = 'D1MAX1.abc';
      await open(t, scanner: (_) async => next);
      await t.tap(find.byKey(const Key('site-scan')));
      await t.pumpAndSettle();
      expect(find.textContaining('机器人的开通码'), findsOneWidget);
      expect(find.byKey(const Key('site-check-note')), findsNothing);
      expect(t.widget<TextField>(find.byKey(const Key('site-name'))).controller!.text, '');
      next = 'https://evil.example/login';
      ScaffoldMessenger.of(t.element(find.byType(SiteAddPage))).clearSnackBars();
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('site-scan')));
      await t.pumpAndSettle();
      expect(find.textContaining('不是 Nexus Warden 站点的配对码'), findsOneWidget);
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

    testWidgets('在驱离：说到第几级、接下来多久升级；「开警笛」升到 3 级，机器人接受了才写已开、灰掉', (t) async {
      final api = FakeApi('guard')..deterRows = [_session()];
      await _alarm(t, api, _p1());
      expect(find.textContaining('最近的人距 A 6.2 米。 驱离第 1 级。'), findsOneWidget);
      expect(find.textContaining('前方'), findsNothing, reason: '站点没给方向');
      expect(_text(t, SiteAlertScenePage.nextKey), contains('30 秒后升到第 2 级'));
      expect(find.byKey(SiteAlertScenePage.deterKey), findsNothing, reason: '已经在驱离：不是「就地驱离」');
      await t.tap(find.byKey(SiteAlertScenePage.sirenKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('deter-level')), ['deter-level A 3'],
          reason: '站点第 3 级才有警笛，第 2 级只有灯和话术');
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
          theme: nwTheme(Brightness.dark),
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
      expect(find.text('Nearest person 6.2 m from A. Deterrence level 1.'), findsOneWidget);
      expect(find.text("I'll handle it"), findsOneWidget);
      expect(find.text('Sound siren (level 3)'), findsOneWidget);
      expect(find.text('Talk: not available'), findsOneWidget);
      expect(find.text('布防中'), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('全部急停：先问；只对在线的机器人发；哪台没停住说出来', (t) async {
      final api = FakeApi('guard');
      await _alarm(t, api, _p1());
      await t.tap(find.byKey(StopAllButton.buttonKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('halt')), isEmpty, reason: '先问');
      await t.tap(find.byKey(StopAllButton.confirmKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('halt')), ['halt A']);
      expect(find.text('1 台机器人都停了。'), findsOneWidget);
      await t.pumpWidget(Container());
    });

    test('多久、几点：0:42、12:05、1:02:03；负数当 0', () {
      expect(SiteAlertScenePageSpan.of(42000), '0:42');
      expect(SiteAlertScenePageSpan.of(725000), '12:05');
      expect(SiteAlertScenePageSpan.of(3723000), '1:02:03');
      expect(SiteAlertScenePageSpan.of(-5), '0:00');
    });
  });

  group('告警页：C-B2 外审（警笛等级、旧状态、迟到的回包）', () {
    const siren = SiteAlertScenePage.sirenKey;

    test('警笛的状态：机器人接受了才算开着；级别到了只算要求了；没装、说不清各说各的', () {
      SirenState of(Map<String, dynamic>? d) => SiteAlertScenePageSiren.of(d);
      expect(of(null), SirenState.unknown);
      expect(of(_session(level: 1)), SirenState.off);
      expect(of(_session(level: 2, active: ['speaker', 'strobe'])), SirenState.off, reason: '第 2 级不是警笛');
      expect(of(_session(level: 3, active: ['strobe'])), SirenState.requested);
      expect(of(_session(level: 4, active: ['strobe'])), SirenState.requested);
      expect(of(_session(level: 3, active: ['siren', 'strobe'])), SirenState.on);
      expect(of(_session(level: 3, active: ['siren'], outputs: ['strobe'])), SirenState.notFitted);
      expect(of(_session(level: 1, outputs: ['strobe'])), SirenState.notFitted);
      expect(of(_session(level: 3, active: null)), SirenState.requested, reason: '老站点不说哪几路开着：不猜');
      expect(of(_session(level: 2, active: null)), SirenState.off);
    });

    testWidgets('I1 第 2 级不写警笛已开，还能按；按了发第 3 级', (t) async {
      final api = FakeApi('guard')..deterRows = [_session(level: 2, active: ['speaker', 'strobe'])];
      await _alarm(t, api, _p1());
      expect(_btn(t, siren), '开警笛（第 3 级）');
      expect(find.text('警笛已开'), findsNothing);
      await t.tap(find.byKey(siren));
      await t.pumpAndSettle();
      expect(api.calls, contains('deter-level A 3'));
      expect(_btn(t, siren), '警笛已开');
      expect(_enabled(t, siren), isFalse);
      expect(find.byKey(SiteAlertScenePage.msgKey), findsNothing);
      await t.pumpWidget(Container());
    });

    testWidgets('I1 机器人没接受开警笛：不写已开，说还没确认，能再试；后来机器人接受了才写已开', (t) async {
      final api = FakeApi('guard')
        ..deterRows = [_session()]
        ..deterDogRefuses = true;
      await _alarm(t, api, _p1());
      await t.tap(find.byKey(siren));
      await t.pumpAndSettle();
      expect(find.text('警笛已开'), findsNothing);
      expect(_btn(t, siren), '警笛没确认，再试');
      expect(_enabled(t, siren), isTrue);
      expect(_text(t, SiteAlertScenePage.msgKey), contains('机器人还没确认警笛开了'));
      api.deterDogRefuses = false;
      await t.tap(find.byKey(siren));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c == 'deter-level A 3').length, 2);
      expect(_btn(t, siren), '警笛已开');
      expect(find.byKey(SiteAlertScenePage.msgKey), findsNothing);
      await t.pumpWidget(Container());
    });

    testWidgets('复查 R1 警笛开着、后来站点说没确认了（续开没成）：不再写已开，能再试；成了再写已开', (t) async {
      final api = FakeApi('guard')..deterRows = [_session(level: 3, auto: false, active: ['siren', 'strobe'])];
      await _alarm(t, api, _p1());
      expect(_btn(t, siren), '警笛已开');
      expect(_enabled(t, siren), isFalse);
      api.deterRows = [_session(level: 3, auto: false, active: ['strobe'])];
      await t.pump(const Duration(milliseconds: 3300));
      await t.pump();
      expect(find.text('警笛已开'), findsNothing);
      expect(_btn(t, siren), '警笛没确认，再试');
      expect(_enabled(t, siren), isTrue);
      await t.tap(find.byKey(siren));
      await t.pumpAndSettle();
      expect(api.calls, contains('deter-level A 3'));
      expect(_btn(t, siren), '警笛已开');
      await t.pumpWidget(Container());
    });

    testWidgets('I1 这台机器人没装警笛：按钮说没有、按不了；老站点到了第 3 级也只说没确认', (t) async {
      final api = FakeApi('guard')..deterRows = [_session(outputs: ['speaker', 'strobe'])];
      await _alarm(t, api, _p1());
      expect(_btn(t, siren), '这台机器人没有警笛');
      expect(_enabled(t, siren), isFalse);
      await t.pumpWidget(Container());
      final old = FakeApi('guard')..deterRows = [_session(level: 3, auto: false, active: null)];
      await _alarm(t, old, _p1());
      expect(_btn(t, siren), '警笛没确认，再试');
      expect(find.text('警笛已开'), findsNothing);
      await t.pumpWidget(Container());
    });

    testWidgets('I2 问到过第 3 级、之后问不到：不再当成现在的 —— 说不清、不写警笛已开、不说接下来；恢复了再跟上', (t) async {
      final api = FakeApi('guard')..deterRows = [_session(level: 2, active: ['strobe'])];
      await _alarm(t, api, _p1());
      expect(find.textContaining('驱离第 2 级。'), findsOneWidget);
      expect(find.byKey(SiteAlertScenePage.nextKey), findsOneWidget);
      api.deterRows = [_session(level: 3, auto: false, active: ['siren', 'strobe'])];
      await t.pump(const Duration(milliseconds: 3300));
      await t.pump();
      expect(_btn(t, siren), '警笛已开');
      api.deterrenceError = const SiteError(502, 'x');
      await t.pump(const Duration(milliseconds: 3300));
      await t.pump();
      expect(find.textContaining('驱离第 3 级。'), findsNothing);
      expect(find.textContaining('说不清有没有在驱离（02:15:18 最后问到时是第 3 级）'), findsOneWidget);
      expect(find.text('警笛已开'), findsNothing);
      expect(_btn(t, siren), '开警笛（第 3 级）');
      expect(_enabled(t, siren), isTrue, reason: '说不清的时候让人能再要求一次');
      expect(find.byKey(SiteAlertScenePage.nextKey), findsNothing);
      expect(_text(t, SiteAlertScenePage.staleKey), contains('02:15:18 起没再问到'));
      expect(find.textContaining('驱离升到第 3 级。'), findsOneWidget, reason: '带时刻的旧事照摆');
      api.deterrenceError = null;
      await t.pump(const Duration(milliseconds: 3300));
      await t.pump();
      expect(find.textContaining('驱离第 3 级。'), findsOneWidget);
      expect(_btn(t, siren), '警笛已开');
      expect(find.byKey(SiteAlertScenePage.staleKey), findsNothing);
      await t.pumpWidget(Container());
    });

    testWidgets('I2 问了一直不回来：过了三个周期也算旧的；人身上的距离（驱离带的）一起收掉', (t) async {
      final a = Alert.fromWire(<String, dynamic>{
        'key': 'A/intrusion#1', 'level': 'P1', 'kind': 'intrusion', 'robot': 'A', 'title': 't',
        'first_ms': _t0, 'last_ms': _t0, 'acked_by': 'bob', 'acked_ms': _t0 + 1000, // 确认了：不闪，没有别的重画
        'context': <String, dynamic>{'zone': 'East wall'}});
      final api = FakeApi('guard')
        ..deterRows = [
          <String, dynamic>{..._session(), 'persons': <String, dynamic>{'count': 1, 'nearest_m': 4.0}}
        ];
      await _alarm(t, api, a);
      expect(find.textContaining('最近的人距 A 4.0 米。 驱离第 1 级。'), findsOneWidget);
      api.deterrenceGate = Completer<void>();
      await t.pump(const Duration(milliseconds: 9600));
      await t.pump();
      expect(find.textContaining('驱离第 1 级。'), findsOneWidget, reason: '还在三个周期里');
      await t.pump(const Duration(milliseconds: 800));
      await t.pump();
      expect(find.textContaining('驱离第 1 级。'), findsNothing);
      expect(find.textContaining('最近的人'), findsNothing);
      expect(find.textContaining('说不清有没有在驱离（'), findsOneWidget);
      expect(find.byKey(SiteAlertScenePage.nextKey), findsNothing);
      // 扣住的那几次终于回来了：是现在问到的，算数
      api.deterrenceGate!.complete();
      api.deterrenceGate = null;
      await t.pump();
      await t.pump();
      expect(find.textContaining('驱离第 1 级。'), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('I2 先发的查询后到：不许盖掉刚按出来的结果', (t) async {
      final api = FakeApi('guard')..deterRows = [_session()];
      await _alarm(t, api, _p1());
      // 一次查询发出去、扣住（它读到的是按之前的第 1 级）
      final gate = api.deterrenceGate = Completer<void>();
      await t.pump(const Duration(milliseconds: 3300));
      final before = api.deterRows;
      api.deterrenceGate = null;
      await t.tap(find.byKey(siren));
      await t.pump();
      expect(_btn(t, siren), '警笛已开');
      api.deterRows = before; // 扣住的那一次读到的还是旧的
      gate.complete();
      await t.pump();
      await t.pump();
      expect(_btn(t, siren), '警笛已开', reason: '旧的查询回来得晚，丢掉');
      await t.pumpWidget(Container());
    });

    for (final (name, finish) in <(String, void Function(Completer<Map<String, dynamic>>))>[
      ('确认上了', (c) => c.complete(_ackedWire('A/dog_sees_person#1'))),
      ('没带正文（老站点）', (c) => c.complete(<String, dynamic>{})),
      ('失败了', (c) => c.completeError(const SiteError(502, 'x'))),
    ]) {
      testWidgets('I3 上一条告警的确认回包迟到（$name）：换上来的这一条不许变', (t) async {
        final api = _Held('guard');
        await _alarm(t, api, _p1());
        await t.tap(find.byKey(SiteAlertScenePage.ackKey));
        await t.pump();
        await _alarm(t, api, _p1b());
        expect(_text(t, SiteAlertScenePage.stateKey), startsWith('没人确认'));
        finish(api.ack.single);
        await t.pump();
        await t.pump(const Duration(milliseconds: 100));
        expect(_text(t, SiteAlertScenePage.stateKey), startsWith('没人确认'), reason: 'B 没在站点确认过');
        expect(_enabled(t, SiteAlertScenePage.ackKey), isTrue);
        expect(find.byKey(SiteAlertScenePage.msgKey), findsNothing, reason: 'A 的成败不写到 B 的页面上');
        expect(api.calls.where((c) => c.startsWith('ack')), ['ack A/dog_sees_person#1']);
        // B 自己的确认照常
        await t.tap(find.byKey(SiteAlertScenePage.ackKey));
        await t.pump();
        api.ack.last.complete(_ackedWire('B/dog_sees_person#7', by: 'u'));
        await t.pump();
        await t.pump();
        expect(api.calls.last, 'ack B/dog_sees_person#7');
        expect(_text(t, SiteAlertScenePage.stateKey), isNot(startsWith('没人确认')));
        expect(_enabled(t, SiteAlertScenePage.ackKey), isFalse);
        await t.pumpWidget(Container());
      });
    }

    testWidgets('I3 站点明说回的是另一条：不算这一条确认上了；没带正文的老站点才在本地记', (t) async {
      final api = _Held('guard');
      await _alarm(t, api, _p1());
      await t.tap(find.byKey(SiteAlertScenePage.ackKey));
      await t.pump();
      api.ack.last.complete(_ackedWire('B/dog_sees_person#7'));
      await t.pump();
      await t.pump();
      expect(_text(t, SiteAlertScenePage.stateKey), startsWith('没人确认'));
      expect(_text(t, SiteAlertScenePage.msgKey), contains('站点回的是另一条告警（B/dog_sees_person#7）'));
      expect(_enabled(t, SiteAlertScenePage.ackKey), isTrue);
      await t.tap(find.byKey(SiteAlertScenePage.ackKey));
      await t.pump();
      api.ack.last.complete(<String, dynamic>{});
      await t.pump();
      await t.pump();
      expect(_text(t, SiteAlertScenePage.stateKey), startsWith('你在 '));
      expect(find.byKey(SiteAlertScenePage.msgKey), findsNothing);
      await t.pumpWidget(Container());
    });

    testWidgets('I3 同一条告警换了个新对象（上一页推来的新状态）：在途的确认照样算数', (t) async {
      final api = _Held('guard');
      await _alarm(t, api, _p1());
      await t.tap(find.byKey(SiteAlertScenePage.ackKey));
      await t.pump();
      await _alarm(t, api, _p1());
      api.ack.single.complete(_ackedWire('A/dog_sees_person#1'));
      await t.pump();
      await t.pump();
      expect(_text(t, SiteAlertScenePage.stateKey), startsWith('alice 在 '));
      // 上一页又推来一份还没确认的旧状态：不退回去
      await _alarm(t, api, _p1());
      expect(_text(t, SiteAlertScenePage.stateKey), startsWith('alice 在 '));
      expect(_enabled(t, SiteAlertScenePage.ackKey), isFalse);
      await t.pumpWidget(Container());
    });

    testWidgets('I3 上一条的「记成误报？」还开着就换了告警：对话框收掉，谁都不关', (t) async {
      final api = FakeApi('guard');
      await _alarm(t, api, _p1());
      await t.ensureVisible(find.byKey(SiteAlertScenePage.falseKey));
      await t.tap(find.byKey(SiteAlertScenePage.falseKey));
      await t.pumpAndSettle();
      expect(find.byKey(SiteAlertScenePage.falseGoKey), findsOneWidget);
      await t.pumpWidget(MaterialApp(
          theme: nwTheme(Brightness.dark),
          home: SiteAlertScenePage(
              api: api, alert: _p1b(), now: () => DateTime.fromMillisecondsSinceEpoch(_t0 + 42000))));
      await t.pumpAndSettle();
      expect(find.byKey(SiteAlertScenePage.falseGoKey), findsNothing, reason: '问的是上一条');
      expect(find.byType(SiteAlertScenePage), findsOneWidget);
      expect(api.calls.where((c) => c.startsWith('resolve')), isEmpty);
      // 这一条自己的误报照常：关的是这一条
      await t.ensureVisible(find.byKey(SiteAlertScenePage.falseKey));
      await t.tap(find.byKey(SiteAlertScenePage.falseKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SiteAlertScenePage.falseGoKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('resolve')), ['resolve B/dog_sees_person#7']);
    });

    testWidgets('I3 上一条的开警笛回包迟到：不写到换上来的这一条上', (t) async {
      final api = _Held('guard')..deterRows = [_session()];
      await _alarm(t, api, _p1());
      await t.tap(find.byKey(siren));
      await t.pump();
      api.deterRows = <Map<String, dynamic>>[];
      await _alarm(t, api, _p1b());
      expect(find.byKey(siren), findsNothing, reason: 'B 没在驱离');
      api.level.single.complete(<String, dynamic>{
        'session': _session(level: 3, auto: false, active: ['siren'])
      });
      await t.pump();
      await t.pump();
      expect(find.byKey(siren), findsNothing);
      expect(find.textContaining('驱离第 3 级'), findsNothing);
      await t.pumpWidget(Container());
    });
  });

  group('竖屏', () {
    testWidgets('顶栏的文字按钮：竖屏收进「⋮」菜单，点得到；横屏一个个摆着', (t) async {
      _portrait(t);
      final api = FakeApi('admin');
      await t.pumpWidget(MaterialApp(
          theme: nwTheme(Brightness.dark), home: SiteStandbyPage(api: api, robotId: 'A', canMarkHere: true)));
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
          theme: nwTheme(Brightness.dark), home: SiteStandbyPage(api: FakeApi('admin'), robotId: 'A', canMarkHere: true)));
      await t.pumpAndSettle();
      expect(find.byKey(barMoreKey), findsNothing);
      expect(find.byKey(SiteStandbyPage.hereKey), findsOneWidget);
    });

    // 2026-10-14 重做：去别的页面的入口从狗列表顶栏的「⋮」菜单挪到了底部导航 —— 值守、地图、记录各是
    // 一栏，事件、排程是记录栏的分段，版本在管理栏里。竖屏也都摆在外面、带文字，不用再点开菜单。
    testWidgets('进了站点：竖屏时去别的页面的入口都在底部导航上、带文字', (t) async {
      _portrait(t);
      await t.pumpWidget(MaterialApp(
          theme: nwTheme(Brightness.dark), home: NwShell(api: FakeApi('admin'), title: 'East Gate Estate')));
      await t.pumpAndSettle();
      expect(find.byKey(barMoreKey), findsNothing, reason: '没有收起来的菜单了');
      for (final k in ['open-watch', 'open-maps', 'open-runs', 'open-health']) {
        expect(find.byKey(Key(k)), findsOneWidget, reason: k);
        expect(t.getRect(find.byKey(Key(k))).bottom, lessThanOrEqualTo(844), reason: k);
      }
      expect(find.descendant(of: find.byKey(const Key('open-maps')), matching: find.text('地图')), findsOneWidget);
      await t.tap(find.byKey(const Key('open-runs')));
      await t.pumpAndSettle();
      for (final k in ['open-incidents', 'open-schedule']) {
        expect(find.byKey(Key(k)), findsOneWidget, reason: k);
      }
      await t.tap(find.byKey(const Key('open-schedule')));
      await t.pumpAndSettle();
      expect(find.byType(SiteSchedulePage), findsOneWidget);
      await t.tap(find.byKey(const Key('open-health')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('open-releases')), findsOneWidget);
      await t.pumpWidget(Container());
    });

    testWidgets('值守屏的告警卡片：竖屏时按钮在下面一行，不挤标题；点得到', (t) async {
      _portrait(t);
      final api = FakeApi('guard');
      await t.pumpWidget(MaterialApp(theme: nwTheme(Brightness.dark), home: SiteWatchPage(api: api)));
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
