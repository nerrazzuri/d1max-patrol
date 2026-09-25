// 站点模式的几屏（W00c4）：按角色显示按钮、列表与单狗页用真站点的夹具、首屏二选一、站点列表存取。
import 'dart:async';

import 'package:d1max_patrol/main.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/store/pin_vault.dart';
import 'package:d1max_patrol/store/registry_store.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_site.dart';

class FakeApi implements SiteApi {
  FakeApi(String role, {Map<String, dynamic>? robotView})
      : session = SiteSession('tok', 'u', role),
        view = robotView ?? siteFixture('site_robot');
  @override
  SiteSession? session;
  Map<String, dynamic> view;

  /// 给了就从第二次起换成它（模拟页面上的旧状态、站点上的新任务）。
  Map<String, dynamic>? laterView;
  int robotCalls = 0;
  int robotsCalls = 0;
  SiteError? patrolError;
  SiteError? robotsError;
  final List<String> calls = <String>[];
  StreamController<Map<String, dynamic>> sse = StreamController.broadcast();
  int eventsOpened = 0;

  @override
  Future<SiteSession> login(String n, String p) async => session!;
  @override
  Future<void> logout() async {}
  @override
  Future<List<Map<String, dynamic>>> robots() async {
    robotsCalls++;
    if (robotsError != null) {
      if (robotsError!.status == 401) session = null;
      throw robotsError!;
    }
    return (siteFixture('site_robots')['robots'] as List).cast<Map<String, dynamic>>();
  }
  @override
  Future<Map<String, dynamic>> robot(String id) async {
    robotCalls++;
    return robotCalls > 1 && laterView != null ? laterView! : view;
  }
  @override
  Future<Map<String, dynamic>> patrol(String id, String m) async {
    calls.add('patrol $id $m');
    if (patrolError != null) throw patrolError!;
    return siteFixture('site_dispatch');
  }
  @override
  Future<Map<String, dynamic>> abort(String id, String t) async {
    calls.add('abort $id $t');
    return {'ack': {'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> returnToStandby(String id) async {
    calls.add('standby $id');
    return {'ack': {'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> schedule() async => siteFixture('site_schedule');
  @override
  Future<List<Map<String, dynamic>>> incidents() async =>
      (siteFixture('site_incidents')['incidents'] as List).cast<Map<String, dynamic>>();
  @override
  Stream<Map<String, dynamic>> events() {
    eventsOpened++;
    return sse.stream;
  }
  @override
  void close() {}
}

/// 狗正在跑任务时的视图：真站点取的（`site_robot_busy.json`）。
Map<String, dynamic> busyView() => siteFixture('site_robot_busy');

String busyTaskId() =>
    ((busyView()['status'] as Map)['task'] as Map)['task_id'] as String;

Future<void> openRobot(WidgetTester t, FakeApi api) async {
  await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
  await t.pumpAndSettle();
  await t.tap(find.byKey(const Key('robot-A')));
  await t.pumpAndSettle();
}

void main() {
  testWidgets('狗的列表：来自真站点的夹具，显示在线与任务', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.text('A'), findsOneWidget);
    expect(find.textContaining('在线'), findsOneWidget);
  });

  testWidgets('保安：能派巡检、回待命点、叫停', (t) async {
    final api = FakeApi('guard', robotView: busyView());
    await openRobot(t, api);
    expect(find.byKey(const Key('btn-patrol')), findsOneWidget);
    expect(find.byKey(const Key('btn-standby')), findsOneWidget);
    await t.tap(find.byKey(const Key('btn-abort')));
    await t.pumpAndSettle();
    expect(api.calls, ['abort A ${busyTaskId()}']);
    await t.tap(find.byKey(const Key('btn-patrol')));
    await t.pumpAndSettle();
    await t.tap(find.text('loop'));
    await t.pumpAndSettle();
    expect(api.calls.last, 'patrol A loop');
  });

  testWidgets('业主：只有叫停', (t) async {
    final api = FakeApi('owner', robotView: busyView());
    await openRobot(t, api);
    expect(find.byKey(const Key('btn-patrol')), findsNothing);
    expect(find.byKey(const Key('btn-standby')), findsNothing);
    expect(find.byKey(const Key('btn-abort')), findsOneWidget);
  });

  testWidgets('没在跑任务就没有叫停按钮', (t) async {
    final idle = Map<String, dynamic>.of(siteFixture('site_robot'));
    final st = Map<String, dynamic>.of(idle['status'] as Map<String, dynamic>)..['task'] = null;
    idle['status'] = st;
    await openRobot(t, FakeApi('guard', robotView: idle));
    expect(find.byKey(const Key('btn-abort')), findsNothing);
  });

  testWidgets('首屏二选一：站点 / 现场直连', (t) async {
    await t.pumpWidget(MaterialApp(
        home: ModePage(
            store: _NoRegistry(), vault: _NoVault(), siteStore: MemorySiteStore())));
    expect(find.byKey(const Key('mode-site')), findsOneWidget);
    expect(find.byKey(const Key('mode-direct')), findsOneWidget);
    await t.tap(find.byKey(const Key('mode-site')));
    await t.pumpAndSettle();
    expect(find.text('还没有站点。右下角添加。'), findsOneWidget);
  });

  testWidgets('添加站点存进去：指纹规范化，口令不存', (t) async {
    final store = MemorySiteStore();
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-add')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-name')), '庄园');
    await t.enterText(find.byKey(const Key('site-url')), 'https://site.local:8443');
    await t.enterText(find.byKey(const Key('site-fp')), 'AB:' * 31 + 'AB');
    await t.enterText(find.byKey(const Key('site-user')), 'gina');
    await t.tap(find.text('保存'));
    await t.pumpAndSettle();
    expect(store.sites.single.fingerprint, 'ab' * 32);
    expect(store.sites.single.toJson().keys, isNot(contains('password')));
  });

