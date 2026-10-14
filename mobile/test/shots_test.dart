// 出图（不是测试）：用真字体把真页面画成 PNG，浅色、深色各一套，人眼看版面。平时跳过；要出图：
//   D1MAX_SHOTS=/某个目录 flutter test test/shots_test.dart
// 可选 D1MAX_SHOTS_CJK=/路径/中文字体.ttf（不给中文是方块）、D1MAX_SHOTS_ICONS=/路径/MaterialIcons-Regular.otf、
// D1MAX_SHOTS_LANG=en|zh|all（缺省 en；all 两种都出，文件名带语言）。
// 2026-10-14 收尾：老主题的出图（原来的 shots_test.dart）和新组件的出图（nw_shots_test.dart）并成这一个。
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:d1max_patrol/design/nw_tokens.g.dart';
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/nw/nw_button.dart';
import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/nw/nw_widgets.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart' show siteFixture;

final _out = Platform.environment['D1MAX_SHOTS'];
final _langs = switch (Platform.environment['D1MAX_SHOTS_LANG']) {
  'zh' => const [AppLang.zh],
  'all' => AppLang.values,
  _ => const [AppLang.en],
};

const _phone = Size(390, 844);
const _wide = Size(800, 360);

Future<void> _font(String family, String path) async {
  final f = File(path);
  if (!f.existsSync()) return;
  final loader = FontLoader(family)..addFont(f.readAsBytes().then((b) => ByteData.view(b.buffer)));
  await loader.load();
}

final _site = SiteEntry(
    name: 'East Gate Estate', url: 'https://192.168.100.200:8443', fingerprint: 'ab' * 32, username: 'night.guard');

final _t0 = DateTime(2026, 10, 14, 2, 14, 36).millisecondsSinceEpoch;

Map<String, dynamic> _alert(String level, String kind, String title, {String detail = '', Map<String, dynamic>? context, int n = 1, int ago = 0}) =>
    <String, dynamic>{
      'key': 'A/$kind#$n', 'level': level, 'kind': kind, 'robot': 'A', 'title': title, 'detail': detail, 'count': 1,
      'first_ms': _t0 - ago * 1000, 'last_ms': _t0 - ago * 1000, 'acked_by': '', 'acked_ms': null,
      'context': context ?? <String, dynamic>{},
    };

/// 没确认的 P1：狗看见人（带防区、距离、位置）。
Map<String, dynamic> _p1Row() => _alert('P1', 'dog_sees_person', '狗看见人了:1 个人,最近 6.2 m', detail: '布防中', context: <String, dynamic>{
      'zone': 'East wall', 'task_id': 'persons',
      'persons': <String, dynamic>{'count': 1, 'nearest_m': 6.2},
      'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': '7', 'x': 3.0, 'y': 4.0},
    });

/// 值守汇总：真站点的夹具，各级告警数换成给的。
Map<String, dynamic> _summary(int p1, int p2) {
  final s = jsonDecode(jsonEncode(siteFixture('site_watch_summary'))) as Map<String, dynamic>;
  for (final r in (s['robots'] as List).cast<Map<String, dynamic>>()) {
    r['alerts'] = <String, dynamic>{'P1': p1, 'P2': p2, 'P3': 0};
    // 电量是刚读的（夹具里那个时刻早过了 10 分钟，狗卡上会不写数）
    if (r['battery_as_of_ms'] != null) r['battery_as_of_ms'] = DateTime.now().millisecondsSinceEpoch;
    if (p1 == 0 && p2 == 0) {
      r['upload_backlog'] = 0;
      r['oldest_backlog_s'] = 0;
    }
  }
  return s;
}

/// 值守：一条没确认的 P1 加两条 P2。
FakeApi _busy(String role) => FakeApi(role)
  ..alertRows = [
    _p1Row(),
    _alert('P2', 'battery_swap', '要换电池', detail: '电量 18%', ago: 720),
    _alert('P2', 'upload_backlog', '证据半小时没传上来', n: 2, ago: 1900),
  ]
  ..summaryBody = _summary(1, 2);

/// 值守：什么事都没有。
FakeApi _clear(String role) => FakeApi(role)
  ..alertRows = <Map<String, dynamic>>[]
  ..summaryBody = _summary(0, 0);

FakeApi _deterring() => FakeApi('guard')
  ..deterRows = [
    <String, dynamic>{'robot_id': 'A', 'level': 1, 'auto': true, 'next_in_s': 30, 'started_ms': _t0 + 4000}
  ];

Future<void> _settle(WidgetTester t) => t.pumpAndSettle();

/// 外壳里点到某一栏。
Future<void> Function(WidgetTester) _tab(Key k) => (t) async {
      await t.pumpAndSettle();
      await t.tap(find.byKey(k));
      await t.pumpAndSettle();
    };

