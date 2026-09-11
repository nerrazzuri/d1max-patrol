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

/// 别人拿着，**在抢的那个人就是我**（挂账 61）。
///
/// `challenging` 是狗那头按会话 ref 算好发过来的一位，跟 `mine` 是对称的
/// 一对（`server.py` 的 `_control_wire`）。
Map<String, dynamic> takeoverByMe({int graceInMs = 15000}) =>
    <String, dynamic>{
      ...takeoverPending(graceInMs: graceInMs),
      'challenging': true,
    };

/// 别人拿着，在抢的是**另一台报着同一个名字的手机**（挂账 61）。
///
/// **除了 `challenging` 这一位，这份报文跟 [takeoverByMe] 一个字都不差** ——
/// 那正是这条挂账要说的事：`challenger` 只带 `{ref, operator}`，而狗记的操作
/// 员姓名**不核实**，现场两个人都报「张三」（或者同一个人手上两台手机）是完
/// 全正常的局面。手机这头连自己的会话 ref 都没有，拿 `challengerRef` 或者
/// `challengerOperator` 去比一律比不出来。所以这两份夹具故意连 ref 都一样：
/// 屏上分得开，就只可能是靠 `challenging` 分开的。
Map<String, dynamic> takeoverBySameName({int graceInMs = 15000}) =>
    <String, dynamic>{
      ...takeoverPending(graceInMs: graceInMs),
      'challenging': false,
    };

/// 把 [lease] 里的 `heartbeat_ms` 换成 [ms]（给 `null` 就是狗发了个 JSON null）。
///
/// 校准周期是**从这个字段算出来的**（`engine/lease.py` 明写客户端不该硬编），
/// 所以要验的那几种数得能一条条摆进去。
Map<String, dynamic> withHeartbeatMs(Map<String, dynamic> lease, int? ms) =>
    <String, dynamic>{...lease, 'heartbeat_ms': ms};

/// 狗压根没发 `heartbeat_ms` 这一项（老版本的狗）。**跟发 `null` 不是一回事**，
/// 两条路都得走到。
Map<String, dynamic> withoutHeartbeatMs(Map<String, dynamic> lease) =>
    <String, dynamic>{...lease}..remove('heartbeat_ms');

/// 把 `expires_ms`（那个**绝对**时刻）换成 [ms]。
///
/// 这个字段在报文里是给审计和日志对时间用的，**界面一眼都不该看它**。
/// 把它摆成一个荒唐的值，就是在问「这块屏是不是偷偷拿它减了手机的钟」。
Map<String, dynamic> withExpiresMs(Map<String, dynamic> lease, int ms) =>
    <String, dynamic>{...lease, 'expires_ms': ms};

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
  Future<Map<String, dynamic>> get(String path, [Duration? deadline]) {
    calls.add(path);
    return super.get(path, deadline);
  }

  @override
  Future<Map<String, dynamic>> post(String path,
      [Object? body, Duration? deadline]) {
    calls.add(path);
    return super.post(path, body, deadline);
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

  /// 狗**真的收到**了几次「谁拿着」。
  ///
  /// 跟 [asked] 不是一回事：[asked] 数的是发出去了几次（客户端那头），这个数的
  /// 是狗那头收到了几次。要证「这一段里确实发生过一次成功的校准」只能数这个 ——
  /// 发出去而没收到的那种，`_apply()` 根本没跑过。
  int get gets =>
      dog.received.where((FakeCall r) => r.path == '/api/control').length;
}

/// 起一只假狗，把面板挂上去，等到它拿到第一份租约。
///
/// **默认不走 `unlock`。** 这个文件要证的事大多跟换 token 无关，而每多一次真
/// 的往返，第一份租约就晚到几百毫秒 —— 而「本地在走」那条测试的窗口是按假时
/// 钟数的，前面耗掉的每一毫秒都从那个窗口里扣。要验请求头带没带 token 的那
/// 一条给 [unlockPin]，它自己付这份钱。
///
/// [syncPeriod] 不给就是**真机上那条路**：周期从狗发的 `heartbeat_ms` 算。
Future<Rig> mount(WidgetTester t, Map<String, dynamic> lease,
    {void Function(bool)? onHeld,
    Duration? syncPeriod,
    Duration tickPeriod = const Duration(seconds: 1),
    String? unlockPin}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/control'] = lease;
  dog.replies['/api/control/acquire'] = lease;
  dog.replies['/api/control/release'] = nobodyHolds();
  dog.replies['/api/control/takeover'] = lease;
  dog.replies['/api/control/takeover/approve'] = nobodyHolds();
  dog.replies['/api/control/heartbeat'] = lease;
  final SpyClient c = SpyClient(dog.baseUrl);
  if (unlockPin != null) {
    // `unlock` 是两步（拿 nonce、交证明），两条路都得备好回复；而且它是真的
    // 网络 I/O，假时钟上推不动，得进 `runAsync`。
    dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
    dog.replies['/api/auth'] = <String, dynamic>{
      'token': fakeToken,
      'operator': '张三',
      'operator_verified': false,
      'readonly': false,
    };
    await t.runAsync(() => c.unlock(unlockPin, operator: '张三'));
  }
  await t.pumpWidget(wrap(ControlPanel(
    client: c,
    onHeld: onHeld,
    syncPeriod: syncPeriod,
    tickPeriod: tickPeriod,
  )));
  final Rig rig = Rig(dog, c);
  await pumpUntil(t, () => settled(t), '面板拿到第一份租约（三个按钮之一露出来）');
  return rig;
}

/// 假狗换给我们的那个 token。断言请求头的时候要一字不差地对上它。
const String fakeToken = 'tok-abc';

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
///
/// **`-?` 那一段不是装饰。** 以前写的是 `\d+`：屏上写着「还剩 -31905341 秒」，
/// 它解出来的是 31905341 —— 号被吃掉了，于是「倒计时减了手机的钟」这件事在
/// 范围断言那儿看着像「数大了一点」。复审的 CUT-A 就栽在这上面。
int remainSeconds(WidgetTester t) {
  final Text w = t.widget<Text>(find.byKey(ControlPanel.remainKey));
  final RegExpMatch? m = RegExp(r'-?\d+').firstMatch(w.data ?? '');
  if (m == null) fail('倒计时那一格里没有数：「${w.data}」');
  return int.parse(m.group(0)!);
}

