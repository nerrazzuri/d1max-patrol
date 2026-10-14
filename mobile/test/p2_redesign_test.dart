// App 重做 P2（2026-10-14）新加的东西：值守首页上的布防模式三段、P1 卡片的「去处理」、健康标签；
// 告警页的四个大按钮在底栏（拇指够得着、不跟着滚）、小地图。
import 'dart:async';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/nw/nw_map_raster.dart';
import 'package:d1max_patrol/ui/nw/nw_p2_duty.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_deter.dart';
import 'package:d1max_patrol/ui/site_deterrence.dart';
import 'package:d1max_patrol/ui/site_mode.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:d1max_patrol/ui/widget/alarm_widgets.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Map<String, dynamic> _mode(String mode) => <String, dynamic>{
      ...siteFixture('site_mode'), 'mode': mode, 'visitor_zones': <String>[], 'until_ms': null};

/// 读模式的回包扣在手里，测试自己放。
class _SlowMode extends FakeApi {
  _SlowMode(super.role);
  final gates = <Completer<Map<String, dynamic>>>[];
  bool hold = false;
  @override
  Future<Map<String, dynamic>> mode() {
    if (!hold) return super.mode();
    gates.add(Completer<Map<String, dynamic>>());
    return gates.last.future;
  }
}

Future<void> _duty(WidgetTester t, FakeApi api, {Size size = const Size(390, 844)}) async {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
  await t.pumpWidget(const SizedBox());
  await t.pumpWidget(MaterialApp(home: SiteWatchPage(api: api)));
  await t.pumpAndSettle();
}

bool _canTap(WidgetTester t, Key k) => t.widget<InkWell>(find.byKey(k)).onTap != null;

Alert _intrusion({Map<String, dynamic>? context}) => Alert.fromWire(<String, dynamic>{
      'key': 'A/intrusion#1', 'level': 'P1', 'kind': 'intrusion', 'robot': 'A',
      'title': '防区 front-yard 有入侵', 'detail': '事件源 nvr-1',
      'first_ms': 1, 'last_ms': 1, 'count': 1, 'acked_by': '', 'acked_ms': null,
      'resolved_by': '', 'resolved_ms': null, 'escalated': 2, 'channel': 'sound',
      'context': context ??
          <String, dynamic>{
            'zone': 'front-yard',
            'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': '7', 'x': 3.0,
              'y': -1.0, 'yaw': 0.0, 'at_ms': 1000},
          },
    });

