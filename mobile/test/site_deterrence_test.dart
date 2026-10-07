// 分级驱离（W22）：首页一场驱离一行、推的帧当场换；驱离页按角色出按钮（保安能跳级、业主只能解除）；
// 跳级、解除；结束了说清楚。横屏 800×360。
import 'package:d1max_patrol/ui/site_deterrence.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

List<Map<String, dynamic>> _rows() =>
    ((siteFixture('site_deterrence')['sessions'] as List).cast<Map<String, dynamic>>())
        .map((m) => Map<String, dynamic>.from(m))
        .toList();

Future<void> _open(WidgetTester t, FakeApi api) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(const SizedBox());
  await t.pumpWidget(MaterialApp(home: SiteDeterrencePage(api: api, robotId: 'A')));
  await t.pumpAndSettle();
}

void main() {
  test('站点夹具：一场驱离说人话', () {
    final s = _rows().first;
    expect(deterSessionText(s), startsWith('L2 语音警告（自动，'));
    expect(deterSessionText(<String, dynamic>{'level': 1, 'auto': false, 'by': 'gina'}), 'L1 灯光（gina 在管）');
    expect(deterSessionText(<String, dynamic>{'level': 3, 'auto': true, 'next_in_s': null}),
        'L3 警笛（自动，已到最高的自动一级）');
  });

  testWidgets('保安：跳级、解除', (t) async {
    final api = FakeApi('guard')..deterRows = _rows();
    await _open(t, api);
    expect(find.descendant(of: find.byKey(const Key('deterrence-now')), matching: find.textContaining('L2 语音警告')),
        findsOneWidget);
    expect(t.widget<ButtonStyleButton>(find.byKey(SiteDeterrencePage.levelKey(2))).onPressed, isNull,
        reason: '当前这一级按不了');
    await t.tap(find.byKey(SiteDeterrencePage.levelKey(4)));
    await t.pumpAndSettle();
    expect(api.calls, contains('deter-level A 4'));
    expect(find.textContaining('L4 人工（u 在管）'), findsOneWidget);
    await t.tap(find.byKey(SiteDeterrencePage.releaseKey));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('deterrence-release-go')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter-release A');
    expect(find.text('没在驱离'), findsOneWidget);
  });

  testWidgets('业主：只能解除，不能跳级', (t) async {
    final api = FakeApi('owner')..deterRows = _rows();
    await _open(t, api);
    expect(find.byKey(SiteDeterrencePage.levelKey(3)), findsNothing);
    expect(find.byKey(SiteDeterrencePage.releaseKey), findsOneWidget);
  });

  testWidgets('站点推「结束了」：当场说清楚', (t) async {
    final api = FakeApi('guard')..deterRows = _rows();
    await _open(t, api);
    api.sse.add(<String, dynamic>{'kind': 'deterrence', 'robot_id': 'A', 'session': null, 'ended': '到 10 分钟自动收'});
    await t.pump(const Duration(milliseconds: 50));
    expect(find.text('没在驱离'), findsOneWidget);
    expect(find.textContaining('到 10 分钟自动收'), findsOneWidget);
  });

  testWidgets('首页：一场驱离一行；推的帧当场换、结束了就没了；点进去是驱离页', (t) async {
    final api = FakeApi('guard')..deterRows = _rows();
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('deterrence-A')), findsOneWidget);
    api.sse.add(<String, dynamic>{'kind': 'deterrence', 'robot_id': 'A',
      'session': <String, dynamic>{..._rows().first, 'level': 3, 'next_in_s': null}});
    await t.pump(const Duration(milliseconds: 100));
    expect(find.textContaining('L3 警笛'), findsOneWidget);
    await t.tap(find.byKey(const Key('deterrence-A')));
    await t.pumpAndSettle();
    expect(find.byType(SiteDeterrencePage), findsOneWidget);
    Navigator.of(t.element(find.byType(SiteDeterrencePage))).pop();
    await t.pumpAndSettle();
    api.deterRows = <Map<String, dynamic>>[];
    api.sse.add(<String, dynamic>{'kind': 'deterrence', 'robot_id': 'A', 'session': null, 'ended': 'x'});
    await t.pump(const Duration(milliseconds: 100));
    expect(find.byKey(const Key('deterrence-A')), findsNothing);
  });

  testWidgets('首页：老站点没有驱离不挡', (t) async {
    final api = FakeApi('guard')..deterrenceError = null;
    await t.binding.setSurfaceSize(const Size(800, 360));
    addTearDown(() => t.binding.setSurfaceSize(null));
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('deterrence-A')), findsNothing);
    expect(find.byKey(SiteRobotsPage.modeKey), findsOneWidget, reason: '别的照常');
  });
}
