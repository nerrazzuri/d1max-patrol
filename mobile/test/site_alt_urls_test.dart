// POC 11-05：同一个站点在不同网里地址不同（现场 3 个 IP）。站点卡上能加备用地址；登录时按顺序试，
// 连不上的换下一个；哪个登上了下次先试它。证书指纹同一个（同一台站点主机）。
import 'package:nexwar_patrol/net/push.dart';
import 'package:nexwar_patrol/net/site_client.dart';
import 'package:nexwar_patrol/store/site_store.dart';
import 'package:nexwar_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

class _Unreachable extends FakeApi {
  _Unreachable() : super('guard');
  @override
  Future<SiteSession> login(String n, String p) async =>
      throw const SiteError(0, '无法连接站点：No route to host');
}

void main() {
  test('站点条目带备用地址：存得下、读得回；老文件没有这一项照旧', () {
    const s = SiteEntry(name: '商场', url: 'https://a:1', fingerprint: 'ab', username: 'g',
        alts: ['https://b:1']);
    final back = SiteEntry.fromJson(s.toJson());
    expect(back.urls, ['https://a:1', 'https://b:1']);
    expect(SiteEntry.fromJson(<String, dynamic>{'name': 'x', 'url': 'https://a:1'}).alts, isEmpty);
    expect(back.preferring('https://b:1').urls, ['https://b:1', 'https://a:1']);
  });

  testWidgets('登录：第一个地址连不上就试备用的；登上了记住先用它', (t) async {
    final store = MemorySiteStore([
      const SiteEntry(name: '商场', url: 'https://a:1', fingerprint: 'ab', username: 'gina',
          alts: ['https://b:1'])
    ]);
    final tried = <String>[];
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(
        home: SiteListPage(
            store: store,
            push: const NoPush(),
            apiFactory: (s) {
              tried.add(s.url);
              return s.url == 'https://a:1' ? _Unreachable() : api;
            })));
    await t.pumpAndSettle();
    await t.tap(find.text('商场'));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.tap(find.text('登录'));
    await t.pumpAndSettle();
    expect(find.byType(NwShell), findsOneWidget);
    expect(tried.take(2), ['https://a:1', 'https://b:1']);
    expect(store.sites.single.urls, ['https://b:1', 'https://a:1'], reason: '下次先试登上的那个');
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('都连不上：每个地址的原因都说', (t) async {
    final store = MemorySiteStore([
      const SiteEntry(name: '商场', url: 'https://a:1', fingerprint: 'ab', username: 'gina',
          alts: ['https://b:1'])
    ]);
    await t.pumpWidget(MaterialApp(
        home: SiteListPage(store: store, push: const NoPush(), apiFactory: (_) => _Unreachable())));
    await t.pumpAndSettle();
    await t.tap(find.text('商场'));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.tap(find.text('登录'));
    await t.pumpAndSettle();
    expect(find.textContaining('https://a:1'), findsWidgets);
    expect(find.textContaining('https://b:1'), findsWidgets);
    expect(find.textContaining('No route to host'), findsOneWidget);
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('站点卡菜单：编辑备用地址，一行一个；不是 https:// 开头的不存', (t) async {
    final store = MemorySiteStore([
      const SiteEntry(name: '商场', url: 'https://a:1', fingerprint: 'ab', username: 'gina')
    ]);
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store, push: const NoPush())));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-menu-商场')));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-alts-商场')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-alts-field')), 'http://c:2');
    await t.tap(find.byKey(const Key('site-alts-save')));
    await t.pumpAndSettle();
    expect(find.textContaining('https://'), findsWidgets);
    expect(store.sites.single.alts, isEmpty);
    await t.enterText(find.byKey(const Key('site-alts-field')), 'https://c:2\n\n https://d:3 \n');
    await t.tap(find.byKey(const Key('site-alts-save')));
    await t.pumpAndSettle();
    expect(store.sites.single.alts, ['https://c:2', 'https://d:3']);
    expect(store.sites.single.url, 'https://a:1');
  });
}
