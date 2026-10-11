// 出图（不是测试）：用真字体把几页画成 PNG，人眼看版面。平时跳过；要出图：
//   D1MAX_SHOTS=/某个目录 flutter test test/shots_test.dart
// 可选 D1MAX_SHOTS_CJK=/路径/中文字体.ttf（不给中文是方块）、D1MAX_SHOTS_ICONS=/路径/MaterialIcons-Regular.otf。
import 'dart:io';
import 'dart:ui' as ui;

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_add.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_mode.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_standby.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:d1max_patrol/ui/theme.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi, busyView;

final _out = Platform.environment['D1MAX_SHOTS'];

Future<void> _font(String family, String path) async {
  final f = File(path);
  if (!f.existsSync()) return;
  final loader = FontLoader(family)..addFont(f.readAsBytes().then((b) => ByteData.view(b.buffer)));
  await loader.load();
}

Alert _p1({bool acked = false}) => Alert.fromWire(<String, dynamic>{
      'key': 'A/dog_sees_person#1', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'A',
      'title': '狗看见人了:1 个人,最近 6.2 m', 'detail': '布防中', 'count': 1,
      'first_ms': DateTime(2026, 10, 11, 2, 14, 36).millisecondsSinceEpoch,
      'last_ms': DateTime(2026, 10, 11, 2, 14, 36).millisecondsSinceEpoch,
      'acked_by': acked ? 'u' : '', 'acked_ms': acked ? DateTime(2026, 10, 11, 2, 15, 20).millisecondsSinceEpoch : null,
      'context': <String, dynamic>{'zone': 'East wall', 'task_id': 'persons',
        'persons': <String, dynamic>{'count': 1, 'nearest_m': 6.2},
        'pose': <String, dynamic>{'map_id': 'estate-1', 'map_version': '7', 'x': 3.0, 'y': 4.0}}});

FakeApi _deterring() => FakeApi('guard')
  ..deterRows = [
    <String, dynamic>{'robot_id': 'A', 'level': 1, 'auto': true, 'next_in_s': 30,
      'started_ms': DateTime(2026, 10, 11, 2, 14, 40).millisecondsSinceEpoch}
  ];

void main() {
  final pages = <String, (Size, Widget Function())>{
    'sites': (const Size(390, 844), () => SiteListPage(store: MemorySiteStore([
          SiteEntry(name: 'East Gate Estate', url: 'https://192.168.1.5:8443', fingerprint: 'ab' * 32, username: 'night.guard')
        ]))),
    'add-site': (const Size(390, 844), () => SiteAddPage(scanner: (_) async => null)),
    'robots': (const Size(390, 844), () => SiteRobotsPage(api: FakeApi('admin'), title: 'East Gate Estate')),
    'robot': (const Size(390, 844), () => SiteRobotPage(api: FakeApi('admin', robotView: busyView()), robotId: 'A')),
    'watch': (const Size(390, 844), () => SiteWatchPage(api: FakeApi('guard'))),
    'alarm': (const Size(390, 844), () => SiteAlertScenePage(api: _deterring(), alert: _p1(),
        now: () => DateTime(2026, 10, 11, 2, 15, 18))),
    'alarm-acked': (const Size(390, 844), () => SiteAlertScenePage(api: _deterring(), alert: _p1(acked: true),
        now: () => DateTime(2026, 10, 11, 2, 15, 30))),
    'alarm-landscape': (const Size(800, 360), () => SiteAlertScenePage(api: _deterring(), alert: _p1(),
        now: () => DateTime(2026, 10, 11, 2, 15, 18))),
    'mode': (const Size(390, 844), () => SiteModePage(api: FakeApi('admin'))),
    'standby': (const Size(390, 844), () => SiteStandbyPage(api: FakeApi('admin'), robotId: 'A', canMarkHere: true)),
  };

  for (final lang in AppLang.values) {
    for (final e in pages.entries) {
      testWidgets('出图 ${lang.name} ${e.key}', skip: _out == null, (t) async {
        await t.runAsync(() async {
          final root = Directory.current.path;
          await _font(fontSans, '$root/assets/fonts/AtkinsonHyperlegibleNext.ttf');
          await _font(fontMono, '$root/assets/fonts/AtkinsonHyperlegibleMono.ttf');
          await _font('MaterialIcons', Platform.environment['D1MAX_SHOTS_ICONS'] ?? '');
          await _font('Noto Sans SC', Platform.environment['D1MAX_SHOTS_CJK'] ?? '');
        });
        appLang.value = lang;
        addTearDown(() => appLang.value = AppLang.zh);
        final size = e.value.$1;
        t.view.physicalSize = size * 2;
        t.view.devicePixelRatio = 2.0;
        addTearDown(t.view.reset);
        const key = Key('shot');
        await t.pumpWidget(
            RepaintBoundary(key: key, child: MaterialApp(debugShowCheckedModeBanner: false, theme: proTheme(), home: e.value.$2())));
        await t.pumpAndSettle();
        final boundary = t.renderObject<RenderRepaintBoundary>(find.byKey(key));
        await t.runAsync(() async {
          final img = await boundary.toImage(pixelRatio: 2.0);
          final png = await img.toByteData(format: ui.ImageByteFormat.png);
          File('$_out/${e.key}-${lang.name}.png')
            ..createSync(recursive: true)
            ..writeAsBytesSync(png!.buffer.asUint8List());
        });
        await t.pumpWidget(Container());
      });
    }
  }
}
