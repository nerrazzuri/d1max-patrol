// 站点模式的值守屏（W00c5a）：告警与汇总从站点来（夹具是 Python 从真站点 API 取的）。
import 'dart:async';

import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/model/site_watch.dart';
import 'package:d1max_patrol/net/site_client.dart' show SiteError;
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

Future<void> _open(WidgetTester t, FakeApi api) async {
  await t.pumpWidget(MaterialApp(home: SiteWatchPage(api: api)));
  await t.pumpAndSettle();
}

void main() {
  testWidgets('保安看到 P1 急停，点确认调站点（不带名字），确认后重读', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    expect(find.textContaining('急停被按下'), findsOneWidget);
    expect(find.byKey(SiteWatchPage.p1NoneKey), findsNothing);
    final before = api.alertsCalls;
    await t.tap(find.byKey(SiteWatchPage.ackKey('A/estop_pressed#1')));
    await t.pumpAndSettle();
    expect(api.calls, contains('ack A/estop_pressed#1'));
    expect(api.alertsCalls, greaterThan(before));
  });

  testWidgets('业主看得见告警，没有确认与解决按钮', (t) async {
    final api = FakeApi('owner');
    await _open(t, api);
    expect(find.textContaining('急停被按下'), findsOneWidget);
    expect(find.byKey(SiteWatchPage.ackKey('A/estop_pressed#1')), findsNothing);
    expect(find.byKey(SiteWatchPage.resolveKey('A/estop_pressed#1')), findsNothing);
  });

  testWidgets('没有 P1 要明确写「没有」', (t) async {
    final api = FakeApi('guard')..alertRows = <Map<String, dynamic>>[];
    await _open(t, api);
    expect(find.byKey(SiteWatchPage.p1NoneKey), findsOneWidget);
  });

  testWidgets('站点回了一份读不懂的告警名单：报读不懂，绝不说「没有 P1」', (t) async {
    final api = FakeApi('guard')..alertsBody = <String, dynamic>{'error': 'captive portal'};
    await _open(t, api);
    expect(find.byKey(SiteWatchPage.alertsErrorKey), findsOneWidget);
    expect(find.byKey(SiteWatchPage.p1NoneKey), findsNothing);
  });

  testWidgets('狗上存储那几项印「不知道」和理由，不写 0', (t) async {
    final api = FakeApi('owner');
    await _open(t, api);
    final tile = find.byKey(SiteWatchPage.robotKey('A'));
    expect(tile, findsOneWidget);
    final text = (t.widget<ListTile>(tile).subtitle! as Text).data!;
    expect(text, contains('盘水位 不知道（狗上的存储情况还没接到站点'));
    expect(text, contains('证据积压 不知道'));
    expect(text, contains('在线'));
  });

  testWidgets('告警读不到不把汇总带走，反过来也一样', (t) async {
    final a = FakeApi('guard')..alertsError = const SiteError(500, '站点内部错误');
    await _open(t, a);
    expect(find.byKey(SiteWatchPage.alertsErrorKey), findsOneWidget);
    expect(find.byKey(SiteWatchPage.robotKey('A')), findsOneWidget);
    final b = FakeApi('guard')..summaryError = const SiteError(500, '站点内部错误');
    await t.pumpWidget(Container());
    await _open(t, b);
    expect(find.byKey(SiteWatchPage.summaryErrorKey), findsOneWidget);
    expect(find.textContaining('急停被按下'), findsOneWidget);
  });

  testWidgets('SSE 来了告警就重读', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    final before = api.alertsCalls;
    api.sse.add(siteFixture('site_alert_frame'));
    await t.pump(const Duration(milliseconds: 400));
    await t.pumpAndSettle();
    expect(api.alertsCalls, greaterThan(before));
  });

  test('只有升到声音档、没人确认、没解决的才响铃', () {
    final f = siteFixture('site_alert_frame');
    expect(alertWantsSound(f), isFalse, reason: '夹具是屏幕档');
    Map<String, dynamic> with_(Map<String, dynamic> kv) => <String, dynamic>{
          ...f,
          'alert': <String, dynamic>{...(f['alert'] as Map<String, dynamic>), ...kv}
        };
    expect(alertWantsSound(with_({'channel': 'sound'})), isTrue);
    expect(alertWantsSound(with_({'channel': 'sound', 'acked_ms': 1})), isFalse);
    expect(alertWantsSound(with_({'channel': 'sound', 'resolved_ms': 1})), isFalse);
    expect(alertWantsSound(<String, dynamic>{'kind': 'status'}), isFalse);
  });

  testWidgets('狗列表页：声音档的告警帧响铃；值守入口打得开', (t) async {
    final api = FakeApi('guard');
    var rang = 0;
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, ring: () => rang++)));
    await t.pumpAndSettle();
    final f = siteFixture('site_alert_frame');
    api.sse.add(f);
    await t.pump();
    expect(rang, 0);
    api.sse.add(<String, dynamic>{
      ...f,
      'alert': <String, dynamic>{...(f['alert'] as Map<String, dynamic>), 'channel': 'sound'}
    });
    await t.pump();
    expect(rang, 1);
    await t.pump(const Duration(seconds: 1));
    await t.tap(find.byKey(const Key('open-watch')));
    await t.pumpAndSettle();
    expect(find.byType(SiteWatchPage), findsOneWidget);
  });

  test('交接班那一张：三种局面的形状都在（真站点的夹具）', () {
    // 同时有没人管且升到顶的、有人确认过的、聚合两次且已解决的。`acked_ms`/`resolved_ms`
    // 恒为 null 的夹具教不会这头它们非空时长什么样 —— 解析错了屏上写「1970-01-01 已确认」。
    final alerts = alertsFromWire(siteFixture('site_alerts_all'));
    final byKind = <String, Alert>{for (final a in alerts) a.kind: a};
    final fallen = byKind['fallen']!;
    expect(fallen.level, 'P1');
    expect(fallen.ackedMs, isNull);
    expect(fallen.resolvedMs, isNull);
    // `channel` 是站点算好发来的，不是这头再算一遍。
    expect(fallen.escalated, 2);
    expect(fallen.channel, 'sound');
    final estop = byKind['estop_pressed']!;
    expect(estop.ackedMs, isNotNull);
    expect(estop.ackedBy, 'alice');
    final skew = byKind['clock_skew']!;
    expect(skew.count, 2);
    expect(skew.lastMs, greaterThan(skew.firstMs));
    expect(skew.resolvedMs, isNotNull);
    expect(skew.ackedMs, isNull, reason: '解决不许冒充确认');
    expect(fallen.key, contains('/'));
    expect(fallen.key, contains('#'));
  });

  test('汇总：整数报成整数也收得下（钟正好对上时 clock_skew_s 是 0）', () {
    final r = SiteWatchRobot.fromWire(
        const <String, dynamic>{'robot_id': 'A', 'clock_skew_s': 0, 'battery_pct': 100});
    expect(r.clockSkewS, 0.0);
    expect(r.batteryPct, 100.0);
    expect(r.diskUsedRatio, isNull, reason: 'null 不许塌成 0');
    expect(r.whyFor('disk_used_ratio'), isNotEmpty);
  });

  testWidgets('屏上写着告警读于几点几分；实时更新断了看得见，并且自己重连', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(
        home: SiteWatchPage(api: api, now: () => DateTime(2026, 9, 25, 21, 7, 3))));
    await t.pumpAndSettle();
    expect(find.text('告警读于 21:07:03；下拉可以再问一次'), findsOneWidget);
    expect(find.byKey(SiteWatchPage.liveLostKey), findsNothing);
    final opened = api.eventsOpened;
    await api.sse.close();
    await t.pump();
    expect(find.byKey(SiteWatchPage.liveLostKey), findsOneWidget);
    api.sse = StreamController<Map<String, dynamic>>.broadcast();
    await t.pump(const Duration(seconds: 6));
    expect(api.eventsOpened, greaterThan(opened), reason: '断了要自己重连');
    api.sse.add(siteFixture('site_alert_frame'));
    await t.pump();
    await t.pump();
    expect(find.byKey(SiteWatchPage.liveLostKey), findsNothing, reason: '连上了就不再挂着');
    await t.pump(const Duration(seconds: 1));
  });
}
