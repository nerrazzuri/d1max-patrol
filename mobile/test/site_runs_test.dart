// 站点模式的运行记录（W00c5d）：列表、一趟、照片、复核（保安、管理员）、重判；业主只看。夹具来自真站点。
import 'package:d1max_patrol/net/site_client.dart' show SiteError;
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_runs.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

Future<void> _pumpRuns(WidgetTester t, FakeApi api) async {
  await t.pumpWidget(MaterialApp(home: SiteRunsPage(key: UniqueKey(), api: api)));
  await t.pump();
  await t.pump();
}

void main() {
  test('时刻给人看：UTC 目录名换成本地时间；结论说人话', () {
    final local = DateTime.utc(2026, 9, 25, 1).toLocal();
    expect(stampText('20260925T010000Z'),
        '${local.year}-09-${local.day.toString().padLeft(2, '0')} '
        '${local.hour.toString().padLeft(2, '0')}:00');
    expect(stampText('not-a-stamp'), 'not-a-stamp');
    expect(verdictText('abnormal'), '异常');
    expect(verdictText(null), '待判读');
  });

  testWidgets('列表：一趟一行、有异常标红；点进去看每张照片的判读与复核', (t) async {
    final api = FakeApi('guard');
    await _pumpRuns(t, api);
    expect(find.textContaining('巡检一'), findsOneWidget);
    expect(find.textContaining('异常 1'), findsOneWidget);
    expect(find.byIcon(Icons.report), findsOneWidget);
    await t.tap(find.byKey(SiteRunsPage.runKey(1)));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('photo-P1__front__20260925T010000Z.jpg')), findsOneWidget);
    expect(find.textContaining('（人已复核）'), findsOneWidget);
    expect(find.textContaining('复核：正常 风吹的'), findsOneWidget);
    expect(find.textContaining('判读：看过了'), findsOneWidget);
    expect(api.calls.where((c) => c.startsWith('photo 1 ')).length, 2);
    await t.tap(find.byKey(SiteRunPage.judgeKey));
    await t.pumpAndSettle();
    expect(api.calls, contains('judge 1'));
    await t.pumpWidget(Container());
  });

  testWidgets('保安复核一张：写一句、点「异常」，回到这一趟重新拿', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteRunPage(api: api, runId: 1)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('photo-P2__front__20260925T010000Z.jpg')));
    await t.pumpAndSettle();
    expect(find.textContaining('模型：正常（把握 70%）'), findsOneWidget);
    await t.enterText(find.byKey(SitePhotoPage.noteKey), '有人翻墙');
    await t.tap(find.byKey(SitePhotoPage.reviewKey('abnormal')));
    await t.pumpAndSettle();
    expect(api.calls, contains('review 1 P2__front__20260925T010000Z.jpg abnormal 有人翻墙'));
    expect(api.calls.where((c) => c == 'run 1').length, 2, reason: '复核之后重新拿这一趟');
    await t.pumpWidget(Container());
  });

  testWidgets('业主只看：没有复核按钮、没有重判', (t) async {
    final api = FakeApi('owner');
    await t.pumpWidget(MaterialApp(home: SiteRunPage(api: api, runId: 1)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteRunPage.judgeKey), findsNothing);
    await t.tap(find.byKey(const Key('photo-P1__front__20260925T010000Z.jpg')));
    await t.pumpAndSettle();
    expect(find.byKey(SitePhotoPage.reviewKey('normal')), findsNothing);
    await t.pumpWidget(Container());
  });

  testWidgets('拿不到记录：说出来，不画成「还没有记录」', (t) async {
    final api = FakeApi('guard')..runsError = const SiteError(502, '站点忙');
    await _pumpRuns(t, api);
    expect(find.textContaining('记录拿不到'), findsOneWidget);
    expect(find.text('还没有记录'), findsNothing);
    await t.pumpWidget(Container());
  });

  testWidgets('狗列表页有「记录」入口', (t) async {
    final api = FakeApi('owner');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, title: '庄园')));
    await t.pump();
    await t.tap(find.byKey(const Key('open-runs')));
    await t.pumpAndSettle();
    expect(find.byType(SiteRunsPage), findsOneWidget);
    await t.pumpWidget(Container());
  });
}
