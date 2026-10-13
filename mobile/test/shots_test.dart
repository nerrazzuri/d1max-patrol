// 出图（不是测试）：新设计系统（2026-10-14 重做）用真字体画成 PNG，浅色、深色各一套，人眼看。平时跳过；
// 要出图：
//   D1MAX_SHOTS=/某个目录 flutter test test/nw_shots_test.dart
// 可选 D1MAX_SHOTS_CJK=/路径/中文字体.ttf（不给中文是方块）、D1MAX_SHOTS_ICONS=/路径/MaterialIcons-Regular.otf。
import 'dart:io';
import 'dart:ui' as ui;

import 'package:d1max_patrol/design/nw_tokens.g.dart';
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/ui/nw/nw_button.dart';
import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_shell.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

final _out = Platform.environment['D1MAX_SHOTS'];

Future<void> _font(String family, String path) async {
  final f = File(path);
  if (!f.existsSync()) return;
  final loader = FontLoader(family)..addFont(f.readAsBytes().then((b) => ByteData.view(b.buffer)));
  await loader.load();
}

/// 组件各摆一个（数据是编的，只为看样子）。
Widget _gallery() => Scaffold(
    body: ListView(padding: const EdgeInsets.all(Nw.s16), children: [
      const NwHeader(title: 'Site Selection', overline: 'Nexus Warden'),
      const SizedBox(height: Nw.s16),
      NwSiteCard(name: 'Blackwood Estate', address: 'https://192.168.100.200:8443', status: 'CONNECTED', p1Count: 2, onEnter: () {}),
      const SizedBox(height: Nw.s16),
      NwSegment<String>(values: const ['ARMED', 'HOME', 'VISITOR'], selected: 'ARMED', onChanged: (_) {}, labelOf: (v) => v),
      const SizedBox(height: Nw.s16),
      const NwSectionLabel('Active incidents'),
      const SizedBox(height: Nw.s8),
      const NwCard(tone: NwTone.error, child: Text('D1-02 sees a person · East wall · 6.2 m')),
      const SizedBox(height: Nw.s8),
      const NwAlertItem(title: 'Low Battery Warning', level: 'P2', tone: NwTone.warning, icon: Icons.battery_alert_rounded, source: 'D1-04', time: '12m ago'),
      const SizedBox(height: Nw.s8),
      const NwDogCard(id: 'D1-02', status: 'P1 ALERT', tone: NwTone.error, activity: 'Intruder Detected - Zone B', battery: 42, p1: true),
      const SizedBox(height: Nw.s8),
      const NwDogCard(id: 'D1-01', status: 'ONLINE', tone: NwTone.success, activity: 'Patrolling north loop', battery: 18),
      const SizedBox(height: Nw.s8),
      const Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
        NwHealthChip(icon: Icons.cloud_done_rounded, label: 'Station Online'),
        NwHealthChip(icon: Icons.cloud_off_rounded, label: 'Backup failed', bad: true),
        NwStatusPill(label: 'IDLE'),
        NwStatusPill(label: 'ONLINE', tone: NwTone.success, dot: true),
        NwStatusPill(label: 'P2', tone: NwTone.warning),
        NwStatusPill(label: 'OFFLINE', tone: NwTone.error, dot: true),
        NwStatusPill(label: 'INFO', tone: NwTone.info),
      ]),
      const SizedBox(height: Nw.s16),
      Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
        for (final v in NwButtonVariant.values) NwButton(label: v.name.toUpperCase(), variant: v, onPressed: () {}),
        const NwButton(label: 'DISABLED'),
        NwButton(label: 'Small', size: NwButtonSize.small, icon: Icons.arrow_forward_rounded, variant: NwButtonVariant.outline, onPressed: () {}),
      ]),
      const SizedBox(height: Nw.s8),
      NwButton(label: "I'M HANDLING IT", size: NwButtonSize.large, fullWidth: true, onPressed: () {}),
      const SizedBox(height: Nw.s16),
      const Row(children: [
        Expanded(child: NwStatTile(label: 'Battery', value: '82%', icon: Icons.battery_full_rounded, tone: NwTone.success)),
        SizedBox(width: Nw.s8),
        Expanded(child: NwStatTile(label: 'Localization', value: 'Good', icon: Icons.my_location_rounded)),
      ]),
      const SizedBox(height: Nw.s8),
      Row(children: [
        Expanded(child: NwActionTile(icon: Icons.explore_rounded, label: 'Patrol', onTap: () {})),
        const SizedBox(width: Nw.s8),
        Expanded(child: NwActionTile(icon: Icons.videogame_asset_rounded, label: 'Teleop', onTap: () {})),
        const SizedBox(width: Nw.s8),
        const Expanded(child: NwActionTile(icon: Icons.campaign_rounded, label: 'Payload')),
      ]),
      const SizedBox(height: Nw.s8),
      const NwDiagnosticRow(icon: Icons.schedule_rounded, label: 'System Clock', value: 'Synchronized', tone: NwTone.success),
      const NwDiagnosticRow(icon: Icons.storage_rounded, label: 'Disk', value: '62% free'),
      const SizedBox(height: Nw.s8),
      const NwPatrolCard(id: 'D1-02', time: 'Today, 10:30 - 10:45', type: 'Scheduled', status: 'Completed · 12 photos'),
      const SizedBox(height: Nw.s8),
      const NwVersionCard(entity: 'Station OS', version: 'v2.4.1', note: 'Update Available'),
      const SizedBox(height: Nw.s8),
      const NwNotice(text: 'Live updates disconnected. Reconnecting…', tone: NwTone.warning),
    ]));

