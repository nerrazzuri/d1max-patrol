/// 「现在是谁在开」这枚常显的 chip：落盘、常显、一步切换（人拍的板 3，挂账 65）。
///
/// **这个文件证的是两条补偿，不是一个 widget 画得出来。** 人在 2026-09-09
/// 拍的那块板选的是「姓名落盘记住、可随时改」，接受的代价写在挂账 65 里：
/// 甲忘了切换，乙的操作签在甲名下。正因为接受了这个代价，两条补偿是硬的：
///
/// 1. **「当前是谁」常显** —— 遥控屏和值守屏上，**不做任何动作**就看得见。
/// 2. **切换一步** —— 点一下 chip 就能改。
///
/// **「常显」这条最容易写成恒真断言。** 只断「找得到 `operator-chip` 这个
/// Key」的话，把 chip 挪进一个二级页、只要那个二级页在测试里也被建出来，
/// 断言照样绿 —— 而屏上已经看不见它了。所以下面每一条「常显」都是：先
/// `pumpUntil` 等**这一屏自己的内容**出来（不是等 chip），再**一次点击都不
/// 做**地断 chip 在树上、可点得到（`hitTestable`）、而且那个名字自己就在屏上。
///
/// **措辞是硬约束（§6.3）：** 狗记下名字但从不核实，所以屏上任何地方都不许
/// 出现「已登录 / 已认证 / 身份已验证 / 当前用户」。
///
/// **起的是真的 `HttpServer`（`support/fake_dog.dart`），不是 mock**：换人
/// 这件事要真的到得了狗（`PUT /api/operator`），那条留痕才建得起来。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/model/registry.dart';
import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:d1max_patrol/net/wire.dart';
import 'package:d1max_patrol/store/registry_store.dart';
import 'package:d1max_patrol/ui/control_panel.dart';
import 'package:d1max_patrol/ui/teleop_page.dart';
import 'package:d1max_patrol/ui/widget/operator_chip.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_dog.dart';
import 'support/pump.dart';

/// 一次挂好的现场：一只假狗、一个解过锁的客户端、可能还有一条健康流。
class Rig {
  Rig(this.dog, this.client, this.health);

  final FakeDog dog;
  final PatrolClient client;
  final StreamController<VideoHealth>? health;

  /// 往狗上报过的那几次换人（`PUT /api/operator`）的 body。
  ///
  /// **解的是 `raw`**（跟 `FakeCall` 那条 docstring 一个理由）：要证「名字真
  /// 的带上去了」，得在没解析过的那一份上证。
  List<Map<String, dynamic>> get puts => <Map<String, dynamic>>[
        for (final FakeCall r in dog.received)
          if (r.method == 'PUT' && r.path == '/api/operator')
            jsonDecode(r.raw.isEmpty ? '{}' : r.raw) as Map<String, dynamic>,
      ];
}

/// 控制权在自己手上（照抄 `teleop_page_test.dart`：同一份 wire 契约夹具）。
Map<String, dynamic> heldByMe() => <String, dynamic>{
      ...jsonDecode(File('test/fixtures/lease_state.json').readAsStringSync())
          as Map<String, dynamic>,
      'mine': true,
    };

Future<FakeDog> startDog(WidgetTester t, String who, {String? echo}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  if (echo != null) {
    // **狗收拾完之后回出来的那个名字。**
    //
    // 不给的话假狗对 `PUT /api/operator` 回一个空的 `{}`，`setOperator` 的
    // 返回值恒为空串 —— 于是「以狗那份为准」那条路在测试里根本走不到，而它
    // 正是两侧清洗规则对不上时唯一的补救（手机只 `trim()`，狗还会压中间的
    // 连续空白、换掉不可打印字符、超长截断）。
    dog.replies['/api/operator'] = <String, dynamic>{
      'name': echo,
      'operator_verified': false,
      'at_ms': 1757400000000,
    };
  }
  dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
  dog.replies['/api/auth'] = <String, dynamic>{
    'token': 'tok-abc',
    'operator': who,
    'operator_verified': false,
    'readonly': false,
  };
  dog.replies['/api/control'] = heldByMe();
  dog.replies['/api/control/heartbeat'] = heldByMe();
  dog.replies['/api/alerts'] = <String, dynamic>{
    'alerts': <Map<String, dynamic>>[],
  };
  return dog;
}

