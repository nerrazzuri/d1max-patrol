// 站点模式的地图（W00c5d 第二部分）：管理员下发图、拿录包重建、录包开始停止；别的角色只看。
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

void main() {
  testWidgets('管理员点一张图、选狗、下发', (t) async {
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(home: SiteMapsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.textContaining('厂商图'), findsOneWidget);
    await t.tap(find.byKey(SiteMapsPage.mapKey('estate-1', '8')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('pick-A')));
    await t.pumpAndSettle();
    expect(api.calls, contains('map A estate-1:8'));
    expect(find.textContaining('在下载、载入'), findsOneWidget);
  });

  testWidgets('管理员拿录包重建：给一个新版本号', (t) async {
    final api = FakeApi('admin');
    await t.pumpWidget(MaterialApp(home: SiteMapsPage(api: api)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteMapsPage.bagKey('A', 'yard-0925')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(SiteMapsPage.versionKey), '9');
    await t.tap(find.byKey(const Key('build-go')));
    await t.pumpAndSettle();
    expect(api.calls, contains('build A yard-0925 estate-1:9'));
  });

  testWidgets('保安只看：点了不下发', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteMapsPage(api: api)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(SiteMapsPage.mapKey('estate-1', '8')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('pick-A')), findsNothing);
    expect(api.calls.where((c) => c.startsWith('map ')), isEmpty);
  });

  testWidgets('单狗页：管理员有录包开始、停止；保安没有', (t) async {
    final admin = FakeApi('admin');
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: admin, robotId: 'A')));
    await t.pump();
    await t.tap(find.byKey(const Key('btn-record-start')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('record-name')), 'yard');
    await t.tap(find.byKey(const Key('record-go')));
    await t.pumpAndSettle();
    expect(admin.calls, contains('mapping A start yard'));
    await t.tap(find.byKey(const Key('btn-record-stop')));
    await t.pumpAndSettle();
    expect(admin.calls, contains('mapping A stop '));
    final guard = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(key: UniqueKey(), api: guard, robotId: 'A')));
    await t.pump();
    expect(find.byKey(const Key('btn-record-start')), findsNothing);
  });

  testWidgets('狗列表页有「地图」入口', (t) async {
    final api = FakeApi('owner');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api, title: '庄园')));
    await t.pump();
    await t.tap(find.byKey(const Key('open-maps')));
    await t.pumpAndSettle();
    expect(find.byType(SiteMapsPage), findsOneWidget);
  });
}