/// 「等对方回应，还剩 N 秒」里的那个 N。
///
/// **正则是锚定的（`^...$`），不是子串。** 以前那条断言写的是
/// `g.data contains '15'`：宽限期发 150000（还剩 150 秒）它照样绿 ——
/// 「150」里有「15」。子串断言在数字上永远分不清 15 和 150、也分不清
/// 「还剩 15 秒」和「15 分钟前」。
int graceSeconds(WidgetTester t) {
  final Text w = t.widget<Text>(find.byKey(ControlPanel.graceKey));
  final RegExpMatch? m =
      RegExp(r'^等对方回应，还剩 (-?\d+) 秒$').firstMatch(w.data ?? '');
  if (m == null) fail('宽限期那一格不是「等对方回应，还剩 N 秒」：「${w.data}」');
  return int.parse(m.group(1)!);
}

/// **某个话槽里的那句话**（空槽读出空串）。
///
/// 屏上那三行长得一模一样，`find.text(...)` 只答得出「这句话上没上屏」，
/// 答不出「它落进了哪个槽」—— 复审把 `_act()` 的失败改写进 `_syncTrouble`，
/// 全仓 155 条一条没红。三个槽 × 两个方向一共六种写错的方式，一条条补测试
/// 是补不完的；按 key 读才是由构造保证。
///
/// 空槽也占着自己的 key（画成带 key 的 `SizedBox.shrink()`），所以「这一格
/// 是空的」跟「这一格里是那句话」是同一种断法。
String troubleIn(WidgetTester t, Key slot) {
  final Finder f =
      find.descendant(of: find.byKey(slot), matching: find.byType(Text));
  if (f.evaluate().isEmpty) return '';
  return t.widget<Text>(f.first).data ?? '';
}

/// 三个槽里现在一共有几句话。**等的是「屏上出了话」，不是「我那一格出了话」**：
/// 只等自己那一格的话，写错格的时候这条要靠 `pumpUntil` 超时才红，红出来的是
/// 一句「等了 10 秒」，看不出话落到哪儿去了。
int troubleCount(WidgetTester t) => <Key>[
      ControlPanel.syncTroubleKey,
      ControlPanel.beatTroubleKey,
      ControlPanel.actTroubleKey,
    ].where((Key k) => troubleIn(t, k).isNotEmpty).length;

