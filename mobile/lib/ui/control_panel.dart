/// 控制权面板。挂在遥控屏顶上的一条窄带。
///
/// 狗那头会动腿的接口全都要控制权（`app/control.py` 的 `CONTROLLED`）。
/// 没有这块屏，人推杆得到的是一串 409，而屏幕上什么解释也没有。
///
/// **倒计时读 `expires_in_ms`，绝不拿 `expires_ms` 去减手机的钟。** 狗上没有
/// NTP（`engine/lease.py` 的 `LeaseState` 写着），两头的钟差几分钟是常态；
/// 减出来的结果要么是负数，要么是「还剩二十分钟」下一秒就掉 —— 两种都会让人
/// 以为 app 坏了，而真正该做的事（把控制权要过来）一件都不会发生。
/// 两个绝对时刻在报文里留着，那是给审计和日志对时间用的，不是给这块屏用的。
///
/// **倒计时在本地跑，隔一段跟狗校准一次。** 每秒问一次狗就是每秒一个 HTTP
/// 往返，在热点上跟视频抢带宽 —— 视频掉一拍的代价比倒计时差一秒大得多。
///
/// **隔多久由狗说了算**（`heartbeat_ms`，见 [ControlPanel.syncPeriodFor]）。
/// `engine/lease.py` 的 `LeaseState` 明写「客户端不该把心跳间隔硬编在自己那
/// 头」：硬编的那头，狗改了配置手机不跟，人正开着狗，杆忽然灰了。
///
/// **「没人拿着」和「已过期」是两种画法**（`engine/lease.py:126-128`）：报文里
/// 前者是 `null`、后者是 0。合并掉的话，一台谁也没在开的狗在屏上写着「已过期」，
/// 人会去等一个不会来的时刻，而该做的只是按一下「取得控制权」。
///
/// **所有 URL 都从 `client.baseUrl` 取。** `Robot.host/port` 的默认值是真狗的
/// `192.168.168.100:8095`：拿 `robot` 拼 URL 的话，测试会去敲真狗的地址、
/// 现场会敲错狗。
library;

import 'dart:async';
import 'dart:developer' as developer;
import 'dart:io';

import 'package:flutter/material.dart';

import '../net/patrol_client.dart';
import '../net/wire.dart';

/// 名字后面那个短标。**它只是个把手，不是全文。**
///
/// 全文（狗发过来的 `notice`）点开这个短标才展开 —— 那条带子上地方小，而
/// 那句话必须在这一屏上看得到，不能只留一个人人各自脑补的「未核实」。
///
/// **只在屏上真的有一个名字的时候才挂。** 标的是那个名字：屏上没有名字还挂
/// 着它，人只会去猜「未核实的是什么」，而它其实在替一件没显示的事背书。
///
/// **别跟 [nameNotCheckedMark] 混。** 那一个是**屏上没有名字**的时候挂的，
/// 标的是狗的规矩；这一个只挂在一个真的显示出来的名字后面。两句话原本只差
/// 一个字（「未」/「不」），编译器对这种「相近而不相同」一句话也不会说 ——
/// 所以两边的名字要差到不可能看错，而且**测试一律引用常量，不许抄字面量**：
/// 抄了字面量的话，谁把措辞统一一下，断言就会开始把两个短标混着匹配，
/// `findsNothing` 那种方向会直接变成永真的空绿。
const String unverifiedMark = '（未核实）';

/// 屏上没有名字的时候，那句话的入口。
///
/// **全文的入口不许只在「有人拿着」的时候才有。** 没人拿着恰恰是人马上要按
/// 「取得控制权」、把自己那个不核实的名字写进狗的账上的时刻 —— 那一刻看不到
/// 这句话，等于这句话在最该被读到的时候是藏着的。
///
/// **别跟 [unverifiedMark] 混**（原因见那一条）。名字里特意写足
/// `nameNotChecked`：短标本身只差一个字，名字再相近的话，改措辞的人两头都
/// 会以为自己改的是同一个东西。
const String nameNotCheckedMark = '（名字不核实）';

/// 续租那条路要说的话。**跟校准那条路的话分开一个槽。**
///
/// **名字里不叫 `beatTroubleHint`**：`teleop_page.dart` 里已经有一个同名的
/// 常量，说的是**发拍心跳**断了（那是另一条路、另一种处置）。两个名字撞在
/// 一起的时候，同时 import 这两个文件的测试连编都编不过；更坏的情况是有人
/// 加个前缀绕过去，于是两句话在测试里被互相当成对方。
///
/// 拎成常量是为了让测试能一字不差地盯住它：这句话在心跳一直不通的时候必须
/// 一直挂着，不许被三秒后一次成功的校准抹掉（Task 11 的 R-1 在这儿原样复发过）。
const String renewTroubleHint = '租约没续上：再过一会儿控制权会自己掉，杆会变灰';

/// 校准那条路（`GET /api/control`）说的话。
const String syncTroubleHint = '没问到控制权的状态，过几秒会自己再试';

