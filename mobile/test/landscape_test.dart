// 横屏（用户 2026-09-26「手机app需要改成横屏模式」）：app 只许横屏；每一页在横屏的手机上
// （800×360，常见手机横过来的大小）都不溢出；遥控页两根杆在两边、画面在中间、「停」一直在屏幕里。
import 'package:d1max_patrol/main.dart' show landscapeOnly, lockLandscape;
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_logs.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_releases.dart';
import 'package:d1max_patrol/ui/site_runs.dart';
import 'package:d1max_patrol/ui/site_teleop.dart';
import 'package:d1max_patrol/ui/site_video.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:d1max_patrol/ui/widget/fields_dialog.dart';
import 'package:d1max_patrol/ui/widget/joystick.dart';
import 'package:d1max_patrol/ui/widget/live_video.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi, busyView;

const _w = 800.0, _h = 360.0;

/// 横屏的手机（逻辑像素 800×360，像素密度 2）。
void _phone(WidgetTester t, {double w = _w}) {
  t.view.physicalSize = Size(w * 2, _h * 2);
  t.view.devicePixelRatio = 2.0;
  addTearDown(t.view.reset);
}

/// 溢出（RenderFlex overflowed）在测试里是报错，页面画出来就算过。
Future<void> _show(WidgetTester t, Widget page) async {
  _phone(t);
  await t.pumpWidget(MaterialApp(home: page));
  await t.pumpAndSettle();
}