/// 把遥控屏挂上去。**注意这里一次点击都不做** —— 「常显」就是这个意思。
Future<Rig> mountTeleop(WidgetTester t,
    {String who = '老王', ValueChanged<String>? onChanged, String? echo}) async {
  final FakeDog dog = await startDog(t, who, echo: echo);
  final PatrolClient c = PatrolClient(dog.baseUrl);
  await t.runAsync(() => c.unlock('864209', operator: who));
  dog.received.clear();
  final StreamController<VideoHealth> health =
      StreamController<VideoHealth>.broadcast();
  await t.pumpWidget(MaterialApp(
      home: TeleopPage(
          client: c,
          robot: const Robot(sn: 'C40221', name: '三号'),
          operatorName: who,
          onOperatorChanged: onChanged,
          health: health.stream)));
  // **等的是这一屏自己的内容（控制权面板），不是等 chip。**
  // 等 chip 的话，「常显」那条就退化成「早晚会冒出来」——而挪进二级页的
  // 版本只要那个二级页也被建出来，照样早晚会冒出来。
  await pumpUntil(
      t,
      () =>
          find.byKey(ControlPanel.releaseKey).evaluate().isNotEmpty ||
          find.byKey(ControlPanel.acquireKey).evaluate().isNotEmpty ||
          find.byKey(ControlPanel.takeoverKey).evaluate().isNotEmpty,
      '遥控屏的控制权面板拿到第一份租约');
  return Rig(dog, c, health);
}

/// 收摊。**客户端也要关**：一条没收的 `HttpClient` 会留着自己那个 15 秒空闲
/// 计时器，`flutter_test` 的 `!timersPending` 会当场把测试打红。
Future<void> unmount(WidgetTester t, Rig rig) async {
  await teardown(t, rig.health);
  rig.client.close();
  await t.pump();
  await t.runAsync(rig.dog.stop);
  expect(rig.dog.errors, isEmpty, reason: '假狗自己抛了：查的是假狗，不是被测代码');
}

/// 整屏上所有 `Text` 的文字拼成一串。给「这几个词一个都不许出现」那条用。
String screenText(WidgetTester t) => t
    .widgetList<Text>(find.byType(Text))
    .map((Text w) => w.data ?? '')
    .join('\n');

/// 弹出来那个框里所有 `Text` 的文字。**只收框里的** —— 要证的是这枚 chip
/// 自己说的话，把整屏拼进来的话，底下那一屏说过的「不核实」会替它作答。
String dialogText(WidgetTester t) => t
    .widgetList<Text>(
        find.descendant(of: find.byType(AlertDialog), matching: find.byType(Text)))
    .map((Text w) => w.data ?? '')
    .join('\n');

/// §6.3 的措辞禁令：狗记下名字但**从不核实**，屏上任何地方都不许暗示它被验过。
///
/// **这是产品级硬约束，而这道闸是它唯一的守卫。** 说成登录的那天，一次冒名
/// 操作在事后的记录里跟本人操作长得一模一样。
void expectNoLoginWords(String words, String where) {
  // 自洽：真的收到字了。收到空串的话下面四条全是恒真的。
  expect(words, isNot(isEmpty), reason: '$where 上一个字都没读到，下面四条会全是恒真的');
  for (final String banned in <String>[
    '已登录',
    '已认证',
    '身份已验证',
    '当前用户',
  ]) {
    expect(words, isNot(contains(banned)), reason: '§6.3：$where 上这不是登录');
  }
}

/// 「不点开任何东西就看得见谁在开」这件事，两屏一个写法。
///
/// 三条一条都不能少：Key 在树上、**点得到**（`hitTestable` —— 藏在折叠起来
/// 的抽屉里、被别的东西整个盖住的都算看不见）、而且**那个名字本身**就在屏上
/// （chip 里名字是自己一个 `Text`，不是拼进一句长话里）。
void expectAlwaysVisible(WidgetTester t, String who) {
  expect(find.byKey(OperatorChip.chipKey), findsOneWidget,
      reason: '「现在是谁在开」不许要点开才看得见（人拍的板 3 的第一条补偿）');
  expect(find.byKey(OperatorChip.chipKey).hitTestable(), findsOneWidget,
      reason: 'chip 在树上还不够，得真的点得到');
  expect(find.text(who), findsOneWidget, reason: '屏上要看得见的是那个名字本身');
}

