/// 控制权面板：谁拿着、还剩多久、怎么要过来。
///
/// **倒计时读 `expires_in_ms`，不减手机的钟。** 狗上没有 NTP
/// （`engine/lease.py` 的 `LeaseState` 写着），两头的钟差几分钟是常态；
/// 减出来的结果要么是负数，要么是「还剩二十分钟」下一秒就掉 —— 两种都会让
/// 人以为 app 坏了，而真正该做的事（去把控制权要过来）一件都不会发生。
///
/// **起的是真的 `HttpServer`（`support/fake_dog.dart`），不是 mock。**
/// 这一屏要证的有一半是「问了狗几次」—— 把 HTTP 那头 mock 掉，恰好把要证的
/// 那件事一起 mock 掉了。
///
/// **URL 一律从 `client.baseUrl` 取。** `Robot.host/port` 的默认值是真狗的
/// `192.168.168.100:8095`：拿 `robot` 拼 URL 的话，测试会去敲真狗的地址、
/// 现场会敲错狗。
///
/// **回复一律从 `test/fixtures/lease_state.json` 改出来，不手搓 JSON 字面量**
/// （那份夹具是狗那头生成的，挂账 11）。手搓的那份会跟狗慢慢分家：分家那天
/// 两边都是绿的。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:d1max_patrol/ui/control_panel.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_dog.dart';
import 'support/pump.dart';

/// 狗那头生成的那份租约报文。**形状的真理源。**
Map<String, dynamic> _fixture() =>
    jsonDecode(File('test/fixtures/lease_state.json').readAsStringSync())
        as Map<String, dynamic>;

/// 没人拿着控制权。
///
/// **`expires_in_ms` 是 `null` 不是 0**：`engine/lease.py:126-128` 明写这两件
/// 事在界面上是两种画法 —— 「没人拿着」画成「已过期」，人会去等一个根本不会
/// 到的时刻。
Map<String, dynamic> nobodyHolds({int remoteSessions = 0, int maxSessions = 3}) =>
    <String, dynamic>{
      ..._fixture(),
      'holder': null,
      'expires_ms': null,
      'expires_in_ms': null,
      'mine': false,
      'remote_sessions': remoteSessions,
      'max_sessions': maxSessions,
    };

/// 控制权在我手上，还剩 [expiresInMs] 毫秒。
Map<String, dynamic> mineHolds({int expiresInMs = 30000, String? notice}) {
  final Map<String, dynamic> m = <String, dynamic>{
    ..._fixture(),
    'mine': true,
    'expires_in_ms': expiresInMs,
  };
  if (notice != null) m['notice'] = notice;
  return m;
}

/// 控制权在别人手上。
Map<String, dynamic> otherHolds(
        {String operator = '李四', int expiresInMs = 20000}) =>
    <String, dynamic>{
      ..._fixture(),
      'holder': <String, dynamic>{'ref': 'ff00ff00', 'operator': operator},
      'mine': false,
      'expires_in_ms': expiresInMs,
    };

/// 别人拿着，而我在抢 —— 宽限期还剩 [graceInMs] 毫秒。
Map<String, dynamic> takeoverPending(
        {int graceInMs = 15000, String operator = '李四'}) =>
    <String, dynamic>{
      ...otherHolds(operator: operator),
      'challenger': <String, dynamic>{'ref': 'ab12cd34', 'operator': '张三'},
      'grace_in_ms': graceInMs,
    };

/// 我拿着，而别人在抢我的。
Map<String, dynamic> mineChallenged(
        {int graceInMs = 15000, String challenger = '王五'}) =>
    <String, dynamic>{
      ...mineHolds(),
      'challenger': <String, dynamic>{'ref': 'cc00cc00', 'operator': challenger},
      'grace_in_ms': graceInMs,
    };

/// 一个会数数的客户端。**数的是「发出去了几次」，不是「狗收到了几次」。**
///
/// 这两件事在假时钟上不是一回事，而且差别正好落在要证的那条测试上：
/// widget 测试跑在假时钟上，真的网络 I/O 只有进了 `runAsync` 才推得动 ——
/// 在一段纯 `pump()` 的窗口里，请求发得出去，狗却一条都收不到。于是
/// 「数狗收到了几次」在那个窗口里恒为 0：**面板改成每秒问一次狗，那条测试
/// 照样是绿的。**（本轮真拆出来过：C4b 拆完 17 条全绿。）
///
/// 在客户端这一头数，秒表就跟被测的行为在同一个时钟上了。
class SpyClient extends PatrolClient {
  SpyClient(super.baseUrl);

  final List<String> calls = <String>[];

  @override
  Future<Map<String, dynamic>> get(String path) {
    calls.add(path);
    return super.get(path);
  }