/// 那四个按钮各自失败时说的话。**一个按钮一句**：四句话说的是四件不同的
/// 补救（再点一次 / 等它自己到期），合成一句的话人不知道下一步该干什么。
///
/// 拎成常量是为了让测试**引用它们**而不是抄字面量。抄字面量的那天起，改
/// 措辞这件事在测试里就不跟着走了 —— 而「按钮那句话落在按钮那一格」正是
/// 靠这几句原文断的（见 [ControlPanel.actTroubleKey]）。
const String acquireFailedHint = '没要到，再点一次';
const String takeoverFailedHint = '没请求成，再点一次';
const String approveFailedHint = '没交出去，再点一次';
const String releaseFailedHint = '没还成，过几秒会自己到期';

/// 狗没说 `heartbeat_ms`（或者说了个不合法的数）时用的校准周期。
const Duration defaultSyncPeriod = Duration(seconds: 3);

/// 校准周期的下限。**再密就是在跟视频抢热点带宽**（一次校准 = 一个往返）。
const Duration minSyncPeriod = Duration(seconds: 1);

/// 校准周期的上限。
///
/// 狗那头 `LEASE_TTL_MS = 30_000`：周期再稀，掉一次就补不回来了。10 秒意味着
/// 连掉两次还剩 10 秒余量。
const Duration maxSyncPeriod = Duration(seconds: 10);

/// 校准和续租这两条**周期路径**各自的超时窗口（必修 4）。
///
/// `PatrolClient` 的默认超时是 10 秒 —— 那是给 `unlock`、盘况这类一次性请求
/// 的。校准等满 10 秒毫无意义：狗那头 30 秒的 TTL 已经过去三分之一，而那一
/// 问的答案早就过期了。**超时之后那条连接是真的被掐掉的**（见
/// `PatrolClient._send`），不是只让 Future 放弃。
const Duration controlPeriodicDeadline = Duration(seconds: 5);

/// 本地倒计时走到 0 那一刻，面板要说的那一句（必修 6）。
///
/// **这句话必须是「你的控制权已经过期」，不许换成别的四句里的任何一句。**
/// 以前的坏法是这样的：`GET /api/control` 单独不通 ⇒ 一发心跳都不发 ⇒ 租约
/// 悄悄过期，而 app 这头 `_held` 仍然是 true、两根杆还亮着还跟着手指走。人
/// 继续推，狗那头回 409，遥控屏打出「任务在跑 / 急停按着 / 控制权在别人手里
/// / 没有画面」—— **四个原因一个都不是真的**，而现场会照着其中一个去查一件
/// 根本不存在的事，一段时间就没了。
///
/// 拎成常量是为了让测试一字不差地盯住它（跟 [renewTroubleHint] 一个理由）。
const String leaseLostHint = '你的控制权已经过期：在这条里重新取一次，取到之前两根杆是灰的';

class ControlPanel extends StatefulWidget {
  const ControlPanel({
    super.key,
    required this.client,
    this.onHeld,
    this.syncPeriod,
    this.tickPeriod = const Duration(seconds: 1),
  });

  final PatrolClient client;

  /// 手上有没有控制权，变一次叫一次。
  ///
  /// 遥控屏靠它把杆变灰：没有控制权还能推，推出来的每一拍都是 409，
  /// 而屏幕上什么解释也没有。
  final void Function(bool held)? onHeld;

  /// 把校准周期钉死成这个值。**不给（真机上就是不给）就从狗发的
  /// `heartbeat_ms` 算**，见 [syncPeriodFor]。
  ///
  /// 这个口子是给测试用的：有几条测试要把「本地在走」和「跟狗校准」两件事
  /// 彻底隔开，得能把校准挪到窗口外面去。**不是为了在真机上调快** ——
  /// 真机上该听狗的，`engine/lease.py` 的 `LeaseState` 明写「客户端不该把
  /// 心跳间隔硬编在自己那头」，而写在这个参数的调用点上跟硬编是一回事。
  ///
  /// **`@visibleForTesting` 是这条规矩的唯一执法者。** 它只是个普通的公开
  /// 具名参数：`TeleopPage` 哪天顺手传一个进去，「周期听狗的」这件事就在
  /// 编译期不报任何错的情况下作废，而且没有一条测试会红 —— 测试自己就是
  /// 靠传它工作的。加了这个注解，`lib/` 里再传就是一条 analyze 错误
  /// （`invalid_use_of_visible_for_testing_member`），`test/` 里照用不误。
  @visibleForTesting
  final Duration? syncPeriod;

  /// 本地倒计时多久减一格。**同样只服务测试**，理由见 [syncPeriod]。
  @visibleForTesting
  final Duration tickPeriod;

  /// 狗说它 [heartbeatMs] 毫秒要一次心跳，那么多久校准/续期一次。
  ///
  /// **取三分之一**：掉两次还追得回来，掉三次才到期。再钳进
  /// [minSyncPeriod]–[maxSyncPeriod]：狗那头哪天配出一个荒唐的数（0、负数、
  /// 或者 50 毫秒），手机不该跟着把热点占满，也不该稀到守不住租约。
  ///
  /// 狗没发这一项、或者发了个 ≤ 0 的数，就回落到 [defaultSyncPeriod]。
  static Duration syncPeriodFor(int? heartbeatMs) {
    if (heartbeatMs == null || heartbeatMs <= 0) return defaultSyncPeriod;
    final int ms = heartbeatMs ~/ 3;
    if (ms < minSyncPeriod.inMilliseconds) return minSyncPeriod;
    if (ms > maxSyncPeriod.inMilliseconds) return maxSyncPeriod;
    return Duration(milliseconds: ms);
  }

