/// 值守屏（§5.1 的三个问题）。
///
/// **起的是真的 `HttpServer`（`support/fake_dog.dart`），不是 mock。**
/// 这一屏最硬的两条守备 ——「确认真的把名字带上去了」和「告警键整段转义」——
/// 都只有在真的网络层上才看得见：把 `HttpClient` mock 掉，恰好把要证的那件事
/// 一起 mock 掉了。
///
/// **假数据里六项一个缺省值都不许有。** 这一卷已经在「天然全零环境」上栽过
/// 三次：断言的是平凡不动点，把渲染层删光写死常量照样绿。所以下面
/// [summaryWire] 的六项全是各不相同的非缺省值，断言断的是**每一格里那个数**，
/// 不是「这个 Key 在不在」。唯一的例外是 `upload_backlog`，它这一卷恒为
/// `null` —— 而那正是要证的那件事。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:d1max_patrol/ui/watch_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_dog.dart';
import 'support/pump.dart';

/// 一条告警在 wire 上的样子（`engine/alerts.py` 的 `Alert.to_wire()`，14 个键）。
Map<String, dynamic> alertWire({
  String key = 'C40221/stuck#1',
  String level = 'P1',
  String kind = 'stuck',
  String robot = 'C40221',
  String title = '狗卡住了',
  String detail = '在阀门间原地打转 3 分钟',
  int firstMs = 1757400000000,
  int lastMs = 1757400180000,
  int count = 4,
  String ackedBy = '',
  int? ackedMs,
  int? resolvedMs,
  int escalated = 0,
  String channel = 'screen',
}) =>
    <String, dynamic>{
      'key': key,
      'level': level,
      'kind': kind,
      'robot': robot,
      'title': title,
      'detail': detail,
      'first_ms': firstMs,
      'last_ms': lastMs,
      'count': count,
      'acked_by': ackedBy,
      'acked_ms': ackedMs,
      'resolved_ms': resolvedMs,
      'escalated': escalated,
      'channel': channel,
    };

Map<String, dynamic> p1Wire({String key = 'C40221/stuck#1'}) =>
    alertWire(key: key);

Map<String, dynamic> p2Wire({String key = 'C40221/disk_80#1'}) => alertWire(
      key: key,
      level: 'P2',
      kind: 'disk_80',
      title: '盘用到 83%',
      detail: '再不清今天之内会拒绝出发',
    );

/// 值守汇总在 wire 上的样子（`app/watch.py` 的 `watch_summary`）。
///
/// **六项全给非缺省值。** 见文件头那段。
Map<String, dynamic> summaryWire([Map<String, dynamic> over = const {}]) =>
    <String, dynamic>{
      'bundle_lag': <String>['b-0912'],
      'clock_skew_s': 12.5,
      // 这一卷恒为 null —— 这台狗还没有回传功能。
      'upload_backlog': null,
      // **已用比例 0-1**，不是 0-100。跟下面那个电量量纲不同。
      'disk_pct': 0.83,
      // **百分数 0-100**，不是 0-1。
      'battery_pct': 36.0,
      'battery_as_of_ms': 1757400000000,
      'alerts_open': 2,
      'mirror': <String, dynamic>{
        'disks': 2,
        'usable': 1,
        'measured': 1,
        'behind': 7,
        'behind_bytes': 9000000000,
        'full': false,
        'last_sync_ms': 1757300000000,
      },
      'detail': <String, dynamic>{
        'upload_backlog': '这台狗还没装回传功能，现场证据全存在本机',
      },
      ...over,
    };

/// `GET /api/state` 那一份快照里这一屏用得着的两段。
Map<String, dynamic> stateWire({
  String state = 'RUNNING',
  String waypointName = '阀门间',
  int waypointIndex = 3,
  int total = 7,
  Map<String, dynamic>? pose = const <String, dynamic>{
    'x': 1.25,
    'y': -3.5,
    'yaw': 0.0,
  },
}) =>
    <String, dynamic>{
      'run': <String, dynamic>{
        'state': state,
        'waypoint_name': waypointName,
        'waypoint_index': waypointIndex,
        'total': total,
      },
      'device': <String, dynamic>{'pose': pose},
    };

/// 一次挂好的现场：一只假狗、一个解过锁的客户端。
class Rig {
  Rig(this.dog, this.client);

  final FakeDog dog;
  final PatrolClient client;