  @override
  Future<Map<String, dynamic>> post(String path, [Object? body]) {
    calls.add(path);
    return super.post(path, body);
  }
}

class Rig {
  Rig(this.dog, this.client);

  final FakeDog dog;
  final SpyClient client;

  /// 问过狗几次「谁拿着」。**只数这一条路径。**
  ///
  /// 数 `calls.length` 会被心跳和别的请求踩到 —— 那样数出来的「多了一次」
  /// 说不清是倒计时在问狗，还是心跳到点了。
  int get asked => client.calls.where((String p) => p == '/api/control').length;

  List<String> get posted => dog.received
      .where((FakeCall r) => r.method == 'POST')
      .map((FakeCall r) => r.path)
      .toList();

  int get beats => dog.received
      .where((FakeCall r) => r.path == '/api/control/heartbeat')
      .length;
}

/// 起一只假狗，把面板挂上去，等到它拿到第一份租约。
///
/// **不走 `unlock`。** 这个文件要证的事一件都跟换 token 无关，而每多一次真的
/// 往返，第一份租约就晚到几百毫秒 —— 而「本地在走」那条测试的窗口是按假时钟
/// 数的，前面耗掉的每一毫秒都从那个窗口里扣。
Future<Rig> mount(WidgetTester t, Map<String, dynamic> lease,
    {void Function(bool)? onHeld, Duration? syncPeriod}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/control'] = lease;
  dog.replies['/api/control/acquire'] = lease;
  dog.replies['/api/control/release'] = nobodyHolds();
  dog.replies['/api/control/takeover'] = lease;
  dog.replies['/api/control/takeover/approve'] = nobodyHolds();
  dog.replies['/api/control/heartbeat'] = lease;
  final SpyClient c = SpyClient(dog.baseUrl);
  await t.pumpWidget(wrap(ControlPanel(
    client: c,
    onHeld: onHeld,
    syncPeriod: syncPeriod ?? const Duration(seconds: 3),
  )));
  final Rig rig = Rig(dog, c);
  await pumpUntil(t, () => settled(t), '面板拿到第一份租约（三个按钮之一露出来）');
  return rig;
}

/// 第一份租约到没到 —— 三种态各有一个按钮，哪个露出来都算到了。
bool settled(WidgetTester t) =>
    find.byKey(ControlPanel.acquireKey).evaluate().isNotEmpty ||
    find.byKey(ControlPanel.releaseKey).evaluate().isNotEmpty ||
    find.byKey(ControlPanel.takeoverKey).evaluate().isNotEmpty;

Future<void> unmount(WidgetTester t, Rig rig) async {
  await teardown(t, null);
  rig.client.close();
  await t.pump();
  await t.runAsync(rig.dog.stop);
  expect(rig.dog.errors, isEmpty, reason: '假狗自己抛了：查的是假狗，不是被测代码');
}

/// 「还剩 N 秒」里的那个 N。
int remainSeconds(WidgetTester t) {
  final Text w = t.widget<Text>(find.byKey(ControlPanel.remainKey));
  final RegExpMatch? m = RegExp(r'\d+').firstMatch(w.data ?? '');
  if (m == null) fail('倒计时那一格里没有数：「${w.data}」');
  return int.parse(m.group(0)!);
}