void main() {
  // 摘掉 `flutter_test` 那个「所有请求都回 400」的 HttpOverrides：这个文件
  // 要证的正是「换人这件事真的发到狗上了」。
  useRealHttp();

  // ------------------------------------------------ 补偿一：常显

  testWidgets('当前是谁在遥控屏上是常显的,不用点开任何东西', (WidgetTester t) async {
    final Rig rig = await mountTeleop(t, who: '老王');
    // **自洽**：这一屏真的画出来了（下面那条才不是空绿）。
    expect(find.text('三号（C40221）'), findsOneWidget, reason: '遥控屏真的画出来了');
    expectAlwaysVisible(t, '老王');
    await unmount(t, rig);
  });

  testWidgets('遥控屏上不许把这个自报的名字说成登录或者认证', (WidgetTester t) async {
    final Rig rig = await mountTeleop(t, who: '老王');
    expectNoLoginWords(screenText(t), '遥控屏');
    await unmount(t, rig);
  });

  testWidgets('点开换人的那个框,里面也不许说登录,而且要把「不核实」说出来',
      (WidgetTester t) async {
    // **这一条是「不核实」那句话的唯一承重点。**
    //
    // 换人的框是人真正会停下来读一句的地方 —— §6.3 那句实话必须在这儿说出来，
    // 而且这儿也一样不许说成登录。
    final Rig rig = await mountTeleop(t, who: '老王');
    await t.tap(find.byKey(OperatorChip.chipKey));
    await pumpUntil(
        t,
        () => find.byKey(OperatorChip.editKey).evaluate().isNotEmpty,
        '改名字的框出来了');
    final String words = dialogText(t);
    expectNoLoginWords(words, '换人的那个框');
    expect(words, contains('不核实'),
        reason: '§6.3：狗记下这个名字但从不核实 —— 这句实话要说在人读得到的地方');
    await t.tap(find.text('算了'));
    await t.pumpAndSettle();
    await unmount(t, rig);
  });

  // ------------------------------------------------ 补偿二：一步切换

  testWidgets('切换是一步:点 chip 就能改', (WidgetTester t) async {
    // 多一道「设置 → 账户 → 编辑 → 保存」，接班的人就不会去改了 —— 而不改
    // 正是挂账 65 那笔代价发生的方式。
    final Rig rig = await mountTeleop(t, who: '老王');
    await t.tap(find.byKey(OperatorChip.chipKey));
    await pumpUntil(
        t,
        () => find.byKey(OperatorChip.editKey).evaluate().isNotEmpty,
        '点一下 chip 就出来了改名字的框');
    expect(find.byKey(OperatorChip.editKey), findsOneWidget);
    await t.tap(find.text('算了'));
    await t.pumpAndSettle();
    await unmount(t, rig);
  });

  testWidgets('换完人,屏上挂的就是新名字,而且报给了名册那一屏', (WidgetTester t) async {
    // 报上去那一下不是装饰：名字的家在名册那一屏（按狗落盘）。不往上报的话，
    // 换完的名字只活在这一屏的 `setState` 里，退出去再进来又变回上一个人。
    final List<String> told = <String>[];
    final Rig rig = await mountTeleop(t, who: '老王', onChanged: told.add);
    await t.tap(find.byKey(OperatorChip.chipKey));
    await pumpUntil(
        t,
        () => find.byKey(OperatorChip.editKey).evaluate().isNotEmpty,
        '改名字的框出来了');
    await t.enterText(find.byKey(OperatorChip.editKey), '小李');
    await t.tap(find.byKey(OperatorChip.saveKey));
    // **等的是 chip 上那个名字换掉了、而且那个框已经关上了。**
    // 光等 `find.text('小李')` 会等到还开着的输入框里那份 —— 那时候 chip 上
    // 挂的还是老王，下一条断言就成了一场跟被测的事无关的赛跑。
    await pumpUntil(
        t,
        () =>
            find
                .descendant(
                    of: find.byKey(OperatorChip.chipKey),
                    matching: find.text('小李'))
                .evaluate()
                .isNotEmpty &&
            find.byKey(OperatorChip.editKey).evaluate().isEmpty,
        '框关上了,chip 上挂的名字换成了小李');
    expect(find.text('老王'), findsNothing, reason: '上一个人的名字不许还挂在屏上');
    expect(told, <String>['小李'], reason: '换人这件事要报给名册那一屏去落盘');
    await unmount(t, rig);
  });

  testWidgets('换人这件事告诉了狗,那条留痕才建得起来', (WidgetTester t) async {
    // 狗那侧不核实这个名字，它记的只是「从这一刻起，账记在这个名字下」——
    // 挂账 65 那笔代价事后翻得出来的唯一凭据，就是那条带时刻的记录。
    final Rig rig = await mountTeleop(t, who: '老王');
    await t.tap(find.byKey(OperatorChip.chipKey));
    await pumpUntil(
        t,
        () => find.byKey(OperatorChip.editKey).evaluate().isNotEmpty,
        '改名字的框出来了');
    await t.enterText(find.byKey(OperatorChip.editKey), '小李');
    await t.tap(find.byKey(OperatorChip.saveKey));
    await pumpUntil(t, () => rig.puts.isNotEmpty, '换人这件事发到狗上了',
        step: const Duration(milliseconds: 20));
    expect(rig.puts.single['name'], '小李');
    await unmount(t, rig);
  });

  testWidgets('狗收拾过的那个名字才是账上那个,屏上跟着它改', (WidgetTester t) async {
    // **两侧的清洗规则本来就不一样。** 这头只 `trim()`；狗那头
    // （`app/identity.py::clean_operator`）还会把中间的连续空白压成一个、
    // 把不可打印字符换成空格、按 `MAX_OPERATOR_LEN` 截断。
    //
    // 不认狗那份的话：屏上和盘上留着「老  王」，狗的审计环里留着「老 王」，
    // 事后拿手机上那份跟审计对账，字符串比较不相等，同一个人对不上。
    //
    // 这一条同时把「换人这件事告诉了狗」那条名不副实的地方补上：那条只看
    // `dog.received`（发出去了就绿，狗回 404 还是 500 都不看），这一条断的是
    // **回来的那份**，往返的形状被握住了。
    final Rig rig = await mountTeleop(t, who: '老王', echo: '老 王');
    await t.tap(find.byKey(OperatorChip.chipKey));
    await pumpUntil(
        t,
        () => find.byKey(OperatorChip.editKey).evaluate().isNotEmpty,
        '改名字的框出来了');
    await t.enterText(find.byKey(OperatorChip.editKey), '老  王');
    await t.tap(find.byKey(OperatorChip.saveKey));
    await pumpUntil(
        t,
        () => find
            .descendant(
                of: find.byKey(OperatorChip.chipKey), matching: find.text('老 王'))
            .evaluate()
            .isNotEmpty,
        'chip 上挂的换成了狗收拾过的那一份',
        step: const Duration(milliseconds: 20));
    expect(rig.puts.single['name'], '老  王', reason: '发上去的是人输的原样');
    expect(find.text('老  王'), findsNothing, reason: '屏上不许留着跟账上对不上的那一份');
    await unmount(t, rig);
  });

  testWidgets('狗还没回话人就退出了这一屏:不许再往拆掉的屏上打回调',
      (WidgetTester t) async {
    // **终审报告建议 7。** `setOperator` 最长 10 秒（`PatrolClient.timeout`）。
    // 人在这期间退出了页面的话，`onChanged(canon)` 打到的是一个已经
    // `dispose()` 的 State，`setState` 抛出来的异常被 `_edit` 外层那个
    // `catch (e)` 吞掉、记成一句「换人没告诉成狗」—— **那句话是假的**：请求
    // 发出去了，狗也回了。真正的后果是那个规范名既没上屏也没落盘，下次进来
    // 还是原字符串（回到必修 5），而日志把查的人指向网络。
    //
    // **挂的是一枚裸 chip，不是整屏遥控。** 挂在遥控屏里的话，回调先在
    // `_TeleopPageState.setState` 上抛一次，加不加这道守卫在外面看起来
    // 一模一样 —— 那种测试是恒真的。
    final FakeDog dog = await startDog(t, '老王', echo: '老 王');
    final PatrolClient c = PatrolClient(dog.baseUrl);
    await t.runAsync(() => c.unlock('864209', operator: '老王'));
    dog.received.clear();
    final Rig rig = Rig(dog, c, null);

    final List<String> told = <String>[];
    await t.pumpWidget(
        wrap(OperatorChip(name: '老王', client: c, onChanged: told.add)));
    await t.pump();
    await t.tap(find.byKey(OperatorChip.chipKey));
    await pumpUntil(
        t,
        () => find.byKey(OperatorChip.editKey).evaluate().isNotEmpty,
        '改名字的框出来了');
    await t.enterText(find.byKey(OperatorChip.editKey), '老  王');
    await t.tap(find.byKey(OperatorChip.saveKey));
    await t.pump();
    // **请求刚起飞，这一屏就没了。** 现场就是这一下：按完「就是我」顺手
    // 退出去，而热点正好抖一下。
    await t.pumpWidget(const SizedBox());
    await t.pump();

    await pumpUntil(t, () => rig.puts.isNotEmpty, '换人那条请求真的到了狗那儿',
        step: const Duration(milliseconds: 20));
    // 请求到了不等于回包落了地 —— 再推几轮真时钟，让 `setOperator` 那条
    // future 走完。不推的话「没打回调」断到的是「还没来得及打」。
    for (int i = 0; i < 10; i++) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 10)));
      await t.pump(const Duration(milliseconds: 20));
    }
    expect(told, <String>['老  王'],
        reason: '退屏之前那一下照旧；屏没了之后，狗回的那份不许再打回来');
    await unmount(t, rig);
  });

  // ------------------------------------------------ 落盘

  test('名字落盘,重开还在', () async {
    // **换一个 `JsonFileStore` 再读** —— 同一个对象读回自己内存里的那份，
    // 证不出「重开 app 还在」。
    final Directory dir =
        Directory.systemTemp.createTempSync('d1max-operator-');
    try {
      final File f = File('${dir.path}/roster.json');
      await JsonFileStore(f).saveOperator(dog: 'dog-1', name: '老王');
      expect(await JsonFileStore(f).readOperator(dog: 'dog-1'), '老王');
    } finally {
      dir.deleteSync(recursive: true);
    }
  });

  test('姓名不写进名册那一份文件 —— 名册是要导出去贴工单的', () async {
    final Directory dir =
        Directory.systemTemp.createTempSync('d1max-operator-');
    try {
      final File f = File('${dir.path}/roster.json');
      final JsonFileStore store = JsonFileStore(f);
      await store.save(RobotRegistry.fromList(
          <Robot>[const Robot(sn: 'C40221', name: '三号')]));
      await store.saveOperator(dog: 'C40221', name: '老王');
      expect(f.readAsStringSync(), isNot(contains('老王')),
          reason: '值班人的姓名不该跟着工单出门');
      expect(await store.readOperator(dog: 'C40221'), '老王');
    } finally {
      dir.deleteSync(recursive: true);
    }
  });

  // 「按狗分别记」这条硬要求，**两套实现各跑一遍**。
  //
  // 只跑 `MemoryRegistryStore` 的版本是形态 5（有分支但夹具保证进不去）：把
  // `JsonFileStore.saveOperator` 里的 `all[dog] = name` 改成 `all['operator']
  // = name`（重构时打错一个键，或者哪天有人想「简化成一个字段」），整套 Dart
  // 测试全绿 —— 而真机上跑的正是 `JsonFileStore`。现场后果是同时开着两只狗
  // 的那位在第二只上看见第一只那个班的名字，更坏的是
  // `roster_page._operatorFor` 读到非空就**不再问**，那个错名字直接进
  // `unlock` 发出去，账就从这一刻起记错了，而屏上一切正常。
  for (final MapEntry<String, RegistryStore Function(Directory)> impl
      in <String, RegistryStore Function(Directory)>{
    '内存那份': (Directory _) => MemoryRegistryStore(),
    '真机上那份': (Directory d) => JsonFileStore(File('${d.path}/roster.json')),
  }.entries) {
    test('按狗分别记 —— 换一只狗可能就是换一个班（${impl.key}）', () async {
      final Directory dir =
          Directory.systemTemp.createTempSync('d1max-operator-');
      try {
        final RegistryStore store = impl.value(dir);
        await store.saveOperator(dog: 'dog-1', name: '老王');
        await store.saveOperator(dog: 'dog-2', name: '小李');
        expect(await store.readOperator(dog: 'dog-1'), '老王');
        expect(await store.readOperator(dog: 'dog-2'), '小李');
      } finally {
        dir.deleteSync(recursive: true);
      }
    });

    test('没记过的那只是空串,不是编一个名字出来（${impl.key}）', () async {
      final Directory dir =
          Directory.systemTemp.createTempSync('d1max-operator-');
      try {
        final RegistryStore store = impl.value(dir);
        await store.saveOperator(dog: 'dog-1', name: '老王');
        expect(await store.readOperator(dog: 'dog-2'), '');
      } finally {
        dir.deleteSync(recursive: true);
      }
    });
  }

  test('两只狗各自的名字,重开 app 之后还是各自的', () async {
    // 上面那两条各自只摸一个 store 实例的内存。**这一条换实例再读** ——
    // 那才是 `JsonFileStore` 真正的行为面：盘上那份 JSON 得按 SN 分格子。
    final Directory dir =
        Directory.systemTemp.createTempSync('d1max-operator-');
    try {
      final File f = File('${dir.path}/roster.json');
      await JsonFileStore(f).saveOperator(dog: 'C40221', name: '老王');
      await JsonFileStore(f).saveOperator(dog: 'C40222', name: '小李');
      expect(await JsonFileStore(f).readOperator(dog: 'C40221'), '老王');
      expect(await JsonFileStore(f).readOperator(dog: 'C40222'), '小李');
    } finally {
      dir.deleteSync(recursive: true);
    }
  });
}