void main() {
  test('只许横屏，两个方向都行', () async {
    TestWidgetsFlutterBinding.ensureInitialized();
    final calls = <MethodCall>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (c) async {
      calls.add(c);
      return null;
    });
    await lockLandscape();
    final set = calls.where((c) => c.method == 'SystemChrome.setPreferredOrientations').single;
    expect(set.arguments, ['DeviceOrientation.landscapeLeft', 'DeviceOrientation.landscapeRight']);
    expect(landscapeOnly, hasLength(2));
  });

  testWidgets('遥控：两根杆在两边、画面在中间，「停」整个在屏幕里', (t) async {
    final api = FakeApi('guard');
    _phone(t);
    await t.pumpWidget(MaterialApp(home: SiteTeleopPage(api: api, robotId: 'A')));
    await t.pump();
    api.link!.ctl.add(<String, dynamic>{'kind': 'granted', 'lease_epoch': 1, 'max_vx': 0.5,
      'max_wz': 0.75});
    await t.pump();
    await t.pump();
    final move = t.getRect(find.byType(Joystick).at(0));
    final turn = t.getRect(find.byType(Joystick).at(1));
    final video = t.getRect(find.byType(SiteVideo));
    expect(move.right, lessThanOrEqualTo(video.left), reason: '左杆在画面左边');
    expect(turn.left, greaterThanOrEqualTo(video.right), reason: '右杆在画面右边');
    expect(move.height, greaterThan(160), reason: '杆竖着占满，拇指够得着');
    final stop = t.getRect(find.byKey(SiteTeleopPage.stopKey));
    expect(stop.top, greaterThanOrEqualTo(0));
    expect(stop.bottom, lessThanOrEqualTo(_h));
    expect(stop.height, greaterThanOrEqualTo(56), reason: '大红「停」要好按');
    expect(t.getRect(find.byKey(SiteTeleopPage.statusKey)).bottom, lessThanOrEqualTo(_h));
    // 左杆照样开得动。
    final g = await t.startGesture(move.center);
    await g.moveBy(const Offset(0, -150));
    await t.pump(const Duration(milliseconds: 250));
    expect(api.link!.sent.last[0], greaterThan(0));
    await g.up();
    await t.pump(const Duration(milliseconds: 150));
    expect(api.link!.sent.last, <double>[0, 0]);
  });

  testWidgets('最窄的手机（568 宽）：两边的杆还放得下一整个圈', (t) async {
    _phone(t, w: 568);
    await t.pumpWidget(MaterialApp(home: SiteTeleopPage(api: FakeApi('guard'), robotId: 'A')));
    await t.pump();
    for (var i = 0; i < 2; i++) {
      expect(t.getRect(find.byType(Joystick).at(i)).width,
          greaterThanOrEqualTo(Joystick.radius * 2));
    }
    // 画面只剩一百多高：没画面时那块板子的字整个缩在画面里（不溢出、不被裁掉）。
    final video = t.getRect(find.byType(LiveVideo));
    final texts = find.descendant(of: find.byType(LiveVideo), matching: find.byType(Text));
    expect(texts, findsWidgets);
    for (var i = 0; i < texts.evaluate().length; i++) {
      final r = t.getRect(texts.at(i));
      expect(r.top >= video.top - 0.5 && r.bottom <= video.bottom + 0.5, isTrue,
          reason: '${t.widget<Text>(texts.at(i)).data} 在 $r,画面在 $video');
    }
  });

  testWidgets('横屏不溢出：站点列表、加站点（键盘弹起来）', (t) async {
    await _show(t, SiteListPage(store: MemorySiteStore()));
    await t.tap(find.byKey(const Key('site-add')));
    await t.pumpAndSettle();
    t.view.viewInsets = const FakeViewPadding(bottom: 400); // 横屏时键盘占掉一大半
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-name')), '庄园');
    await t.pumpAndSettle();
  });

  testWidgets('横屏不溢出：登录框（键盘弹起来）', (t) async {
    final store = MemorySiteStore([
      SiteEntry(name: '庄园', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'gina')
    ]);
    await _show(t, SiteListPage(store: store, apiFactory: (_) => FakeApi('guard')));
    await t.tap(find.text('庄园'));
    await t.pumpAndSettle();
    t.view.viewInsets = const FakeViewPadding(bottom: 400);
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.pumpAndSettle();
  });

  testWidgets('横屏不溢出：填几栏的对话框（设位置输坐标，键盘弹起来）', (t) async {
    _phone(t);
    await t.pumpWidget(MaterialApp(
        home: Builder(
            builder: (c) => TextButton(
                onPressed: () => showFieldsDialog(c,
                    title: '设位置',
                    fields: const [DialogField('x (m)'), DialogField('y (m)'), DialogField('朝向 (°)')],
                    confirm: '设'),
                child: const Text('开')))));
    await t.tap(find.text('开'));
    await t.pumpAndSettle();
    t.view.viewInsets = const FakeViewPadding(bottom: 400);
    await t.pumpAndSettle();
    expect(find.text('设'), findsOneWidget);
  });

  testWidgets('横屏不溢出：狗列表', (t) async {
    await _show(t, SiteRobotsPage(api: FakeApi('admin'), title: '庄园'));
  });

  testWidgets('横屏不溢出：单狗页（管理员、在跑任务）', (t) async {
    await _show(t, SiteRobotPage(api: FakeApi('admin', robotView: busyView()), robotId: 'A'));
    expect(t.getRect(find.byKey(const Key('btn-abort'))).bottom, lessThanOrEqualTo(_h),
        reason: '叫停一进来就看得见');
    final v = t.getRect(find.byType(LiveVideo));
    expect(v.height, lessThanOrEqualTo(_h * 0.6 + 0.5), reason: '画面不比屏幕还高（滚到它能看全）');
    expect(v.width / v.height, closeTo(16 / 9, 0.01), reason: '不拉伸');
  });

  testWidgets('横屏不溢出：值守屏', (t) async {
    await _show(t, SiteWatchPage(api: FakeApi('guard')));
  });

  testWidgets('横屏不溢出：地图、版本、记录、事件、排程', (t) async {
    final api = FakeApi('admin');
    await _show(t, SiteMapsPage(api: api));
    await _show(t, SiteReleasesPage(api: api));
    await _show(t, SiteRunsPage(api: api));
    await _show(t, SiteRunPage(api: api, runId: 1));
    await _show(t, SiteIncidentsPage(api: api));
    await _show(t, SiteSchedulePage(api: api));
    await _show(t, SiteProcLogsPage(api: api, robotId: 'A'));
    await _show(t, SiteProcLogPage(api: api, robotId: 'A', name: 'slam'));
    await _show(t, SiteMapPreviewPage(api: api, mapId: 'estate-1', version: '8'));
  });

  testWidgets('横屏不溢出：录包轨迹（有点、没点）', (t) async {
    final api = FakeApi('admin')
      ..trailReplies = [
        <String, dynamic>{'points': [[0, 0], [30, 0], [30, 12]], 'since': 0, 'total': 3,
          'full': true, 'recording': true},
      ];
    _phone(t);
    await t.pumpWidget(MaterialApp(home: SiteMappingTrailPage(api: api, robotId: 'A')));
    await t.pump();
    await t.pump();
    expect(t.getRect(find.byKey(SiteMappingTrailPage.canvasKey)).height, greaterThan(150),
        reason: '画布要占住大半屏');
    await t.pumpWidget(const SizedBox());
  });
}
