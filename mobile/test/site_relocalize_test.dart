// 设位置（W00c6e，里程锚定）：单狗页显示定位状态；能派单的人给狗设位置 —— 「狗在原点」一键，或者输坐标。
import 'dart:math';

import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_site.dart';
import 'site_page_test.dart' show FakeApi;

Map<String, dynamic> _view(Map<String, dynamic>? loc, {bool canReloc = true}) {
  final v = Map<String, dynamic>.of(siteFixture('site_robot'));
  v['loc'] = loc;
  final caps = Map<String, dynamic>.of(v['capabilities'] as Map<String, dynamic>);
  final tasks = Map<String, dynamic>.of(caps['tasks'] as Map<String, dynamic>);
  if (canReloc) {
    tasks['relocalize'] = <String, dynamic>{'needs_pose': true};
  } else {
    tasks.remove('relocalize');
  }
  caps['tasks'] = tasks;
  v['capabilities'] = caps;
  return v;
}

void main() {
  test('配了定位器的狗：设位置被拒的原因说人话（W09a）', () {
    expect(ackReasonText('localizer_unavailable: 定位器没连上'), '定位器没连上或没回：定位器没连上');
    expect(ackReasonText('localizer_refused: 初值离地图太远'), '定位器没接这个位置：初值离地图太远');
  });

  test('配了定位器的狗：定位状态按来源说人话（W09a）', () {
    Map<String, dynamic> l(bool a, String reason, String source, [double s = 0.12]) =>
        {'localizer': 'bridge', 'anchored': a, 'reason': reason, 'source': source, 'sigma_m': s};
    expect(locText(l(false, '定位器没连上', 'localizer')), '定位：定位器还没给出位置 —— 定位器没连上');
    expect(locText(l(true, '定位器跟里程对不上(位移差 0.54 m),等它稳下来', 'scan_match')),
        '定位不可信：定位器跟里程对不上(位移差 0.54 m),等它稳下来');
    expect(locText(l(true, '', 'scan_match')), '定位：点云匹配，偏差约 0.1 m');
    expect(locText(l(true, '', 'rtk', 0.03)), '定位：RTK，偏差约 0.0 m');
    expect(locText(l(true, '', 'fused')), '定位：融合，偏差约 0.1 m');
    expect(locText(l(true, '', 'dead_reckoning', 0.3)), '定位：里程推算中（定位器一时没来），偏差约 0.3 m');
  });

  test('定位状态的人话', () {
    expect(locText(null), '');
    expect(locText({'anchored': false, 'reason': '换了地图,要重新设位置', 'source': 'odom_anchor'}),
        startsWith('定位：没设位置'));
    expect(locText({'anchored': true, 'reason': '', 'sigma_m': 0.64, 'source': 'odom_anchor'}),
        '定位：偏差约 0.6 m');
    expect(locText({'anchored': true, 'reason': '设位置之后走了 8.2 m', 'sigma_m': 2.02}),
        contains('不可信'));
    expect(locText({'anchored': true, 'reason': '', 'sigma_m': 0.0, 'source': 'odom_identity'}),
        contains('仿真'));
  });

  testWidgets('单狗页显示定位状态', (t) async {
    final api = FakeApi('guard',
        robotView: _view({'anchored': false, 'reason': '还没设位置：开机之后要人给一次', 'source': 'odom_anchor'}));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('robot-loc')), findsOneWidget);
    expect(find.textContaining('没设位置'), findsWidgets);
  });

  testWidgets('狗在原点：一键设位置', (t) async {
    final api = FakeApi('guard', robotView: _view({'anchored': false, 'reason': 'x'}));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-relocalize')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('reloc-home')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('relocalize')), isEmpty, reason: '先确认');
    await t.tap(find.byKey(const Key('reloc-home-go')));
    await t.pumpAndSettle();
    expect(api.calls, contains('relocalize A home'));
  });

  testWidgets('狗在原点：确认框点「算了」不发', (t) async {
    final api = FakeApi('guard', robotView: _view({'anchored': false, 'reason': 'x'}));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-relocalize')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('reloc-home')));
    await t.pumpAndSettle();
    await t.tap(find.text('算了'));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('relocalize')), isEmpty);
  });

  testWidgets('输坐标：朝向按度输，发出去是弧度；不是数字不发', (t) async {
    final api = FakeApi('admin', robotView: _view({'anchored': false, 'reason': 'x'}));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-relocalize')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('reloc-xy')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('reloc-x')), 'abc');
    await t.enterText(find.byKey(const Key('reloc-y')), '2');
    await t.tap(find.byKey(const Key('reloc-go')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('relocalize')), isEmpty);
    expect(find.textContaining('数字'), findsOneWidget);
    await t.tap(find.byKey(const Key('btn-relocalize')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('reloc-xy')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('reloc-x')), '1.5');
    await t.enterText(find.byKey(const Key('reloc-y')), '-2');
    await t.enterText(find.byKey(const Key('reloc-yaw')), '90');
    await t.tap(find.byKey(const Key('reloc-go')));
    await t.pumpAndSettle();
    final call = api.calls.lastWhere((c) => c.startsWith('relocalize'));
    final parts = call.split(' ');
    expect(parts.sublist(0, 4), ['relocalize', 'A', '1.5', '-2.0']);
    expect(double.parse(parts[4]), closeTo(pi / 2, 1e-9));
  });

  testWidgets('业主没有设位置', (t) async {
    final owner = FakeApi('owner', robotView: _view({'anchored': false, 'reason': 'x'}));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: owner, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-relocalize')), findsNothing);
  });

  testWidgets('狗不支持设位置：没有按钮', (t) async {
    final guard = FakeApi('guard', robotView: _view(null, canReloc: false));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: guard, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-relocalize')), findsNothing);
  });

  test('拒收的原因说人话', () {
    expect(ackReasonText('moving'), contains('停'));
    expect(ackReasonText('no_home'), contains('原点'));
    expect(ackReasonText('map_mismatch'), contains('图'));
  });
}
