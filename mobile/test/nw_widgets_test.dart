// 新设计系统的组件：浅色、深色、不带主题三种都画得出来；390 宽、中文长文案不溢出；按钮的 Key 落在
// 里面那个标准按钮上；状态色不会同色字压同色底（设计稿里有三处是那样，看不见）。
import 'dart:math' as math;

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/ui/nw/nw_button.dart';
import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

const _long = '东门外第三号围墙拐角处的机器狗看见了一个没有授权的人正在靠近';
const _longEn = 'Unauthorized person approaching the third perimeter corner outside the East Gate';

/// 三种主题：浅色、深色、不带（光秃秃的 MaterialApp）。
final _themes = <String, ThemeData?>{
  'light': nwTheme(Brightness.light),
  'dark': nwTheme(Brightness.dark),
  'no theme': null,
};

Future<void> _pump(WidgetTester t, Widget child,
    {ThemeData? theme, Size size = const Size(390, 844), bool scroll = false}) async {
  t.view.physicalSize = size * 2;
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
  final body = Padding(padding: const EdgeInsets.all(Nw.s24), child: child);
  await t.pumpWidget(MaterialApp(
      theme: theme, home: Scaffold(body: scroll ? SingleChildScrollView(child: body) : Center(child: body))));
  await t.pumpAndSettle();
}

double _lum(Color c) {
  double f(double x) => x <= 0.03928 ? x / 12.92 : math.pow((x + 0.055) / 1.055, 2.4).toDouble();
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
}

double _contrast(Color a, Color b) {
  final hi = math.max(_lum(a), _lum(b)), lo = math.min(_lum(a), _lum(b));
  return (hi + 0.05) / (lo + 0.05);
}

Color _textColor(WidgetTester t, String text) => t.widget<Text>(find.text(text)).style!.color!;

/// 每个组件各摆一个，文案故意很长。
Widget _gallery(String long) => Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      NwHeader(
          title: long,
          overline: 'Nexus Warden',
          subtitle: long,
          leading: const BackButton(),
          trailing: [IconButton(onPressed: () {}, icon: const Icon(Icons.add_rounded))]),
      NwSectionLabel(long, trailing: const Text('12')),
      for (final v in NwButtonVariant.values)
        for (final s in NwButtonSize.values)
          Align(
              alignment: Alignment.centerLeft,
              child: NwButton(label: long, icon: Icons.arrow_forward_rounded, variant: v, size: s, onPressed: () {})),
      NwButton(label: long, fullWidth: true, onPressed: null),
      for (final tone in NwTone.values) ...[
        Align(alignment: Alignment.centerLeft, child: NwStatusPill(label: long, tone: tone, dot: true)),
        NwCard(tone: tone, child: Text(long)),
        NwStatTile(label: long, value: long, icon: Icons.battery_full_rounded, tone: tone),
        NwDiagnosticRow(icon: Icons.schedule_rounded, label: long, value: long, tone: tone),
        NwNotice(text: long, tone: tone),
        NwAlertItem(title: long, level: 'P1', tone: tone, source: long, time: long, onTap: () {}),
        NwDogCard(id: long, status: long, tone: tone, activity: long, battery: 42, p1: tone == NwTone.error),
        NwPatrolCard(
            id: long,
            time: long,
            type: long.substring(0, 9),
            status: long,
            anomaly: tone != NwTone.neutral,
            tone: tone,
            thumbs: const [ColoredBox(color: Colors.grey), ColoredBox(color: Colors.grey), ColoredBox(color: Colors.grey)],
            more: 4),
        NwVersionCard(entity: long, version: long, note: long, noteTone: tone),
      ],
      Align(
          alignment: Alignment.centerLeft,
          child: NwHealthChip(icon: Icons.cloud_done_rounded, label: long)),
      Align(
          alignment: Alignment.centerLeft,
          child: NwHealthChip(icon: Icons.cloud_off_rounded, label: long, bad: true)),
      NwSegment<int>(values: const [1, 2, 3], selected: 2, onChanged: (_) {}, labelOf: (v) => '$long $v'),
      Row(children: [
        Expanded(child: NwActionTile(icon: Icons.explore_rounded, label: long, onTap: () {})),
        const SizedBox(width: Nw.s8),
        Expanded(child: NwActionTile(icon: Icons.videogame_asset_rounded, label: long)),
        const SizedBox(width: Nw.s8),
        Expanded(
            child: NwActionTile(icon: Icons.delete_rounded, label: long, tone: NwTone.error, onTap: () {})),
      ]),
      const Row(children: [NwBattery(percent: 5), NwBattery(percent: 80), NwBattery(percent: null)]),
      NwSiteCard(
          name: long,
          address: long,
          status: long.substring(0, 6),
          p1Count: 2,
          onEnter: () {},
          menu: IconButton(onPressed: () {}, icon: const Icon(Icons.more_vert_rounded))),
      NwSiteCard(name: long, address: long, connected: false, status: 'OFFLINE', p1Count: 0, onEnter: () {}),
      NwSiteCard(name: long, address: long),
      SizedBox(height: 240, child: NwEmpty(icon: Icons.inbox_rounded, text: long, hint: long, action: NwButton(label: long))),
    ]);

