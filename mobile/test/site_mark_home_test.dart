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

  test('忙、落不了盘、在走：说人话', () {
    expect(ackReasonText('busy: 在跑任务 goto-1'), '它正忙着别的：在跑任务 goto-1');
    expect(ackReasonText('busy'), '它正忙着别的');
    expect(ackReasonText('persist_failed: [Errno 28] No space left'), contains('记不下'));
    expect(ackReasonText('moving'), isNot(contains('设位置')), reason: '标原点也用这句');
  });

  testWidgets('同名的点在别的图上：再问一次，说了替换才搬', (t) async {
    final api = _TakenApi();
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-mark-home')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('mark-home-go')));
    await t.pumpAndSettle();
    expect(find.textContaining('other:9'), findsWidgets);
    expect(api.calls.where((c) => c.startsWith('markHome')), ['markHome A']);
    await t.tap(find.byKey(const Key('mark-home-replace')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('markHome')), ['markHome A', 'markHome A replace']);
    expect(find.textContaining('原点标好了'), findsOneWidget);
  });

  testWidgets('同名的点在别的图上：点「算了」不搬', (t) async {
    final api = _TakenApi();
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-mark-home')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('mark-home-go')));
    await t.pumpAndSettle();
    await t.tap(find.text('算了'));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('markHome')), ['markHome A']);
    expect(find.textContaining('没标'), findsOneWidget);
  });

  testWidgets('站点登记没成：说狗上已经换了、给坐标好手工登记', (t) async {
    final api = _ErrApi(const SiteError(500, '狗上的原点已经换了,站点登记待命点没成:库写不进去',
        body: <String, dynamic>{
          'data': <String, dynamic>{'map_id': 'm', 'map_version': '1', 'x': 4.2, 'y': 1.0, 'yaw': 0.5},
        }));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-mark-home')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('mark-home-go')));
    await t.pumpAndSettle();
    expect(find.textContaining('(4.2, 1.0)'), findsOneWidget);
    expect(find.textContaining('没标'), findsNothing, reason: '狗上已经换了，不许说「没标」');
  });

  testWidgets('等回话超时：照站点说的（狗可能已经标了）', (t) async {
    final api = _ErrApi(const SiteError(504, '等狗回话超时:狗可能已经标了 —— 过一会儿刷新看看'));
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-mark-home')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('mark-home-go')));
    await t.pumpAndSettle();
    expect(find.textContaining('可能已经标了'), findsOneWidget);
    expect(find.textContaining('没标'), findsNothing);
  });
}

class _TakenApi extends FakeApi {
  _TakenApi() : super('admin', robotView: _view());
  @override
  Future<Map<String, dynamic>> markHome(String robotId,
      {String name = '', bool replace = false}) async {
    if (!replace) {
      calls.add('markHome $robotId');
      throw const SiteError(409, '待命点 home 登记在 other:9 上,狗现在用的是 m:1:换个名字,或者说明要替换',
          body: <String, dynamic>{
            'name_taken': <String, dynamic>{'map_id': 'other', 'map_version': '9'},
          });
    }
    return super.markHome(robotId, name: name, replace: replace);
  }
}

class _ErrApi extends FakeApi {
  _ErrApi(this.err) : super('admin', robotView: _view());
  final SiteError err;
  @override
  Future<Map<String, dynamic>> markHome(String robotId,
          {String name = '', bool replace = false}) async =>
      throw err;
}

class _RefuseApi extends FakeApi {
  _RefuseApi() : super('admin', robotView: _view());
  @override
  Future<Map<String, dynamic>> markHome(String robotId,
          {String name = '', bool replace = false}) async =>
      throw const SiteError(409, '狗没标:loc_poor: 位置偏差可能到 0.9 m');
}
