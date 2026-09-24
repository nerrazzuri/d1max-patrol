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
  final List<String> calls = <String>[];
  final StreamController<Map<String, dynamic>> sse = StreamController.broadcast();

  @override
  Future<SiteSession> login(String n, String p) async => session!;
  @override
  Future<void> logout() async {}
  @override
  Future<List<Map<String, dynamic>>> robots() async =>
      (siteFixture('site_robots')['robots'] as List).cast<Map<String, dynamic>>();
  @override
  Future<Map<String, dynamic>> robot(String id) async => view;
  @override
  Future<Map<String, dynamic>> patrol(String id, String m) async {
    calls.add('patrol $id $m');
    return {'ack': {'result': 'accepted'}};
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
  Stream<Map<String, dynamic>> events() => sse.stream;
  @override
  void close() {}
}

Map<String, dynamic> busyView() {
  final v = Map<String, dynamic>.of(siteFixture('site_robot'));
  final st = Map<String, dynamic>.of(v['status'] as Map<String, dynamic>);
  st['task'] = {'task_id': 'task-7', 'kind': 'patrol', 'state': 'running'};
  v['status'] = st;
  return v;
}

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
    expect(api.calls, ['abort A task-7']);
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
}

class _NoRegistry implements RegistryStore {
  @override
  dynamic noSuchMethod(Invocation i) => super.noSuchMethod(i);
}

class _NoVault implements PinVault {
  @override
  dynamic noSuchMethod(Invocation i) => super.noSuchMethod(i);
}
