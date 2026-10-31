// 进入的几页按设计稿重做（2026-10-14，P1）：选站点、登录（从弹窗改成一页）、加站点。
// 行为不变的那些在 site_page_test / app_v2_test / desktop_test / landscape_test 里；这里是新样子带来的：
// 登录页留得住人（口令错了说原因、能改了再试）、「换站点」回列表、删除收进卡片的菜单，
// 和这几页在各种屏幕、主题、语言、键盘弹起来时不溢出。
import 'dart:async';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/site_add.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

SiteEntry _site([String name = '庄园']) =>
    SiteEntry(name: name, url: 'https://192.168.100.200:8443', fingerprint: 'ab' * 32, username: 'gina');

/// 口令不对就拒；记下每次拿什么口令来登的。
class _PwApi extends FakeApi {
  _PwApi() : super('guard');
  final List<String> tried = <String>[];
  int closed = 0;
  @override
  Future<SiteSession> login(String n, String p) async {
    tried.add('$n/$p');
    if (p != 'right') throw const SiteError(401, '账号或口令不对');
    return session!;
  }

  @override
  void close() => closed++;
}

/// 登录卡在半路（站点慢）：测试自己放行。
class _SlowApi extends FakeApi {
  _SlowApi() : super('guard');
  final Completer<void> gate = Completer<void>();
  int logins = 0;
  @override
  Future<SiteSession> login(String n, String p) async {
    logins++;
    await gate.future;
    return session!;
  }
}

Future<void> _openLogin(WidgetTester t, SiteApi Function() make, {SiteStore? store}) async {
  await t.pumpWidget(MaterialApp(
      home: SiteListPage(store: store ?? MemorySiteStore([_site()]), apiFactory: (_) => make())));
  await t.pumpAndSettle();
  await t.tap(find.text('庄园'));
  await t.pumpAndSettle();
}

const _sizes = <Size>[Size(390, 844), Size(800, 360), Size(1280, 720), Size(1920, 1080)];

final _themes = <String, ThemeData? Function()>{
  'light': () => nwTheme(Brightness.light),
  'dark': () => nwTheme(Brightness.dark),
  'no theme': () => null,
};