  /// 这一屏问了几次告警。
  int get alertAsks => dog.received
      .where((FakeCall r) => r.method == 'GET' && r.path == '/api/alerts')
      .length;
}

/// 起一只假狗、解一次锁、把值守屏挂上去。
///
/// **`operatorName` 是构造参数，不是假狗身上的字段**（裁决十六）：`FakeDog`
/// 扮的是**狗**，而操作员姓名按计划就不在狗那侧 —— 它在手机上，由
/// `roster_page.dart::_open` 里那个 `who` 一路传下来。
Future<Rig> mount(
  WidgetTester t, {
  List<Map<String, dynamic>>? alerts,
  Map<String, dynamic>? summary,
  Map<String, dynamic>? state,
  /// 整个响应体照这份发。**给了就绕过上面那个 `{'alerts': [...]}` 的封皮**
  /// —— 「狗回 200 但体解不出来/形状不对」那几条要的正是一份没有封皮的回复。
  Map<String, dynamic>? alertsRaw,
  String operatorName = '老王',
  int Function()? nowMs,
  int? alertsStatus,
  int? summaryStatus,
  int? stateStatus,
}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
  dog.replies['/api/auth'] = <String, dynamic>{
    'token': 'tok-abc',
    'operator': operatorName,
    'operator_verified': false,
    'readonly': false,
  };
  dog.replies['/api/alerts'] = alertsRaw ??
      <String, dynamic>{
        'alerts': alerts ?? <Map<String, dynamic>>[],
      };
  dog.replies['/api/watch/summary'] = summary ?? summaryWire();
  dog.replies['/api/state'] = state ?? stateWire();
  if (alertsStatus != null) dog.statusCodes['/api/alerts'] = alertsStatus;
  if (summaryStatus != null) {
    dog.statusCodes['/api/watch/summary'] = summaryStatus;
  }
  if (stateStatus != null) dog.statusCodes['/api/state'] = stateStatus;

  final PatrolClient c = PatrolClient(dog.baseUrl);
  await t.runAsync(() => c.unlock('864209', operator: operatorName));
  // 解锁那两条不是这一屏发的，从账上抹掉。
  dog.received.clear();
  await t.pumpWidget(MaterialApp(
    home: WatchPage(
      client: c,
      robot: const Robot(sn: 'C40221', name: '三号'),
      operatorName: operatorName,
      // 不给就走真墙上钟（真机上那条路）。给了，「这一屏是什么时候的」
      // 才断得出来 —— 真时钟每次跑都不一样，只能断「有那么一格」。
      nowMs: nowMs ?? wallClockMs,
    ),
  ));
  // **三段都得等到。** 三条路各读各的（一条断了不许把另外两段带走），所以
  // 只等其中一段的话，另外两段可能还在转圈 —— 那时候断言读到的是「还没问
  // 回来」，而不是被测的那句话。
  bool got(Key ok, Key bad) =>
      find.byKey(ok).evaluate().isNotEmpty ||
      find.byKey(bad).evaluate().isNotEmpty;
  await pumpUntil(
      t,
      () =>
          got(WatchPage.alertsSectionKey, WatchPage.alertsErrorKey) &&
          got(WatchPage.engineKey, WatchPage.stateErrorKey) &&
          got(WatchPage.backlogKey, WatchPage.summaryErrorKey),
      '值守屏三段都问回来（或者报出读不到）',
      step: const Duration(milliseconds: 20));
  return Rig(dog, c);
}

/// 收摊。**客户端也要关**：一条没收的 `HttpClient` 会留着自己那个 15 秒空闲
/// 计时器，`flutter_test` 的 `!timersPending` 会当场把测试打红。
Future<void> unmount(WidgetTester t, Rig rig) async {
  await teardown(t, null);
  rig.client.close();
  await t.pump();
  await t.runAsync(rig.dog.stop);
  expect(rig.dog.errors, isEmpty, reason: '假狗自己抛了：查的是假狗，不是被测代码');
}

/// 整屏上所有 `Text` 的文字，拼成一串。给「这几个词一个都不许出现」那几条用。
String screenText(WidgetTester t) => t
    .widgetList<Text>(find.byType(Text))
    .map((Text w) => w.data ?? '')
    .join('\n');

/// 某个 Key 上那句原话。找不到就是空串。
String keyText(WidgetTester t, Key key) {
  final Iterable<Element> got = find.byKey(key).evaluate();
  if (got.isEmpty) return '';
  return (got.first.widget as Text).data ?? '';
}

