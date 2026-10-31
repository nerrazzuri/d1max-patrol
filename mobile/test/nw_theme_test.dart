// 新设计系统的主题（2026-10-14 重做）：颜色从上下文取、跟着浅色深色走；上下文里没有我们的主题也不抛
// （大多数测试是光秃秃的 MaterialApp）；字体打在包里；最小字号 12。
import 'dart:io';

import 'package:d1max_patrol/design/nw_tokens.g.dart';
import 'package:d1max_patrol/main.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Future<NwColors> _colorsUnder(WidgetTester t, ThemeData? theme) async {
  late NwColors got;
  // 先清掉上一棵：同一个 MaterialApp 换主题有一小段过渡，第一帧还是旧的
  await t.pumpWidget(const SizedBox());
  await t.pumpWidget(MaterialApp(
      theme: theme,
      home: Builder(builder: (c) {
        got = Nw.of(c);
        return const SizedBox();
      })));
  return got;
}

void main() {
  group('Nw.of', () {
    testWidgets('上下文里没有我们的主题：不抛，按那个主题是亮是暗回一套', (t) async {
      expect(await _colorsUnder(t, null), same(NwColors.light), reason: '光秃秃的 MaterialApp 是亮的');
      expect(await _colorsUnder(t, ThemeData.dark()), same(NwColors.dark));
      expect(t.takeException(), isNull);
    });

    testWidgets('有我们的主题：浅色给浅色那套，深色给深色那套', (t) async {
      expect(await _colorsUnder(t, nwTheme(Brightness.light)), same(NwColors.light));
      expect(await _colorsUnder(t, nwTheme(Brightness.dark)), same(NwColors.dark));
    });

    testWidgets('连 MaterialApp 都没有（只有一个 Directionality）也不抛', (t) async {
      late NwColors got;
      await t.pumpWidget(Directionality(
          textDirection: TextDirection.ltr,
          child: Builder(builder: (c) {
            got = Nw.of(c);
            return const SizedBox();
          })));
      expect(got, same(NwColors.light));
    });
  });

  group('nwTheme', () {
    test('浅色、深色各一份：亮暗、底色、主色、扩展都对得上令牌', () {
      for (final (b, c) in [(Brightness.light, NwColors.light), (Brightness.dark, NwColors.dark)]) {
        final th = nwTheme(b);
        expect(th.brightness, b);
        expect(th.colorScheme.brightness, b);
        expect(th.scaffoldBackgroundColor, c.bg);
        expect(th.colorScheme.primary, c.primary);
        expect(th.colorScheme.onPrimary, c.onPrimary);
        expect(th.colorScheme.error, c.error);
        expect(th.extension<NwColors>(), same(c));
        expect(th.appBarTheme.backgroundColor, c.surface);
        expect(th.cardTheme.color, c.surface);
        expect(th.colorScheme.shadow, Colors.transparent, reason: '设计稿没有阴影');
      }
      expect(NwColors.light.bg, const Color(0xFFFDFDFD));
      expect(NwColors.light.primary, const Color(0xFF1A1C1E));
      expect(NwColors.dark.bg, const Color(0xFF0A0C10));
      expect(NwColors.dark.primary, const Color(0xFFD1E4FF));
    });

    test('正文 Inter 14；标题 Space Grotesk；标签也是 Inter（W60：等宽只给编号、数字）；中文落到系统字体', () {
      for (final b in Brightness.values) {
        final tt = nwTheme(b).textTheme;
        expect(tt.bodyMedium!.fontFamily, 'Inter');
        expect(tt.bodyMedium!.fontSize, 14);
        expect(tt.bodyMedium!.fontFamilyFallback, contains('Noto Sans SC'));
        expect(tt.headlineMedium!.fontFamily, 'Space Grotesk');
        expect(tt.headlineMedium!.fontSize, 28);
        expect(tt.headlineMedium!.fontWeight, FontWeight.w600);
        expect(tt.labelMedium!.fontFamily, 'Inter');
        expect(tt.titleMedium!.fontFamily, 'Inter');
        expect(tt.titleMedium!.fontWeight, FontWeight.w600);
      }
    });

    test('最小字号 12（设计稿里 10 的抬到了 12）；主题里每一种字都带颜色', () {
      for (final s in NwText.all) {
        expect(s.fontSize, greaterThanOrEqualTo(12));
        expect(s.color, isNull, reason: 'NwText 不带颜色，用的地方自己配');
      }
      expect(NwText.labelSmall.fontSize, 12);
      for (final b in Brightness.values) {
        final th = nwTheme(b);
        final tt = th.textTheme;
        for (final s in [
          tt.headlineMedium, tt.headlineSmall, tt.titleLarge, tt.titleMedium, tt.titleSmall, tt.labelLarge,
          tt.labelMedium, tt.labelSmall, tt.bodyLarge, tt.bodyMedium, tt.bodySmall,
          th.appBarTheme.titleTextStyle, th.listTileTheme.titleTextStyle, th.listTileTheme.subtitleTextStyle,
        ]) {
          expect(s!.fontSize, greaterThanOrEqualTo(12));
          expect(s.color, isNotNull);
        }
      }
    });

    test('间距、圆角是设计稿那几档', () {
      expect([Nw.s4, Nw.s8, Nw.s16, Nw.s24, Nw.s32], [4, 8, 16, 24, 32]);
      expect([Nw.r2, Nw.r4, Nw.r8, Nw.r12, Nw.rPill], [2, 4, 8, 12, 9999]);
    });

    test('浅色深色之间过渡：每个色各自渐变，两头就是两套本身', () {
      final mid = NwColors.light.lerp(NwColors.dark, 0.5);
      expect(mid.bg, Color.lerp(NwColors.light.bg, NwColors.dark.bg, 0.5));
      expect(mid.error, Color.lerp(NwColors.light.error, NwColors.dark.error, 0.5));
      expect(NwColors.light.lerp(NwColors.dark, 0).onScrim, NwColors.light.onScrim);
      final end = NwColors.light.lerp(NwColors.dark, 1);
      expect(end.primary, NwColors.dark.primary);
      expect(end.scrim, NwColors.dark.scrim);
      expect(end.textMuted, NwColors.dark.textMuted);
      expect(NwColors.light.lerp(null, 0.5), same(NwColors.light));
      expect(NwColors.dark.copyWith(), same(NwColors.dark));
    });
  });

  group('整个 App', () {
    testWidgets('浅色、深色两套都挂上了，跟着系统走：系统是深色，页面拿到的就是深色那套', (t) async {
      addTearDown(t.platformDispatcher.clearPlatformBrightnessTestValue);
      t.platformDispatcher.platformBrightnessTestValue = Brightness.light;
      await t.pumpWidget(PatrolApp(siteStore: MemorySiteStore()));
      await t.pumpAndSettle();
      final app = t.widget<MaterialApp>(find.byType(MaterialApp));
      expect(app.themeMode, ThemeMode.system);
      expect(app.theme!.extension<NwColors>(), same(NwColors.light));
      expect(app.darkTheme!.extension<NwColors>(), same(NwColors.dark));
      final ctx = t.element(find.byType(SiteListPage));
      expect(Nw.of(ctx), same(NwColors.light));
      expect(Theme.of(ctx).scaffoldBackgroundColor, NwColors.light.bg);
      t.platformDispatcher.platformBrightnessTestValue = Brightness.dark;
      await t.pumpAndSettle();
      expect(Theme.of(ctx).brightness, Brightness.dark);
      expect(Nw.of(ctx).bg, NwColors.dark.bg);
      expect(Theme.of(ctx).scaffoldBackgroundColor, NwColors.dark.bg);
      expect(Theme.of(ctx).textTheme.bodyMedium!.fontFamily, 'Inter');
    });
  });

  group('色调', () {
    test('红只给 P1、急停、离线、不可撤回；橙只给 P2、低电；绿给正常；不认得的不上色', () {
      const red = {'p1', 'estop', 'offline', 'destructive'};
      const orange = {'p2', 'lowBattery'};
      for (final use in nwUsage.keys) {
        expect(nwToneOf(use) == NwTone.error, red.contains(use), reason: use);
        expect(nwToneOf(use) == NwTone.warning, orange.contains(use), reason: use);
      }
      for (final use in [...red, ...orange]) {
        expect(nwUsage, contains(use));
      }
      expect(nwToneOf('online'), NwTone.success);
      expect(nwToneOf('connected'), NwTone.success);
      expect(nwToneOf('p3'), NwTone.neutral);
      expect(nwToneOf('idle'), NwTone.neutral);
      expect(nwToneOf('没有这种用途'), NwTone.neutral);
    });

    test('色调落到颜色上：字色、软底、实底各是各的，不会同色压同色', () {
      for (final c in [NwColors.light, NwColors.dark]) {
        for (final tone in NwTone.values) {
          expect(c.fg(tone), isNot(c.soft(tone)), reason: '$tone');
          final (bg, fg) = c.solid(tone);
          expect(fg, isNot(bg), reason: '$tone');
        }
        expect(c.fg(NwTone.error), c.error);
        expect(c.soft(NwTone.error), c.errorSoft);
        expect(c.solid(NwTone.warning), (c.warning, c.onWarning));
        expect(c.fg(NwTone.neutral), c.text);
      }
    });
  });

  group('字体打在包里', () {
    final pubspec = File('pubspec.yaml').readAsStringSync();

    test('三种字体都在 pubspec 里登记了、文件在；不靠运行时去取', () {
      for (final family in [NwFont.display, NwFont.sans, NwFont.mono]) {
        expect(pubspec, contains('- family: $family'), reason: family);
      }
      final assets = RegExp(r'- asset: (assets/fonts/\S+\.ttf)').allMatches(pubspec).map((m) => m.group(1)!);
      expect(assets.length, greaterThanOrEqualTo(3));
      for (final a in assets) {
        expect(File(a).existsSync(), isTrue, reason: a);
        expect(File(a).lengthSync(), greaterThan(10000), reason: a);
      }
      expect(pubspec, isNot(contains('google_fonts')));
    });

    test('许可证：每种字体一份 OFL，登记进许可证页的文件都打进了包里', () {
      final families = fontLicenses.expand((e) => e.$1).toSet();
      expect(families, containsAll(<String>[NwFont.display, NwFont.sans, NwFont.mono]));
      for (final (_, path) in fontLicenses) {
        expect(File(path).existsSync(), isTrue, reason: path);
        expect(File(path).readAsStringSync(), contains('SIL OPEN FONT LICENSE'), reason: path);
        expect(pubspec, contains('- $path'), reason: '$path 要列在 pubspec 的 assets 里，不然 rootBundle 读不到');
      }
    });
  });
}