void main() {
  group('每个组件都画得出来、不溢出', () {
    for (final th in _themes.entries) {
      for (final lang in AppLang.values) {
        for (final size in const [Size(390, 844), Size(360, 640), Size(800, 360)]) {
          testWidgets('[${th.key}] [${lang.name}] ${size.width.toInt()}×${size.height.toInt()}', (t) async {
            appLang.value = lang;
            addTearDown(() => appLang.value = AppLang.zh);
            await _pump(t, _gallery(lang == AppLang.zh ? _long : _longEn),
                theme: th.value, size: size, scroll: true);
            expect(t.takeException(), isNull);
            // 没有永远在动的动画：上面的 pumpAndSettle 回得来
          });
        }
      }
    }

    testWidgets('字号都不小于 12', (t) async {
      await _pump(t, _gallery(_long), theme: nwTheme(Brightness.light), scroll: true);
      final small = <String>[];
      for (final e in find.byType(Text, skipOffstage: false).evaluate()) {
        final text = e.widget as Text;
        final size = text.style?.fontSize ?? DefaultTextStyle.of(e).style.fontSize!;
        if (size < 12) small.add('${text.data} @ $size');
      }
      expect(small, isEmpty);
    });
  });

  group('NwButton', () {
    testWidgets('buttonKey 落在里面的标准按钮上：实底的是 FilledButton，细边的是 OutlinedButton，没边的是 TextButton', (t) async {
      const want = <NwButtonVariant, Type>{
        NwButtonVariant.primary: FilledButton,
        NwButtonVariant.secondary: FilledButton,
        NwButtonVariant.destructive: FilledButton,
        NwButtonVariant.outline: OutlinedButton,
        NwButtonVariant.ghost: TextButton,
      };
      expect(want.keys.toSet(), NwButtonVariant.values.toSet());
      for (final th in _themes.values) {
        for (final e in want.entries) {
          var taps = 0;
          await _pump(
              t,
              NwButton(
                  key: const Key('outer'),
                  buttonKey: const Key('inner'),
                  label: '我来处理',
                  variant: e.key,
                  onPressed: () => taps++),
              theme: th);
          expect(t.widget(find.byKey(const Key('inner'))).runtimeType, e.value, reason: '${e.key}');
          expect(t.widget(find.byKey(const Key('outer'))), isA<NwButton>());
          await t.tap(find.byKey(const Key('inner')));
          expect(taps, 1);
          await t.tap(find.text('我来处理'));
          expect(taps, 2);
        }
      }
    });

    testWidgets('不给 onPressed 就是禁用：里面的按钮点不动、换成灰的', (t) async {
      for (final b in Brightness.values) {
        final c = b == Brightness.dark ? NwColors.dark : NwColors.light;
        for (final v in NwButtonVariant.values) {
          await _pump(t, NwButton(buttonKey: const Key('b'), label: '叫停', variant: v), theme: nwTheme(b));
          final button = t.widget<ButtonStyleButton>(find.byKey(const Key('b')));
          expect(button.enabled, isFalse, reason: '$v');
          expect(button.style!.foregroundColor!.resolve({WidgetState.disabled}), c.textMuted, reason: '$v');
        }
      }
    });

    testWidgets('颜色：主色实底；红的只有 destructive；细边的边是 outline', (t) async {
      for (final b in Brightness.values) {
        final c = b == Brightness.dark ? NwColors.dark : NwColors.light;
        Color? bg(NwButtonVariant v) =>
            t.widget<ButtonStyleButton>(find.byKey(Key(v.name))).style!.backgroundColor?.resolve({});
        Color? fg(NwButtonVariant v) =>
            t.widget<ButtonStyleButton>(find.byKey(Key(v.name))).style!.foregroundColor?.resolve({});
        await _pump(
            t,
            Column(mainAxisSize: MainAxisSize.min, children: [
              for (final v in NwButtonVariant.values)
                NwButton(buttonKey: Key(v.name), label: v.name, variant: v, onPressed: () {}),
            ]),
            theme: nwTheme(b));
        expect((bg(NwButtonVariant.primary), fg(NwButtonVariant.primary)), (c.primary, c.onPrimary));
        expect((bg(NwButtonVariant.secondary), fg(NwButtonVariant.secondary)), (c.secondary, c.onPrimary));
        expect((bg(NwButtonVariant.destructive), fg(NwButtonVariant.destructive)), (c.error, c.onError));
        expect(fg(NwButtonVariant.outline), c.text);
        expect(fg(NwButtonVariant.ghost), c.primary);
        for (final v in NwButtonVariant.values) {
          if (v != NwButtonVariant.destructive) expect(bg(v), isNot(c.error), reason: '$v');
        }
        final side = t
            .widget<OutlinedButton>(find.byKey(Key(NwButtonVariant.outline.name)))
            .style!
            .side!
            .resolve({})!;
        expect(side.color, c.outline);
      }
    });

    testWidgets('三种大小：圆角 4 / 8 / 12，越大越高；都不小于手指点得准的宽', (t) async {
      final heights = <double>[];
      for (final (s, radius) in [(NwButtonSize.small, 4.0), (NwButtonSize.medium, 8.0), (NwButtonSize.large, 12.0)]) {
        await _pump(t, NwButton(buttonKey: const Key('b'), label: 'OK', size: s, onPressed: () {}),
            theme: nwTheme(Brightness.light));
        final style = t.widget<FilledButton>(find.byKey(const Key('b'))).style!;
        expect((style.shape!.resolve({})! as RoundedRectangleBorder).borderRadius, BorderRadius.circular(radius));
        expect(style.textStyle!.resolve({})!.fontSize, greaterThanOrEqualTo(12));
        final size = t.getSize(find.byKey(const Key('b')));
        expect(size.width, greaterThanOrEqualTo(44));
        expect(size.height, greaterThanOrEqualTo(44), reason: '看着小，能点的范围不小');
        heights.add(style.minimumSize!.resolve({})!.height);
      }
      expect(heights, [32, 40, 52]);
    });

    testWidgets('390 宽、中文长文案：截掉，不溢出；fullWidth 撑满', (t) async {
      await _pump(
          t,
          Column(mainAxisSize: MainAxisSize.min, children: [
            NwButton(buttonKey: const Key('a'), label: _long * 2, icon: Icons.warning_rounded, size: NwButtonSize.large),
            const NwButton(buttonKey: Key('full'), label: '登录', fullWidth: true),
            const NwButton(buttonKey: Key('hug'), label: '登录'),
          ]),
          theme: nwTheme(Brightness.light));
      expect(t.takeException(), isNull);
      expect(t.getSize(find.byKey(const Key('a'))).width, lessThanOrEqualTo(390 - 2 * Nw.s24));
      expect(t.getSize(find.byKey(const Key('full'))).width, 390 - 2 * Nw.s24);
      expect(t.getSize(find.byKey(const Key('hug'))).width, lessThan(200));
    });
  });

  group('状态色看得见（设计稿是同色字压同色底）', () {
    for (final b in Brightness.values) {
      final c = b == Brightness.dark ? NwColors.dark : NwColors.light;
      testWidgets('[${b.name}] 药丸：软底 + 同色字，对比够 4.5', (t) async {
        for (final tone in NwTone.values) {
          await _pump(t, NwStatusPill(label: 'ONLINE', tone: tone, dot: true), theme: nwTheme(b));
          final box = t.widget<Container>(find.ancestor(of: find.text('ONLINE'), matching: find.byType(Container)).first);
          final bg = (box.decoration! as BoxDecoration).color!;
          final fg = _textColor(t, 'ONLINE');
          expect(fg, c.fg(tone));
          expect(bg, c.soft(tone));
          expect(_contrast(fg, bg), greaterThanOrEqualTo(4.5), reason: '$tone');
        }
      });

      testWidgets('[${b.name}] 告警行：图标压图标格看得见；级别标签的字压实底够 4.5', (t) async {
        for (final tone in NwTone.values) {
          await _pump(
              t,
              NwAlertItem(
                  title: '电量低', level: 'P2', tone: tone, icon: Icons.battery_alert_rounded, source: 'D1-04', time: '12 分钟前'),
              theme: nwTheme(b));
          final icon = t.widget<Icon>(find.byIcon(Icons.battery_alert_rounded));
          final cell = t.widget<Container>(
              find.ancestor(of: find.byIcon(Icons.battery_alert_rounded), matching: find.byType(Container)).first);
          final cellBg = (cell.decoration! as BoxDecoration).color!;
          expect(icon.color, isNot(cellBg));
          expect(_contrast(icon.color!, cellBg), greaterThanOrEqualTo(3), reason: '$tone 图标');
          final tag = t.widget<Container>(find.ancestor(of: find.text('P2'), matching: find.byType(Container)).first);
          expect(_contrast(_textColor(t, 'P2'), (tag.decoration! as BoxDecoration).color!), greaterThanOrEqualTo(4.5),
              reason: '$tone 标签');
          expect(_contrast(_textColor(t, '电量低'), c.surface), greaterThanOrEqualTo(4.5));
        }
      });

      testWidgets('[${b.name}] P1 卡片：软红底、正文色的字、2px 红框', (t) async {
        await _pump(t, const NwCard(tone: NwTone.error, child: Text('狗看见人')), theme: nwTheme(b));
        final m = t.widget<Material>(find.ancestor(of: find.text('狗看见人'), matching: find.byType(Material)).first);
        final side = (m.shape! as RoundedRectangleBorder).side;
        expect(m.color, c.errorSoft);
        expect((side.color, side.width), (c.errorFrame, 2.0));
        final ctx = t.element(find.text('狗看见人'));
        expect(_contrast(DefaultTextStyle.of(ctx).style.color!, c.errorSoft), greaterThanOrEqualTo(4.5),
            reason: '缺省的字色压软红底');
        // 普通卡片：卡片底、1px 淡边
        await _pump(t, const NwCard(child: Text('x')), theme: nwTheme(b));
        final plain = t.widget<Material>(find.ancestor(of: find.text('x'), matching: find.byType(Material)).first);
        expect(plain.color, c.surface);
        expect((plain.shape! as RoundedRectangleBorder).side, BorderSide(color: c.outline));
        expect((plain.shape! as RoundedRectangleBorder).borderRadius, BorderRadius.circular(12));
      });

      testWidgets('[${b.name}] 健康标签：不行了才是红的', (t) async {
        await _pump(t, const NwHealthChip(icon: Icons.cloud_off_rounded, label: '站点离线', bad: true), theme: nwTheme(b));
        expect(_textColor(t, '站点离线'), c.error);
        expect(t.widget<Icon>(find.byIcon(Icons.cloud_off_rounded)).color, c.error);
        await _pump(t, const NwHealthChip(icon: Icons.cloud_done_rounded, label: '站点在线'), theme: nwTheme(b));
        expect(_textColor(t, '站点在线'), c.text);
        expect(t.widget<Icon>(find.byIcon(Icons.cloud_done_rounded)).color, c.success);
      });

      testWidgets('[${b.name}] 电量：低电是橙的（不是红的）、换图标；够的是绿的；不知道写 --、不上色', (t) async {
        await _pump(t, const NwBattery(percent: 19.6), theme: nwTheme(b));
        expect(find.text('20%'), findsOneWidget, reason: '显示四舍五入，判低电按原值');
        expect(t.widget<Icon>(find.byIcon(Icons.battery_alert_rounded)).color, c.warning);
        await _pump(t, const NwBattery(percent: 20), theme: nwTheme(b));
        expect(t.widget<Icon>(find.byIcon(Icons.battery_full_rounded)).color, c.success);
        await _pump(t, const NwBattery(percent: null), theme: nwTheme(b));
        expect(find.text('--'), findsOneWidget);
        expect(t.widget<Icon>(find.byIcon(Icons.battery_unknown_rounded)).color, c.textSecondary);
      });

      testWidgets('[${b.name}] 狗卡：有没处理的 P1 才是红框、警告图标；状态药丸按给的色调', (t) async {
        await _pump(t, const NwDogCard(id: 'D1-02', status: 'P1', tone: NwTone.error, activity: '东墙', battery: 42, p1: true),
            theme: nwTheme(b));
        Material card() =>
            t.widget<Material>(find.ancestor(of: find.text('D1-02'), matching: find.byType(Material)).first);
        expect((card().shape! as RoundedRectangleBorder).side, BorderSide(color: c.errorFrame, width: 2));
        expect(find.byIcon(Icons.warning_rounded), findsOneWidget);
        expect(_textColor(t, 'P1'), c.error);
        await _pump(t, const NwDogCard(id: 'D1-02', status: '在线', tone: NwTone.success, activity: '巡检中', battery: 42),
            theme: nwTheme(b));
        expect((card().shape! as RoundedRectangleBorder).side, BorderSide(color: c.outline));
        expect(find.byIcon(Icons.warning_rounded), findsNothing);
        expect(find.byIcon(Icons.smart_toy_rounded), findsOneWidget);
        expect(_textColor(t, '在线'), c.success);
        expect(find.text('42%'), findsOneWidget);
      });
    }
  });

  group('NwSegment', () {
    testWidgets('点哪格回哪格的值；每格的 Key 在能点的那一层；选中的那格有底', (t) async {
      final got = <String>[];
      await _pump(
          t,
          NwSegment<String>(
              values: const ['armed', 'home', 'visitor'],
              selected: 'home',
              onChanged: got.add,
              labelOf: (v) => v.toUpperCase(),
              keyOf: (v) => Key('seg-$v')),
          theme: nwTheme(Brightness.dark));
      expect(t.widget(find.byKey(const Key('seg-armed'))), isA<InkWell>());
      await t.tap(find.byKey(const Key('seg-visitor')));
      await t.tap(find.text('ARMED'));
      expect(got, ['visitor', 'armed']);
      Color bgOf(String v) => t
          .widget<Material>(find.ancestor(of: find.byKey(Key('seg-$v')), matching: find.byType(Material)).first)
          .color!;
      expect(bgOf('home'), NwColors.dark.surfaceVariant);
      expect(bgOf('armed'), Colors.transparent);
      expect(_textColor(t, 'HOME'), NwColors.dark.text);
      expect(_textColor(t, 'ARMED'), NwColors.dark.textSecondary);
      // 三格等宽，每格够手指点
      final w = [for (final v in ['armed', 'home', 'visitor']) t.getSize(find.byKey(Key('seg-$v')))];
      expect(w.map((s) => s.width).toSet().length, 1);
      expect(w.first.height, greaterThanOrEqualTo(40));
    });

    testWidgets('不给 onChanged（没权限）：点了没反应', (t) async {
      await _pump(
          t,
          NwSegment<int>(
              values: const [1, 2], selected: 1, onChanged: null, labelOf: (v) => '$v', keyOf: (v) => Key('s$v')));
      expect(t.widget<InkWell>(find.byKey(const Key('s2'))).onTap, isNull);
    });
  });

  group('NwActionTile', () {
    testWidgets('tileKey 在能点的那一层；点了回调；不给 onTap 置灰', (t) async {
      var taps = 0;
      await _pump(
          t,
          Row(children: [
            Expanded(
                child: NwActionTile(
                    icon: Icons.explore_rounded, label: '派巡检', tileKey: const Key('btn-x'), onTap: () => taps++)),
            const Expanded(
                child: NwActionTile(icon: Icons.videogame_asset_rounded, label: '遥控', tileKey: Key('btn-y'))),
          ]),
          theme: nwTheme(Brightness.light));
      expect(t.widget(find.byKey(const Key('btn-x'))), isA<InkWell>());
      await t.tap(find.byKey(const Key('btn-x')));
      expect(taps, 1);
      expect(t.widget<InkWell>(find.byKey(const Key('btn-y'))).onTap, isNull);
      expect(_textColor(t, '遥控'), NwColors.light.textMuted);
      expect(_textColor(t, '派巡检'), NwColors.light.text);
      expect(t.widget<Icon>(find.byIcon(Icons.explore_rounded)).color, NwColors.light.primary);
    });
  });

  group('NwSiteCard', () {
    testWidgets('P1 的数目：有才出红徽标，0 写「没有」，不知道就什么都不摆（设计稿把条件写反了）', (t) async {
      await _pump(t, const NwSiteCard(name: '庄园', address: 'https://h:1', p1Count: 2), theme: nwTheme(Brightness.light));
      expect(find.text('P1 · 2 条'), findsOneWidget);
      expect(find.text('没有未处理的 P1'), findsNothing);
      await _pump(t, const NwSiteCard(name: '庄园', address: 'https://h:1', p1Count: 0), theme: nwTheme(Brightness.light));
      expect(find.textContaining('P1 ·'), findsNothing);
      expect(find.text('没有未处理的 P1'), findsOneWidget);
      await _pump(t, const NwSiteCard(name: '庄园', address: 'https://h:1'), theme: nwTheme(Brightness.light));
      expect(find.textContaining('P1'), findsNothing);
    });

    testWidgets('连接状态没有数据就不显示药丸；有的话连着是绿的、断了是红的', (t) async {
      await _pump(t, const NwSiteCard(name: '庄园', address: 'a'), theme: nwTheme(Brightness.light));
      expect(find.byType(NwStatusPill), findsNothing);
      await _pump(t, const NwSiteCard(name: '庄园', address: 'a', status: '已连接'), theme: nwTheme(Brightness.light));
      expect(_textColor(t, '已连接'), NwColors.light.success);
      await _pump(t, const NwSiteCard(name: '庄园', address: 'a', status: '离线', connected: false),
          theme: nwTheme(Brightness.light));
      expect(_textColor(t, '离线'), NwColors.light.error);
    });

    testWidgets('进入：点按钮（TextButton，带 Key）或者点整张卡片；菜单点了不算进入', (t) async {
      var entered = 0, menu = 0;
      await _pump(
          t,
          NwSiteCard(
              name: '庄园',
              address: 'a',
              onEnter: () => entered++,
              enterKey: const Key('enter'),
              menu: IconButton(key: const Key('menu'), onPressed: () => menu++, icon: const Icon(Icons.more_vert_rounded))),
          theme: nwTheme(Brightness.light));
      expect(t.widget(find.byKey(const Key('enter'))), isA<TextButton>());
      expect(find.text('进入站点'), findsOneWidget);
      await t.tap(find.byKey(const Key('enter')));
      expect(entered, 1);
      await t.tap(find.text('庄园'));
      expect(entered, 2);
      await t.tap(find.byKey(const Key('menu')));
      expect((entered, menu), (2, 1));
    });
  });

  group('别的', () {
    testWidgets('小节标题、版本格的名字：英文大写', (t) async {
      appLang.value = AppLang.en;
      addTearDown(() => appLang.value = AppLang.zh);
      await _pump(
          t,
          const Column(mainAxisSize: MainAxisSize.min, children: [
            NwSectionLabel('Active incidents'),
            NwVersionCard(entity: 'Station OS', version: 'v2.4.1', note: 'Update available'),
          ]),
          theme: nwTheme(Brightness.light));
      expect(find.text('ACTIVE INCIDENTS'), findsOneWidget);
      expect(find.text('STATION OS'), findsOneWidget);
      expect(_textColor(t, 'Update available'), NwColors.light.info, reason: '「有新版」不是告警，不用橙');
    });

    testWidgets('巡检卡：有异常换警告图标、结果那一行加粗；没有照片就不摆那一行', (t) async {
      await _pump(t, const NwPatrolCard(id: 'D1-02', time: '10:30', status: '完成'), theme: nwTheme(Brightness.light));
      expect(find.byIcon(Icons.check_circle_outline_rounded), findsOneWidget);
      expect(find.byType(ClipRRect), findsNothing);
      await _pump(
          t,
          const NwPatrolCard(
              id: 'D1-02',
              time: '10:30',
              status: '2 处异常',
              anomaly: true,
              thumbs: [ColoredBox(color: Colors.grey)],
              more: 4),
          theme: nwTheme(Brightness.light));
      expect(find.byIcon(Icons.warning_amber_rounded), findsOneWidget);
      expect(t.widget<Text>(find.text('2 处异常')).style!.fontWeight, FontWeight.w700);
      expect(find.text('+4'), findsOneWidget);
      expect(t.getSize(find.byType(ClipRRect).first), const Size(80, 60));
    });
  });
}
