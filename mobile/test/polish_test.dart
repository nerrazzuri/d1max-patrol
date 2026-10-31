// 2026-10-14 重做之后的收尾：用户看截图挑出来的几处版面毛病，各钉一条。
import 'dart:io';

import 'package:d1max_patrol/design/nw_tokens.g.dart';
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_runs.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

/// 真字体（测试的缺省字体每个字一样宽，量不出真的折行）。中文字体机器上有就用，没有落到缺省字体（更宽，更严）。
Future<void> _realFonts(WidgetTester t) => t.runAsync(() async {
      Future<void> load(String family, String path) async {
        final f = File(path);
        if (!f.existsSync()) return;
        final loader = FontLoader(family)..addFont(f.readAsBytes().then((b) => ByteData.view(b.buffer)));
        await loader.load();
      }

      await load(NwFont.sans, '${Directory.current.path}/assets/fonts/Inter.ttf');
      await load(NwFont.mono, '${Directory.current.path}/assets/fonts/JetBrainsMono.ttf');
      await load('Noto Sans SC', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc');
    });

Alert _p1() => Alert.fromWire(<String, dynamic>{
      'key': 'A/dog_sees_person#1', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'A',
      'title': '机器人看见人了', 'detail': '', 'count': 1, 'first_ms': 1000, 'last_ms': 1000, 'acked_by': '',
      'acked_ms': null, 'context': <String, dynamic>{'zone': 'East wall'},
    });

void main() {
  for (final lang in AppLang.values) {
    testWidgets('告警页底栏：390×844 上四个按钮的字都是一行（${lang.name}）', (t) async {
      await _realFonts(t);
      appLang.value = lang;
      addTearDown(() => appLang.value = AppLang.zh);
      t.view.physicalSize = const Size(390, 844) * 2;
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      final api = FakeApi('guard')
        ..deterRows = [
          <String, dynamic>{'robot_id': 'A', 'level': 1, 'auto': true, 'next_in_s': 30, 'started_ms': 2000}
        ];
      await t.pumpWidget(MaterialApp(
          home: SiteAlertScenePage(api: api, alert: _p1(), now: () => DateTime.fromMillisecondsSinceEpoch(40000))));
      await t.pumpAndSettle();
      final labels = [
        tr("I'll handle it", '我来处理'),
        tr('Sound siren (level 3)', '开警笛（第 3 级）'),
        tr('False alarm', '误报'),
        tr('Talk: not available', '对讲：暂不支持'),
      ];
      for (final l in labels) {
        final f = find.descendant(of: find.byKey(SiteAlertScenePage.barKey), matching: find.text(l));
        expect(f, findsOneWidget, reason: l);
        final style = t.widget<Text>(f).style ?? DefaultTextStyle.of(t.element(f)).style;
        final fontSize = DefaultTextStyle.of(t.element(f)).style.merge(style).fontSize!;
        expect(fontSize, greaterThanOrEqualTo(14), reason: '告警页按钮的字不低于 14');
        expect(t.getSize(f).height, lessThan(fontSize * 2), reason: '"$l" 折成了两行');
      }
    });
  }

  group('巡检卡：有异常只把异常那一段标红', () {
    TextSpan spanOf(WidgetTester t, String plain) =>
        t.widget<Text>(find.text(plain)).textSpan! as TextSpan;

    testWidgets('卡片：异常那一段错误色加粗，其余次要字色、不加粗', (t) async {
      const status = 'A · 2 photos · Abnormal 1 · Normal 1 · 1 reviewed';
      await t.pumpWidget(MaterialApp(
          theme: nwTheme(Brightness.light),
          home: const Scaffold(
              body: NwPatrolCard(
                  id: 'loop', time: '10:30', status: status, anomaly: true, tone: NwTone.error, flagged: 'Abnormal 1'))));
      final text = t.widget<Text>(find.text(status));
      expect(text.style!.color, NwColors.light.textSecondary);
      expect(text.style!.fontWeight, FontWeight.w400);
      final parts = spanOf(t, status).children!.cast<TextSpan>();
      expect(parts.map((p) => p.text), ['A · 2 photos · ', 'Abnormal 1', ' · Normal 1 · 1 reviewed']);
      expect(parts[0].style, isNull, reason: '照常：用整行的次要字色');
      expect(parts[1].style!.color, NwColors.light.error);
      expect(parts[1].style!.fontWeight, FontWeight.w700);
      expect(parts[2].style, isNull);
    });

    testWidgets('记录页：「异常 1」红，「正常 / 已复核」不红；报告图标还在', (t) async {
      await t.pumpWidget(MaterialApp(
          theme: nwTheme(Brightness.dark),
          home: Scaffold(body: SiteRunsPage(api: FakeApi('guard'), embedded: true))));
      await t.pumpAndSettle();
      expect(find.byIcon(Icons.report), findsOneWidget);
      final card = t.widget<NwPatrolCard>(find.byType(NwPatrolCard));
      expect(card.anomaly, isTrue);
      expect(card.flagged, '异常 1');
      final parts = spanOf(t, card.status).children!.cast<TextSpan>();
      final hot = parts.singleWhere((p) => p.style != null);
      expect(hot.text, '异常 1');
      expect(hot.style!.color, NwColors.dark.error);
      expect(t.widget<Text>(find.text(card.status)).style!.color, NwColors.dark.textSecondary);
    });
  });

  testWidgets('地图栏：深色下只有栅格底图过反色滤镜，上面画的不过；浅色没有滤镜', (t) async {
    for (final b in Brightness.values) {
      await t.pumpWidget(MaterialApp(theme: nwTheme(b), home: Scaffold(body: SiteMapTab(api: FakeApi('guard')))));
      await t.pumpAndSettle();
      expect(find.byKey(SiteMapTab.canvasKey), findsOneWidget);
      final filter = find.byKey(SiteMapTab.darkRasterKey);
      if (b == Brightness.light) {
        expect(filter, findsNothing);
        continue;
      }
      expect(filter, findsOneWidget);
      expect(t.widget<ColorFiltered>(filter).colorFilter, darkMapRaster);
      expect(find.descendant(of: filter, matching: find.byType(Image)), findsOneWidget);
      expect(find.descendant(of: filter, matching: find.byType(CustomPaint)), findsNothing);
      expect(find.ancestor(of: find.byKey(SiteMapTab.standbyKey), matching: filter), findsNothing);
      expect(find.ancestor(of: find.byKey(SiteMapTab.zonesKey), matching: filter), findsNothing);
    }
  });

  testWidgets('值守页：高不到 480（横着拿）页头压成一行、变矮；竖屏照旧', (t) async {
    addTearDown(t.view.reset);
    t.view.devicePixelRatio = 1.0;
    final heights = <Size, double>{};
    for (final size in const [Size(390, 844), Size(800, 360), Size(1280, 720)]) {
      t.view.physicalSize = size;
      await t.pumpWidget(MaterialApp(
          theme: nwTheme(Brightness.light), home: SiteWatchPage(api: FakeApi('guard'), title: 'East Gate Estate')));
      await t.pumpAndSettle();
      final header = t.widget<NwHeader>(find.byKey(SiteWatchPage.headerKey));
      expect(header.compact, size.height < 480, reason: '$size');
      expect(find.descendant(of: find.byKey(SiteWatchPage.headerKey), matching: find.textContaining('East Gate Estate')), findsOneWidget, reason: '标题还在');
      heights[size] = t.getSize(find.byKey(SiteWatchPage.headerKey)).height;
      expect(t.takeException(), isNull);
    }
    expect(heights[const Size(800, 360)]!, lessThan(heights[const Size(390, 844)]! - 16),
        reason: '矮屏的页头至少矮 16');
    expect(heights[const Size(800, 360)]!, greaterThanOrEqualTo(28));
  });
}