/// 三个槽一次读完。**每条测试都把三格全断一遍** —— 只断自己那一格的话，
/// 「话写对了地方」证得了，「话没同时漏进别的地方」证不了。
void expectTroubles(WidgetTester t,
    {String sync = '', String beat = '', String act = '', String lost = ''}) {
  expect(troubleIn(t, ControlPanel.syncTroubleKey), sync,
      reason: '校准那一格里该是「$sync」');
  expect(troubleIn(t, ControlPanel.beatTroubleKey), beat,
      reason: '心跳那一格里该是「$beat」');
  expect(troubleIn(t, ControlPanel.actTroubleKey), act,
      reason: '按钮那一格里该是「$act」');
  // 第四格（必修 6：租约掉了）。**加进来是必须的**：不断它的话，
  // 上面每一条「屏上一个字都没有」的断言都漏掉了这一格。
  expect(troubleIn(t, ControlPanel.leaseLostKey), lost,
      reason: '「控制权过期了」那一格里该是「$lost」');
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

  // ------------------------------------------------------------------------
  // 「本地在走」原来是**一条**测试背两件事（既证「没多问狗」又证「数在减」），
  // 于是它红出来的时候说不清是哪一件坏了 —— 复审只把 `syncPeriod` 这个**测试
  // 输入**改成 1 秒（生产代码一个字没动），就得到了跟真刀一模一样的那句红。
  // 拆成两条，各背一件。

  testWidgets('倒计时是本地在减的:这一秒里没有多问狗一次', (WidgetTester t) async {
    /// **每秒问一次狗就是每秒一个 HTTP 往返**，在热点上跟视频抢带宽 ——
    /// 而视频掉一拍的代价比倒计时差一秒大得多。
    ///
    /// **这条只管「tick 有没有顺手去问狗」。** 数字减没减归下一条。
    ///
    /// 两个周期都是显式注入的，为的是让这条红出来的时候只有一个解释：
    /// - 校准挪到 60 秒开外 —— 这 1 秒的窗口里**没有任何一次校准该到点**，
    ///   于是窗口里多出来的每一次 `/api/control` 都只可能是 tick 发的；
    /// - tick 调成 250 毫秒 —— 这 1 秒里跳 **4** 次。tick 真去问狗的话多出
    ///   4 次，而「校准周期被人配短了」那种假象最多多出 1 次：**两种毛病红出
    ///   来的数不一样**，不至于像以前那样一句话读不出是哪件事。
    ///
    /// 这对数是靠自洽断言钉住的，见下面第一句 —— 复审只把 `tickPeriod` 改回
    /// 1 秒（生产代码一个字没动），这条就跟真刀红成一模一样，而且 reason 里
    /// 那句「跳了 4 次」当场变成假话。
    const Duration window = Duration(seconds: 1);
    const Duration tick = Duration(milliseconds: 250);
    const Duration farAway = Duration(seconds: 60);
    const int ticksInWindow = 4;
    expect(window.inMilliseconds ~/ tick.inMilliseconds, ticksInWindow,
        reason: '这条的分辨力全靠「tick 在窗口里跳 $ticksInWindow 次」：'
            '把 tickPeriod 调回 1 秒，它红出来的数就跟「校准被配短了」一模一样，'
            'reason 里那句「跳了 4 次」还会变成假话');
    expect(farAway, greaterThan(window),
        reason: '校准得挪到窗口外面去，不然窗口里多出来的那次说不清是谁发的');
    final Rig rig = await mount(t, mineHolds(expiresInMs: 30000),
        syncPeriod: farAway, tickPeriod: tick);
    final int asked = rig.asked;
    final int before = remainSeconds(t);
    await t.pump(window);
    expect(rig.asked, asked,
        reason: '这一秒里 tick 跳了 $ticksInWindow 次，'
            '而 /api/control 多出了 ${rig.asked - asked} 次：'
            '每一次都是一个往返，在热点上跟视频抢带宽');
    // **先证 tick 真的跳过。** 不证的话这条是空绿的：把 tick 定时器整条删掉，
    // 「没多问狗」在一个「什么都不发生」的世界里当然成立（复审的 CUT-R1）。
    // 窗口正好 1 秒、`_secondsOf` 是向上取整，所以这个差必须**正好是 1**。
    expect(before - remainSeconds(t), 1,
        reason: 'tick 在这一秒里一共把倒计时推了 ${before - remainSeconds(t)} 秒：'
            '推 0 秒就是 tick 根本没跳（定时器没了？）—— 不跳的东西当然不会去'
            '问狗，上面那句「没多问」就是空绿的');
    expect(rig.dog.received.where((FakeCall r) => r.path == '/api/control'),
        isNotEmpty,
        reason: '一次都没真的问过狗的话，上面那句「没多问」是空绿的');
    await unmount(t, rig);
  });

  testWidgets('倒计时是本地在减的:数字自己往下走,不等狗回话', (WidgetTester t) async {
    /// **这条只管「数字在本地减」。** 问没问狗归上一条。
    ///
    /// 窗口里一次真的往返都完成不了（假时钟上 I/O 推不动），所以这一秒里数字
    /// 还能少 1，只可能是本地减出来的。
    final Rig rig = await mount(t, mineHolds(expiresInMs: 30000));
    final int before = remainSeconds(t);
    expect(before, inInclusiveRange(28, 30),
        reason: '倒计时的初值来自狗给的 expires_in_ms（30 秒），不是别处算出来的');
    await t.pump(const Duration(seconds: 1));
    expect(remainSeconds(t), before - 1, reason: '本地每秒减 1');
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
    ///
    /// **断的是常量本身，不是抄一份「未核实」的字面量。** 抄字面量的那天起
    /// 改措辞在测试里就不跟着走了；更坏的是 [unverifiedMark]（挂在名字后面）
    /// 和 [nameNotCheckedMark]（屏上没名字时挂的）原本只差一个字，子串断言
    /// 会把两个标混着匹配 —— 那时候「findsNothing」那种方向直接变成永真。
    final Rig rig = await mount(t, otherHolds(operator: '李四'));
    expect(find.text(unverifiedMark), findsOneWidget);
    expect(find.text(nameNotCheckedMark), findsNothing,
        reason: '屏上有名字，挂的该是名字后面那个短标，不是「屏上没名字」那个');
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
    /// **断的是那个数本身，不是「文本里有 15 这两个字符」。**
    /// 子串断言在数字上分不清 15 和 150：宽限期发 150000，屏上写着「还剩 150
    /// 秒」，`contains('15')` 照样绿。
    final Rig rig = await mount(t, takeoverPending(graceInMs: 15000));
    expect(find.textContaining('等对方'), findsOneWidget);
    expect(graceSeconds(t), inInclusiveRange(14, 15),
        reason: '宽限期读 grace_in_ms（相对量，狗给的是 15000），'
            '不拿 grace_ends_ms 减手机的钟；本地可能已经先减掉一格，所以容 14');
    await unmount(t, rig);
  });

  testWidgets('我在抢的时候屏上说的是「你」不是「有人」', (WidgetTester t) async {
    /// 挂账 61。**这一行是旁观者和排队的人唯一分得开的地方。**
    final Rig rig = await mount(t, takeoverByMe());
    expect(find.byKey(ControlPanel.challengeMineKey), findsOneWidget);
    expect(find.byKey(ControlPanel.challengeOtherKey), findsNothing);
    await unmount(t, rig);
  });

  testWidgets('同名的另一台手机在抢,屏上不能说是我', (WidgetTester t) async {
    /// 挂账 61 那个原始场景：现场两个人都报「张三」。这份报文跟上一条**只差
    /// `challenging` 一位**，所以这两条一起才说明问题 —— 单看哪一条，一个
    /// 「永远画『你』」或者「永远画『有人』」的实现都能过。
    ///
    /// 画错的后果不是一句话说错：旁观者以为自己已经在排队了，于是干等一个
    /// 不会属于他的宽限期 —— 而这 15 秒他**什么也按不了**（别人拿着、又有
    /// 人在抢，`_buttons()` 只给一个「请求接手」，`grace != null` 时它是灰
    /// 的），等到宽限期走完，控制权落进那个真排了队的人手里，他还得从头再
    /// 排一次。这一位画反，赔进去的就是这一整个宽限期。
    final Rig rig = await mount(t, takeoverBySameName());
    expect(find.byKey(ControlPanel.challengeOtherKey), findsOneWidget);
    expect(find.byKey(ControlPanel.challengeMineKey), findsNothing);
    await unmount(t, rig);
  });

  testWidgets('没人在抢的时候这一行根本不画', (WidgetTester t) async {
    /// 「有人正在要接管」常驻的话，这一行就从「一件要留意的事」变成了背景 ——
    /// 而它存在的全部理由是让人在**真有人在抢**的那一刻抬头看一眼。
    final Rig rig = await mount(t, otherHolds());
    expect(find.byKey(ControlPanel.challengeMineKey), findsNothing);
    expect(find.byKey(ControlPanel.challengeOtherKey), findsNothing);
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
    ///
    /// **顺带钉住方向**：校准那条路的话只许落进校准那一格。三行长得一模一样，
    /// 按 key 断才分得出来（见 [troubleIn]）。
    final Rig rig = await mount(t, mineHolds());
    rig.dog.statusCodes['/api/control'] = 500;
    rig.dog.replies['/api/control'] = <String, dynamic>{
      'error': 'ZeroDivisionError',
      'detail': 'Traceback (most recent call last)',
    };
    await pumpUntil(t, () => troubleCount(t) > 0, '出错之后那句人话');
    expectTroubles(t, sync: syncTroubleHint);
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

  // ------------------------------------------------------ 校准周期听狗的

  test('heartbeat_ms 换算成校准周期:取三分之一,钳在 1 到 10 秒之间', () {
    /// `engine/lease.py` 的 `LeaseState` 明写「`ttl_ms`/`heartbeat_ms` 跟着
    /// 一起发出去 —— **客户端不该把心跳间隔硬编在自己那头**」。
    ///
    /// 取三分之一：掉两次还追得回来，掉三次才到期。两头都钳住，是因为狗那边
    /// 配出一个荒唐的数不是不可能的事，而手机这头不该跟着把热点占满（太密），
    /// 也不该稀到守不住租约（太稀）。
    expect(ControlPanel.syncPeriodFor(10000), const Duration(milliseconds: 3333),
        reason: '出厂值 LEASE_HEARTBEAT_MS = 10_000，三分之一是 3.3 秒');
    expect(ControlPanel.syncPeriodFor(30000), maxSyncPeriod,
        reason: '狗说 30 秒，周期该跟着变长 —— 钳在 10 秒的上限');
    expect(ControlPanel.syncPeriodFor(600), minSyncPeriod,
        reason: '狗说 0.6 秒也不跟：一秒一个往返就是在跟视频抢热点带宽');
    expect(ControlPanel.syncPeriodFor(null), defaultSyncPeriod,
        reason: '老版本的狗没发这一项，回落到 3 秒');
    expect(ControlPanel.syncPeriodFor(0), defaultSyncPeriod,
        reason: '0 是个不合法的数，不是「一直问」');
    expect(ControlPanel.syncPeriodFor(-1), defaultSyncPeriod,
        reason: '负数同上，别拿它去 new 一个负周期的 Timer');
  });

  testWidgets('校准周期听狗的:狗说心跳 30 秒,这 4 秒里就不该再问', (WidgetTester t) async {
    /// 面板**没有**拿到 `syncPeriod`（真机上就是这条路），周期只能从狗发的
    /// `heartbeat_ms` 算：30000 → 三分之一是 10 秒（钳在上限）。
    /// 硬编 3 秒的话，这 4 秒的窗口里会多出一次 `/api/control`。
    ///
    /// **「不该再问」是个否定命题**：校准定时器整条删掉它照样成立（复审的
    /// CUT-R5 就是这么把它照绿的）。所以窗口之后还得再等一个整周期，证明
    /// 校准**确实还活着、而且就在狗说的那个点上到点** —— 下界和上界一起断，
    /// 这条才守得住「听狗的」这件事。
    const int heartbeatMs = 30000;
    const Duration window = Duration(seconds: 4);
    final Duration period = ControlPanel.syncPeriodFor(heartbeatMs);
    expect(period, greaterThan(window),
        reason: '窗口得落在一个周期之内，不然「这 4 秒里不该再问」自己就不成立');
    expect(defaultSyncPeriod, lessThan(window),
        reason: '窗口还得比硬编的回落值长，不然硬编 3 秒也能悄悄绿过去');
    final Rig rig = await mount(t, withHeartbeatMs(mineHolds(), heartbeatMs));
    final int asked = rig.asked;
    await t.pump(window);
    expect(rig.asked, asked,
        reason: '狗说 10 秒续一次，手机还按 3 秒问 —— 周期是硬编的，没听狗的');
    await t.pump(period);
    expect(rig.asked, greaterThan(asked),
        reason: '又过了一整个周期（一共 ${(window + period).inSeconds} 秒）还是一次没问：'
            '校准根本没在跑 —— 那上面那句「不该再问」是在一个「压根不会问」的'
            '世界里成立的，是空绿的');
    await unmount(t, rig);
  });

  testWidgets('校准周期听狗的:狗没发 heartbeat_ms 就回落到 3 秒', (WidgetTester t) async {
    /// 回落**不是「不校准了」**：不校准的话，控制权被别人接管走了这块屏
    /// 也不知道，杆还亮着。
    final Rig rig = await mount(t, withoutHeartbeatMs(mineHolds()));
    final int asked = rig.asked;
    await t.pump(const Duration(milliseconds: 2500));
    expect(rig.asked, asked, reason: '2.5 秒就问了 —— 回落值比 3 秒短');
    await t.pump(const Duration(seconds: 1));
    expect(rig.asked, asked + 1,
        reason: '3.5 秒了还没问 —— 回落值比 3 秒长，或者压根不校准了');
    await unmount(t, rig);
  });

  // ------------------------------------------------------ 三条路各说各的话

  testWidgets('心跳一直不通:那句话不许被后面成功的校准抹掉', (WidgetTester t) async {
    /// Task 11 的 R-1 在这儿原样复发了一次：`_apply()` 无条件清空那个话槽，
    /// 于是心跳一直失败时，那句「租约没续上」会被三秒后一次成功的校准抹掉 ——
    /// 人只看见红字闪一下，而**那正是最该被看见的一句**：租约续不上，再过
    /// 一会儿控制权自己就掉了，杆会在人正开着狗的时候变灰。
    ///
    /// **闪着的字比没有字更糟**：人当它是花屏，于是真断了也不当回事。
    ///
    /// 这条测试非走到那条路不可 —— 心跳失败、校准成功，两件事同时发生。
    /// Task 11 那次的原始 bug 之所以没被任何测试看见，就是因为那条测试从头
    /// 到尾没推过杆，根本没碰到「成功那一下会不会抹掉别人的话」。
    final Rig rig = await mount(t, mineHolds());
    rig.dog.statusCodes['/api/control/heartbeat'] = 500;
    await pumpUntil(t, () => troubleCount(t) > 0, '心跳失败之后那句「租约没续上」');
    // 心跳那条路的话只许落进心跳那一格。
    expectTroubles(t, beat: renewTroubleHint);
    final int gotBefore = rig.gets;
    int missing = 0;
    for (int i = 0; i < 40; i++) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 5)));
      await t.pump(const Duration(milliseconds: 200));
      if (troubleIn(t, ControlPanel.beatTroubleKey) != renewTroubleHint) {
        missing++;
      }
    }
    // **先证这一段里真的有成功的校准**，否则「没被抹掉」是空绿的。
    expect(rig.gets - gotBefore, greaterThanOrEqualTo(2),
        reason: '这 8 秒里狗只被问到了 ${rig.gets - gotBefore} 次：'
            '那这条测试根本没碰到「一次成功的校准会不会抹掉心跳那句」');
    expect(missing, 0,
        reason: '心跳还断着，那句话却在 40 次采样里没了 $missing 次：'
            '校准成功那一下把别人写的话也清了');
    await unmount(t, rig);
  });

  testWidgets('按钮那句话落在按钮那一格,不许被后面成功的校准抹掉',
      (WidgetTester t) async {
    /// **B-1**：三格分离上一轮只守住了「校准别抹心跳」一个方向。把 `_act()`
    /// 的失败改写进 `_syncTrouble`，全仓 155 条一条不红 —— 而后果正是 Task 11
    /// 的 R-1 在按钮方向上原样复发：人点了「请求接手」，狗回了个错，屏上写出
    /// 那句话，三秒后一次**成功的后台校准**把它悄悄抹掉；人只看见自己点了一下、
    /// 什么都没发生。
    ///
    /// 这条同时断两件事，缺一件都不够：
    /// - **落在哪一格**（按 key 断，三格全断一遍）—— 六个方向由构造保证；
    /// - **活不活得过一次成功的校准** —— 时间上的那一维。
    final Rig rig = await mount(t, otherHolds());
    rig.dog.statusCodes['/api/control/takeover'] = 500;
    await t.tap(find.byKey(ControlPanel.takeoverKey));
    await pumpUntil(t, () => troubleCount(t) > 0, '「请求接手」失败之后那句话');
    expectTroubles(t, act: takeoverFailedHint);
    final int gotBefore = rig.gets;
    int missing = 0;
    for (int i = 0; i < 40; i++) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 5)));
      await t.pump(const Duration(milliseconds: 200));
      if (troubleIn(t, ControlPanel.actTroubleKey) != takeoverFailedHint) {
        missing++;
      }
    }
    // **先证这一段里真的有成功的校准**，否则「没被抹掉」是空绿的。
    expect(rig.gets - gotBefore, greaterThanOrEqualTo(2),
        reason: '这 8 秒里狗只被问到了 ${rig.gets - gotBefore} 次：'
            '那这条根本没碰到「一次成功的校准会不会抹掉按钮那句」');
    expect(missing, 0,
        reason: '按钮那句话在 40 次采样里没了 $missing 次：'
            '校准成功那一下把别人写的话也清了');
    await unmount(t, rig);
  });

  testWidgets('交还失败那句话不许被后面成功的续期抹掉', (WidgetTester t) async {
    /// 上一条测出来的是「校准别抹按钮那句」；这条是它的对偶：
    /// **续期（`_beat()`）别抹按钮那句**。前两轮把「往哪一格写」的六个方向
    /// 用 key 锁死了，但「把哪一格清掉」是另一根轴 —— 跟续期有关的两格
    /// （这条 + 下面那一段）复审之前一条测试都没守住：现有的两条相关测试
    /// 都跑在 `otherHolds()`（别人拿着）的局面上，`mine == false` 时
    /// `_sync()` 里那句 `if (_lease?.mine == true) unawaited(_beat());`
    /// 永不成立 —— 续期那条路一次都没跑过。局面必须换成 `mineHolds()`。
    final Rig rig = await mount(t, mineHolds());
    rig.dog.statusCodes['/api/control/release'] = 500;
    await t.tap(find.byKey(ControlPanel.releaseKey));
    await pumpUntil(t, () => troubleCount(t) > 0, '「交还」失败之后那句话');
    expectTroubles(t, act: releaseFailedHint);
    final int beatBefore = rig.beats;
    int missing = 0;
    for (int i = 0; i < 40; i++) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 5)));
      await t.pump(const Duration(milliseconds: 200));
      if (troubleIn(t, ControlPanel.actTroubleKey) != releaseFailedHint) {
        missing++;
      }
      // 顺带守住另一个方向：这个局面下续期是通的，心跳那一格该一直是空的 ——
      // 它自己没有理由变成别的样子。真正堵住 CUT-13b 的是下面那一段
      // （心跳先断出话、再按一下按钮，看那句话还在不在）；这里只是多一层
      // 「本来就该是空的」的日常保底。
      if (troubleIn(t, ControlPanel.beatTroubleKey) != '') {
        fail('心跳那一格不该有话，却冒出来了：'
            '「${troubleIn(t, ControlPanel.beatTroubleKey)}」');
      }
    }
    // 先证这一段里真的有成功的续期,否则「没被抹掉」是空绿的。
    expect(rig.beats - beatBefore, greaterThanOrEqualTo(2),
        reason: '这 8 秒里只续期成功了 ${rig.beats - beatBefore} 次：'
            '那这条根本没碰到「一次成功的续期会不会抹掉按钮那句」，'
            '底下那句「话没被抹掉」就是空绿的');
    expect(missing, 0,
        reason: '续期成功那一下把按钮写的话也清了');

    // 再堵 CUT-13b 那个方向：按钮（`_act()`）不许把心跳那一格的话碰掉。
    // 上面那段窗口里心跳一直是通的，从没冒出过话可清 —— 单靠「一直是空的」
    // 这句断言堵不住这一刀：CUT-13b 加的那行本身是 `if (_beatTrouble
    // .isNotEmpty) ...`，心跳那格本来就是空的时候，这行按不按都一个样，
    // 空绿。真要逼它现形，得先让心跳那格真的有话、再按一下按钮，看那句话
    // 还在不在。
    rig.dog.statusCodes['/api/control/heartbeat'] = 500;
    await pumpUntil(
        t,
        () => troubleIn(t, ControlPanel.beatTroubleKey) == renewTroubleHint,
        '心跳失败之后那句「租约没续上」');
    await t.tap(find.byKey(ControlPanel.releaseKey));
    await t.pump();
    expect(troubleIn(t, ControlPanel.beatTroubleKey), renewTroubleHint,
        reason: '按一下「交还」就把心跳那句「租约没续上」带没了：'
            '按钮不该碰心跳那一格');
    await unmount(t, rig);
  });

  testWidgets('局面一换,按钮那句过时的话就收掉', (WidgetTester t) async {
    /// 「只有下一次按按钮才清」太死：持有者松手之后，面板已经画成「没人拿着」、
    /// 「请求接手」那个按钮也没了，屏上却还留着「控制权在别人手里：用「请求
    /// 接手」…」—— 一句既过时又跟屏上其余的字自相矛盾的指示。
    final Rig rig = await mount(t, otherHolds());
    rig.dog.statusCodes['/api/control/takeover'] = 500;
    await t.tap(find.byKey(ControlPanel.takeoverKey));
    await pumpUntil(t, () => troubleCount(t) > 0, '「请求接手」失败之后那句话');
    expectTroubles(t, act: takeoverFailedHint);
    // 对方松手了：下一次校准把局面换成「没人拿着」。
    rig.dog.replies['/api/control'] = nobodyHolds();
    await pumpUntil(t, () => find.byKey(ControlPanel.acquireKey).evaluate().isNotEmpty,
        '局面换成「没人拿着」');
    expectTroubles(t);
    await unmount(t, rig);
  });

  testWidgets('按按钮不许把校准和续期往后推', (WidgetTester t) async {
    /// **SF-3**：`_apply()` 是 `_sync()`、`_beat()` 和 `_act()` 三条路共用的。
    /// 每次 `_apply()` 都把校准定时器 `cancel()` 再 `periodic()` 的话，定时器
    /// 永远从零重新数 —— 人按得比周期勤，校准就永远轮不到；而续期只由一次
    /// **成功的校准**触发，于是租约续期跟着一起被推后，按得够勤就永远续不上，
    /// 人正开着狗，杆忽然灰了。
    const Duration period = Duration(seconds: 3);
    const Duration between = Duration(seconds: 1);
    const int taps = 12;
    expect(between, lessThan(period),
        reason: '按钮得按得比校准周期勤，不然「往后推」这件事根本发生不了');
    final Rig rig = await mount(t, mineHolds(), syncPeriod: period);
    // 交还回的还是「在我手上」：这条盯的是定时器，不是局面变化。
    rig.dog.replies['/api/control/release'] = mineHolds();
    final int asked = rig.asked;
    final int beat = rig.beats;
    for (int i = 0; i < taps; i++) {
      await t.tap(find.byKey(ControlPanel.releaseKey));
      await pumpUntil(
          t,
          () =>
              rig.posted.where((String p) => p == '/api/control/release').length >
              i,
          '第 ${i + 1} 次交还回到面板');
      await t.pump(between);
    }
    final int expected = (taps * between.inSeconds) ~/ period.inSeconds;
    expect(rig.asked - asked, greaterThanOrEqualTo(expected - 1),
        reason: '这 ${taps * between.inSeconds} 秒里校准只到点了 ${rig.asked - asked} 次'
            '（${period.inSeconds} 秒一次，该有 $expected 次左右）：'
            '每按一次按钮就把定时器从零重起，人按得比周期勤，校准和续期就永远轮不到');
    expect(rig.beats, greaterThan(beat),
        reason: '这一段里一次都没续上租约：续期只由成功的校准触发，校准被推后，'
            '租约就跟着一起掉');
    await unmount(t, rig);
  });

  // ------------------------------------------------------ 那句 notice 的入口

  testWidgets('没人拿着的时候,那句 notice 也够得着', (WidgetTester t) async {
    /// 派工令要求「全文必须在这一屏上能看到」。以前这个入口只在**有人拿着**
    /// 的时候才画 —— 而没人拿着恰恰是人马上要按「取得控制权」、把自己那个
    /// 不核实的名字写进狗的账上的时刻：那一刻看不到这句话，等于它在最该被
    /// 读到的时候藏着。
    final Rig rig = await mount(t, nobodyHolds());
    expect(find.text(unverifiedMark), findsNothing,
        reason: '屏上一个名字都没有，还挂着名字后面那个短标的话，'
            '标的是一个没显示出来的名字');
    expect(find.text(nameNotCheckedMark), findsOneWidget,
        reason: '入口没了的话，上面那句 findsNothing 是空绿的：'
            '屏上本来就什么标都没有');
    await t.tap(find.byKey(ControlPanel.noticeKey));
    await t.pump();
    expect(find.byKey(ControlPanel.noticeFullKey), findsOneWidget);
    expect(find.text(nobodyHolds()['notice'] as String), findsOneWidget,
        reason: '狗发的那句话（夹具里那一句）得原样在这一屏上看得到');
    await unmount(t, rig);
  });

  testWidgets('自己拿着的时候,把狗账上记的那个名字写出来', (WidgetTester t) async {
    /// 以前这一态写的是「控制权在你手上」（屏上没有名字），旁边却挂着
    /// 「（未核实）」—— 那个标标的是一个没显示出来的名字。
    ///
    /// 补的是名字，不是撤掉标：「在你手上」说的是**这台手机**，而人要核对的
    /// 是**狗账上记的是谁**。两者对不上的时候（换过人、换过 PIN、别人拿着
    /// 你的手机解过锁），正是最该看出来的一刻。
    final Rig rig = await mount(t, mineHolds());
    expect(find.textContaining('张三'), findsOneWidget,
        reason: '狗账上记的名字（holder.operator）没写出来，'
            '旁边那个「未核实」就没有着落');
    expect(find.text(unverifiedMark), findsOneWidget);
    expect(find.text(nameNotCheckedMark), findsNothing,
        reason: '屏上有名字了，该挂的是名字后面那个短标');
    await unmount(t, rig);
  });

  // ------------------------------------------------------ token 与手机的钟

  testWidgets('面板发出去的每一条都带着 Authorization: Bearer', (WidgetTester t) async {
    /// 狗那头会动腿的接口全都要控制权，而控制权那几条接口全都要身份
    /// （`app/control.py`）。token 漏在某一条上的表现是「那一条 401」——
    /// 而面板对 401 的说法是「退出去重新输 PIN」，人会去重输一个本来就对的
    /// PIN，查的是完全另一件事。
    ///
    /// **token 走请求头，不走查询串**（`auth.py`：URL 会进日志、进历史）。
    final Rig rig = await mount(t, mineHolds(), unlockPin: '864209');
    await pumpUntil(t, () => rig.beats > 0, '续期那一条 POST（挑一条 POST 一起验）');
    final List<FakeCall> control = rig.dog.received
        .where((FakeCall r) => r.path.startsWith('/api/control'))
        .toList();
    expect(control, isNotEmpty, reason: '一条都没发出去的话，下面那句是空绿的');
    for (final FakeCall r in control) {
      expect(r.auth, 'Bearer $fakeToken',
          reason: '${r.method} ${r.path} 没带 token：这一条在真狗上是 401');
    }
    await unmount(t, rig);
  });

  testWidgets('手机的钟差几十年也不影响倒计时:expires_ms 落在过去', (WidgetTester t) async {
    /// 狗上没有 NTP（`engine/lease.py`），它的 RTC 跟手机的钟对不上是常态 ——
    /// 差几十年也不是稀奇事（RTC 电池没电就回到出厂那一天）。
    /// 倒计时读的必须是 `expires_in_ms` 这个**相对量**。
    ///
    /// **这条盯的是「钳零那一行盖不住的那一半」。** 减出来是个巨大的负数时，
    /// `_onTick` 的钳零会把它一拍按成 0、显示「已过期」，反而把「减错了钟」
    /// 盖住 —— 所以这里断的是**减之前那一刻屏上的数**。
    final Rig rig = await mount(
        t, withExpiresMs(mineHolds(expiresInMs: 30000), 946684800000));
    expect(remainSeconds(t), inInclusiveRange(28, 30),
        reason: '倒计时拿 expires_ms（2000 年那个时刻）减了手机的钟');
    expect(find.textContaining('-'), findsNothing, reason: '负数不许上屏');
    await unmount(t, rig);
  });

  testWidgets('手机的钟差几十年也不影响倒计时:expires_ms 落在未来', (WidgetTester t) async {
    /// 对偶的一半：钟差朝另一个方向的时候，减出来是「还剩两千多万秒」——
    /// 那是个正数，钳零盖不住它，可它同样是错的：人会以为这份租约不会掉，
    /// 而下一秒它就掉了。
    final Rig rig = await mount(
        t, withExpiresMs(mineHolds(expiresInMs: 30000), 4102444800000));
    expect(remainSeconds(t), inInclusiveRange(28, 30),
        reason: '倒计时拿 expires_ms（2100 年那个时刻）减了手机的钟');
    await unmount(t, rig);
  });

  // ------------------------------------------------- 必修 6：续租跟校准解耦

  testWidgets('GET /api/control 不通的时候,心跳仍然在发', (WidgetTester t) async {
    /// **这一条是必修 6 的正题，别的都绕着它转。**
    ///
    /// 老写法里续租是**链在校准成功之后**的（`_sync()` 成了才 `_beat()`）。
    /// 于是 `GET /api/control` 一旦不通 —— 热点抖一下、狗那头忙着扫盘 ——
    /// 一条心跳都出不去。30 秒之后狗把租约收了，而 app 这头 `_held` 还是
    /// true、两根杆还亮着、还跟着手指走。人推杆，狗不动，屏上给的是「任务在
    /// 跑 / 急停按着 / 控制权在别人手上 / 没画面」里的某一条 —— 四条没有一条
    /// 是真的。
    ///
    /// 续租**唯一**该依赖的事实是「这份租约还在我手上」，跟「我刚才问狗成没
    /// 成功」没有一点关系。
    final Rig rig = await mount(t, mineHolds(),
        syncPeriod: const Duration(milliseconds: 200));
    await pumpUntil(t, () => rig.gets > 0, '第一次校准真的到了狗那儿');
    rig.dog.statusCodes['/api/control'] = 500;
    final int before = rig.beats;
    await pumpUntil(t, () => rig.beats >= before + 3, '校准全挂着的时候，心跳照发',
        step: const Duration(milliseconds: 100));
    expect(troubleIn(t, ControlPanel.syncTroubleKey), isNotEmpty,
        reason: '校准这会儿必须是真的挂了 —— 不挂的话这条测试压根没进到要测的局面');
    expect(find.byKey(ControlPanel.releaseKey), findsOneWidget,
        reason: '租约续上了，屏上就该还是「我拿着」这一档');
    rig.dog.statusCodes.remove('/api/control');
    await unmount(t, rig);
  });

  testWidgets('租约在本地走到 0:杆灰掉,屏上说的是「控制权过期了」', (WidgetTester t) async {
    /// 必修 6 的后半段：**租约掉了之后界面要说实话。**
    ///
    /// 掉线那一刻屏上原本什么也不变 —— 两根杆还亮着，倒计时停在 0，而人一
    /// 推杆收到的是 409，屏上给的理由是那四条里的一条。这一条钉住的是：杆
    /// 要灰（`onHeld(false)`），而且要有一句**专门说这件事**的话，不许挤进
    /// 校准/心跳/按钮那三格里去 —— 挤进去它读起来就成了「网络不好」，
    /// 而真正该做的事（重新按一次「取得控制权」）没人会去做。
    final List<bool> held = <bool>[];
    final Rig rig = await mount(t, mineHolds(expiresInMs: 2000),
        onHeld: held.add,
        // 校准周期放得很长：这一条要证的是**本地这一头**自己认出了过期，
        // 不是「又问了一次狗，狗说没了」。
        syncPeriod: const Duration(hours: 1));
    expect(held.last, isTrue, reason: '一开始租约在我手上');
    for (int i = 0; i < 4; i++) {
      await t.pump(const Duration(seconds: 1));
    }
    expect(held.last, isFalse, reason: '租约过期了，两根杆必须灰掉');
    expectTroubles(t, lost: leaseLostHint);
    await unmount(t, rig);
  });

  // ------------------------------------------------------ 必修 1：生命周期

  testWidgets('切到后台:主动把控制权还回去,而且不再问狗', (WidgetTester t) async {
    /// 熄屏、切出去、来一个电话 —— 人已经不在这块屏前面了，而狗那头的租约
    /// 还在被心跳续着。别人（或者他自己换一台手机）想接管，得等满宽限期，
    /// 而这段时间里狗是**没人管**的。
    ///
    /// 还要停表：人不在了还在一秒几个往返地问狗，白耗电、白占热点上行。
    final List<bool> held = <bool>[];
    final Rig rig = await mount(t, mineHolds(),
        onHeld: held.add, syncPeriod: const Duration(milliseconds: 200));
    await pumpUntil(t, () => rig.gets > 0, '第一次校准到了狗那儿');
    await setLifecycle(t, AppLifecycleState.paused);
    await pumpUntil(t, () => rig.posted.contains('/api/control/release'),
        '切到后台那一下，控制权真的还回去了', step: const Duration(milliseconds: 100));
    expect(held.last, isFalse, reason: '已经还回去了，就不许还说「在我手上」');
    final int asked = rig.asked;
    final int beats = rig.beats;
    for (int i = 0; i < 10; i++) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 5)));
      await t.pump(const Duration(milliseconds: 200));
    }
    expect(rig.asked, asked, reason: '人都不在了还在问狗：白耗电，还占着热点的上行');
    expect(rig.beats, beats, reason: '已经还回去的租约还在续 —— 那正是这条要堵的洞');
    await unmount(t, rig);
  });

  testWidgets('通知栏拉一下(inactive)不算走:控制权不还,表不停', (WidgetTester t) async {
    /// **`inactive` 不当成「走了」，这是一个有代价的选择，理由写在这儿。**
    ///
    /// Android 上 `inactive` 会在通知栏下拉、音量条弹出、权限框弹出、来电
    /// 横幅这些时候发出来 —— 这些时刻人还举着手机、眼睛还在看着狗。把它当
    /// 成「走了」的话，每划一下通知栏控制权就掉一次；而回到前台这一头是
    /// **故意不自动取回**的（见下一条），于是人得手动再取一次，还要再等一轮
    /// 校准。现场遥控一台会动腿的狗，这一下比多续一个租约周期危险得多。
    ///
    /// 真的要走的时候一定会跟着一个 `hidden`/`paused`（`inactive` 只是去后台
    /// 路上的第一站），所以这样并不会漏掉「走了」。
    final Rig rig = await mount(t, mineHolds(),
        syncPeriod: const Duration(milliseconds: 200));
    await pumpUntil(t, () => rig.gets > 0, '第一次校准到了狗那儿');
    await setLifecycle(t, AppLifecycleState.inactive);
    final int asked = rig.asked;
    await pumpUntil(t, () => rig.asked >= asked + 2, 'inactive 之后表还在走',
        step: const Duration(milliseconds: 100));
    expect(rig.posted, isNot(contains('/api/control/release')),
        reason: '拉一下通知栏就把控制权还掉，人得手动再取一次');
    expect(find.byKey(ControlPanel.releaseKey), findsOneWidget,
        reason: '控制权还在我手上，屏上就该还是「我拿着」这一档');
    await unmount(t, rig);
  });

  testWidgets('回到前台不自动把控制权取回来', (WidgetTester t) async {
    /// **回来这一头故意什么都不做。** 自动取回等于「手机一亮，狗就归你了」：
    /// 这中间很可能已经有另一个人接管了，而他不会收到任何提示。取控制权是
    /// 一个人要按一下的动作，不是一个状态恢复。
    final Rig rig = await mount(t, mineHolds(),
        syncPeriod: const Duration(milliseconds: 200));
    await pumpUntil(t, () => rig.gets > 0, '第一次校准到了狗那儿');
    await setLifecycle(t, AppLifecycleState.paused);
    await pumpUntil(t, () => rig.posted.contains('/api/control/release'),
        '切到后台那一下，控制权真的还回去了', step: const Duration(milliseconds: 100));
    // 还回去之后，狗那头就是「没人拿着」了。
    rig.dog.replies['/api/control'] = nobodyHolds();
    await setLifecycle(t, AppLifecycleState.resumed);
    await pumpUntil(
        t,
        () => find.byKey(ControlPanel.acquireKey).evaluate().isNotEmpty,
        '回到前台之后照样问狗，问到的是「没人拿着」',
        step: const Duration(milliseconds: 100));
    expect(rig.posted, isNot(contains('/api/control/acquire')),
        reason: '手机一亮就把狗抢回来 —— 这中间很可能已经换人在开了');
    await unmount(t, rig);
  });

  // -------------------------------------------------------- 必修 4：在途闸

  testWidgets('上一条校准还在路上的时候,这一拍不再发一条', (WidgetTester t) async {
    /// 周期路径原本一条闸都没有：狗那头慢一拍（或者干脆只发了响应头就沉
    /// 默），下一个周期照样再发一条。200 毫秒一拍，一次卡 5 秒就是二十几条
    /// 谁也不回收的请求压在同一条热点上 —— 视频先掉，然后是摇杆。
    final Rig rig = await mount(t, mineHolds(),
        syncPeriod: const Duration(milliseconds: 200));
    await pumpUntil(t, () => rig.gets > 0, '第一次校准到了狗那儿');
    rig.dog.hangPaths.add('/api/control');
    final int before = rig.gets;
    // 3 秒 = 15 个周期，都落在这条请求自带的 5 秒期限之内。
    for (int i = 0; i < 15; i++) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 5)));
      await t.pump(const Duration(milliseconds: 200));
    }
    expect(rig.gets, before + 1, reason: '没有在途闸的话，这三秒里会堆出十几条来');
    // 闸不是单向的：期限到了要真的放开，不然这条路就永远哑了。
    rig.dog.hangPaths.remove('/api/control');
    await pumpUntil(t, () => rig.gets > before + 1, '期限到了之后闸放开，下一拍发得出去',
        step: const Duration(milliseconds: 500));
    await unmount(t, rig);
  });
}