/// 遥控：等站点给了控制权（摇杆、STOP 都亮着）。
FakeApi? _teleopApi;
Future<void> _granted(WidgetTester t) async {
  await t.pump();
  _teleopApi!.link!.ctl.add(<String, dynamic>{'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5, 'max_wz': 0.75});
  await t.pump();
  await t.pump(const Duration(milliseconds: 300));
}

class _Shot {
  final Size size;
  final Widget Function() build;
  final Future<void> Function(WidgetTester) ready;
  final List<Brightness> themes;
  const _Shot(this.size, this.build, {this.ready = _settle, this.themes = Brightness.values});
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
  final pages = <String, _Shot>{
    '00-gallery': _Shot(const Size(390, 2000), _gallery),
    '01-sites': _Shot(_phone, () => SiteListPage(store: MemorySiteStore([_site]))),
    '02-login': _Shot(_phone, () => SiteLoginPage(site: _site, signIn: (_) async => FakeApi('guard'))),
    '03-duty-p1': _Shot(_phone, () => NwShell(api: _busy('guard'), title: 'East Gate Estate', ring: () {})),
    '04-duty-clear': _Shot(_phone, () => NwShell(api: _clear('guard'), title: 'East Gate Estate', ring: () {})),
    '05-alarm': _Shot(
        _phone,
        () => SiteAlertScenePage(
            api: _deterring(), alert: Alert.fromWire(_p1Row()), now: () => DateTime.fromMillisecondsSinceEpoch(_t0 + 42000))),
    '06-dogs': _Shot(_phone, () => NwShell(api: _clear('guard'), title: 'East Gate Estate', ring: () {}),
        ready: _tab(NwShell.dogsKey)),
    '07-robot-guard': _Shot(_phone, () => SiteRobotPage(api: FakeApi('guard'), robotId: 'A')),
    '08-map': _Shot(_phone, () => NwShell(api: _clear('admin'), title: 'East Gate Estate', ring: () {}),
        ready: _tab(NwShell.mapKey)),
    '09-history': _Shot(_phone, () => NwShell(api: _clear('admin'), title: 'East Gate Estate', ring: () {}),
        ready: _tab(NwShell.historyKey)),
    '10-admin': _Shot(_phone, () => NwShell(api: _clear('admin'), title: 'East Gate Estate', ring: () {}),
        ready: _tab(NwShell.adminKey)),
    '11-teleop': _Shot(_wide, () => SiteTeleopPage(api: _teleopApi = FakeApi('guard'), robotId: 'A'),
        ready: _granted, themes: const [Brightness.dark]),
    '12-duty-landscape': _Shot(_wide, () => NwShell(api: _busy('guard'), title: 'East Gate Estate', ring: () {})),
  };
  for (final lang in _langs) {
    for (final e in pages.entries) {
      for (final b in e.value.themes) {
        testWidgets('出图 ${lang.name} ${b.name} ${e.key}', skip: _out == null, (t) async {
          await t.runAsync(() async {
            final root = Directory.current.path;
            await _font(NwFont.display, '$root/assets/fonts/SpaceGrotesk.ttf');
            await _font(NwFont.sans, '$root/assets/fonts/Inter.ttf');
            await _font(NwFont.mono, '$root/assets/fonts/JetBrainsMono.ttf');
            await _font('MaterialIcons', Platform.environment['D1MAX_SHOTS_ICONS'] ?? '');
            await _font('Noto Sans SC', Platform.environment['D1MAX_SHOTS_CJK'] ?? '');
          });
          appLang.value = lang;
          addTearDown(() => appLang.value = AppLang.zh);
          final size = e.value.size;
          t.view.physicalSize = size * 2;
          t.view.devicePixelRatio = 2.0;
          addTearDown(t.view.reset);
          const key = Key('shot');
          await t.pumpWidget(RepaintBoundary(
              key: key,
              child: MaterialApp(debugShowCheckedModeBanner: false, theme: nwTheme(b), home: e.value.build())));
          await e.value.ready(t);
          final boundary = t.renderObject<RenderRepaintBoundary>(find.byKey(key));
          final suffix = _langs.length > 1 ? '-${lang.name}' : '';
          await t.runAsync(() async {
            final img = await boundary.toImage(pixelRatio: 2.0);
            final png = await img.toByteData(format: ui.ImageByteFormat.png);
            File('$_out/${e.key}-${b.name}$suffix.png')
              ..createSync(recursive: true)
              ..writeAsBytesSync(png!.buffer.asUint8List());
          });
          await t.pumpWidget(Container());
        });
      }
    }
  }
}