  testWidgets('叫停用的是现取的任务号，不是页面上几秒前的', (t) async {
    final later = Map<String, dynamic>.of(busyView());
    final st = Map<String, dynamic>.of(later['status'] as Map<String, dynamic>);
    st['task'] = Map<String, dynamic>.of(st['task'] as Map<String, dynamic>)
      ..['task_id'] = 'task-new';
    later['status'] = st;
    final api = FakeApi('guard', robotView: busyView())..laterView = later;
    await openRobot(t, api);
    await t.tap(find.byKey(const Key('btn-abort')));
    await t.pumpAndSettle();
    expect(api.calls, ['abort A task-new']);
  });

  testWidgets('站点回 403：错误原样显示，不假装成功', (t) async {
    final api = FakeApi('guard')..patrolError = const SiteError(403, 'guard 没有 dispatch 权限');
    await openRobot(t, api);
    await t.tap(find.byKey(const Key('btn-patrol')));
    await t.pumpAndSettle();
    await t.tap(find.text('loop'));
    await t.pumpAndSettle();
    expect(find.textContaining('没成'), findsOneWidget);
    expect(find.textContaining('403'), findsOneWidget);
  });

  testWidgets('事件来了就刷新；事件流断了显示提示并重连', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    final before = api.robotsCalls;
    api.sse.add(siteFixture('site_event_frame'));
    await t.pump(const Duration(milliseconds: 600));
    expect(api.robotsCalls, greaterThan(before));
    expect(find.byKey(const Key('live-lost')), findsNothing);
    final old = api.sse;
    api.sse = StreamController.broadcast();
    await old.close();
    await t.pump();
    expect(find.byKey(const Key('live-lost')), findsOneWidget);
    await t.pump(const Duration(seconds: 6));
    expect(api.eventsOpened, 2, reason: '5 秒后自己重连');
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('令牌过期（401）退回站点列表并说明', (t) async {
    final store = MemorySiteStore([
      SiteEntry(name: '庄园', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'gina')
    ]);
    final api = FakeApi('guard')..robotsError = const SiteError(401, '没登录或登录已过期');
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store, apiFactory: (_) => api)));
    await t.pumpAndSettle();
    await t.tap(find.text('庄园'));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.tap(find.text('登录'));
    await t.pumpAndSettle();
    expect(find.text('还没有站点。右下角添加。'), findsNothing);
    expect(find.textContaining('重新登录'), findsOneWidget);
  });

  testWidgets('登录失败（比如证书指纹对不上）：提示原因，不进去', (t) async {
    final store = MemorySiteStore([
      SiteEntry(name: '庄园', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'gina')
    ]);
    await t.pumpWidget(MaterialApp(
        home: SiteListPage(store: store, apiFactory: (_) => _FailingApi())));
    await t.pumpAndSettle();
    await t.tap(find.text('庄园'));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.tap(find.text('登录'));
    await t.pumpAndSettle();
    expect(find.textContaining('指纹对不上'), findsOneWidget);
    expect(find.byType(SiteRobotsPage), findsNothing);
  });

  testWidgets('添加站点：坏指纹不保存', (t) async {
    final store = MemorySiteStore();
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-add')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-name')), '庄园');
    await t.enterText(find.byKey(const Key('site-url')), 'https://site.local:8443');
    await t.enterText(find.byKey(const Key('site-fp')), 'abc');
    await t.enterText(find.byKey(const Key('site-user')), 'gina');
    await t.tap(find.text('保存'));
    await t.pumpAndSettle();
    expect(store.sites, isEmpty);
    expect(find.textContaining('没保存'), findsOneWidget);
  });

  testWidgets('站点文件坏了：说出来，不给添加（免得覆盖掉其余站点）；能删站点', (t) async {
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: _BrokenStore())));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('site-load-error')), findsOneWidget);
    expect(find.byKey(const Key('site-add')), findsNothing);
    final store = MemorySiteStore([
      SiteEntry(name: 'a', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'u')
    ]);
    await t.pumpWidget(MaterialApp(home: SiteListPage(key: const Key('fresh'), store: store)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-remove-a')));
    await t.pumpAndSettle();
    expect(store.sites, isEmpty);
  });

  testWidgets('事件页与排程页读的是真站点的形状', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteIncidentsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.textContaining('yard'), findsWidgets);
    await t.pumpWidget(MaterialApp(home: SiteSchedulePage(api: api)));
    await t.pumpAndSettle();
    expect(find.textContaining('nightly'), findsOneWidget);
    expect(find.textContaining('estate-kl'), findsOneWidget);
  });
}

class _NoRegistry implements RegistryStore {
  @override
  dynamic noSuchMethod(Invocation i) => super.noSuchMethod(i);
}

class _NoVault implements PinVault {
  @override
  dynamic noSuchMethod(Invocation i) => super.noSuchMethod(i);
}

class _FailingApi extends FakeApi {
  _FailingApi() : super('guard');
  @override
  Future<SiteSession> login(String n, String p) async =>
      throw const SiteError(0, '站点证书指纹对不上：实际是 cd…');
}

class _BrokenStore implements SiteStore {
  @override
  Future<List<SiteEntry>> load() async => throw const FormatException('站点文件的顶层应该是个数组');
  @override
  Future<void> save(List<SiteEntry> s) async => throw StateError('不许写');
}