  static const Key acquireKey = ValueKey<String>('control-acquire');
  static const Key releaseKey = ValueKey<String>('control-release');
  static const Key takeoverKey = ValueKey<String>('control-takeover');
  static const Key approveKey = ValueKey<String>('control-approve');
  static const Key noticeKey = ValueKey<String>('control-notice');
  static const Key noticeFullKey = ValueKey<String>('control-notice-full');
  static const Key remainKey = ValueKey<String>('control-remain');
  static const Key graceKey = ValueKey<String>('control-grace');

  /// 「谁在要接管」那一行的两个 key。**两句话两个 key，不是一个 key 两种
  /// 文案**：文案里那个「你」/「有人」是这一行全部的信息量，按 key 断言才
  /// 分得清「说错了人」和「压根没说」——同一个 key 两种字的话，测试只能去
  /// 比字符串，而屏上那句话哪天改一个字，测试红的是措辞不是判断。
  ///
  /// 挂账 61：这两句话以前分不出来。`challenger` 只带 `{ref, operator}`，
  /// 而狗那头记的名字**不核实**——现场两个人都报「张三」，或者同一个人手上
  /// 两台手机，报文里长得一模一样。判据只能是狗按会话 ref 算好的
  /// `challenging`（`LeaseView.challenging`）。
  static const Key challengeMineKey = ValueKey<String>('challenge-mine');
  static const Key challengeOtherKey = ValueKey<String>('challenge-other');
  static const Key seatsKey = ValueKey<String>('control-seats');

  /// 三个话槽各自的 key。**槽是按 key 认的，不是按屏上有没有这句话认的。**
  ///
  /// 这三行在屏上长得一模一样。没有 key 的时候，「这句话上没上屏」是能断言
  /// 的，「这句话落进了哪个槽」不能 —— 于是把 `_act()` 的失败写进
  /// [syncTroubleKey] 那一格，全仓测试一条都不会红，而人看到的是 Task 11
  /// 的 R-1 原样复发：按钮那句话被三秒后一次成功的后台校准悄悄抹掉。
  ///
  /// 三个槽 × 两个方向 = 六种写错的方式。一条条补测试是补不完的；按 key 断言
  /// 才是由构造保证：**每条路只写自己那个 key 的那一格。**
  ///
  /// **空着的时候槽也在**（画成一个带 key 的 `SizedBox.shrink()`），这样
  /// 「这一格是空的」跟「这一格里是那句话」是同一种断法。
  static const Key syncTroubleKey = ValueKey<String>('control-trouble-sync');
  static const Key beatTroubleKey = ValueKey<String>('control-trouble-beat');
  static const Key actTroubleKey = ValueKey<String>('control-trouble-act');

  /// 「你的控制权已经过期」那一格（必修 6）。**自己一个槽。**
  ///
  /// 不并进上面三个槽里：那三个说的是「某条路不通」，这一个说的是「你手上
  /// 那份租约没了」—— 前三句人读完会去看网，这一句人读完要去按「取得控制
  /// 权」。合成一个槽的话，一次成功的校准会把它顺手抹掉，而它恰恰是校准
  /// 不通的时候唯一还说得出真话的那一行。
  static const Key leaseLostKey = ValueKey<String>('control-lease-lost');

  @override
  State<ControlPanel> createState() => _ControlPanelState();
}

