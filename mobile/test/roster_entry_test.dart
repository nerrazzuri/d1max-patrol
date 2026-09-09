/// 从名册进得去那两屏（Ruling 83 的销账点）。
///
/// **在这个任务之前，`roster_page.dart` 里一处 `Navigator.push` 都没有**，
/// 而 Task 11/12 做的遥控屏至今只在测试里被直接 `pumpWidget` 挂起来 ——
/// 全 app 没有一条路走得到它。一屏没有入口，等于它在真机上从来没被打开过。
///
/// **起的是真的 `HttpServer`（`support/fake_dog.dart`），不是 mock。** 这条
/// 路上最容易坏的一段是解锁：读 PIN、报名字、质询-应答换 token。把 HTTP 那头
/// mock 掉，正好把这一段一起 mock 掉。
///
/// **`Robot.host/port` 在这里指向假狗**：`Robot` 的默认值是真狗的
/// `192.168.168.100:8095`，照默认跑的话这些测试会去敲一台不在的机器，然后靠
/// 超时红掉。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符。
library;

import 'dart:io';

import 'package:d1max_patrol/model/registry.dart';
import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/store/pin_vault.dart';
import 'package:d1max_patrol/store/registry_store.dart';
import 'package:d1max_patrol/ui/roster_page.dart';
import 'package:d1max_patrol/ui/storage_page.dart';
import 'package:d1max_patrol/ui/teleop_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_dog.dart';
import 'support/pump.dart';

const Duration _step = Duration(milliseconds: 20);

/// 一个只管数 `close()` 的 `HttpClient` 外壳。
///
/// **「连接收没收」这件事只有在客户端那一侧数得准。** 从假狗那侧数连接数是
/// 不行的：`HttpClient` 池子里的空闲连接过一阵子自己也会散，于是「忘了
/// close」和「close 了」在服务器那侧最终长得一模一样 —— 那条断言看着很硬，
/// 实测**拦不住删掉 `client.close()`**（本轮亲手试过，全绿）。
///
/// 只转发 `PatrolClient` 真正用到的那几样（`connectionTimeout` / `openUrl` /
/// `close`）。别的走 `noSuchMethod`：真有人用到了就当场抛，**不会悄悄给一个
/// 假值**，那样测试会绿在一件没发生过的事上。
class CountingHttpClient implements HttpClient {
  CountingHttpClient(this._inner);

  final HttpClient _inner;

  /// 被 `close()` 了几次。
  int closes = 0;

  @override
  Duration? get connectionTimeout => _inner.connectionTimeout;

  @override
  set connectionTimeout(Duration? value) => _inner.connectionTimeout = value;

  @override
  Future<HttpClientRequest> openUrl(String method, Uri url) =>
      _inner.openUrl(method, url);

