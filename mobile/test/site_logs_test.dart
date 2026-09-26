// 建图进程日志（W00c6g）：管理员在单狗页点「建图日志」→ 狗上录包、重建子进程的日志列表 → 点开看尾巴
// （等宽字、能滚、有刷新）。日志经站点现取，站点不存。
import 'dart:async';

import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/site_logs.dart';
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
    tasks['proc_log'] = <String, dynamic>{};
  } else {
    tasks.remove('proc_log');
  }
  caps['tasks'] = tasks;
  v['capabilities'] = caps;
  return v;
}

void main() {
  testWidgets('管理员：单狗页 →「建图日志」→ 列表 → 点开看尾巴 → 刷新再取一次', (t) async {
    final api = FakeApi('admin', robotView: _view());
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('btn-proc-logs')));
    await t.pumpAndSettle();
    expect(api.calls, contains('procLogs A'));
    expect(find.text('slam'), findsOneWidget);
    expect(find.text('bagrecord'), findsOneWidget);
    await t.tap(find.byKey(SiteProcLogsPage.refreshKey));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c == 'procLogs A').length, 2, reason: '列表也能刷新');
    await t.tap(find.text('slam'));
    await t.pumpAndSettle();
    expect(api.calls, contains('procLog A slam'));
    expect(find.textContaining('[slam_toolbox] 回环'), findsOneWidget);
    expect(find.textContaining('只显示最后'), findsOneWidget, reason: '前面还有没给的，要说');
    final n = api.calls.where((c) => c == 'procLog A slam').length;
    await t.tap(find.byKey(SiteProcLogPage.refreshKey));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c == 'procLog A slam').length, n + 1);
  });

  testWidgets('保安、狗不支持：没有入口', (t) async {
    await t.pumpWidget(MaterialApp(
        home: SiteRobotPage(api: FakeApi('guard', robotView: _view()), robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-proc-logs')), findsNothing);
    await t.pumpWidget(MaterialApp(
        home: SiteRobotPage(
            key: UniqueKey(), api: FakeApi('admin', robotView: _view(can: false)), robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('btn-proc-logs')), findsNothing);
  });

  testWidgets('没有日志、取不到：说清楚，不是一片空白', (t) async {
    final api = FakeApi('admin', robotView: _view())..procLogRows = <Map<String, dynamic>>[];
    await t.pumpWidget(MaterialApp(home: SiteProcLogsPage(api: api, robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.textContaining('还没有日志'), findsOneWidget);
    api.procLogError = const SiteError(404, '狗没给:no_such_log');
    await t.pumpWidget(MaterialApp(
        home: SiteProcLogPage(key: UniqueKey(), api: api, robotId: 'A', name: 'slam')));
    await t.pumpAndSettle();
    expect(find.textContaining('取不到'), findsOneWidget);
    expect(find.textContaining('no_such_log'), findsOneWidget);
  });

  testWidgets('尾巴很长：打开就在最底下（最新的）；刷新时有进度条，内容换成新的', (t) async {
    final api = FakeApi('admin', robotView: _view())
      ..procLogText = [for (var i = 0; i < 300; i++) 'line $i'].join('\n');
    await t.pumpWidget(MaterialApp(home: SiteProcLogPage(api: api, robotId: 'A', name: 'slam')));
    await t.pumpAndSettle();
    final pos = t.state<ScrollableState>(find.byType(Scrollable).first).position;
    expect(pos.maxScrollExtent, greaterThan(0));
    expect(pos.pixels, pos.maxScrollExtent, reason: '最新的在最底下，打开就停在那儿');
    api.procLogGate = Completer<void>();
    api.procLogText = '换过了';
    await t.tap(find.byKey(SiteProcLogPage.refreshKey));
    await t.pump();
    expect(find.byType(LinearProgressIndicator), findsOneWidget, reason: '看得出在刷新');
    api.procLogGate!.complete();
    await t.pumpAndSettle();
    expect(find.byType(LinearProgressIndicator), findsNothing);
    expect(find.textContaining('换过了'), findsOneWidget);
  });

  test('日志大小说人话', () {
    expect(logSizeText(900), '900 B');
    expect(logSizeText(64 * 1024), '64 KB');
    expect(logSizeText(5 * 1024 * 1024 + 1), '5.0 MB');
  });
}
