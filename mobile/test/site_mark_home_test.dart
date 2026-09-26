// 在当前位置标原点（W00c6f）：管理员在单狗页点「在这儿标原点」，确认之后狗用它此刻的位置当原点，
// 站点登记成默认待命点；定位不好狗会拒。
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Map<String, dynamic> _view({bool can = true}) {
  final v = Map<String, dynamic>.of(siteFixture('site_robot'));
  final caps = Map<String, dynamic>.of(v['capabilities'] as Map<String, dynamic>);
  final tasks = Map<String, dynamic>.of(caps['tasks'] as Map<String, dynamic>);
  if (can) {
    tasks['mark_home'] = <String, dynamic>{};
  } else {
    tasks.remove('mark_home');
  }
  caps['tasks'] = tasks;
  v['capabilities'] = caps;
  return v;
}

void main() {
  testWidgets('管理员：确认之后标，显示标在哪', (t) async {
    final api = FakeApi('admin', robotView: _view());
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-mark-home')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('markHome')), isEmpty, reason: '没确认之前不发');
    await t.tap(find.byKey(const Key('mark-home-go')));
    await t.pumpAndSettle();
    expect(api.calls, contains('markHome A'));
    expect(find.textContaining('(1.5, -2.0)'), findsOneWidget);
  });

  testWidgets('点「算了」：不发', (t) async {
    final api = FakeApi('admin', robotView: _view());
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-mark-home')));
    await t.pumpAndSettle();
    await t.tap(find.text('算了'));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('markHome')), isEmpty);
  });

  testWidgets('保安、狗不支持：没有按钮', (t) async {
    final guard = FakeApi('guard', robotView: _view());
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: guard, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-mark-home')), findsNothing);
  });

  testWidgets('狗不支持：没有按钮', (t) async {
    final admin = FakeApi('admin', robotView: _view(can: false));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: admin, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-mark-home')), findsNothing);
  });

  testWidgets('狗拒了：原因说人话', (t) async {
    final api = _RefuseApi();
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-mark-home')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('mark-home-go')));
    await t.pumpAndSettle();
    expect(find.textContaining('定位不够好：位置偏差可能到 0.9 m'), findsOneWidget);
  });

  test('定位不好的拒收说人话', () {
    expect(ackReasonText('loc_poor: 位置偏差可能到 0.9 m'), contains('偏差'));
  });
}

class _RefuseApi extends FakeApi {
  _RefuseApi() : super('admin', robotView: _view());
  @override
  Future<Map<String, dynamic>> markHome(String robotId, {String name = ''}) async =>
      throw const SiteError(409, '狗没标:loc_poor: 位置偏差可能到 0.9 m');
}
