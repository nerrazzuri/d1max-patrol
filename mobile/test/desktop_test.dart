// 桌面版（W15）：跟手机同一套代码，窗口大得多、用鼠标键盘。常见的两种窗口大小（1280×720、1920×1080）
// 里主要几页都不溢出；值守屏宽了分两栏（告警一栏、每台狗一栏）。
import 'package:d1max_patrol/main.dart' show PatrolApp;
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_standby.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi, busyView;

const _sizes = [Size(1280, 720), Size(1920, 1080)];

Future<void> _show(WidgetTester t, Size size, Widget page) async {
  t.view.physicalSize = size;
  t.view.devicePixelRatio = 1.0;
  addTearDown(t.view.reset);
  await t.pumpWidget(MaterialApp(home: page));
  await t.pumpAndSettle();
  expect(t.takeException(), isNull);
}

void main() {
  for (final size in _sizes) {
    final label = '${size.width.toInt()}×${size.height.toInt()}';
    testWidgets('桌面 $label 不溢出：狗列表、单狗页、地图、排程、待命点', (t) async {
      await _show(t, size, SiteRobotsPage(api: FakeApi('admin'), title: '庄园'));
      await _show(t, size, SiteRobotPage(api: FakeApi('admin', robotView: busyView()), robotId: 'A'));
      await _show(t, size, SiteMapsPage(api: FakeApi('admin')));
      await _show(t, size, SiteSchedulePage(api: FakeApi('admin')));
      await _show(t, size, SiteStandbyPage(api: FakeApi('admin'), robotId: 'A', canMarkHere: true));
    });

    testWidgets('桌面 $label：值守屏分两栏', (t) async {
      await _show(t, size, SiteWatchPage(api: FakeApi('guard')));
      final a = t.getRect(find.byKey(SiteWatchPage.alertsColumnKey));
      final r = t.getRect(find.byKey(SiteWatchPage.robotsColumnKey));
      expect(a.right, lessThanOrEqualTo(r.left + 1), reason: '告警在左、每台狗在右');
    });

    testWidgets('桌面 $label 不溢出：遥控', (t) async {
      final api = FakeApi('guard');
      await _show(t, size, SiteTeleopPage(api: api, robotId: 'A'));
      expect(find.byKey(SiteTeleopPage.stopKey), findsOneWidget);
      await t.pumpWidget(Container());
    });
  }

  testWidgets('登录：口令框弹出来就能打字，回车就登录', (t) async {
    final store = MemorySiteStore([
      SiteEntry(name: '庄园', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'gina')
    ]);
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store, apiFactory: (_) => api)));
    await t.pumpAndSettle();
    await t.tap(find.text('庄园'));
    await t.pumpAndSettle();
    final field = t.widget<TextField>(find.byKey(const Key('site-password')));
    expect(field.focusNode?.hasFocus ?? field.autofocus, isTrue);
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.testTextInput.receiveAction(TextInputAction.done);
    await t.pumpAndSettle();
    expect(find.byType(SiteRobotsPage), findsOneWidget, reason: '回车就登录进去了');
  });

  testWidgets('鼠标拖也能滚（桌面上「下拉刷新」才用得了）', (t) async {
    await t.pumpWidget(PatrolApp(siteStore: MemorySiteStore()));
    final ctx = t.element(find.byType(Scaffold).first);
    expect(ScrollConfiguration.of(ctx).dragDevices, contains(PointerDeviceKind.mouse));
  });

  testWidgets('窄的（横屏手机）：值守屏照旧一栏', (t) async {
    await _show(t, const Size(800, 360), SiteWatchPage(api: FakeApi('guard')));
    expect(find.byKey(SiteWatchPage.alertsColumnKey), findsNothing);
  });
}
