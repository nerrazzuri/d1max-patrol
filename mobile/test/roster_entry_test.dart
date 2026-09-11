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
import 'package:d1max_patrol/ui/watch_page.dart';
import 'package:d1max_patrol/ui/widget/operator_chip.dart';
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

/// [canon] 是**狗收拾完之后回出来的那个名字**（`POST /api/auth` 的
/// `operator` 字段）。狗那头 `app/identity.py::clean_operator` 会把中间的
/// 连续空白压成一个、把不可打印字符换成空格、超长截断 —— 手机这头只
/// `trim()`。不给就还是「张三」，跟人填的一样，那条路走不到（必修 5）。
Future<Rig> mount(WidgetTester t,
    {String pin = '864209', String canon = '张三'}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
  dog.replies['/api/auth'] = <String, dynamic>{
    'token': 'tok-abc',
    'operator': canon,
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

/// 从子屏退回名册。**退回来那一下 `PatrolClient` 才会被收掉** ——
/// 不收的话它自己那个 15 秒空闲计时器会把测试打红（`!timersPending`）。
///
/// [rig] 给上的时候，多等一件事：**遥控屏在 `dispose()` 里补的那一拍全零**
/// （必修 2 —— 手一离开那一屏，狗必须收到一次「停」）。那一拍是真的网络
/// I/O，只有在 `runAsync` 里才推得动；屏一没了就走人的话，它那条超时定时器
/// 还挂在假时钟上，收摊时照样 `!timersPending`。**红的会是「测试没等」，
/// 不是「代码没发」** —— 所以这儿等的是狗真的收到了那一拍。
Future<void> goBack(WidgetTester t, Type screen, [Rig? rig]) async {
  final int before = rig == null
      ? 0
      : rig.dog.received.where((FakeCall c) => c.path == '/api/teleop').length;
  t.state<NavigatorState>(find.byType(Navigator).first).pop();
  await pumpUntil(t, () => find.byType(screen).evaluate().isEmpty, '退回名册',
      step: _step);
  if (rig == null) return;
  await pumpUntil(
      t,
      () =>
          rig.dog.received.where((FakeCall c) => c.path == '/api/teleop').length >
          before,
      '退屏那一拍全零真的到了狗那儿',
      step: _step);
  // 请求到了不等于这头收完了回包 —— 再推几轮真时钟，让那条 future 落地，
  // 它身上的超时定时器才会被取消。
  for (int i = 0; i < 5; i++) {
    await t.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 10)));
    await t.pump(_step);
  }
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

    await goBack(t, TeleopPage, rig);
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
    // **这一条以前断的是 `2`**（「第二次是一次新的解锁」）——那正是终审报告
    // 建议 11 指的那件事：每进一次屏就烧一个会话名额。判断反过来了，断言
    // 跟着反过来，理由见下面那条「同一条狗只开一个会话」。
    expect(rig.unlocks, 1, reason: '第二次进屏复用上一条连接，不再烧一个会话名额');
    expect(rig.reportedOperator, '张三');

    await goBack(t, TeleopPage, rig);
    await unmount(t, rig);
  });

  testWidgets('第一次连狗:屏上和盘上留的都是狗收拾过的那个名字', (WidgetTester t) async {
    // **必修 5。** 两侧的清洗规则本来就不一样：手机这头只 `trim()`，狗那头
    // （`app/identity.py::clean_operator`）还会把中间的连续空白压成一个、把
    // 不可打印字符换成空格、超长截断。
    //
    // 不认狗那份的话：手机盘上、遥控屏那枚常显 chip、值守屏 `signedAs()`
    // 那一行、记名确认送出去的 `who` 全是「老  王」，而狗的审计环里从 unlock
    // 那一刻起记的是「老 王」。事后交接班查「这条 P1 是谁确认的、那趟遥控是
    // 谁开的」，两边字符串对不上 —— 而这正是 §6.3 那套「记下但不核实」唯一
    // 还剩下的对账手段。
    //
    // **盘上那一份尤其要紧**：每只狗一辈子只问这一次，落进去的要是原字符串，
    // 它会一直用下去，往后每一次 unlock 报上去的都是它。
    final Rig rig = await mount(t, canon: '老 王');
    await enter(t, RosterPage.teleopKeyFor('C40221'), who: '老  王');
    await pumpUntil(t, () => find.byType(TeleopPage).evaluate().isNotEmpty,
        '进到遥控屏', step: _step);

    expect(rig.reportedOperator, '老  王', reason: '发上去的是人输的原样');
    await pumpUntil(
        t,
        () => find
            .descendant(
                of: find.byKey(OperatorChip.chipKey), matching: find.text('老 王'))
            .evaluate()
            .isNotEmpty,
        '屏上那枚常显 chip 挂的是狗收拾过的那一份',
        step: _step);
    expect(find.text('老  王'), findsNothing,
        reason: '屏上不许留着一份跟狗的审计环对不上的名字');
    expect(await rig.store.readOperator(dog: 'C40221'), '老 王',
        reason: '盘上留的也得是账上那一份 —— 每只狗一辈子只问这一次，这份会一直用下去');

    await goBack(t, TeleopPage, rig);
    await unmount(t, rig);
  });

  testWidgets('同一条狗只开一个会话:进出三屏也只解锁一次', (WidgetTester t) async {
    // **建议 11。** `client.close()` 只关本地那一头的 socket，狗那头那个
    // token 一个都不会少 —— 热点通道上闲置期是 30 分钟
    // （`auth.AP_TOKEN_IDLE_S`），而 §3.6 的非本机会话名额是个位数。
    //
    // **同名换座救不了这件事**：`TokenStore.issue_with_quota` 只在
    // `len(pool) >= cap` 那一刻才去找同名的老会话挤掉，名额没满之前照发
    // 不误。所以老写法下，一台手机依次点盘况、退出、点遥控、退出、点值守，
    // 三个名额全被它自己占了；第 4 个人（另一个名字）来的时候 `mine` 是空
    // 的，当场 403 —— 而现场执行单 9.5/9.6/9.7 量的正是「三个远端名额」和
    // 「第 4 个人被拒」，那三条会因此量出假象。
    final Rig rig = await mount(t);

    await enter(t, RosterPage.storageKeyFor('C40221'));
    await pumpUntil(t, () => find.byType(StoragePage).evaluate().isNotEmpty,
        '进到盘况屏', step: _step);
    await goBack(t, StoragePage);

    await t.tap(find.byKey(RosterPage.teleopKeyFor('C40221')));
    await pumpUntil(t, () => find.byType(TeleopPage).evaluate().isNotEmpty,
        '进到遥控屏', step: _step);
    await goBack(t, TeleopPage, rig);

    await t.tap(find.byKey(RosterPage.watchKeyFor('C40221')));
    await pumpUntil(t, () => find.byType(WatchPage).evaluate().isNotEmpty,
        '进到值守屏', step: _step);
    await goBack(t, WatchPage);

    // 自洽：三屏是真的一个个进过的，否则下面那条断言断的是一件没发生过的事。
    expect(rig.dog.received.any((FakeCall c) => c.path == '/api/storage'), isTrue,
        reason: '盘况屏真的问过狗');
    expect(rig.dog.received.any((FakeCall c) => c.path == '/api/teleop'), isTrue,
        reason: '遥控屏真的发过拍');
    expect(rig.unlocks, 1, reason: '进出三屏只许烧一个会话名额');
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

  testWidgets('按系统返回键离开遥控屏:狗先收到一拍全零,然后才退回名册',
      (WidgetTester t) async {
    /// **必修 2 的上半段。**
    ///
    /// 人从遥控屏退出去有两条路：按系统返回键，和这一屏被拆掉。后一条由
    /// `teleop_page_test.dart` 那条「这一屏被拆掉之前」钉着；这一条钉的是
    /// 前一条 —— 也是现场真正会走的那条。
    ///
    /// **不能拿 `NavigatorState.pop()` 去测。** `pop()` 是硬退，它绕开
    /// `PopScope`；真机上按返回键走的是 `maybePop()`。拿 `pop()` 测的话，
    /// `PopScope` 整个被跳过，测出来的是「什么都没做也绿」。
    ///
    /// **顺序是这条测试的正题**：那一拍全零要在这一屏还在的时候就到狗那儿。
    /// 先退屏再补一拍的话，连接已经被上一层收掉了，那一拍连一个字节都出不
    /// 去 —— 而屏幕上看起来一模一样。
    final Rig rig = await mount(t);
    await enter(t, RosterPage.teleopKeyFor('C40221'));
    await pumpUntil(t, () => find.byType(TeleopPage).evaluate().isNotEmpty,
        '进到遥控屏', step: _step);
    final int before =
        rig.dog.received.where((FakeCall c) => c.path == '/api/teleop').length;

    await pressBack(t);
    await pumpUntil(
        t,
        () =>
            rig.dog.received
                .where((FakeCall c) => c.path == '/api/teleop')
                .length >
            before,
        '返回键那一拍全零真的到了狗那儿',
        step: _step);
    expect(find.byType(TeleopPage), findsOneWidget,
        reason: '那一拍是在退屏之前发的 —— 退完再发的话它根本出不去');
    final Map<String, dynamic> last = rig.dog.received
        .lastWhere((FakeCall c) => c.path == '/api/teleop')
        .body as Map<String, dynamic>;
    expect(<num>[last['fwd'] as num, last['lat'] as num, last['yaw'] as num],
        everyElement(0),
        reason: '离屏那一拍必须是全零 —— 别的什么都等于让狗接着走');

    await pumpUntil(t, () => find.byType(TeleopPage).evaluate().isEmpty,
        '补完那一拍之后，才真的退回名册', step: _step);
    await unmount(t, rig);
  });
}
