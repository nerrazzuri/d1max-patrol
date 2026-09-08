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
const String unverifiedMark = '（未核实）';

/// 屏上没有名字的时候，那句话的入口。
///
/// **全文的入口不许只在「有人拿着」的时候才有。** 没人拿着恰恰是人马上要按
/// 「取得控制权」、把自己那个不核实的名字写进狗的账上的时刻 —— 那一刻看不到
/// 这句话，等于这句话在最该被读到的时候是藏着的。
const String noticeMark = '（名字不核实）';

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

/// 狗没说 `heartbeat_ms`（或者说了个不合法的数）时用的校准周期。
const Duration defaultSyncPeriod = Duration(seconds: 3);

/// 校准周期的下限。**再密就是在跟视频抢热点带宽**（一次校准 = 一个往返）。
const Duration minSyncPeriod = Duration(seconds: 1);

/// 校准周期的上限。
///
/// 狗那头 `LEASE_TTL_MS = 30_000`：周期再稀，掉一次就补不回来了。10 秒意味着
/// 连掉两次还剩 10 秒余量。
const Duration maxSyncPeriod = Duration(seconds: 10);

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
  final Duration? syncPeriod;

  /// 本地倒计时多久减一格。
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
  static const Key seatsKey = ValueKey<String>('control-seats');

  @override
  State<ControlPanel> createState() => _ControlPanelState();
}

class _ControlPanelState extends State<ControlPanel> {
  Timer? _tickTimer;
  Timer? _syncTimer;

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

  /// [_actTrouble]：人按下那几个按钮之后的那条路。**下一次按按钮才清** ——
  /// 按钮是人主动按的，回话也该等到人再按一次，不该被后台的校准顺手抹掉。
  String _actTrouble = '';

  /// 眼下这个校准周期。见 [ControlPanel.syncPeriodFor]。
  late Duration _syncEvery;

  @override
  void initState() {
    super.initState();
    // 第一份租约还没到，狗说多久还不知道 —— 先按缺省的走，`_apply` 里再改表。
    _syncEvery = widget.syncPeriod ?? defaultSyncPeriod;
    // **立刻问一次，不等第一个周期。** 等的话，人进来先看见一片空白，
    // 而那几秒里他会去点一个还没画出来的按钮。
    unawaited(_sync());
    _tickTimer = Timer.periodic(widget.tickPeriod, (_) => _onTick());
    _syncTimer = Timer.periodic(_syncEvery, (_) => unawaited(_sync()));
  }

  @override
  void dispose() {
    _tickTimer?.cancel();
    _syncTimer?.cancel();
    super.dispose();
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
  }

  // ------------------------------------------------------------ 跟狗说话

  Future<void> _sync() async {
    try {
      final Map<String, dynamic> m = await widget.client.get('/api/control');
      if (!mounted) return;
      _apply(m);
      // **只清自己写的那一格。** 心跳那句和按钮那句归它们自己那条路收。
      if (_syncTrouble.isNotEmpty) setState(() => _syncTrouble = '');
      // 自己拿着才续。别人拿着还发心跳的话，狗那头只会回一串错，
      // 而屏上会挂着一句跟当前处境毫无关系的红字。
      if (_lease?.mine == true) unawaited(_beat());
    } catch (e) {
      _fail(e, '没问到控制权的状态，过几秒会自己再试',
          (String s) => _syncTrouble = s);
    }
  }

  Future<void> _beat() async {
    try {
      final Map<String, dynamic> m =
          await widget.client.post('/api/control/heartbeat');
      if (!mounted) return;
      // 续期回的就是最新那份租约，顺手拿来校准，省一次往返。
      _apply(m);
      if (_beatTrouble.isNotEmpty) setState(() => _beatTrouble = '');
    } catch (e) {
      _fail(e, renewTroubleHint, (String s) => _beatTrouble = s);
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
    final LeaseView v = LeaseView.fromJson(m);
    setState(() {
      _lease = v;
      _remainMs = v.expiresInMs;
      _graceMs = v.graceInMs;
    });
    _retimeSync(v);
    if (v.mine != _held) {
      _held = v.mine;
      widget.onHeld?.call(v.mine);
    }
  }

  /// 按狗说的 `heartbeat_ms` 重新对表。
  ///
  /// **每次跟狗说上话都重起一次表**：周期量的是「距上次问到现在」，而刚问完
  /// 的这一刻正好是零点。不重起的话，狗把周期改长之后，旧表还会再响一次。
  void _retimeSync(LeaseView v) {
    _syncEvery = widget.syncPeriod ?? ControlPanel.syncPeriodFor(v.heartbeatMs);
    _syncTimer?.cancel();
    _syncTimer = Timer.periodic(_syncEvery, (_) => unawaited(_sync()));
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
        _troubleLine(_syncTrouble),
        _troubleLine(_beatTrouble),
        _troubleLine(_actTrouble),
      ],
    );
  }

  Widget _troubleLine(String s) => s.isEmpty
      ? const SizedBox.shrink()
      : Padding(
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
        // 没名字就挂 [noticeMark]（它标的是狗的规矩）。
        if ((v?.notice ?? '').isNotEmpty)
          InkWell(
            key: ControlPanel.noticeKey,
            onTap: () => setState(() => _noticeOpen = !_noticeOpen),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 2, vertical: 2),
              child: Text(named ? unverifiedMark : noticeMark,
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
              () => _act('/api/control/takeover/approve', '没交出去，再点一次')),
        _button(ControlPanel.releaseKey, '交还',
            () => _act('/api/control/release', '没还成，过几秒会自己到期')),
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
                ? () => _act('/api/control/takeover', '没请求成，再点一次')
                : null),
      ];
    }
    return <Widget>[
      _button(ControlPanel.acquireKey, '取得控制权',
          full ? null : () => _act('/api/control/acquire', '没要到，再点一次')),
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