/// 「这一屏的数据读于 …」那一格上的原话。
String asOfText(WidgetTester t) {
  final Iterable<Element> got = find.byKey(WatchPage.asOfKey).evaluate();
  if (got.isEmpty) return '';
  return (got.first.widget as Text).data ?? '';
}

void main() {
  // 摘掉 `flutter_test` 那个「所有请求都回 400」的 HttpOverrides：这个文件
  // 要证的正是「确认那一下真的发出去了、路径和 body 长什么样」。
  useRealHttp();

  // ------------------------------------------- 问题一：有没有需要我动身的事

  testWidgets('一条 P1 都没有的时候明确说没有,不是留白', (WidgetTester t) async {
    // 留白和「还没加载出来」长得一模一样 —— 值班的人分不出「今晚太平」和
    // 「这一屏挂了」。
    final Rig rig = await mount(t, alerts: <Map<String, dynamic>>[p2Wire()]);
    // **自洽**：这一屏真的画出来了、也真的收到了告警，下面那条才不是空绿。
    expect(find.byKey(WatchPage.alertKeyFor(0)), findsOneWidget,
        reason: 'P2 那条画出来了 —— 这一屏不是一片空白');
    expect(find.byKey(WatchPage.p1NoneKey), findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('有 P1 的时候不许还挂着「没有」那句', (WidgetTester t) async {
    // 另一个局面。两个局面都得跑到 —— 全跑在「没有 P1」上的话，
    // 「有 P1 就别说没有」这一支一次都没执行过。
    final Rig rig = await mount(t, alerts: <Map<String, dynamic>>[p1Wire()]);
    expect(find.byKey(WatchPage.alertKeyFor(0)), findsOneWidget);
    expect(find.byKey(WatchPage.p1NoneKey), findsNothing,
        reason: '屏上明明挂着一条 P1，还写着「没有需要动身的事」是最坏的一种说谎');
    await unmount(t, rig);
  });

  testWidgets('狗把 P1 排在最上面时,屏上第一条就是 P1', (WidgetTester t) async {
    // §5.1 的第一个问题在屏上的样子。排序在狗那侧
    // （`AlertBook.open()`，判据只在 `engine/` 说一次）。
    final Rig rig =
        await mount(t, alerts: <Map<String, dynamic>>[p1Wire(), p2Wire()]);
    final AlertTile first =
        t.widget<AlertTile>(find.byKey(WatchPage.alertKeyFor(0)));
    expect(first.alert.level, 'P1');
    await unmount(t, rig);
  });

  testWidgets('拿到什么顺序就渲染什么顺序,手机不再排一遍', (WidgetTester t) async {
    // **手机端不许自己再排一遍。** 排法（先级别再 `last_ms` 倒序）是判据的
    // 一部分，判据只许在 `engine/` 里说一次；手机这头再排一遍就是同一件事
    // 有两个出处，而对不上的那天，屏幕第一行显示的不是该起身的那件事。
    //
    // 所以这里故意喂一个**不是**狗那侧顺序的列表：屏上要原样照登。
    final Rig rig =
        await mount(t, alerts: <Map<String, dynamic>>[p2Wire(), p1Wire()]);
    expect(t.widget<AlertTile>(find.byKey(WatchPage.alertKeyFor(0))).alert.level,
        'P2',
        reason: '手机在渲染层又排了一遍 —— 判据从此有两个出处');
    expect(t.widget<AlertTile>(find.byKey(WatchPage.alertKeyFor(1))).alert.level,
        'P1');
    await unmount(t, rig);
  });

  // ------------------------------------------------- 问题二：狗在哪、在干什么

  testWidgets('引擎状态和位姿看得见', (WidgetTester t) async {
    final Rig rig = await mount(t);
    final String engine =
        t.widget<Text>(find.byKey(WatchPage.engineKey)).data ?? '';
    expect(engine, contains('RUNNING'));
    expect(engine, contains('阀门间'));
    expect(engine, contains('3'), reason: '第几个点位');
    expect(engine, contains('7'), reason: '一共几个点位');
    final String pose =
        t.widget<Text>(find.byKey(WatchPage.poseKey)).data ?? '';
    expect(pose, contains('1.25'));
    expect(pose, contains('-3.50'));
    await unmount(t, rig);
  });

  testWidgets('位姿还没收到过的时候说不知道,不许画成 0,0', (WidgetTester t) async {
    // 一个看着像真事的假坐标比 `null` 更坏：屏上写着 (0.00, 0.00) 的时候，
    // 人会以为狗在原点，而事实是根本没收到过位姿。
    final Rig rig = await mount(t, state: stateWire(pose: null));
    final String pose =
        t.widget<Text>(find.byKey(WatchPage.poseKey)).data ?? '';
    expect(pose, contains('不知道'));
    expect(pose, isNot(contains('0.00')));
    await unmount(t, rig);
  });

  // ------------------------------------------ 问题三：有没有什么正在悄悄坏掉

  testWidgets('回传积压是 null 时说这一档没有回传,不显示 0', (WidgetTester t) async {
    // `0` 会让人以为证据都传上去了，而它们全在狗上堆着（§4.3）。
    final Rig rig = await mount(t);
    final Text w = t.widget<Text>(find.byKey(WatchPage.backlogKey));
    expect(w.data, isNot(contains('0')));
    expect(w.data, contains('没有回传'));
    await unmount(t, rig);
  });

  testWidgets('回传积压真有数的时候把数写出来', (WidgetTester t) async {
    // 另一个局面（这一卷的狗恒报 null，所以夹具驱动不到这一支）。第 9 卷
    // 装上回传之后这条就是主路径 —— 那时候「有 12 条堆着」不许还写成
    // 「这一档没有回传」。
    final Rig rig = await mount(
        t, summary: summaryWire(<String, dynamic>{'upload_backlog': 12}));
    final Text w = t.widget<Text>(find.byKey(WatchPage.backlogKey));
    expect(w.data, contains('12'));
    expect(w.data, isNot(contains('没有回传')));
    await unmount(t, rig);
  });

  testWidgets('六项一个不少', (WidgetTester t) async {
    final Rig rig = await mount(t);
    for (final Key k in WatchPage.sixSlotKeys) {
      expect(find.byKey(k), findsOneWidget, reason: '§5.1 六项少了 $k');
    }
    await unmount(t, rig);
  });

  testWidgets('六项各说各的数,盘水位和电量的量纲不许串', (WidgetTester t) async {
    // **这一条是「六项一个不少」的另一半。** 光数 Key 的话，六个 Key 挂六个
    // 写死的字符串照样绿 —— 而屏上一个真数字都没有。
    //
    // 盘水位是**已用比例 0-1**，电量是**百分数 0-100**（`app/watch.py` 里
    // 那两段注释专门写了这件事）。共用一个格式化函数的话，一块 83% 满的盘
    // 会被画成「0.83%」—— 反着报，而且是以最安静的方式。
    final Rig rig = await mount(t);
    String slot(Key k) => t.widget<Text>(find.byKey(k)).data ?? '';

    expect(slot(WatchPage.diskKey), contains('83%'),
        reason: '0.83 是已用比例，屏上要写 83%');
    expect(slot(WatchPage.diskKey), isNot(contains('0.83%')),
        reason: '拿电量那个格式化函数画盘水位，一块快满的盘会显示成 0.83%');
    expect(slot(WatchPage.batteryKey), contains('36%'),
        reason: '36.0 已经是百分数，不许再乘一次 100');
    expect(slot(WatchPage.batteryKey), isNot(contains('3600')));
    expect(slot(WatchPage.clockSkewKey), contains('12.5'));
    expect(slot(WatchPage.bundleLagKey), contains('b-0912'));
    expect(slot(WatchPage.mirrorKey), contains('7'), reason: '落后 7 趟');
    await unmount(t, rig);
  });

  testWidgets('六项里报不出来的那一档说不知道,不许画成 0', (WidgetTester t) async {
    // `0` 是「查过了，没有」，`null` 是「这一拍不知道」—— 两句话在屏幕上
    // 长得一样，在现场差着一次事故（`app/watch.py` 的模块 docstring）。
    final Rig rig = await mount(
        t,
        summary: summaryWire(<String, dynamic>{
          'clock_skew_s': null,
          'disk_pct': null,
          'battery_pct': null,
          'bundle_lag': null,
          'mirror': null,
        }));
    for (final Key k in <Key>[
      WatchPage.clockSkewKey,
      WatchPage.diskKey,
      WatchPage.batteryKey,
      WatchPage.bundleLagKey,
      WatchPage.mirrorKey,
    ]) {
      final String s = t.widget<Text>(find.byKey(k)).data ?? '';
      expect(s, contains('不知道'), reason: '$k 报不出来时要说「不知道」');
      expect(s, isNot(contains('0%')), reason: '$k 不许把「不知道」画成 0');
    }
    await unmount(t, rig);
  });

  testWidgets('狗附的那句为什么原样上屏', (WidgetTester t) async {
    // 一个光秃秃的「不知道」摆在屏上，人分不清是「没查」还是「查了没事」——
    // 那本身就是一次新的静默失败。措辞归狗那头（`app/watch.py` 的
    // `NO_UPLOADER`），手机不重新组织。
    final Rig rig = await mount(t);
    expect(find.text('这台狗还没装回传功能，现场证据全存在本机'), findsOneWidget);
    await unmount(t, rig);
  });

  // ------------------------------------------------------------- 记名确认

  testWidgets('确认按钮把当前操作员的名字带上去', (WidgetTester t) async {
    // **姓名来自 `WatchPage` 的构造参数**（裁决十六）：`roster_page.dart`
    // 里那个 `who` 一路传下来。空姓名狗那侧回 400 —— 确认会把升级链停下来，
    // 匿名确认等于任何人都能把声音关掉而没人负责（§5.3）。
    final Rig rig =
        await mount(t, alerts: <Map<String, dynamic>>[p1Wire()], operatorName: '老王');
    await t.tap(find.byKey(WatchPage.ackKeyFor(0)));
    await pumpUntil(t, () => rig.dog.acks.isNotEmpty, '确认那一下真的发出去了',
        step: const Duration(milliseconds: 20));
    expect(rig.dog.acks.single['who'], '老王');
    await unmount(t, rig);
  });

  testWidgets('换一个操作员,确认里带的就是换过的那个', (WidgetTester t) async {
    // **钉住「这个值不是写死的」。** 上一条单独看的话，一个
    // `{'who': '老王'}` 的字面量照样让它绿。
    final Rig rig = await mount(t,
        alerts: <Map<String, dynamic>>[p1Wire()], operatorName: '李四');
    await t.tap(find.byKey(WatchPage.ackKeyFor(0)));
    await pumpUntil(t, () => rig.dog.acks.isNotEmpty, '确认那一下真的发出去了',
        step: const Duration(milliseconds: 20));
    expect(rig.dog.acks.single['who'], '李四');
    await unmount(t, rig);
  });

  testWidgets('告警键里的斜杠和井号整段转义,不许裸着拼进路径', (WidgetTester t) async {
    // 键形如 `robot/kind#seq`（`engine/alerts.py` 的 `_key`）。**不转义狗那边
    // 会 404**：`#` 会被 `Uri.parse` 当 fragment 从请求里整段切掉，剩下的
    // 路径连 `/ack` 都没有了。狗那侧为此专门开了一个跨斜杠的路由占位符，
    // 但那救不了一个在客户端就已经被砍断的请求。
    final Rig rig = await mount(t,
        alerts: <Map<String, dynamic>>[p1Wire(key: 'C40221/stuck#7')]);
    await t.tap(find.byKey(WatchPage.ackKeyFor(0)));
    await pumpUntil(t, () => rig.dog.acks.isNotEmpty, '确认那一下真的发出去了',
        step: const Duration(milliseconds: 20));

    final FakeCall call = rig.dog.received
        .lastWhere((FakeCall r) => r.method == 'POST' && r.path.endsWith('/ack'));
    // `Uri.path` 返回的是**还带着转义的**那一份（这台机器上验过），所以中间
    // 那一段真的能看出转没转。
    const String head = '/api/alerts/';
    const String tail = '/ack';
    expect(call.path, startsWith(head));
    final String seg =
        call.path.substring(head.length, call.path.length - tail.length);
    expect(seg, isNot(contains('/')), reason: '键里的斜杠要转义成 %2F');
    expect(seg, isNot(contains('#')), reason: '键里的井号要转义成 %23');
    // 转过头也不行：狗那边解出来得还是原来那个键。
    expect(Uri.decodeComponent(seg), 'C40221/stuck#7');
    await unmount(t, rig);
  });

  testWidgets('确认完了要再问一次,屏上那条不能停在旧样子', (WidgetTester t) async {
    // 确认是有后果的动作：不重问的话，屏上那条还挂着「未确认」，人会再点
    // 一次、再点一次 —— 而每一次都真的发出去了。
    final Rig rig = await mount(t, alerts: <Map<String, dynamic>>[p1Wire()]);
    expect(rig.alertAsks, 1, reason: '这一屏真的问过狗 —— 否则下面数的是一片空白');
    rig.dog.replies['/api/alerts'] = <String, dynamic>{
      'alerts': <Map<String, dynamic>>[
        alertWire(ackedBy: '老王', ackedMs: 1757400200000),
      ],
    };
    await t.tap(find.byKey(WatchPage.ackKeyFor(0)));
    await pumpUntil(t, () => rig.alertAsks >= 2, '确认完了真的又问了一次',
        step: const Duration(milliseconds: 20));
    await pumpUntil(
        t,
        () => (t
                    .widget<AlertTile>(find.byKey(WatchPage.alertKeyFor(0)))
                    .alert
                    .ackedMs) !=
            null,
        '屏上那条换成了已确认的样子',
        step: const Duration(milliseconds: 20));
    expect(
        t.widget<AlertTile>(find.byKey(WatchPage.alertKeyFor(0))).alert.ackedBy,
        '老王');
    await unmount(t, rig);
  });

  // ------------------------------------------------------- 措辞上的硬约束

  testWidgets('自报的名字不许说成登录或者已认证', (WidgetTester t) async {
    // `Session.operatorVerified` 那条注释写着：**狗记下名字但不核实**
    // （§6.3）。界面上不许说成「已登录：张三」—— 它就是一个自报的署名，
    // 而现场判断「屏上这个名字能信到什么程度」全靠这一句话不说错。
    final Rig rig = await mount(t, operatorName: '老王');
    // **自洽**：名字真的画出来了，下面那一圈才不是在一片空白上转。
    expect(find.byKey(WatchPage.operatorKey), findsOneWidget);
    expect(t.widget<Text>(find.byKey(WatchPage.operatorKey)).data,
        contains('老王'));
    final String all = screenText(t);
    for (final String bad in <String>['已登录', '登录', '已认证', '身份已验证', '已验证']) {
      expect(all, isNot(contains(bad)),
          reason: '狗记下名字但不核实（§6.3）：屏上不许出现「$bad」');
    }
    await unmount(t, rig);
  });

  // ------------------------------------------------------------- 一路断了

  testWidgets('告警读不到不许把另外两段一起带走', (WidgetTester t) async {
    // 值守屏是最后一块必须还能亮的玻璃。狗那侧的 `_watch_summary` 写着
    // 「探针卡住不许把另外五项一起带走」—— 手机这头是同一条纪律：一条路
    // 断了，别的两段照样得答完。
    final Rig rig = await mount(t, alertsStatus: 500);
    expect(find.byKey(WatchPage.alertsErrorKey), findsOneWidget);
    expect(find.byKey(WatchPage.backlogKey), findsOneWidget,
        reason: '六项那一段还在');
    expect(find.byKey(WatchPage.engineKey), findsOneWidget,
        reason: '引擎那一段还在');
    // `PatrolError` 那份原文是「狗回了 500」：它既不告诉人下一步该干什么，
    // 又会把内部细节推到客户面前。
    expect(screenText(t), isNot(contains('500')));
    await unmount(t, rig);
  });

  testWidgets('六项读不到不许把告警一起带走', (WidgetTester t) async {
    final Rig rig = await mount(t,
        alerts: <Map<String, dynamic>>[p1Wire()], summaryStatus: 500);
    expect(find.byKey(WatchPage.summaryErrorKey), findsOneWidget);
    expect(find.byKey(WatchPage.alertKeyFor(0)), findsOneWidget,
        reason: '告警那一段还在');
    await unmount(t, rig);
  });

  testWidgets('读不到之后点刷新,狗回话了就画出来', (WidgetTester t) async {
    final Rig rig = await mount(t, alertsStatus: 500);
    expect(find.byKey(WatchPage.alertsErrorKey), findsOneWidget);
    rig.dog.statusCodes.remove('/api/alerts');
    rig.dog.replies['/api/alerts'] = <String, dynamic>{
      'alerts': <Map<String, dynamic>>[p1Wire()],
    };
    await t.tap(find.byKey(WatchPage.refreshKey));
    await pumpUntil(
        t,
        () => find.byKey(WatchPage.alertKeyFor(0)).evaluate().isNotEmpty,
        '再问一次之后告警画出来了',
        step: const Duration(milliseconds: 20));
    expect(find.byKey(WatchPage.alertsErrorKey), findsNothing);
    await unmount(t, rig);
  });

  // ---------------------------------------- 屏上这堆数是什么时候读回来的

  // **这一屏不轮询**（`watch_page.dart` 文件头）：每一格都是上一次问回来的
  // 样子，之后再没变过。狗在地下室断链四十分钟，这一屏还稳稳地摆着四十分钟
  // 前的告警数和位姿 —— 跟三秒前的一屏在玻璃上长得一模一样。
  //
  // **下面这两条断的都不是「有 `watch-as-of` 这个 Key」。** 那句话是恒真的：
  // 把整段渲染删成一个空 `Text`，它照样绿。断的是**屏上那个时刻跟着喂进去
  // 的读取时刻走** —— 喂两个不同的时刻，屏上就得是两个不同的显示。

  testWidgets('屏上的读取时刻跟着注入的钟走,不是写死的一句', (WidgetTester t) async {
    Future<String> shownAt(DateTime when) async {
      final Rig rig =
          await mount(t, nowMs: () => when.millisecondsSinceEpoch);
      final String shown = asOfText(t);
      await unmount(t, rig);
      return shown;
    }

    final String early = await shownAt(DateTime.utc(2026, 9, 10, 7, 0));
    final String later = await shownAt(DateTime.utc(2026, 9, 10, 9, 30));
    expect(early, contains('2026-09-10 07:00 UTC'));
    expect(later, contains('2026-09-10 09:30 UTC'));
    // 两个时刻喂进去，屏上就得是两句不一样的话。写死一句常量的版本在这儿红。
    expect(early, isNot(equals(later)));
    expect(later, isNot(contains('07:00')));
  });

  testWidgets('再问一次之后,屏上写的是这一次的时刻', (WidgetTester t) async {
    // 只在 `initState` 里取一次时刻的版本在这儿红：人点了刷新、数据换了，
    // 而屏上还写着两小时前 —— 那比不写更坏，它是一句会骗人的话。
    int now = DateTime.utc(2026, 9, 10, 7, 0).millisecondsSinceEpoch;
    final Rig rig = await mount(t, nowMs: () => now);
    expect(asOfText(t), contains('2026-09-10 07:00 UTC'));

    now = DateTime.utc(2026, 9, 10, 9, 30).millisecondsSinceEpoch;
    await t.tap(find.byKey(WatchPage.refreshKey));
    await pumpUntil(t, () => asOfText(t).contains('2026-09-10 09:30 UTC'),
        '再问一次之后屏上的读取时刻换成这一次的',
        step: const Duration(milliseconds: 20));
    expect(asOfText(t), isNot(contains('07:00')));
    await unmount(t, rig);
  });

  // ------------------------------------- 狗回了 200，但体里的东西读不懂
  //
  // **这条路今天就走得到。** `patrol_client.dart` 的 `_send` 里
  // `jsonDecode` 抛 `FormatException` 是被吞掉的（S-2，为了让状态码报得
  // 对），于是一份 200 + 一页门户 HTML 会原样变成一个空的 `{}` 交到解析
  // 函数手上。现场最像的触发：手机没连上狗的热点，连到了带强制门户的网，
  // 门户对任何 GET 都回 200 加一页登录页。
  //
  // **要证的不是「没崩」，是「没说让人放心的假话」。** 这一屏最坏的一种
  // 错不是留白，是一句带着「刚问过狗」四个字的假保证。

  testWidgets('告警读不懂的时候说读不到,不许说成没有告警', (WidgetTester t) async {
    // 体里压根没有 `alerts` 这个键（门户回的那页 HTML 解出来就是这样）。
    final Rig rig = await mount(t, alertsRaw: <String, dynamic>{});
    // **先断这一条。** 它红的时候，报错里印出来的就是屏上那句让人放心的
    // 假话本身 —— 那才是这一刀要证的东西。
    expect(screenText(t), isNot(contains(noP1Hint)),
        reason: '这一屏没读懂回复，却打出「现在没有需要立刻动身的事」—— '
            '而狗上此刻可能正挂着一条 P1。这是这一屏能犯的最坏的一种错');
    expect(find.byKey(WatchPage.p1NoneKey), findsNothing);
    expect(find.byKey(WatchPage.alertsErrorKey), findsOneWidget,
        reason: '读不懂就得说读不到');
    await unmount(t, rig);
  });

  testWidgets('名单在那儿但不是个名单时也算读不到', (WidgetTester t) async {
    // 将来狗那侧改成 `{"items": [...]}`、或者某一拍回 `{"alerts": null}`，
    // 走的都是这一支。
    final Rig rig = await mount(
        t, alertsRaw: <String, dynamic>{'alerts': null});
    expect(find.byKey(WatchPage.alertsErrorKey), findsOneWidget);
    expect(find.byKey(WatchPage.p1NoneKey), findsNothing);
    await unmount(t, rig);
  });

  testWidgets('名单里有一条读不懂,整份都不算数,不许悄悄少一条', (WidgetTester t) async {
    // 静默丢一条最坏的形态：屏上还剩几条、看着一切正常，而丢掉的那条正是
    // P1。跟 `store/registry_store.dart` 一个规矩 ——「名册里有条目不是
    // 对象」是整份读不出，不是少一只狗。
    final Rig rig = await mount(t, alertsRaw: <String, dynamic>{
      'alerts': <Object>[p1Wire(), 'this-is-not-an-alert'],
    });
    expect(find.byKey(WatchPage.alertsErrorKey), findsOneWidget);
    expect(find.byKey(WatchPage.p1NoneKey), findsNothing);
    await unmount(t, rig);
  });

  testWidgets('狗的状态读不懂的时候说读不到,不许报第 0/0 个点位', (WidgetTester t) async {
    final Rig rig = await mount(t, state: <String, dynamic>{});
    expect(find.byKey(WatchPage.stateErrorKey), findsOneWidget);
    expect(screenText(t), isNot(contains('0/0')),
        reason: '没读懂回复却报两个凭空捏的数，比一句「读不到」坏得多');
    await unmount(t, rig);
  });

  testWidgets('run 里没有点位进度的时候说不知道,不许凭空报一个 0/0',
      (WidgetTester t) async {
    // 这一份是读得懂的：引擎状态在，点位进度那两个数不在。**读得懂的那半句
    // 照说，读不出的那半句说「不知道」** —— 跟同一个函数下面 `_poseText`
    // 里那条「不许画成 (0.00, 0.00)」是同一条规矩。
    final Rig rig = await mount(t, state: <String, dynamic>{
      'run': <String, dynamic>{'state': 'RUNNING'},
      'device': <String, dynamic>{'pose': null},
    });
    final String engine = keyText(t, WatchPage.engineKey);
    expect(engine, contains('RUNNING'), reason: '读得懂的那半句照说');
    expect(engine, contains(unknownText));
    expect(engine, isNot(contains('0/0')));
    expect(engine, isNot(contains('第 0')));
    await unmount(t, rig);
  });

  // ------------------------------------------------------- 补的几条覆盖

  testWidgets('这事没了那个按钮真的发得出去,键也是转义过的', (WidgetTester t) async {
    // 这个按钮原先一条测试都没有：它真的画在每条告警上、真的会发
    // `POST …/resolve`，全靠跟 ack 共用 `_alertPath` 搭便车。
    final Rig rig = await mount(t, alerts: <Map<String, dynamic>>[
      p1Wire(key: 'C40221/stuck#7'),
    ]);
    await t.tap(find.byKey(WatchPage.resolveKeyFor(0)));
    await pumpUntil(
        t,
        () => rig.dog.received
            .any((FakeCall r) => r.method == 'POST' && r.path.endsWith('/resolve')),
        '解决那一下真的发出去了',
        step: const Duration(milliseconds: 20));
    final FakeCall call = rig.dog.received
        .firstWhere((FakeCall r) => r.path.endsWith('/resolve'));
    final String seg = call.path.split('/')[3];
    expect(seg, isNot(contains('#')), reason: '井号裸着拼进去，请求会在客户端就被截断');
    expect(seg, isNot(contains('%2F/')));
    expect(Uri.decodeComponent(seg), 'C40221/stuck#7');
    await unmount(t, rig);
  });

  testWidgets('狗附的那句为什么挂在自己的 Key 上,不是靠找那句中文找到的',
      (WidgetTester t) async {
    // 原先唯一覆盖这一行的断言是 `find.text('长长的一句中文')`：狗那头改一个
    // 标点它就红，而红的原因跟被测的事无关（挂账 62 的形状）。
    final Rig rig = await mount(t);
    expect(keyText(t, WatchPage.whyKeyFor('upload_backlog')),
        '这台狗还没装回传功能，现场证据全存在本机');
    await unmount(t, rig);
  });
}
