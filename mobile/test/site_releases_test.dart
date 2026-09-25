// 站点模式的版本（W00c5d 第三部分）：管理员给狗装、切、退；切、退要确认；别的角色只看。
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_releases.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

void main() {
  testWidgets('每台狗在跑哪一版；管理员装一版、切过去（要确认）、退回', (t) async {
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(home: SiteReleasesPage(api: api)));
    await t.pumpAndSettle();
    expect(find.text('2026-09-20-aaaaaa'), findsOneWidget);
    expect(find.textContaining('夜巡修了一个 bug'), findsOneWidget);
    await t.tap(find.byKey(SiteReleasesPage.actionKey('A', 'install')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('pick-rel-2026-09-25-bbbbbb')));
    await t.pumpAndSettle();
    expect(api.calls, contains('release A install 2026-09-25-bbbbbb'));
    await t.tap(find.byKey(SiteReleasesPage.actionKey('A', 'activate')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('pick-rel-2026-09-25-bbbbbb')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.contains('activate')), isEmpty, reason: '没确认之前不发');
    await t.tap(find.byKey(const Key('rel-confirm')));
    await t.pumpAndSettle();
    expect(api.calls, contains('release A activate 2026-09-25-bbbbbb'));
    await t.tap(find.byKey(SiteReleasesPage.actionKey('A', 'rollback')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('rel-confirm')));
    await t.pumpAndSettle();
    expect(api.calls, contains('release A rollback '));
  });

  testWidgets('狗没报在跑哪一版（不支持或不在线）：没有装切退按钮', (t) async {
    final api = _TwoDogsApi();
    await t.pumpWidget(MaterialApp(home: SiteReleasesPage(api: api)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteReleasesPage.actionKey('A', 'install')), findsOneWidget);
    expect(find.byKey(SiteReleasesPage.actionKey('B', 'install')), findsNothing);
  });

  testWidgets('保安只看：没有装切退按钮', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteReleasesPage(api: api)));
    await t.pumpAndSettle();
    expect(find.byKey(SiteReleasesPage.robotKey('A')), findsOneWidget);
    expect(find.byKey(SiteReleasesPage.actionKey('A', 'install')), findsNothing);
  });

  testWidgets('狗列表页有「版本」入口', (t) async {
    final api = FakeApi('owner');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, title: '庄园')));
    await t.pump();
    await t.tap(find.byKey(const Key('open-releases')));
    await t.pumpAndSettle();
    expect(find.byType(SiteReleasesPage), findsOneWidget);
  });
}

/// 两台狗：A 报了在跑哪一版，B 没报（不支持站点下发版本，或者不在线）。
class _TwoDogsApi extends FakeApi {
  _TwoDogsApi() : super('admin');
  @override
  Future<Map<String, dynamic>> releases() async => {
        'releases': <Object>[],
        'robots': {'A': '2026-09-20-aaaaaa', 'B': null},
      };
}