void main() {
  group('值守首页：布防模式三段', () {
    testWidgets('业主：写着现在是什么模式；切到在家先问一遍，走的是 setMode', (t) async {
      final api = FakeApi('owner')..modeView = _mode('armed');
      await _duty(t, api);
      expect(find.text('现在：布防'), findsOneWidget);
      expect(_canTap(t, SiteModeBar.armKey), isFalse, reason: '已经是布防');
      await t.tap(find.byKey(SiteModeBar.homeKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('mode')), isEmpty, reason: '撤防先问');
      await t.tap(find.text('算了'));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('mode')), isEmpty);
      await t.tap(find.byKey(SiteModeBar.homeKey));
      await t.pumpAndSettle();
      await t.tap(find.byKey(SiteModeBar.homeGoKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('mode')), ['mode home']);
      expect(find.text('现在：在家'), findsOneWidget);
      await t.tap(find.byKey(SiteModeBar.armKey));
      await t.pumpAndSettle();
      expect(api.calls.last, 'mode armed', reason: '布防不用问');
      expect(find.text('现在：布防'), findsOneWidget);
    });

    testWidgets('保安：只能布防；在家、访客点不动，说了要业主切', (t) async {
      final api = FakeApi('guard')..modeView = _mode('home');
      await _duty(t, api);
      expect(_canTap(t, SiteModeBar.homeKey), isFalse);
      expect(_canTap(t, SiteModeBar.visitorKey), isFalse);
      expect(find.text('撤防（在家、访客）要业主切'), findsOneWidget);
      await t.tap(find.byKey(SiteModeBar.homeKey), warnIfMissed: false);
      await t.tap(find.byKey(SiteModeBar.visitorKey), warnIfMissed: false);
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('mode')), isEmpty);
      expect(find.byType(SiteModePage), findsNothing);
      await t.tap(find.byKey(SiteModeBar.armKey));
      await t.pumpAndSettle();
      expect(api.calls.where((c) => c.startsWith('mode')), ['mode armed']);
      expect(find.text('现在：布防'), findsOneWidget);
    });

    testWidgets('切不成：说的是模式页那句话，模式不变', (t) async {
      final api = FakeApi('guard')..modeView = _mode('home');
      await _duty(t, api);
      api.modeError = const SiteError(403, '没有权限');
      await t.tap(find.byKey(SiteModeBar.armKey));
      await t.pumpAndSettle();
      expect(find.byKey(SiteModeBar.msgKey), findsOneWidget);
      expect(find.textContaining('切到布防没成'), findsOneWidget);
      expect(find.text('现在：在家'), findsOneWidget);
    });

    testWidgets('访客要挑防区和时长：进模式页', (t) async {
      final api = FakeApi('owner')..modeView = _mode('armed');
      await _duty(t, api);
      await t.tap(find.byKey(SiteModeBar.visitorKey));
      await t.pumpAndSettle();
      expect(find.byType(SiteModePage), findsOneWidget);
      expect(api.calls.where((c) => c.startsWith('mode')), isEmpty);
    });

    testWidgets('老站点没开布防模式：不显示这一条', (t) async {
      final api = FakeApi('owner')..modeError = const SiteError(404, '没开');
      await _duty(t, api);
      expect(find.byKey(SiteModeBar.armKey), findsNothing);
      expect(find.byKey(SiteWatchPage.p1NoneKey), findsNothing, reason: '夹具里有 P1，值守屏照常');
      expect(find.textContaining('急停被按下'), findsOneWidget);
    });

    testWidgets('还没读到、读不到：不说现在是哪个模式；读不到说原因', (t) async {
      final slow = _SlowMode('owner')
        ..modeView = _mode('home')
        ..hold = true;
      await _duty(t, slow);
      expect(find.text('布防模式还没读到'), findsOneWidget);
      expect(find.textContaining('现在：'), findsNothing);
      slow.gates.single.complete(slow.modeView);
      await t.pumpAndSettle();
      expect(find.text('现在：在家'), findsOneWidget);
      // 之后读不到了：上一次的不再当成现在的
      slow
        ..hold = false
        ..modeError = const SiteError(502, '站点没回');
      await t.pump(const Duration(seconds: 31));
      await t.pumpAndSettle();
      expect(find.textContaining('布防模式读不到'), findsOneWidget);
      expect(find.textContaining('现在：'), findsNothing);
    });

    testWidgets('别的手机切了：站点推一帧当场换；断线重连后重新拉', (t) async {
      final api = FakeApi('guard')..modeView = _mode('armed');
      await _duty(t, api);
      api.sse.add(<String, dynamic>{'kind': 'mode', 'mode': _mode('home')});
      await t.pump(const Duration(milliseconds: 50));
      expect(find.text('现在：在家'), findsOneWidget);
      expect(_canTap(t, SiteModeBar.armKey), isTrue);
      final old = api.sse;
      api.sse = StreamController<Map<String, dynamic>>.broadcast();
      api.modeView = _mode('armed');
      await old.close();
      await t.pump(const Duration(seconds: 6));
      await t.pump();
      expect(find.text('现在：布防'), findsOneWidget);
      await t.pump(const Duration(seconds: 1));
    });

    testWidgets('先发的读取后到：不许盖掉刚切出来的结果', (t) async {
      final api = _SlowMode('guard')..modeView = _mode('home');
      await _duty(t, api);
      api.hold = true;
      await t.pump(const Duration(seconds: 31)); // 兜底的那一次读，扣住
      expect(api.gates, hasLength(1));
      api.hold = false;
      await t.tap(find.byKey(SiteModeBar.armKey));
      await t.pumpAndSettle();
      expect(find.text('现在：布防'), findsOneWidget);
      api.gates.single.complete(_mode('home')); // 切之前发出去的，现在才回来
      await t.pumpAndSettle();
      expect(find.text('现在：布防'), findsOneWidget);
    });
  });

  group('值守首页：P1 卡片、健康标签', () {
    testWidgets('没确认的 P1 有红色的「去处理」，点了进告警页；确认了的换成「看现场」', (t) async {
      final api = FakeApi('guard');
      await _duty(t, api);
      const key = 'A/estop_pressed#1';
      final go = t.widget<FilledButton>(find.byKey(SiteWatchPage.respondKey(key)));
      expect(go.onPressed, isNotNull);
      final size = t.getSize(find.byKey(SiteWatchPage.respondKey(key)));
      expect(size.height, greaterThanOrEqualTo(48));
      await t.tap(find.byKey(SiteWatchPage.respondKey(key)));
      await t.pumpAndSettle();
      expect(find.byType(SiteAlertScenePage), findsOneWidget);
      // 确认过的 P1：还在 P1 那一段（「没有」只在一条 P1 都没有时才说），按钮不再是红的那个
      final row = Map<String, dynamic>.from(
          (siteFixture('site_alerts')['alerts'] as List).first as Map);
      final acked = FakeApi('guard')
        ..alertRows = [<String, dynamic>{...row, 'acked_by': 'alice', 'acked_ms': 5}];
      await _duty(t, acked);
      expect(find.byKey(SiteWatchPage.p1NoneKey), findsNothing);
      expect(find.byKey(SiteWatchPage.respondKey(key)), findsNothing);
      expect(t.widget<OutlinedButton>(find.byKey(SiteWatchPage.viewKey(key))).onPressed, isNotNull);
    });

    testWidgets('健康标签用的是汇总里的数；汇总读不到就没有标签', (t) async {
      final api = FakeApi('guard');
      await _duty(t, api);
      expect(find.byType(NwHealthChip, skipOffstage: false), findsNWidgets(3));
      expect(find.text('狗在线 1/1', skipOffstage: false), findsOneWidget);
      expect(find.text('排程正常', skipOffstage: false), findsWidgets);
      expect(find.text('备份正常', skipOffstage: false), findsOneWidget);
      // 掉线、备份过期：标签是坏的那种；没开排程、没配备份的不摆（不许拿绿色冒充正常）
      final body = siteFixture('site_watch_summary');
      ((body['robots'] as List).first as Map<String, dynamic>)['online'] = false;
      final site = body['site'] as Map<String, dynamic>;
      site['schedule_ok'] = null;
      (site['backup'] as Map<String, dynamic>)['stale'] = true;
      await _duty(t, FakeApi('guard')..summaryBody = body);
      final chips = t.widgetList<NwHealthChip>(find.byType(NwHealthChip, skipOffstage: false)).toList();
      expect(chips.map((c) => (c.label, c.bad)), [('狗在线 0/1', true), ('备份过期了', true)]);
      await _duty(t, FakeApi('guard')..summaryError = const SiteError(500, '站点内部错误'));
      expect(find.byType(NwHealthChip, skipOffstage: false), findsNothing);
      expect(find.byKey(SiteWatchPage.summaryErrorKey, skipOffstage: false), findsOneWidget);
    });

    testWidgets('页头：给了站点名就写站点名', (t) async {
      t.view.physicalSize = const Size(780, 1688);
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      await t.pumpWidget(MaterialApp(home: SiteWatchPage(api: FakeApi('guard'), title: '东门庄园')));
      await t.pumpAndSettle();
      expect(find.text('东门庄园'), findsOneWidget);
    });
  });

  group('告警页：底栏、小地图', () {
    Future<void> scene(WidgetTester t, FakeApi api, Alert a, Size size, {ThemeData? theme}) async {
      t.view.physicalSize = size * 2;
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      api.alertRows = <Map<String, dynamic>>[];
      await t.pumpWidget(const SizedBox());
      await t.pumpWidget(MaterialApp(theme: theme, home: SiteAlertScenePage(api: api, alert: a)));
      await t.pumpAndSettle();
    }

    for (final size in const [Size(390, 844), Size(800, 360)]) {
      testWidgets('四个大按钮在底栏：不跟着滚、够高、字不小于 14（${size.width.toInt()}）', (t) async {
        final api = FakeApi('guard');
        await scene(t, api, _intrusion(), size, theme: nwTheme(Brightness.dark));
        final buttons = find.byWidgetPredicate((w) =>
            (w is FilledButton || w is OutlinedButton) &&
            find.ancestor(of: find.byWidget(w), matching: find.byKey(SiteAlertScenePage.barKey)).evaluate().isNotEmpty);
        expect(buttons, findsNWidgets(4));
        for (final k in [SiteAlertScenePage.ackKey, SiteAlertScenePage.falseKey]) {
          expect(find.ancestor(of: find.byKey(k), matching: find.byKey(SiteAlertScenePage.barKey)), findsOneWidget);
          expect(find.ancestor(of: find.byKey(k), matching: find.byType(Scrollable)), findsNothing);
          final r = t.getRect(find.byKey(k));
          expect(r.height, greaterThanOrEqualTo(48));
          expect(r.bottom, lessThanOrEqualTo(size.height));
          expect(r.top, greaterThan(size.height / 2), reason: '在屏幕下半边');
          final text = t.widget<Text>(find.descendant(of: find.byKey(k), matching: find.byType(Text)));
          final style = DefaultTextStyle.of(t.element(find.byWidget(text))).style.merge(text.style);
          expect(style.fontSize, greaterThanOrEqualTo(14));
        }
        await t.pumpWidget(Container());
      });
    }

    testWidgets('有位置的告警：小地图取的是站点那张图，点了进地图；没有位置的没有这一块', (t) async {
      final api = FakeApi('guard');
      await scene(t, api, _intrusion(), const Size(390, 844));
      expect(api.calls.where((c) => c.startsWith('mapPreviewPng')), ['mapPreviewPng estate-1:7']);
      expect(find.byKey(SiteAlertScenePage.mapKey), findsOneWidget);
      final none = FakeApi('guard');
      await scene(t, none, _intrusion(context: <String, dynamic>{'zone': 'z'}), const Size(390, 844));
      expect(none.calls.where((c) => c.startsWith('mapPreviewPng')), isEmpty);
      expect(find.byKey(SiteAlertScenePage.mapKey), findsNothing);
      await t.pumpWidget(Container());
    });

    testWidgets('小地图：深色下只有栅格底图过地图栏同一个反色滤镜，下面那行字不过；浅色没有滤镜', (t) async {
      for (final b in Brightness.values) {
        await scene(t, FakeApi('guard'), _intrusion(), const Size(390, 844), theme: nwTheme(b));
        expect(find.byKey(SiteAlertScenePage.mapKey), findsOneWidget);
        final thumb = find.byType(NwMapThumb);
        expect(find.descendant(of: thumb, matching: find.byType(Image)), findsOneWidget);
        final filter = find.byKey(NwMapThumb.darkRasterKey);
        if (b == Brightness.light) {
          expect(filter, findsNothing);
          expect(find.descendant(of: thumb, matching: find.byType(ColorFiltered)), findsNothing);
          continue;
        }
        expect(filter, findsOneWidget);
        expect(t.widget<ColorFiltered>(filter).colorFilter, darkMapRaster);
        expect(find.descendant(of: filter, matching: find.byType(Image)), findsOneWidget);
        expect(find.descendant(of: filter, matching: find.byType(Text)), findsNothing);
        expect(find.descendant(of: filter, matching: find.byType(Icon)), findsNothing);
      }
      await t.pumpWidget(Container());
    });

    testWidgets('英文界面的值守首页、告警页：新加的字都有英文', (t) async {
      appLang.value = AppLang.en;
      addTearDown(() => appLang.value = AppLang.zh);
      final api = FakeApi('owner')..modeView = _mode('armed');
      await _duty(t, api);
      expect(find.text('Now: Armed'), findsOneWidget);
      expect(find.text('RESPOND TO INCIDENT'), findsOneWidget);
      expect(find.text('Robots online 1/1', skipOffstage: false), findsOneWidget);
    });
  });

  // 版面矩阵（layout_matrix_test）里这几页用的是最省事的数据（没在驱离、没接上装）。这里换成东西最多的
  // 样子，手机竖屏横屏、桌面两种窗口、中英文、浅色深色都画一遍：不溢出，字不小于 12。
  group('这一批几页：东西最多的样子也不溢出、字不小于 12', () {
    final t0 = DateTime(2026, 10, 11, 2, 14, 36).millisecondsSinceEpoch;
    Alert seen({bool acked = false, String level = 'P1'}) => Alert.fromWire(<String, dynamic>{
          'key': 'A/dog_sees_person#1', 'level': level, 'kind': 'dog_sees_person', 'robot': 'A',
          'title': '狗看见人了:1 个人,最近 6.2 m', 'detail': '布防中', 'count': 3,
          'first_ms': t0, 'last_ms': t0 + 9000,
          'acked_by': acked ? 'night.guard' : '', 'acked_ms': acked ? t0 + 44000 : null,
          'context': <String, dynamic>{'zone': 'East wall', 'task_id': 'persons',
            'persons': <String, dynamic>{'count': 1, 'nearest_m': 6.2},
            'intercept': <String, dynamic>{'name': 'gate', 'map_id': 'estate-1', 'map_version': '7',
              'x': 1.0, 'y': 0.0},
            'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': '7', 'x': 3.0, 'y': 4.0,
              'at_ms': t0}}});
    FakeApi deterring(String role) => FakeApi(role)
      ..alertRows = <Map<String, dynamic>>[]
      ..deterRows = [
        <String, dynamic>{'robot_id': 'A', 'level': 3, 'auto': true, 'next_in_s': 30, 'zone': 'yard',
          'ends_in_s': 539, 'started_ms': t0 + 4000, 'level_ms': t0 + 9000, 'active': ['strobe'],
          'outputs': ['siren', 'speaker', 'strobe'],
          'persons': <String, dynamic>{'count': 2, 'nearest_m': 7.5}, 'standoff': 'retreat'}
      ];
    FakeApi busy() {
      final rows = (siteFixture('site_alerts')['alerts'] as List).cast<Map<String, dynamic>>();
      return FakeApi('admin')
        ..alertRows = [
          rows.first,
          <String, dynamic>{...rows.first, 'key': 'B/fallen#2', 'robot': 'B', 'kind': 'fallen',
            'acked_by': 'night.guard', 'acked_ms': 5, 'escalated': 2, 'channel': 'sound', 'count': 4},
          <String, dynamic>{...rows.first, 'key': 'A/low_battery#3', 'level': 'P2', 'kind': 'low_battery',
            'title': '电量低', 'detail': '电量 12%，还在巡检'},
          <String, dynamic>{...rows.first, 'key': 'site/clock_skew#4', 'level': 'P3', 'robot': 'site',
            'kind': 'clock_skew', 'title': '钟差偏大', 'detail': ''},
        ];
    }

    final pages = <String, Widget Function()>{
      'duty (admin, busy)': () => SiteWatchPage(api: busy(), title: 'East Gate Estate North Compound'),
      'duty (guard)': () => SiteWatchPage(api: FakeApi('guard')),
      'duty (mode unreadable)': () =>
          SiteWatchPage(api: FakeApi('owner')..modeError = const SiteError(502, 'bad gateway from the site')),
      'alarm (deterring)': () => SiteAlertScenePage(api: deterring('guard'), alert: seen()),
      'alarm (acked, owner)': () => SiteAlertScenePage(api: deterring('owner'), alert: seen(acked: true)),
      'alarm (P2)': () => SiteAlertScenePage(api: FakeApi('guard'), alert: seen(level: 'P2')),
      'mode (admin)': () => SiteModePage(api: FakeApi('admin')),
      'mode (guard)': () => SiteModePage(api: FakeApi('guard')),
      'deterrence (session)': () => SiteDeterrencePage(api: deterring('guard'), robotId: 'A'),
      'payload (all)': () => SiteDeterPage(api: FakeApi('guard'), robotId: 'A', caps: const <String, dynamic>{
            'outputs': ['strobe', 'siren', 'spotlight', 'speaker'],
            'clips': ['warn-zh', 'warn-en', 'leave-now-private-property'],
            'tts': true}),
    };
    for (final b in Brightness.values) {
      for (final lang in AppLang.values) {
        for (final size in const [Size(390, 844), Size(800, 360), Size(1280, 720), Size(1920, 1080)]) {
          for (final e in pages.entries) {
            testWidgets('[${b.name} ${lang.name} ${size.width.toInt()}×${size.height.toInt()}] ${e.key}', (t) async {
              appLang.value = lang;
              addTearDown(() => appLang.value = AppLang.zh);
              t.view.physicalSize = size;
              t.view.devicePixelRatio = 1.0;
              addTearDown(t.view.reset);
              await t.pumpWidget(MaterialApp(theme: nwTheme(b), home: e.value()));
              await t.pumpAndSettle();
              expect(t.takeException(), isNull);
              for (final p in t.renderObjectList<RenderParagraph>(find.byType(RichText))) {
                final px = p.text.style?.fontSize;
                expect(px == null || px >= 12, isTrue, reason: '字小于 12：「${p.text.toPlainText()}」 $px');
              }
              await t.pumpWidget(Container());
            });
          }
        }
      }
    }

    testWidgets('告警页按了之后的那句话很长：在底栏里自己滚，不把按钮挤出屏幕（横屏 800×360）', (t) async {
      t.view.physicalSize = const Size(800, 360);
      t.view.devicePixelRatio = 1.0;
      addTearDown(t.view.reset);
      final api = deterring('guard')..robotsError = SiteError(502, '站点没回 ' * 120);
      await t.pumpWidget(MaterialApp(home: SiteAlertScenePage(api: api, alert: seen())));
      await t.pumpAndSettle();
      await t.tap(find.byKey(StopAllButton.buttonKey));
      await t.pumpAndSettle();
      expect(t.takeException(), isNull);
      expect(find.byKey(SiteAlertScenePage.msgKey), findsOneWidget);
      expect(t.getRect(find.byKey(SiteAlertScenePage.ackKey)).bottom, lessThanOrEqualTo(360));
      await t.pumpWidget(Container());
    });
  });
}