  @override
  void close({bool force = false}) {
    closes++;
    _inner.close(force: force);
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

/// 让这一条测试里造出来的每一个 `HttpClient` 都套上 [CountingHttpClient]。
///
/// `roster_page` 的 `_open()` 自己 `new` 一个 `PatrolClient`，测试拿不到那个
/// 对象 —— 从 `HttpOverrides` 那一层套进去是唯一不改产品代码的seam。
class CountingHttpOverrides extends HttpOverrides {
  final List<CountingHttpClient> made = <CountingHttpClient>[];

  @override
  HttpClient createHttpClient(SecurityContext? context) {
    final CountingHttpClient c =
        CountingHttpClient(super.createHttpClient(context));
    made.add(c);
    return c;
  }
}

/// 一次挂好的名册：一只假狗、一本存着 PIN 的名册。
class Rig {
  Rig(this.dog, this.store, this.vault);

  final FakeDog dog;
  final RegistryStore store;
  final PinVault vault;

  /// 解锁那一步真的发出去了几次。
  int get unlocks =>
      dog.received.where((FakeCall r) => r.path == '/api/auth').length;

  /// `POST /api/auth` 的 body 里报上去的名字。
  String get reportedOperator => (dog.received
          .firstWhere((FakeCall r) => r.path == '/api/auth')
          .body as Map<String, dynamic>)['operator'] as String;
}

Future<Rig> mount(WidgetTester t, {String pin = '864209'}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
  dog.replies['/api/auth'] = <String, dynamic>{
    'token': 'tok-abc',
    'operator': '张三',
    'operator_verified': false,
    'readonly': false,
  };
  // 两屏各自进去之后会问的那几条。备着，免得它们各自红在一件跟入口无关的事上。
  dog.replies['/api/storage'] = <String, dynamic>{
    'used_bytes': 420000000000,
    'total_bytes': 1000000000000,
    'used_ratio': 0.42,
    'runs_total': 0,
    'runs_bytes': 0,
    'baselines_bytes': 0,
    'exports_bytes': 0,
    'notice_written': true,
    'notice_detail': '',
    'forecast': <String, dynamic>{
      'generated_at': '20260904T000000Z',
      'used_ratio': 0.42,
      'bytes_at_risk': 0,
      'runs': <dynamic>[],
      'detail': '盘 42%,7 天内没有归档到期。',
    },
    'backup': <String, dynamic>{'level': 'neutral', 'detail': '未配备份盘。'},
  };
  dog.replies['/api/video/health'] = <String, dynamic>{
    'cameras': <String, dynamic>{},
  };
  dog.replies['/api/control'] = <String, dynamic>{'notice': ''};

  final Robot robot = Robot(
    sn: 'C40221',
    name: '三号',
    host: '127.0.0.1',
    port: Uri.parse(dog.baseUrl).port,
  );
  final RegistryStore store = MemoryRegistryStore();
  await store.save(RobotRegistry()..add(robot));
  final PinVault vault = MemoryPinVault();
  if (pin.isNotEmpty) await vault.write('C40221', pin);

  await t.pumpWidget(
      MaterialApp(home: RosterPage(store: store, vault: vault)));
  await pumpUntil(t, () => find.text('三号（C40221）').evaluate().isNotEmpty,
      '名册画出来', step: _step);
  return Rig(dog, store, vault);
}

Future<void> unmount(WidgetTester t, Rig rig) async {
  await teardown(t, null);
  await t.pump();
  await t.runAsync(rig.dog.stop);
  expect(rig.dog.errors, isEmpty, reason: '假狗自己抛了：查的是假狗，不是被测代码');
}

/// 点入口 → 填名字 → 连上。**两个入口共用这一段**，因为它们在实现里也共用。
Future<void> enter(WidgetTester t, Key entry, {String who = '张三'}) async {
  await t.tap(find.byKey(entry));
  await pumpUntil(
      t,
      () => find.byKey(RosterPage.operatorFieldKey).evaluate().isNotEmpty,
      '问「你是谁」那个框弹出来',
      step: _step);
  await t.enterText(find.byKey(RosterPage.operatorFieldKey), who);
  await t.tap(find.text('连上'));
}

/// 从子屏退回名册。**退回来那一下 `_open` 才会把 `PatrolClient` 收掉** ——
/// 不收的话它自己那个 15 秒空闲计时器会把测试打红（`!timersPending`）。
Future<void> goBack(WidgetTester t, Type screen) async {
  t.state<NavigatorState>(find.byType(Navigator).first).pop();
  await pumpUntil(t, () => find.byType(screen).evaluate().isEmpty, '退回名册',
      step: _step);
}

void main() {
  // 摘掉 `flutter_test` 那个「所有请求都回 400」的 HttpOverrides：这个文件
  // 要证的正是「解锁真的发出去了、然后真的进了那一屏」。
  useRealHttp();

  testWidgets('从名册点进盘况屏,真的到了那一屏', (WidgetTester t) async {
    final Rig rig = await mount(t);
    expect(find.byType(StoragePage), findsNothing, reason: '一开始不在那一屏上');

    await enter(t, RosterPage.storageKeyFor('C40221'));
    await pumpUntil(t, () => find.byType(StoragePage).evaluate().isNotEmpty,
        '进到盘况屏', step: _step);
    // 只是 push 了一个 widget 还不算数 —— 那一屏得真的拿着一个解过锁的
    // 客户端，问得到狗。
    await pumpUntil(
        t,
        () => find.byKey(StoragePage.capacityKey).evaluate().isNotEmpty,
        '盘况屏真的问到了狗',
        step: _step);
    expect(rig.unlocks, 1);
    expect(rig.reportedOperator, '张三',
        reason: '报上去的是人填的名字，不是狗的名字');

    await goBack(t, StoragePage);
    await unmount(t, rig);
  });

  testWidgets('从名册点进遥控屏,真的到了那一屏', (WidgetTester t) async {
    // Task 11/12 做完之后，这一屏在全 app 里没有任何入口 —— 只在测试里被
    // 直接挂起来过。
    final Rig rig = await mount(t);
    expect(find.byType(TeleopPage), findsNothing, reason: '一开始不在那一屏上');

    await enter(t, RosterPage.teleopKeyFor('C40221'));
    await pumpUntil(t, () => find.byType(TeleopPage).evaluate().isNotEmpty,
        '进到遥控屏', step: _step);
    expect(rig.unlocks, 1);
    // **限定在遥控屏里面找。** 名册那一屏还留在路由栈底下，它上面也有一个
    // 「三号（C40221）」—— 不限定的话找到两个，而且找到的那个可能是名册的。
    expect(
        find.descendant(
            of: find.byType(TeleopPage), matching: find.text('三号（C40221）')),
        findsOneWidget,
        reason: '进去的是名册上点的那一只');

    await goBack(t, TeleopPage);
    await unmount(t, rig);
  });

  testWidgets('名字问过一次就不再问', (WidgetTester t) async {
    // 另一个局面：`_operator` 已经有值那一支。每次都问的话，人进出两屏之间
    // 要填四遍名字，然后就会开始随手填一个字 —— 那笔账在狗那头是要留痕的。
    final Rig rig = await mount(t);
    await enter(t, RosterPage.storageKeyFor('C40221'));
    await pumpUntil(t, () => find.byType(StoragePage).evaluate().isNotEmpty,
        '进到盘况屏', step: _step);
    await goBack(t, StoragePage);

    await t.tap(find.byKey(RosterPage.teleopKeyFor('C40221')));
    await pumpUntil(t, () => find.byType(TeleopPage).evaluate().isNotEmpty,
        '第二次不问名字,直接进遥控屏', step: _step);
    expect(find.byKey(RosterPage.operatorFieldKey), findsNothing);
    expect(rig.unlocks, 2, reason: '第二次是一次新的解锁，不是复用上一条连接');
    expect(rig.reportedOperator, '张三');

    await goBack(t, TeleopPage);
    await unmount(t, rig);
  });

  testWidgets('按了算了就不连狗', (WidgetTester t) async {
    final Rig rig = await mount(t);
    await t.tap(find.byKey(RosterPage.storageKeyFor('C40221')));
    await pumpUntil(
        t,
        () => find.byKey(RosterPage.operatorFieldKey).evaluate().isNotEmpty,
        '问「你是谁」那个框弹出来',
        step: _step);
    await t.tap(find.text('算了'));
    await t.pumpAndSettle();
    expect(find.byType(StoragePage), findsNothing);
    expect(rig.unlocks, 0, reason: '人放弃了，就一个字节也别往狗上发');
    await unmount(t, rig);
  });

  testWidgets('名字留空就按连上,一个字节也不往狗上发', (WidgetTester t) async {
    // `_operatorName()` 里 `if (asked.isEmpty)` 那道守卫 —— **它的前提以前
    // 从没被造出来过**：把它改成 `if (false)`（没填名字也照连），182 条全绿。
    //
    // 空名字在狗的账上是「未具名(ref)」（§6.3），事后谁也说不清那一趟是谁
    // 开的 —— 而这是要拿去应付客户的一笔账。
    final Rig rig = await mount(t);
    await t.tap(find.byKey(RosterPage.storageKeyFor('C40221')));
    await pumpUntil(
        t,
        () => find.byKey(RosterPage.operatorFieldKey).evaluate().isNotEmpty,
        '问「你是谁」那个框弹出来',
        step: _step);
    // **一个字都不填**，直接按「连上」。
    await t.tap(find.text('连上'));
    await t.pumpAndSettle();
    // **要推真时钟。** 解锁是真的网络 I/O，`pumpAndSettle` 只推假时钟推不动
    // 它 —— 只 settle 一下就断 `unlocks == 0`，断到的是「还没来得及发」，
    // 不是「不发」。守卫拆掉之后它照样绿，那正是这一卷栽过十几次的那种空绿。
    // 所以在这儿等到局面分晓：要么拦下来说了一句，要么真去连了狗。
    await pumpUntil(
        t,
        () =>
            find.textContaining('填一个名字').evaluate().isNotEmpty ||
            find.byType(StoragePage).evaluate().isNotEmpty ||
            rig.unlocks > 0,
        '要么屏上拦了一句，要么（守卫没了的话）解锁真的发出去了',
        step: _step);

    expect(rig.unlocks, 0, reason: '空名字不许拿去连狗：狗的账上要记下是谁开的');
    expect(find.byType(StoragePage), findsNothing);
    // 拦下来还得说一声 —— 不说的话，人按了「连上」什么也没发生，会以为按钮坏了。
    expect(find.textContaining('填一个名字'), findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('连不上狗时说人话,而且把那条连接收干净', (WidgetTester t) async {
    // `_open()` 里 `unlock` 抛异常那一支 —— **以前一条测试都没有**：把
    // `client.close()` 和那句 `_toast` 一起删掉（连不上时既不收连接、也不跟
    // 人说一声），182 条全绿。
    final Rig rig = await mount(t);
    // 从这里开始造的 `HttpClient` 都套上数 `close()` 的壳。`useRealHttp()` 的
    // `tearDown` 会把 `HttpOverrides.global` 装回去，不会漏给别的测试。
    final CountingHttpOverrides counting = CountingHttpOverrides();
    HttpOverrides.global = counting;
    // 狗把解锁那一步打回来。现场最常见的两种：PIN 记错了、连错了别人家的热点。
    rig.dog.statusCodes['/api/auth'] = 401;

    await enter(t, RosterPage.storageKeyFor('C40221'));
    await pumpUntil(
        t,
        () => find.textContaining(connectFailedHint).evaluate().isNotEmpty,
        '屏上说了「连不上」那一句',
        step: _step);

    expect(rig.unlocks, 1,
        reason: '解锁那一步真的发出去过 —— 否则下面查的是一条压根没建过的连接');
    expect(find.byType(StoragePage), findsNothing, reason: '没连上就不许进那一屏');
    // **异常原文不上屏。** `PatrolError` 那份原文里带着状态码，它既不告诉人
    // 下一步该干什么，又会把内部细节推到客户面前。
    expect(find.textContaining('401'), findsNothing);

    // **连接要收。** `PatrolClient` 自己造的那个 `HttpClient` 带 keep-alive
    // 和一个 15 秒空闲计时器；连不上就撒手的话，人每按一次就多挂一条。
    expect(counting.made, hasLength(1),
        reason: '这一趟真的造过一个 HttpClient —— 否则下面数的是一份空名单');
    expect(counting.made.single.closes, 1,
        reason: '连不上也要把 PatrolClient 收掉：撒手不管的话每按一次就多挂一条连接');

    await unmount(t, rig);
  });

  testWidgets('没存 PIN 的那只,点进去只说话不连狗', (WidgetTester t) async {
    // **不拿空 PIN 去撞一次。** 空 PIN 换到的是一个 401，而报错会指向
    // 「PIN 不对」，人于是去翻本子找那个其实从来没填过的号。
    final Rig rig = await mount(t, pin: '');
    await t.tap(find.byKey(RosterPage.storageKeyFor('C40221')));
    await t.pumpAndSettle();
    expect(find.textContaining(noPinHint), findsOneWidget);
    expect(find.byKey(RosterPage.operatorFieldKey), findsNothing,
        reason: '连 PIN 都没有，问名字没有意义');
    expect(find.byType(StoragePage), findsNothing);
    expect(rig.unlocks, 0);
    await unmount(t, rig);
  });
}