void main() {
  final pages = <String, (Size, Widget Function())>{
    'nw-gallery': (const Size(390, 2000), _gallery),
    'nw-shell-duty': (const Size(390, 844), () => NwShell(api: FakeApi('admin'), title: 'East Gate Estate')),
    'nw-shell-landscape': (const Size(800, 360), () => NwShell(api: FakeApi('guard'), title: 'East Gate Estate')),
  };
  for (final b in Brightness.values) {
    for (final e in pages.entries) {
      testWidgets('出图 ${b.name} ${e.key}', skip: _out == null, (t) async {
        await t.runAsync(() async {
          final root = Directory.current.path;
          await _font(NwFont.display, '$root/assets/fonts/SpaceGrotesk.ttf');
          await _font(NwFont.sans, '$root/assets/fonts/Inter.ttf');
          await _font(NwFont.mono, '$root/assets/fonts/JetBrainsMono.ttf');
          await _font('MaterialIcons', Platform.environment['D1MAX_SHOTS_ICONS'] ?? '');
          await _font('Noto Sans SC', Platform.environment['D1MAX_SHOTS_CJK'] ?? '');
        });
        appLang.value = AppLang.en;
        addTearDown(() => appLang.value = AppLang.zh);
        final size = e.value.$1;
        t.view.physicalSize = size * 2;
        t.view.devicePixelRatio = 2.0;
        addTearDown(t.view.reset);
        const key = Key('shot');
        await t.pumpWidget(RepaintBoundary(
            key: key,
            child: MaterialApp(debugShowCheckedModeBanner: false, theme: nwTheme(b), home: e.value.$2())));
        await t.pumpAndSettle();
        final boundary = t.renderObject<RenderRepaintBoundary>(find.byKey(key));
        await t.runAsync(() async {
          final img = await boundary.toImage(pixelRatio: 2.0);
          final png = await img.toByteData(format: ui.ImageByteFormat.png);
          File('$_out/${e.key}-${b.name}.png')
            ..createSync(recursive: true)
            ..writeAsBytesSync(png!.buffer.asUint8List());
        });
        await t.pumpWidget(Container());
      });
    }
  }
}