void main() {
  // 摘掉 `flutter_test` 那个「所有请求都回 400」的 HttpOverrides：这个文件
  // 要证的正是「问了狗几次、问的是哪条路」。
  useRealHttp();

  testWidgets('没人拿的时候给一个「取得控制权」', (WidgetTester t) async {
    final Rig rig = await mount(t, nobodyHolds());
    expect(find.text('取得控制权'), findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('没人拿着的时候不许画成「已过期」', (WidgetTester t) async {
    /// `engine/lease.py:126-128`：**「没有持有者」和「还剩 0 毫秒」是两件事。**
    /// 报文里前者是 `null`、后者是 `0`。合并掉的话，一台谁也没在开的狗在屏上
    /// 写着「已过期」—— 人会以为是自己的租约掉了，去等一个不会来的时刻，
    /// 而该做的只是按一下「取得控制权」。
    final Rig rig = await mount(t, nobodyHolds());
    expect(find.byKey(ControlPanel.remainKey), findsNothing,
        reason: '没人拿着就没有「还剩多久」可言');
    expect(find.textContaining('已过期'), findsNothing);
    await unmount(t, rig);
  });

  testWidgets('点「取得控制权」就去要', (WidgetTester t) async {
    final Rig rig = await mount(t, nobodyHolds());
    await t.tap(find.byKey(ControlPanel.acquireKey));
    await pumpUntil(t, () => rig.posted.contains('/api/control/acquire'),
        'POST /api/control/acquire');
    await unmount(t, rig);
  });

  testWidgets('自己拿着就走倒计时,而且是本地在走', (WidgetTester t) async {
    /// **每秒问一次狗就是每秒一个 HTTP 往返**，在热点上跟视频抢带宽 ——
    /// 而视频掉一拍的代价比倒计时差一秒大得多。
    ///
    /// 观察窗口是 **1 秒**：校准周期是 3 秒，1 秒的窗口里一次校准都不该有。
    /// 窗口再宽就会踩到校准，那时候「多问了一次」说不清是倒计时在问狗还是
    /// 校准到点了。
    final Rig rig = await mount(t, mineHolds(expiresInMs: 30000));
    final int before = remainSeconds(t);
    expect(before, inInclusiveRange(28, 30),
        reason: '倒计时的初值来自狗给的 expires_in_ms（30 秒），不是别处算出来的');
    final int asked = rig.asked;
    await t.pump(const Duration(seconds: 1));
    expect(remainSeconds(t), before - 1, reason: '本地每秒减 1');
    expect(rig.asked, asked,
        reason: '这一秒里又问了狗一次：每秒一个往返，在热点上跟视频抢带宽');
    expect(rig.dog.received.where((FakeCall r) => r.path == '/api/control'),
        isNotEmpty,
        reason: '一次都没真的问过狗的话，上面那句「没多问」是空绿的');
    await unmount(t, rig);
  });

  testWidgets('别人拿着就给「请求接手」,并且写出是谁', (WidgetTester t) async {
    final Rig rig = await mount(t, otherHolds(operator: '李四'));
    expect(find.byKey(ControlPanel.takeoverKey), findsOneWidget);
    expect(find.textContaining('李四'), findsOneWidget,
        reason: '不写出是谁的话，人只知道「拿不到」，不知道该去找谁');
    await unmount(t, rig);
  });

  testWidgets('点「请求接手」就去抢', (WidgetTester t) async {
    final Rig rig = await mount(t, otherHolds());
    await t.tap(find.byKey(ControlPanel.takeoverKey));
    await pumpUntil(t, () => rig.posted.contains('/api/control/takeover'),
        'POST /api/control/takeover');
    await unmount(t, rig);
  });

  testWidgets('名字旁边跟着一个「未核实」的短标', (WidgetTester t) async {
    /// §6.3 / `auth.OPERATOR_NOTICE`：**狗记下名字但不核实。**
    /// 光写「李四」等于替一个没验过的名字背书；真出了事，现场只会指着这块屏
    /// 说「上面写着是李四」。
    final Rig rig = await mount(t, otherHolds(operator: '李四'));
    expect(find.textContaining('未核实'), findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('notice 是原样上屏的,不是硬编的三个字', (WidgetTester t) async {
    /// **狗每次都把 `OPERATOR_NOTICE` 原样发过来**（`server.py` 的
    /// `_control_wire`）。手机这头硬编一句「未核实」的话，狗那头改了措辞
    /// （加一条限制、改一个说法），手机上还是老话 —— 而这句话是现场判断
    /// 「屏上这个名字能信到什么程度」的唯一依据。
    const String custom = '这台狗换了措辞:名字只是记账用的,别拿它当身份';
    final Rig rig = await mount(t, mineHolds(notice: custom));
    await t.tap(find.byKey(ControlPanel.noticeKey));
    await t.pump();
    expect(find.text(custom), findsOneWidget,
        reason: '狗发的那句话必须原样在这一屏上看得到');
    await unmount(t, rig);
  });

  testWidgets('抢的时候把宽限期倒计时显示出来', (WidgetTester t) async {
    final Rig rig = await mount(t, takeoverPending(graceInMs: 15000));
    expect(find.textContaining('等对方'), findsOneWidget);
    final Text g = t.widget<Text>(find.byKey(ControlPanel.graceKey));
    expect(g.data, contains('15'),
        reason: '宽限期读 grace_in_ms（相对量），不拿 grace_ends_ms 减手机的钟');
    await unmount(t, rig);
  });

  testWidgets('别人在抢我的,给一个「同意移交」', (WidgetTester t) async {
    /// **不给这个按钮，唯一的移交方式就是干等 15 秒。**
    /// 现场那 15 秒里，要开狗的人站在狗旁边，狗谁也不听。
    final Rig rig = await mount(t, mineChallenged(challenger: '王五'));
    expect(find.byKey(ControlPanel.approveKey), findsOneWidget);
    await t.tap(find.byKey(ControlPanel.approveKey));
    await pumpUntil(
        t,
        () => rig.posted.contains('/api/control/takeover/approve'),
        'POST /api/control/takeover/approve');
    await unmount(t, rig);
  });

  testWidgets('倒计时归零显示「已过期」,不显示负数', (WidgetTester t) async {
    /// 本地减到 0 就**钳在 0**。负数上屏是「app 坏了」的样子，而它其实
    /// 只是在如实描述「已经没了」。
    ///
    /// 校准周期在这条里挪到 30 秒：这条盯的是**本地钳零**，混进一次校准的话
    /// 红/绿都说不清是哪一件事。
    final Rig rig = await mount(t, mineHolds(expiresInMs: 1500),
        syncPeriod: const Duration(seconds: 30));
    await t.pump(const Duration(milliseconds: 900));
    await t.pump(const Duration(milliseconds: 900));
    await t.pump(const Duration(milliseconds: 900));
    expect(find.textContaining('-'), findsNothing);
    expect(find.textContaining('已过期'), findsOneWidget);
    await unmount(t, rig);
  });

  testWidgets('拿着的时候在续期', (WidgetTester t) async {
    /// 心跳周期必须**小于** `LEASE_HEARTBEAT_MS = 10_000`；3 秒有富余，
    /// 掉一两次也还追得回来。不续的话租约到期，人正开着狗，杆忽然灰了。
    final Rig rig = await mount(t, mineHolds());
    await pumpUntil(t, () => rig.beats > 0, '续期那一条 POST /api/control/heartbeat');
    await unmount(t, rig);
  });

  testWidgets('没拿着的时候不发心跳', (WidgetTester t) async {
    /// 续的是**自己那份租约**。别人拿着的时候还发，狗那头只会回一串错，
    /// 而屏上会挂着一句跟当前处境毫无关系的红字。
    final Rig rig = await mount(t, otherHolds());
    await pumpUntil(t, () => rig.asked >= 2, '第二次校准（说明这段时间真的在跑）');
    expect(rig.beats, 0);
    await unmount(t, rig);
  });

  testWidgets('席位满了要说人话', (WidgetTester t) async {
    /// §3.6：3 个会话封顶。第 4 个人连上来看到的必须是「满了」，
    /// 不是一个点了没反应的按钮。
    final Rig rig =
        await mount(t, nobodyHolds(remoteSessions: 3, maxSessions: 3));
    expect(find.textContaining('席位已满'), findsOneWidget);
    expect(find.textContaining('3/3'), findsOneWidget,
        reason: '写出几比几，人才知道是要等一个人下线，还是自己连错了');
    final Widget btn = t.widget(find.byKey(ControlPanel.acquireKey));
    expect((btn as TextButton).onPressed, isNull,
        reason: '点了没反应的按钮比灰掉的按钮更难懂');
    await unmount(t, rig);
  });

  testWidgets('丢了控制权要通知外面', (WidgetTester t) async {
    /// 遥控屏靠这个信号把杆变灰 —— 没有控制权还能推，推出来的每一拍都是
    /// 409，而屏幕上什么解释也没有。
    final List<bool> seen = <bool>[];
    final Rig rig = await mount(t, mineHolds(), onHeld: seen.add);
    expect(seen.last, isTrue);
    rig.dog.replies['/api/control'] = otherHolds();
    await pumpUntil(t, () => seen.last == false, '下一次校准之后那声「没了」');
    await unmount(t, rig);
  });

  testWidgets('狗回错的时候屏上是人话,异常原文只进日志', (WidgetTester t) async {
    /// 跟 `teleop_page.dart` 的 `_human` 一个规矩：**异常原文绝不上屏。**
    /// 屏幕上要的是「该做什么」，Dart 的异常文本回答不了这个。
    final Rig rig = await mount(t, mineHolds());
    rig.dog.statusCodes['/api/control'] = 500;
    rig.dog.replies['/api/control'] = <String, dynamic>{
      'error': 'ZeroDivisionError',
      'detail': 'Traceback (most recent call last)',
    };
    await pumpUntil(t, () => find.textContaining('没问到').evaluate().isNotEmpty,
        '出错之后那句人话');
    expect(find.textContaining('Traceback'), findsNothing);
    expect(find.textContaining('ZeroDivisionError'), findsNothing);
    await unmount(t, rig);
  });

  testWidgets('点「交还」就把控制权还回去', (WidgetTester t) async {
    /// 交还是**幂等**的（`server.py` 的 `_control_release`）：app 退出时
    /// 无脑发一次就行。给按钮是因为人走开之前该能主动放手，而不是让下一个
    /// 人干等 30 秒 TTL。
    final Rig rig = await mount(t, mineHolds());
    await t.tap(find.byKey(ControlPanel.releaseKey));
    await pumpUntil(t, () => rig.posted.contains('/api/control/release'),
        'POST /api/control/release');
    await unmount(t, rig);
  });
}