class _ControlPanelState extends State<ControlPanel>
    with WidgetsBindingObserver {
  Timer? _tickTimer;
  Timer? _syncTimer;

  /// 续租自己一个表（必修 6）。**不再挂在「这一次校准成功」上。**
  ///
  /// 老写法是 `_sync()` 成功之后链着发一次心跳，于是 `GET /api/control` 这
  /// 一条路就成了租约的单点：它不通 ⇒ 一发心跳都不发。狗那头 TTL 是 30 秒，
  /// 校准周期封顶 10 秒，每次超时又要吃满一整个窗口 —— 连着三次失败就正好
  /// 把 30 秒吃光，而这三次失败在狗自己的热点上是很平常的一段抖动。
  Timer? _beatTimer;

  LeaseView? _lease;

  /// 还剩多久（毫秒）。**本地在减的就是这个。** 没人拿着时是 null。
  int? _remainMs;

  /// 宽限期还剩多久（毫秒）。没人在抢时是 null。
  int? _graceMs;

  /// 上一次告诉外面的那个答案。**只在变了的时候叫。**
  bool _held = false;

  /// 那句 notice 展开了没有。
  bool _noticeOpen = false;

  /// 三条路各占一个槽。**每条路只清自己写的那一格。**
  ///
  /// 合成一个槽的时候是这样坏的（Task 11 在 `teleop_page.dart` 上刚犯过一次，
  /// 这儿又原样犯了一次）：心跳一直不通，屏上挂着「租约没续上」，而三秒后一次
  /// 成功的校准把它抹掉了 —— 人只看见红字闪一下，而那正是最该被看见的一句。
  ///
  /// **异常原文只进日志**（跟 `teleop_page.dart` 的 `_human` 一个规矩）：
  /// 屏幕上要的是「该做什么」，Dart 的异常文本回答不了这个。
  ///
  /// [_syncTrouble]：`GET /api/control` 那条路。
  String _syncTrouble = '';

  /// [_beatTrouble]：`POST /api/control/heartbeat` 那条路。见 [renewTroubleHint]。
  String _beatTrouble = '';

  /// [_actTrouble]：人按下那几个按钮之后的那条路。**后台的校准不许碰它** ——
  /// 按钮是人主动按的，回话不该被一次跟它无关的成功校准顺手抹掉（Task 11
  /// 的 R-1 就是这么犯的）。
  ///
  /// 但「只有下一次按按钮才清」又太死：这句话是对**当时那个局面**的回话，
  /// 局面变了它就过期，甚至跟屏上其余的字自相矛盾 —— 持有者松手之后，面板
  /// 已经画成「没人拿着控制权」、「请求接手」那个按钮也没了，屏上却还留着
  /// 「控制权在别人手里：用「请求接手」…」。所以再加一个清除时机：
  /// **局面一换就收**，见 [_sceneOf]。
  String _actTrouble = '';

  /// 上一句 [_actTrouble] 是对哪个局面说的。见 [_sceneOf]。
  String _actScene = '';

  /// 眼下这个校准周期。见 [ControlPanel.syncPeriodFor]。
  late Duration _syncEvery;

  /// 本地倒计时已经走到 0 了 —— 租约当成掉了（必修 6）。见 [leaseLostHint]。
  ///
  /// **一份新的快照（校准或者续租回来的那份）就地把它清掉**：狗说的算，
  /// 本地这条推断只在「问不到狗」的那段时间里作数。
  bool _leaseLost = false;

  /// 校准那条路上一发还没回来（必修 4）。
  bool _syncing = false;

  /// 续租那条路上一发还没回来（必修 4）。
  bool _beating = false;

  /// app 还在前台。**进了后台就不许把表重新起起来**（必修 1）。
  ///
  /// [_retimeSync] 是「周期变了就重建定时器」，它不知道人在不在；少了这一位
  /// 的话，后台里一次成功的续租回来就会把刚停掉的表又点着。
  bool _awake = true;

  @override
  void initState() {
    super.initState();
    // 第一份租约还没到，狗说多久还不知道 —— 先按缺省的走，`_apply` 里再改表。
    _syncEvery = widget.syncPeriod ?? defaultSyncPeriod;
    // **立刻问一次，不等第一个周期。** 等的话，人进来先看见一片空白，
    // 而那几秒里他会去点一个还没画出来的按钮。
    unawaited(_sync());
    _tickTimer = Timer.periodic(widget.tickPeriod, (_) => _onTick());
    _startPolling();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _tickTimer?.cancel();
    _syncTimer?.cancel();
    _beatTimer?.cancel();
    super.dispose();
  }

  // ------------------------------------------------------------ 人在不在

  /// 切后台 / 锁屏 / 来电（必修 1）。
  ///
  /// **`inactive` 不算「人不在了」，只有 `hidden`/`paused`/`detached` 才算。**
  /// Android 上 `inactive` 是给「短暂盖住」用的：通知栏下拉、音量条、权限框、
  /// 来电的横幅还没接起来 —— 这几下人还握着手机、还看着那只狗。把它也算成
  /// 「人不在了」的话，每划一次通知栏就弃一次租；而我们**回前台是有意不自动
  /// 把控制权拿回来的**（让人重新拿），于是误划一下就要人手动重新取一次控制
  /// 权。那比现在这个病更难在现场忍受。`paused` 才是 Android 给「这个 app
  /// 已经看不见了」的那一位（Home、锁屏、切应用、接起电话），而且去后台这条
  /// 路上 `inactive` 后面必定跟着 `hidden`/`paused`，卡在 `paused` 上不漏事。
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      case AppLifecycleState.resumed:
        _onResumed();
      case AppLifecycleState.inactive:
        break;
      case AppLifecycleState.hidden:
      case AppLifecycleState.paused:
      case AppLifecycleState.detached:
        _onGone();
    }
  }

  /// 人不在了：停表 + **主动弃租**。
  ///
  /// **不是「停掉续租、让它按 TTL 自己掉」就够了。** 那要白等 30 秒，而这
  /// 30 秒里现场另一个人接不了管，占着控制权的那台手机屏幕是黑的。
  void _onGone() {
    if (!_awake) return;
    _awake = false;
    _syncTimer?.cancel();
    _syncTimer = null;
    _beatTimer?.cancel();
    _beatTimer = null;
    if (_held) unawaited(_releaseOnLeave());
  }

  /// 回前台。**不自动把控制权拿回来** —— 让人重新拿。
  ///
  /// 自动拿回来的话，「我刚才是不是还握着」这件事就没有一个人做过决定：手机
  /// 在兜里的那几分钟别人可能已经接管了，回来悄悄抢一次是最坏的一种。
  void _onResumed() {
    if (_awake) return;
    _awake = true;
    _startPolling();
    unawaited(_sync());
  }

  /// 交还控制权，并且**先把杆关掉再发请求**。
  ///
  /// 顺序是有意的：`onHeld(false)` 那一下会让遥控屏立刻归零并发一拍全零停，
  /// 那件事不该等一次网络往返 —— 请求发不出去的时候它更要先发生。
  Future<void> _releaseOnLeave() async {
    _held = false;
    widget.onHeld?.call(false);
    try {
      await widget.client
          .post('/api/control/release', null, controlPeriodicDeadline);
    } catch (e) {
      // 弃租失败只进日志：人已经不在这块屏前面了，屏上写什么都没人读，
      // 而狗那头 30 秒的 TTL 是这条路真正的兜底。
      developer.log('$e', name: 'control');
    }
  }

  /// 起校准和续租两张表。**两张，不是一张**（必修 6）。
  void _startPolling() {
    _syncTimer?.cancel();
    _beatTimer?.cancel();
    _syncTimer = Timer.periodic(_syncEvery, (_) => unawaited(_sync()));
    _beatTimer = Timer.periodic(_syncEvery, (_) => unawaited(_beatIfHeld()));
  }

  // ------------------------------------------------------------ 倒计时

  /// 本地减一格。**减到 0 就钳在 0，负数不许上屏。**
  ///
  /// 负数是「app 坏了」的样子，而它其实只是在如实描述「已经没了」。
  void _onTick() {
    final int? r = _remainMs;
    final int? g = _graceMs;
    if (r == null && g == null) return;
    final int step = widget.tickPeriod.inMilliseconds;
    setState(() {
      if (r != null) _remainMs = r > step ? r - step : 0;
      if (g != null) _graceMs = g > step ? g - step : 0;
    });
    _loseLeaseIfExpired();
  }

  /// 倒计时走到 0 ⇒ **把租约当成掉了，并且当场说出来**（必修 6）。
  ///
  /// 这一步是「说真话」那一半。光把续租解耦还不够：热点抖得够久的话，续租和
  /// 校准会一起不通，租约照样会掉 —— 而屏上那时候仍然写着「控制权在你手上」，
  /// 两根杆仍然亮着。人推一下，得到的是狗回的 409，遥控屏念出那四个原因，
  /// 没有一个是真的。
  ///
  /// **只在自己拿着的时候管。** 别人的租约过期不关这块屏的事，那一格屏上
  /// 本来就写着「已过期」。
  void _loseLeaseIfExpired() {
    if (!_held || _remainMs != 0 || _leaseLost) return;
    setState(() => _leaseLost = true);
    _held = false;
    // 杆立刻变灰。**不等下一次校准** —— 校准正不通，等它就是永远不说。
    widget.onHeld?.call(false);
  }

  // ------------------------------------------------------------ 跟狗说话

  /// 跟狗校准一次。**这里不再链着发心跳**（必修 6）：续租有自己那张表。
  Future<void> _sync() async {
    // **上一发还没回来就跳过这一拍**（必修 4）。堆起来的话每一发各占一条
    // 连接，把狗自己的热点上行挤死，而屏上报的是「看不见就不许开狗」。
    if (_syncing) return;
    _syncing = true;
    try {
      final Map<String, dynamic> m =
          await widget.client.get('/api/control', controlPeriodicDeadline);
      if (!mounted) return;
      _apply(m);
      // **只清自己写的那一格。** 心跳那句和按钮那句归它们自己那条路收。
      if (_syncTrouble.isNotEmpty) setState(() => _syncTrouble = '');
    } catch (e) {
      _fail(e, syncTroubleHint, (String s) => _syncTrouble = s);
    } finally {
      _syncing = false;
    }
  }

  /// 续租这一拍。**条件只有一个：上一次已知租约还在自己手上**（必修 6）。
  ///
  /// 不是「这一次校准成功」。校准不通照发 —— 狗不认自然会拒，那是它该干的
  /// 事，而拒回来的那句话有 [renewTroubleHint] 那个槽接着（以前那个槽永远
  /// 是空的：校准不通时 `_beat` 压根没被调用过）。
  ///
  /// 别人拿着的时候不发：狗那头只会回一串错，而屏上会挂着一句跟当前处境毫无
  /// 关系的红字。
  Future<void> _beatIfHeld() async {
    if (!_held) return;
    await _beat();
  }

  Future<void> _beat() async {
    if (_beating) return;
    _beating = true;
    try {
      final Map<String, dynamic> m = await widget.client
          .post('/api/control/heartbeat', null, controlPeriodicDeadline);
      if (!mounted) return;
      // 续期回的就是最新那份租约，顺手拿来校准，省一次往返。
      _apply(m);
      if (_beatTrouble.isNotEmpty) setState(() => _beatTrouble = '');
    } catch (e) {
      _fail(e, renewTroubleHint, (String s) => _beatTrouble = s);
    } finally {
      _beating = false;
    }
  }

  /// 按一下那几个按钮。回的都是同一份租约报文，拿来立刻刷屏。
  Future<void> _act(String path, String whenFailed) async {
    // 上一次按出来的那句话，这一次按下去就该收掉 —— 人重新问了一遍，
    // 屏上不该还挂着上一遍的回答。
    if (_actTrouble.isNotEmpty) setState(() => _actTrouble = '');
    try {
      final Map<String, dynamic> m = await widget.client.post(
          path, path.endsWith('/takeover') ? const <String, dynamic>{} : null);
      if (!mounted) return;
      _apply(m);
    } catch (e) {
      _fail(e, whenFailed, (String s) => _actTrouble = s);
    }
  }

  /// 刷屏。**这里不碰那三个话槽** —— 一次成功的校准只说明「这一问通了」，
  /// 说明不了心跳那条路也通了。碰了就是替另一条路报平安。
  void _apply(Map<String, dynamic> m) {
    // **人已经走了的话，这一份回话作废。**（必修 1）
    //
    // 切后台那一下我们主动把控制权还了回去（[_releaseOnLeave]），可是那一刻
    // 往往还有一条校准/心跳在路上 —— 它带回来的那份快照里 `mine` 还是 true，
    // 照着刷的话 `_held` 会被重新点亮：狗那头已经没人拿着了，屏上却说「在我
    // 手上」，两根杆跟着亮回来。**顺序上谁后到不该决定谁说了算**：
    // 「人走了」是本地这一头已经定下的事实，比一条起飞更早的回话新。
    if (!_awake) return;
    final LeaseView v = LeaseView.fromJson(m);
    final String scene = _sceneOf(v);
    setState(() {
      _lease = v;
      _remainMs = v.expiresInMs;
      _graceMs = v.graceInMs;
      // 狗刚发了一份新的快照 —— 过没过期以这份为准，本地那条推断作废。
      _leaseLost = false;
      // **局面换了才收按钮那句话**（见 [_actTrouble]）。「同一个局面里的一次
      // 成功校准」照样不许碰它 —— 那正是 Task 11 的 R-1。
      if (scene != _actScene && _actTrouble.isNotEmpty) _actTrouble = '';
      _actScene = scene;
    });
    _retimeSync(v);
    if (v.mine != _held) {
      _held = v.mine;
      widget.onHeld?.call(v.mine);
    }
  }

  /// 「屏上这一刻是个什么局面」。[_actTrouble] 的保质期就是它。
  ///
  /// 只看**谁拿着、谁在抢、是不是我**这几件事：倒计时每秒都在变，跟着它清
  /// 的话按钮那句话活不过一秒。
  ///
  /// **`challenging` 也进来了，但它进来是为了将来，不是为了今天。** 给定一
  /// 个会话，`challenging` 是 `challengerRef` 的严格函数（同一台手机上，抢
  /// 的人换了 `challengerRef` 必变），所以今天把它从这个串里删掉，仓库里没有
  /// 一条测试会红 —— 这不是测试漏了，是这两件事此刻确实等价。列在这儿是因为
  /// 这个串的口径是「屏上这一刻是个什么局面」，而 `challenging` 决定屏上那一
  /// 行写的是「你」还是「有人」，它就是局面的一部分；哪天狗那头换了会话 ref
  /// 的算法、或者 `challenging` 不再只由 `challengerRef` 决定，漏了它就是一
  /// 次真的漏刷。
  ///
  /// **但同一句话对 `mine` 也一字不差地成立（`mine` 就是「holder 是我」，
  /// 它自己就排在这个串的第一位），而 `mine` 绝不许照这个理由删掉。** 两者
  /// 的差别不在「是不是严格函数」，在「翻了之后看不看得见」——下面两条是
  /// 对称的两侧，一侧看不见、一侧看得见：
  ///
  /// * `challenging` 这一位翻而 `challengerRef` 不变 ⇒ 两侧都有 challenger。
  ///   狗那头 `ask_takeover()` 对「控制权已经在你手上了」是直接拒的，所以
  ///   challenger 永远不等于 holder ⇒ 这一侧 `mine` 必为假。**光到这儿还落
  ///   不到 `someone` 那一支上** —— `mine` 为假还可以是「没人拿着」那条兜底
  ///   支（按钮「取得控制权」，灰不灰只看 `full`，不看 `grace`）。闭合这条链
  ///   的那一步是：`ask_takeover()` 在没人持有时是**当场把控制权给他、根本
  ///   不排队**，所以**有 challenger ⇒ holder 必在** ⇒ `someone`（就是
  ///   `v.held`，即 `holderRef != null`）必为真，兜底支走不到。而
  ///   `_challenger` 和 `_grace_ends` 在 `lease.py` 里**同生共死**，加上
  ///   `_settle()` 在宽限期一走完就把挑战者扶正，所以带 challenger 的快照
  ///   `grace_in_ms` 必然不是 null。（这两条 Python 侧的事实不再由这行注释
  ///   记账 —— 绊线在 `tests/engine/test_lease_takeover.py` 的
  ///   `test_挑战者和宽限终点永远同生共死`，只赋一半就红在那儿。）于是这一
  ///   侧走的是 [_buttons] 的 `someone` 那一支，**那一支唯一的按钮「请求接
  ///   手」在 `grace != null` 时 `onPressed` 是 null**；而 [_actTrouble] 唯
  ///   一变非空的地方就是 [_act] 的 catch。按不动，这句话就压根挂不上去
  ///   —— 翻了也没人看得见。
  /// * `mine` 这一位翻而 `holderRef` 不变的那条对称路径（我拿着租约、会话
  ///   ref 换了）**没有 challenger、没有 grace**，走的是 `mine` 那一支，
  ///   而那一支的「交还」「同意移交」是**无条件活的**（见 [_buttons]：宽限
  ///   期里也活，不活的话唯一的移交方式就是干等 15 秒）。持有者按「交还」
  ///   失败是实打实能把 [_actTrouble] 填上的 ⇒ 那一位漏刷就是**看得见的**
  ///   一次漏刷。
  ///
  /// **别把「`grace` 在就全灰」记成一条通则** —— 灰的只有 `someone` 那一支。
  /// 上面这条链能成立，靠的是「`challenging` 为真的那一侧一定不是持有者」，
  /// 不是靠 grace 本身。
  String _sceneOf(LeaseView v) =>
      '${v.mine}|${v.holderRef ?? ''}|${v.challengerRef ?? ''}'
      '|${v.challenging}';

  /// 按狗说的 `heartbeat_ms` 重新对表。
  ///
  /// **周期真的变了才动表。** 每次 `_apply()` 都 `cancel()` 再 `periodic()`
  /// 的话，定时器永远从零重新数 —— 而 `_apply()` 是 `_sync()`、`_beat()`
  /// **和 `_act()`** 三条路共用的：人在一个周期里反复按按钮，每按一次就把
  /// 下一次校准整整往后推一个周期。续租那张表跟校准那张表是一起重起的
  /// （[_startPolling]），所以这条规矩对它同样是必须的：按得够勤就永远续不上。
  /// （续租**触发的条件**已经跟校准解耦了，见 [_beatIfHeld]；这里说的是
  /// 两张表的**起点**都不该被按钮推着走。）
  void _retimeSync(LeaseView v) {
    final Duration next =
        widget.syncPeriod ?? ControlPanel.syncPeriodFor(v.heartbeatMs);
    if (_syncTimer != null && next == _syncEvery) return;
    _syncEvery = next;
    // **后台里不许把表重新点着**（必修 1）：这个函数只管周期，不知道人在
    // 不在。回前台时 [_onResumed] 会拿着这个新周期重新起表。
    if (!_awake) return;
    _startPolling();
  }

  /// 出岔子了。**原文只进日志，屏上是人话，而且只写自己那一格。**
  void _fail(Object e, String fallback, void Function(String) into) {
    developer.log('$e', name: 'control');
    if (!mounted) return;
    setState(() => into(_human(e, fallback)));
  }

  String _human(Object e, String fallback) {
    if (e is PatrolError && e.status == HttpStatus.conflict) {
      return '控制权在别人手里：用「请求接手」，硬点是点不下来的';
    }
    if (e is PatrolError && e.status == HttpStatus.forbidden) {
      return '这个身份不许要控制权。换一把有权限的钥匙';
    }
    if (e is PatrolError && e.status == HttpStatus.unauthorized) {
      return '狗不认这个身份了。退出去重新输 PIN';
    }
    return fallback;
  }

  // ------------------------------------------------------------ 画

  /// 还剩多少秒。**向上取整**：还剩 200 毫秒的时候写「0 秒」，人会以为已经
  /// 没了，而那一刻杆还是能推的。
  static int _secondsOf(int ms) => (ms + 999) ~/ 1000;

  @override
  Widget build(BuildContext context) {
    final LeaseView? v = _lease;
    final int? remain = _remainMs;
    final int? grace = _graceMs;
    final bool mine = v?.mine ?? false;
    final bool someone = v?.held ?? false;
    final bool full = v != null &&
        v.maxSessions > 0 &&
        v.remoteSessions >= v.maxSessions &&
        !mine &&
        !someone;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Row(
          children: <Widget>[
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  _who(v, mine, someone),
                  // **没人拿着的时候这一格根本不画。** 画成「0 秒 / 已过期」
                  // 就是把「这件事不存在」说成「这件事刚结束」。
                  if (remain != null)
                    Text(
                      remain == 0 ? '已过期' : '还剩 ${_secondsOf(remain)} 秒',
                      key: ControlPanel.remainKey,
                      style: const TextStyle(
                          color: Colors.white70, fontSize: 13),
                    ),
                  // 有人在要接管的时候，**先说清楚那个人是不是你**（挂账
                  // 61）。旁边那台同名的手机上写的是「有人」，这一行才是
                  // 它们唯一分得开的地方 —— 分不开的后果是旁观者以为自己
                  // 已经在排队了，于是干等一个不会属于他的宽限期。
                  if (v != null && v.challenged)
                    Text(
                      v.challenging ? '你正在要接管' : '有人正在要接管',
                      key: v.challenging
                          ? ControlPanel.challengeMineKey
                          : ControlPanel.challengeOtherKey,
                      style: const TextStyle(
                          color: Color(0xFFFFD08A), fontSize: 13),
                    ),
                  if (grace != null)
                    Text(
                      mine
                          ? '对方在等你移交，还剩 ${_secondsOf(grace)} 秒'
                          : '等对方回应，还剩 ${_secondsOf(grace)} 秒',
                      key: ControlPanel.graceKey,
                      style: const TextStyle(
                          color: Colors.white70, fontSize: 13),
                    ),
                  if (full)
                    Text(
                      '席位已满（${v.remoteSessions}/${v.maxSessions}）：'
                      '等一个人下线，或者请人退出来',
                      key: ControlPanel.seatsKey,
                      style: const TextStyle(
                          color: Color(0xFFFFB4A9), fontSize: 13),
                    ),
                ],
              ),
            ),
            ..._buttons(v, mine, someone, grace, full),
          ],
        ),
        // 那句话的全文。**原样上屏**（`auth.OPERATOR_NOTICE`）：这头硬编一句
        // 的话，狗那头改了措辞手机不跟，而这句话是现场判断「屏上这个名字能信
        // 到什么程度」的唯一依据。
        if (_noticeOpen && (v?.notice ?? '').isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(v!.notice,
                key: ControlPanel.noticeFullKey,
                style: const TextStyle(color: Colors.white70, fontSize: 12)),
          ),
        // **三条路各占一行，谁也不盖谁。** 挤一个槽的话，两条路会互相抹，
        // 人看到的是一行在闪 —— 闪着的字比没有字更容易被当成花屏。
        _troubleLine(ControlPanel.syncTroubleKey, _syncTrouble),
        _troubleLine(ControlPanel.beatTroubleKey, _beatTrouble),
        _troubleLine(ControlPanel.actTroubleKey, _actTrouble),
        // **租约掉了这一句排在最后一格，而且自己一格**（必修 6）：上面三句
        // 说的是「某条路不通」，这一句说的是「你手上那份没了」——人读完要
        // 做的事完全不同。
        _troubleLine(ControlPanel.leaseLostKey, _leaseLost ? leaseLostHint : ''),
      ],
    );
  }

  /// 一个话槽。**空着也占着这个 key**，见 [ControlPanel.syncTroubleKey]。
  Widget _troubleLine(Key key, String s) => s.isEmpty
      ? SizedBox.shrink(key: key)
      : Padding(
          key: key,
          padding: const EdgeInsets.only(top: 4),
          child: Text(s,
              style: const TextStyle(color: Color(0xFFFFB4A9), fontSize: 13)),
        );

  /// 谁拿着这一行。**屏上出现名字的时候，名字后面永远跟着那个短标。**
  ///
  /// 「控制权在你手上」也把名字写出来（狗那头 `holder.operator` 记的就是这次
  /// 会话报上去的名字）：现场三个人各拿一台手机，「在你手上」说的是这台手机，
  /// 而人核对的是**狗账上记的是谁** —— 两者对不上的时候（换过人、换过 PIN），
  /// 恰恰是最该看出来的一刻。写出来了，那个「（未核实）」才有着落。
  Widget _who(LeaseView? v, bool mine, bool someone) {
    final String name = v?.holderOperator ?? '';
    // 屏上到底有没有一个名字。**这一位决定挂哪个短标**。
    final bool named = (mine || someone) && name.isNotEmpty;
    final String line = v == null
        ? '控制权：还没问到'
        : mine
            ? (named ? '控制权在你手上：$name' : '控制权在你手上')
            : someone
                ? (named ? '现在是 $name' : '控制权在别人手上')
                : '没人拿着控制权';
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Flexible(
          child: Text(line,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Colors.white, fontSize: 14)),
        ),
        // **全文的入口不管谁拿着都在。** 以前只有「有人拿着」时才画，于是
        // 没人拿着的时候那句话在这一屏上完全看不到 —— 而那正是人马上要把
        // 自己那个不核实的名字写进狗的账上的时刻。
        //
        // 短标分两种：屏上有名字就挂 [unverifiedMark]（它标的是那个名字），
        // 没名字就挂 [nameNotCheckedMark]（它标的是狗的规矩）。
        if ((v?.notice ?? '').isNotEmpty)
          InkWell(
            key: ControlPanel.noticeKey,
            onTap: () => setState(() => _noticeOpen = !_noticeOpen),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 2, vertical: 2),
              child: Text(named ? unverifiedMark : nameNotCheckedMark,
                  style: const TextStyle(
                      color: Color(0xFFFFD08A), fontSize: 12)),
            ),
          ),
      ],
    );
  }

  List<Widget> _buttons(
      LeaseView? v, bool mine, bool someone, int? grace, bool full) {
    if (v == null) return const <Widget>[];
    if (mine) {
      return <Widget>[
        // 别人在抢我的：**给一个「同意移交」**。不给的话，唯一的移交方式
        // 就是干等宽限期走完 —— 那 15 秒里要开狗的人站在狗旁边，狗谁也不听。
        if (v.challenged)
          _button(ControlPanel.approveKey, '同意移交',
              () => _act('/api/control/takeover/approve', approveFailedHint)),
        _button(ControlPanel.releaseKey, '交还',
            () => _act('/api/control/release', releaseFailedHint)),
      ];
    }
    if (someone) {
      return <Widget>[
        _button(
            ControlPanel.takeoverKey,
            '请求接手',
            // 宽限期里已经在等对方回应了，再点一次没有别的效果，
            // 只会让人以为「点了没反应」。
            grace == null
                ? () => _act('/api/control/takeover', takeoverFailedHint)
                : null),
      ];
    }
    return <Widget>[
      _button(ControlPanel.acquireKey, '取得控制权',
          full ? null : () => _act('/api/control/acquire', acquireFailedHint)),
    ];
  }

  Widget _button(Key key, String label, VoidCallback? onPressed) => TextButton(
        key: key,
        onPressed: onPressed,
        style: TextButton.styleFrom(
          minimumSize: const Size(56, 36),
          padding: const EdgeInsets.symmetric(horizontal: 8),
          foregroundColor: Colors.white,
          disabledForegroundColor: Colors.white38,
          backgroundColor: Colors.white24,
        ),
        child: Text(label),
      );
}