void main() {
  group('选站点', () {
    testWidgets('页头：小标、标题、设置、加号；一个站点一张卡，卡上是名字、地址和账号，没有编出来的状态', (t) async {
      await t.pumpWidget(MaterialApp(home: SiteListPage(store: MemorySiteStore([_site(), _site('别院')]))));
      await t.pumpAndSettle();
      expect(find.text('NEXUS WARDEN'), findsOneWidget);
      expect(find.text('选择站点'), findsOneWidget);
      expect(find.byKey(const Key('open-settings')), findsOneWidget, reason: 'W55：语言、工程师工具在设置里');
      expect(find.byType(NwSiteCard), findsNWidgets(2));
      final card = t.widget<NwSiteCard>(find.byType(NwSiteCard).first);
      expect(card.name, '庄园');
      expect(card.address, contains('https://192.168.100.200:8443'));
      expect(card.address, contains('gina'));
      expect(card.status, isNull, reason: '登录之前不知道连没连上：不显示');
      expect(card.p1Count, isNull, reason: '登录之前不知道有没有 P1：不显示');
    });

    testWidgets('页头的加号和底下的「添加站点」进的是同一页', (t) async {
      await t.pumpWidget(MaterialApp(home: SiteListPage(store: MemorySiteStore())));
      await t.pumpAndSettle();
      expect(find.descendant(of: find.byKey(const Key('site-add')), matching: find.text('添加站点')),
          findsOneWidget);
      await t.tap(find.byKey(const Key('site-add-top')));
      await t.pumpAndSettle();
      expect(find.byType(SiteAddPage), findsOneWidget);
      await t.pageBack();
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('site-add')));
      await t.pumpAndSettle();
      expect(find.byType(SiteAddPage), findsOneWidget);
    });

    testWidgets('「进入站点」和点整张卡一样，开登录页', (t) async {
      await t.pumpWidget(MaterialApp(home: SiteListPage(store: MemorySiteStore([_site()]))));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('site-enter-庄园')));
      await t.pumpAndSettle();
      expect(find.byType(SiteLoginPage), findsOneWidget);
    });

    testWidgets('删除在卡片的菜单里：只删点的那一个，没点「删掉」不删', (t) async {
      final store = MemorySiteStore([_site(), _site('别院')]);
      await t.pumpWidget(MaterialApp(home: SiteListPage(store: store)));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('site-remove-别院')), findsNothing, reason: '收在菜单里，平时看不见');
      await t.tap(find.byKey(const Key('site-menu-别院')));
      await t.pumpAndSettle();
      expect(store.sites.length, 2, reason: '只是打开菜单');
      await t.tap(find.byKey(const Key('site-remove-别院')));
      await t.pumpAndSettle();
      expect(store.sites.map((s) => s.name), ['庄园']);
      expect(find.text('别院'), findsNothing);
      expect(find.byType(SiteLoginPage), findsNothing, reason: '点菜单不算点卡片');
    });

    testWidgets('站点文件坏了：页头的加号也没有', (t) async {
      await t.pumpWidget(MaterialApp(home: SiteListPage(store: _BrokenStore())));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('site-load-error')), findsOneWidget);
      expect(find.byKey(const Key('site-add-top')), findsNothing);
      expect(find.byKey(const Key('site-add')), findsNothing);
    });
  });

  group('登录页', () {
    testWidgets('是一页，不是弹窗：品牌、站点名和地址、口令框；没有数字键盘、指纹、重置', (t) async {
      await _openLogin(t, () => FakeApi('guard'));
      expect(find.byType(SiteLoginPage), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing);
      expect(find.text('Nexus Warden'), findsOneWidget);
      expect(find.text('庄园'), findsOneWidget);
      expect(find.text('https://192.168.100.200:8443'), findsOneWidget);
      final field = t.widget<TextField>(find.byKey(const Key('site-password')));
      expect(field.controller, isNotNull);
      expect(field.obscureText, isTrue);
      expect(field.autofocus, isTrue);
      expect(find.byIcon(Icons.fingerprint_rounded), findsNothing);
      expect(find.byIcon(Icons.backspace_rounded), findsNothing);
      expect(find.textContaining('Reset'), findsNothing);
    });

    testWidgets('口令不对：原因摆在这一页上、人还在这一页；改对了再登就进去', (t) async {
      final api = _PwApi();
      await _openLogin(t, () => api);
      await t.enterText(find.byKey(const Key('site-password')), 'wrong');
      await t.tap(find.text('登录'));
      await t.pumpAndSettle();
      expect(find.byType(SiteLoginPage), findsOneWidget, reason: '没登上：留在登录页');
      expect(find.byType(NwShell), findsNothing);
      expect(find.text('登录失败：账号或口令不对（401）'), findsOneWidget);
      expect(api.closed, 1, reason: '没登上的那个连接关掉');
      await t.enterText(find.byKey(const Key('site-password')), 'right');
      await t.testTextInput.receiveAction(TextInputAction.done);
      await t.pumpAndSettle();
      expect(api.tried, ['gina/wrong', 'gina/right']);
      expect(find.byType(NwShell), findsOneWidget);
      expect(find.byType(SiteLoginPage), findsNothing);
    });

    testWidgets('「换站点」回列表，没去登', (t) async {
      final api = _PwApi();
      await _openLogin(t, () => api);
      await t.tap(find.byKey(const Key('login-switch')));
      await t.pumpAndSettle();
      expect(find.byType(SiteLoginPage), findsNothing);
      expect(find.byType(NwSiteCard), findsOneWidget);
      expect(api.tried, isEmpty);
      expect(find.byType(SnackBar), findsNothing);
    });

    testWidgets('正在登的时候再点、再回车不会登第二次，也退不出去', (t) async {
      final api = _SlowApi();
      await _openLogin(t, () => api);
      await t.enterText(find.byKey(const Key('site-password')), 'pw');
      await t.tap(find.byKey(const Key('site-login')));
      await t.pump();
      expect(t.widget<FilledButton>(find.byKey(const Key('site-login'))).onPressed, isNull);
      expect(t.widget<TextButton>(find.byKey(const Key('login-switch'))).onPressed, isNull);
      await t.testTextInput.receiveAction(TextInputAction.done);
      await t.pump();
      expect(api.logins, 1);
      api.gate.complete();
      await t.pumpAndSettle();
      expect(find.byType(NwShell), findsOneWidget);
    });

    for (final th in _themes.entries) {
      for (final lang in AppLang.values) {
        for (final size in _sizes) {
          testWidgets('不溢出 [${th.key} ${lang.name} ${size.width.toInt()}×${size.height.toInt()}]，报了错也不溢出',
              (t) async {
            appLang.value = lang;
            addTearDown(() => appLang.value = AppLang.zh);
            t.view.physicalSize = size * 2;
            t.view.devicePixelRatio = 2.0;
            addTearDown(t.view.reset);
            final site = SiteEntry(
                name: 'East Gate Estate North Perimeter Station',
                url: 'https://[fd00:1234:5678:9abc::200]:8443',
                fingerprint: 'ab' * 32,
                username: 'night.guard');
            await t.pumpWidget(MaterialApp(
                theme: th.value(),
                home: SiteLoginPage(
                    site: site,
                    signIn: (_, _) async =>
                        throw const SiteError(0, '站点证书指纹对不上：实际是 cd…，存的是 ab…，删了重加'))));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
            await t.enterText(find.byKey(const Key('site-password')), 'pw');
            await t.testTextInput.receiveAction(TextInputAction.done);
            await t.pumpAndSettle();
            expect(find.byKey(const Key('site-login-error')), findsOneWidget);
            expect(t.takeException(), isNull);
            await t.pumpWidget(Container());
          });
        }
      }
    }

    testWidgets('横屏键盘弹起来：整页能滚，「登录」滚得到', (t) async {
      t.view.physicalSize = const Size(1600, 720);
      t.view.devicePixelRatio = 2.0;
      addTearDown(t.view.reset);
      await _openLogin(t, () => _PwApi());
      t.view.viewInsets = const FakeViewPadding(bottom: 400);
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('site-password')), 'wrong');
      await t.testTextInput.receiveAction(TextInputAction.done);
      await t.pumpAndSettle();
      expect(t.takeException(), isNull);
      expect(find.byKey(const Key('site-login-error'), skipOffstage: false), findsOneWidget);
      expect(
          find.ancestor(
              of: find.byKey(const Key('site-login'), skipOffstage: false),
              matching: find.byType(SingleChildScrollView)),
          findsOneWidget);
    });
  });

  group('加站点', () {
    for (final th in _themes.entries) {
      for (final lang in AppLang.values) {
        for (final size in _sizes) {
          testWidgets('不溢出 [${th.key} ${lang.name} ${size.width.toInt()}×${size.height.toInt()}]，带核对提示',
              (t) async {
            appLang.value = lang;
            addTearDown(() => appLang.value = AppLang.zh);
            t.view.physicalSize = size * 2;
            t.view.devicePixelRatio = 2.0;
            addTearDown(t.view.reset);
            await t.pumpWidget(MaterialApp(
                theme: th.value(), home: SiteAddPage(scanner: (_) async => _code)));
            await t.pumpAndSettle();
            await t.tap(find.byKey(const Key('site-scan')));
            await t.pumpAndSettle();
            expect(find.byKey(const Key('site-check-note')), findsOneWidget);
            expect(t.takeException(), isNull);
            await t.tap(find.byKey(const Key('site-paste')));
            await t.pumpAndSettle();
            expect(find.byKey(const Key('pair-code')), findsOneWidget);
            expect(t.takeException(), isNull);
            await t.pumpWidget(Container());
          });
        }
      }
    }

    testWidgets('四栏还是带 controller 的 TextField；按钮的 Key 在标准按钮上', (t) async {
      await t.pumpWidget(MaterialApp(home: SiteAddPage(scanner: (_) async => null)));
      await t.pumpAndSettle();
      for (final k in ['site-name', 'site-url', 'site-fp']) {
        expect(t.widget<TextField>(find.byKey(Key(k))).controller, isNotNull, reason: k);
      }
      expect(t.widget<TextField>(find.byKey(const Key('site-url'))).controller!.text, 'https://');
      expect(t.widget(find.byKey(const Key('site-scan'))), isA<FilledButton>());
      expect(t.widget(find.byKey(const Key('site-paste'))), isA<OutlinedButton>());
      expect(t.widget(find.byKey(const Key('site-save'))), isA<FilledButton>());
      // 桌面版的小窗口（800×600）上不滚就点得到「保存」：老测试是直接点的
      expect(t.getRect(find.byKey(const Key('site-save'))).bottom, lessThanOrEqualTo(600));
    });
  });
}

// 站点的 d1max_site.pairing.encode 真出的码（见 pairing_test.dart）。
const _code = 'D1MAXSITE1.eyJmIjoiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYiIsIm4iOiLluoTlm63kuIDlj7ciLCJ1IjoiaHR0cHM6Ly8xOTIuMTY4LjEuNTo4NDQzIn0';

class _BrokenStore implements SiteStore {
  @override
  Future<List<SiteEntry>> load() async => throw const FormatException('站点文件格式错误（顶层应为数组）');
  @override
  Future<void> save(List<SiteEntry> s) async => throw StateError('不许写');
}
