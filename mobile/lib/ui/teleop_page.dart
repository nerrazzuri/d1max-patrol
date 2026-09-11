/// 遥控屏：视频铺满 + 两个虚拟摇杆（左单轴平移 / 右转向）、持续发拍与松手
/// 主动停、视频掉线即变灰（§7.7、§5.9）。
///
/// **所有 URL 都从 `client.baseUrl` 取，`robot` 只用来显示名字。**
/// `Robot.host/port` 的默认值是真狗的 `192.168.168.100:8095` —— 拿 `robot`
/// 去拼 URL，测试会去敲真狗的地址，现场则会在换过地址的部署上敲错狗。
library;

import 'dart:async';
import 'dart:developer' as developer;
import 'dart:io';

import 'package:flutter/material.dart';

import '../model/robot.dart';
import '../net/patrol_client.dart';
import '../net/wire.dart';
import 'control_panel.dart';
import 'widget/joystick.dart';
import 'widget/live_video.dart';
import 'widget/operator_chip.dart';

/// 现场档那一行轻提示（§7.8）。
///
/// **是提示不是弹框。** 现场档是默认档，每次进来都弹一个框的话，它就是那个
/// 「第三次被条件反射点掉」的框 —— 弹得越勤，它拦住的事越少。
const String onsiteHint = '请与机器狗保持在同一视线范围内';

/// 切远程之前问的那一句。
///
/// 狗那头也有一份（`app/teleop.py` 的 `REMOTE_CONFIRM`，措辞的真理源在
/// 那儿）；这里留一份是因为**这个框要在发请求之前就弹出来** —— 先发一次
/// 讨那句话回来，等于「没确认就已经切了一半」。
const String remoteConfirmAsk = '远程遥控：你看不见狗周围的实际情况，'
    '只能看见画面里的那一块。这次确认会记在案。';

/// 心跳连着断了一秒之后，状态带上挂的那一句。
///
/// **只是一句话，不变灰、不弹框。** 心跳 5 Hz 地跑，拿它去关控制通路是过敏；
/// 而这条路断了的时候狗那头的守死人到期只会 `stop()` —— 狗是停着的，不是失控。
/// 该说出来的是「你和狗之间有一条路已经断了」，让人现在就往那台狗走过去。
const String beatTroubleHint = '心跳没到狗那儿：你和狗之间有一条路断了。'
    '狗那头到点会自己停 —— 别再往远处开';

/// 没有控制权那道闸落下来时说的那一句。
///
/// **跟「没有画面」那一句分开，绝不合成一句「不能操作」。** 两道闸都让杆变灰，
/// 可处置一点都不像：没有控制权要在上面那条面板里要一份（或者请对方交还），
/// 没有画面要去看相机和网。合成一句的话，人会朝错误的方向排查 —— 而排查的
/// 那几分钟里，狗就停在原地占着现场。
const String noControlHint = '没有控制权：先在上面那条里取得，取到之前两根杆是灰的';

/// 没有画面那道闸落下来时说的那一句（§5.9）。见 [noControlHint]：**两句话
/// 分开**，一句说该去要控制权，一句说该去把画面弄回来。
const String noVideoHint = '看不见就不许开狗：没有画面，两根杆是灰的';

/// 自己这头已经知道没有控制权了，而狗还是回了 409 时说的那一句（必修 6）。
///
/// **这一句存在的全部理由是不许再念那四个原因。** 409 那一句列的是「任务在
/// 跑、急停按着、控制权在别人手里、没有画面」——租约掉了的时候那四个一个都
/// 不是真的，而现场会照着其中一个去查一件根本不存在的事。自己这头
/// （[_TeleopPageState._held]）已经知道控制权不在手上，就该直说。
const String noControlPulseHint = '这一拍狗没收：控制权不在你手上。'
    '先在上面那条里重新取一次';

/// 发拍/心跳这两条**周期路径**的超时窗口（必修 4）。
///
/// `PatrolClient` 的默认超时是 10 秒 —— 那是给 `unlock`、盘况这类一次性请求
/// 的。一条 300 毫秒一发的路上等满 10 秒毫无意义：那一拍的答案早就过期了，
/// 而在这 10 秒里请求会一发一发堆起来，每一发各占一条连接。**超时之后那条
/// 连接是真的被掐掉的**（见 `PatrolClient._send`），不是只让 Future 放弃。
const Duration teleopPeriodicDeadline = Duration(seconds: 3);

/// 顶部那条带子的外壳：**限高，超了在里面滚，不往下长。**
///
/// **抽成一个函数是为了让测试对着同一个壳子验它吃不吃手势。** 这个壳子里的
/// `Scrollable` 挂的是一个 `HitTestBehavior.opaque` 的手势识别器：**它自己
/// 那块矩形里的指针，一律到不了 `Stack` 里排在它下面的东西**（滚不滚得动都
/// 一样，内容没溢出的时候也照吃）。所以这条规矩是：
///
/// **别往这条带子底下藏任何要接手势的东西。** 两根摇杆在屏幕底部、而且在
/// `Stack` 里排在带子后面（压在带子上面），不受影响 —— `teleop_page_test.dart`
/// 里那两条把这两件事都量出来了：壳子只吃自己那块矩形，且那块矩形够不着摇杆。
///
/// 另搭一个长得像的壳子去测的话，那个壳子明天就跟这里分家了 —— 分家那天两边
/// 都是绿的。
Widget bandShell({required double maxHeight, required Widget child}) =>
    ConstrainedBox(
      constraints: BoxConstraints(maxHeight: maxHeight),
      child: SingleChildScrollView(child: child),
    );

class TeleopPage extends StatefulWidget {
  const TeleopPage({
    super.key,
    required this.client,
    required this.robot,
    required this.operatorName,
    this.onOperatorChanged,
    this.health,
  });

  final PatrolClient client;

  /// **只用来显示名字**（[Robot.label]）。见文件头。
  final Robot robot;

  /// 这一趟是谁在开。来源是 `roster_page.dart::_open`（按狗落盘的那一份）。
  ///
  /// **屏上常显**（[OperatorChip]，人拍的板 3 的第一条补偿）：开狗的人一眼
  /// 看得见账记在谁头上。**不许说成「已登录」**（§6.3）。
  final String operatorName;

  /// 在这一屏上点 chip 换了人。**落盘是名册那一屏的事**，这儿只往上报。
  final ValueChanged<String>? onOperatorChanged;

  /// 「这一刻有没有画面」从哪儿来。
  ///
  /// 不给就自己起一个 [VideoHealthPoller]（真机上走的就是这条）。
  /// **给得进来是必须的，不是为测试开的口子**：Task 10 的 [LiveVideo] 本来
  /// 就要求外面递一条流进去，这一屏无论如何都得产出这条流；能注入，测试才
  /// 说得出「画面掉了」这句话 —— 否则只能靠等一台真狗超时。
  ///
  /// **必须是广播流**：这一屏和 [LiveVideo] 各听一份。不是广播的会自动包一层。
  final Stream<VideoHealth>? health;

  /// 顶部那两个档位按钮，和确认框上那个「切过去」。测试点它们。
  static const Key onsiteKey = ValueKey<String>('teleop-mode-onsite');
  static const Key remoteKey = ValueKey<String>('teleop-mode-remote');
  static const Key confirmRemoteKey = ValueKey<String>('teleop-remote-confirm');

  // ---------------------------------------------- 带子上每个会变的槽，一格一个 key
  //
  // **裁决 124：屏上会变的槽，一个都不许靠找那句中文找到。**
  // 这个病在控制权面板上治好过，第 7 卷里又复发了四次，所以这一屏是一次
  // 过完的，不是「哪条测试要用就补哪一个」。
  //
  // 按中文找有两种坏法，而且两种都不报错：
  //
  // - **改文案就红**，红在一条跟文案毫无关系的测试上 —— 于是下一个人把断言
  //   里的字串跟着改一遍，改完它还是绿的，可它此刻盯的是「屏上某处有这句
  //   话」，不再是「这一格里写着这句话」。
  // - **换了格子却不红**：同一句话被挪进另一个 `Column`、挪进一个点开才看得
  //   见的抽屉、甚至挪到面板里去，`find.text` 照样找得到 —— 而现场的人再也
  //   看不见它了。
  //
  // 挂 key 的那一格必须是**那个 `Text` 本身**，不是它外面的 `Padding`：挂在
  // `Padding` 上的话，测试读不到 `Text.data`，就只能退回去按中文比对。

  /// 「你正在开的是哪一台狗」那一格（[Robot.label]）。
  ///
  /// **这是防「开错狗」的最后一道视觉防线**，Ruling 84 拦着不许把别的东西
  /// 挤进它那一行，就是为了它不被压成省略号。挂上 key，测试才说得出
  /// 「这一格里写的是这一台」而不是「屏上某处出现过这个名字」。
  static const Key labelKey = ValueKey<String>('teleop-robot-label');

  /// 现场档那一行轻提示（[onsiteHint]）。**档位切走就该没有这一格。**
  static const Key onsiteHintKey = ValueKey<String>('teleop-onsite-hint');

  /// 发拍那条路要说的话。见 `_trouble`。
  static const Key troubleKey = ValueKey<String>('teleop-trouble');

  /// 心跳那条路要说的话（[beatTroubleHint]）。见 `_beatTrouble`。
  ///
  /// **跟 [troubleKey] 是两格，不是两种文案。** 合成一格的话，两条路会互相
  /// 抹，人看到的是一行在闪。
  static const Key beatTroubleKey = ValueKey<String>('teleop-beat-trouble');

  /// 没有控制权那道闸的话（[noControlHint]）。
  static const Key noControlKey = ValueKey<String>('teleop-no-control');

  /// 没有画面那道闸的话（[noVideoHint]）。**跟 [noControlKey] 分开**：
  /// 两道闸的处置一点都不像，合成一句人会朝错误的方向排查。
  static const Key noVideoKey = ValueKey<String>('teleop-no-video');

  /// 这一屏画哪一路。
  ///
  /// 前置那一路是开狗时唯一看得见「正前方有没有东西」的画面 —— 换路子的开关
  /// 归后面的任务，这里不给，免得人在开着的时候把画面切走。
  static const String camera = 'front';

  @override
  State<TeleopPage> createState() => _TeleopPageState();
}

class _TeleopPageState extends State<TeleopPage> with WidgetsBindingObserver {
  /// 发拍周期。
  ///
  /// 一拍最长 2 秒（狗那头 `MAX_PULSE_S`），而 `roam` 档一拍只有零点几秒 ——
  /// 推着不放必须续。不续的话狗走完一拍就停，人感觉像卡顿，会本能地把杆推
  /// 得更狠。
  static const Duration _pulsePeriod = Duration(milliseconds: 300);

  /// 心跳周期。**跟发拍分开。**
  ///
  /// 手停在零位的时候发拍是不发的（发零拍等于反复叫狗停，白占热点带宽），
  /// 可心跳不能跟着停：停了狗那头的守死人会判掉线，一恢复推杆就要重新起
  /// 一拍，中间那一下人感觉是卡了。
  static const Duration _beatPeriod = Duration(milliseconds: 200);

  /// 连着这么多拍被拒就把杆变灰。**狗不回话跟看不见画面是一回事。**
  ///
  /// 一两次是热点抖，连着三次就不是抖了 —— 而那时候屏幕上那根杆还亮着、还
  /// 跟着手指走，人以为狗在走。
  static const int _failsToGrey = 3;

  /// 变灰之后每隔几个周期探一次（300ms × 4 ≈ 1.2 秒）。
  ///
  /// **探的是同一个端点，而且探的是全零。** 全零那一拍在狗那头走的是
  /// `stop()`（`teleop.py` 里停车早返在视频闸的前面），看不见也允许，探一次
  /// 不会让狗动一下。不探的话杆一旦灰下去就再也回不来：灰的杆推不动，推不动
  /// 就没有下一拍，没有下一拍就永远不知道狗已经回话了。
  static const int _probeEvery = 4;

  /// 松手之后**接着再补几拍全零**（接下来 2 个周期，合计 3 拍 ≈ 0.6 秒）。
  ///
  /// 松手那一拍是立刻发的，可它要是丢了（热点抖一下就够），今天什么都不会
  /// 接上来 —— 零位不发拍那条抑制正好把补救也一起抑制掉了，只能退回狗那头
  /// 「一拍 0.4 秒走完 + 守死人 0.6 秒」。**那是兜底，不是停车方式。**
  /// 补发这几拍**绕过零位抑制、也不看杆是不是灰的**：停车这件事说三遍不贵，
  /// 说漏了要拿撞上去来还。
  static const int _stopBursts = 2;

  /// 心跳连着这么多次失败就在状态带上说一句（200ms × 5 ≈ 1 秒）。见
  /// [beatTroubleHint]。**不并进 [_failsToGrey]、不动 [_enabled]。**
  static const int _beatFailsToWarn = 5;

  /// 节奏档。`roam` 是默认档（Task 2 的 `PROFILES`）。
  ///
  /// **`seconds` 一律不传**，让狗那头按这一档算 —— 一拍多长是狗的手感参数，
  /// 标定改了不该要求每个客户端跟着改。
  static const String _profile = 'roam';

  late final Stream<VideoHealth> _health;
  StreamSubscription<VideoHealth>? _healthSub;
  VideoHealthPoller? _poller;
  Timer? _pulseTimer;
  Timer? _beatTimer;

  /// 左杆（平移）和右杆（转向）此刻指到哪儿。向右为正、向上为正。
  Offset _left = Offset.zero;
  Offset _right = Offset.zero;

  bool _live = false;
  int _fails = 0;
  int _probeTick = 0;

  /// 还欠几拍全零。见 [_stopBursts]。
  int _stopsLeft = 0;

  /// 心跳连着失败了几次。见 [_beatFailsToWarn]。
  int _beatFails = 0;

  /// 发拍那条路要说的话。**只有 [_post]（和视频回来那一下）写它、清它。**
  ///
  /// 跟 [_beatTrouble] 分开是必须的，不是洁癖：合在一个槽里的时候，发拍每
  /// 300 毫秒成功一次就把心跳那句话抹掉、心跳每 200 毫秒再挂回去 —— 那行字
  /// 在人正推着杆的时候一直在闪，而「心跳端点挂了、发拍还通」恰恰是这句话
  /// 唯一存在的理由。**每条路只清自己写的那一句。**
  String _trouble = '';

  /// 心跳那条路要说的话（[beatTroubleHint]）。**只有 [_beat] 写它、清它。**
  String _beatTrouble = '';

  String _mode = 'onsite';

  /// 屏上此刻挂着谁的名字。初值来自 [TeleopPage.operatorName]，点 chip 就换。
  ///
  /// **换了立刻生效，不等狗回话。** 名字的家在手机上（按狗落盘），狗那头只是
  /// 记一条留痕；等一次往返才换的话，热点抖一下人就会以为没改成、再点一次。
  late String _operator = widget.operatorName;

  /// 确认过一次就不再问（§7.8）。
  ///
  /// **每次都弹同一个框，第三次就被条件反射点掉了。** 真正兜底的是狗那头
  /// 那条留痕（Task 7），不是这个框弹了几次。
  ///
  /// **切档请求失败了也不回滚这一位 —— 跟 [_mode] 的不对称是有意的。**
  /// 两个变量记的不是同一件事：[_mode] 是**对狗的状态的断言**，它决定屏幕上
  /// 写着哪一档，而档位就是速度上限，屏幕和狗不一致人就会按错的手感推，所以
  /// 它必须跟着请求的成败走；这一位记的是**这个人被告知过一次** —— 那件事
  /// 已经发生了，POST 落没落地改变不了它。真「补上对称」的话，热点抖一下就
  /// 要重弹一次框，正好把这个框推向 §7.8 说的那个结局（第三次被条件反射点
  /// 掉）。也没有留下漏洞：失败时 [_mode] 退回 `onsite`，`_setMode` 开头那句
  /// `if (name == _mode) return;` 堵不住人；重试那一次照样带 `confirmed`，
  /// 狗那头的留痕在请求真正落地的那一次建得起来。
  bool _remoteConfirmed = false;

  /// 控制权在不在自己手上。**由上面那条 [ControlPanel] 说了算。**
  bool _held = false;

  /// 发拍那条路上一发还没回来（必修 4）。
  ///
  /// **只挡周期发拍和探针，不挡停车那几拍。** 停车说三遍不贵，说漏了要拿
  /// 撞上去来还（见 [_stopBursts]）。
  bool _posting = false;

  /// 心跳那条路上一发还没回来（必修 4）。
  bool _beating = false;

  /// app 还在前台（必修 1）。**进了后台就不发拍、不发心跳。**
  bool _awake = true;

  /// 退出这一屏的那一拍全零已经发过了（必修 2）。
  ///
  /// [_leaveThenPop] 和 [dispose] 两条路都会发它，而正常退出走的是前者 ——
  /// 这一位让后者不再补一发。
  bool _farewellSent = false;

  bool get _moving => _left != Offset.zero || _right != Offset.zero;

  /// 杆是不是亮的。**三道闸，任何一道落下都变灰。**
  ///
  /// 控制权这一道不是可选的：狗那头会动腿的接口全都要控制权
  /// （`app/control.py` 的 `CONTROLLED`）。少了它，人推杆得到的是一串 409，
  /// 而杆还亮着、还跟着手指走 —— 人以为狗在走。
  bool get _enabled => _live && _held && _fails < _failsToGrey;

  @override
  void initState() {
    super.initState();
    final Stream<VideoHealth>? given = widget.health;
    if (given != null) {
      _health = given.isBroadcast ? given : given.asBroadcastStream();
    } else {
      final VideoHealthPoller p = VideoHealthPoller(client: widget.client);
      _poller = p;
      _health = p.stream;
    }
    _healthSub = _health.listen(_onHealth);
    _startTimers();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    // **临走再发一拍全零（必修 2）。** 正常退出走的是 [_leaveThenPop]，那条
    // 路上这一拍已经发过而且等过它落地了；这儿接的是别的走法（整个
    // `Navigator` 被换掉、`TeleopPage` 被从树上摘走）。
    //
    // **这一拍必须排在两个 `cancel()` 前面。** 至于「连接会不会先被收掉」
    // 那一半，从建议 11 那一轮起不再是时序问题：连接按狗缓存在
    // `roster_page._clients` 里，**退屏根本不收它**，这一拍发给的是一个
    // 照常活着的 `HttpClient`。`dispose()` 里等不了它落地，所以它只是
    // 兜底，不是停车方式。
    if (!_farewellSent) {
      _farewellSent = true;
      unawaited(_post(_stopBody()));
    }
    _pulseTimer?.cancel();
    _beatTimer?.cancel();
    _healthSub?.cancel();
    _poller?.stop();
    super.dispose();
  }

  void _startTimers() {
    _pulseTimer?.cancel();
    _beatTimer?.cancel();
    _pulseTimer = Timer.periodic(_pulsePeriod, (_) => _tick());
    _beatTimer = Timer.periodic(_beatPeriod, (_) => unawaited(_beat()));
  }

  void _stopTimers() {
    _pulseTimer?.cancel();
    _pulseTimer = null;
    _beatTimer?.cancel();
    _beatTimer = null;
  }

  // ------------------------------------------------------------ 人在不在

  /// 切后台 / 锁屏 / 来电（必修 1）。
  ///
  /// 以前这一屏一处生命周期都没有。人手里推着左杆让狗前进，这时候来电铃响、
  /// 或者按了 Home、或者屏幕到点自动锁屏 —— `Timer.periodic` 在 Android 上
  /// 进后台照常跑（引擎只停 vsync，不停 isolate），[_pulseTimer] 会拿着最后
  /// 那个非零的 [_left] 一直发下去，**狗可能不停**。停车唯一的指望是 Android
  /// 把 `ACTION_CANCEL` 送进来触发摇杆的松手，而锁屏/Home 这条路上发不发
  /// cancel 是依机型和 ROM 的。
  ///
  /// **`inactive` 不算「人不在了」** —— 理由跟 `control_panel.dart` 里那条
  /// 一字不差（通知栏下拉、音量条、权限框那几下人还握着手机）。
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

  /// 人不在了：**先把杆归零并立刻发一拍全零，再停表。**
  ///
  /// 顺序不许换。先停表的话，那一拍全零就得靠 [_post] 自己走出去，而
  /// [_stopNow] 排的那几拍补发（[_stopBursts]）全都落进一个已经死掉的定时器
  /// 里 —— 松手那一拍丢了就没有第二次机会。
  ///
  /// **弃租不在这儿**：那是 [ControlPanel] 的事（它自己也挂了生命周期），
  /// 它弃完会回调 [_onHeld]`(false)`，这一屏跟着再变一次灰。
  void _onGone() {
    if (!_awake) return;
    _awake = false;
    if (_moving) {
      setState(() {
        _left = Offset.zero;
        _right = Offset.zero;
      });
    }
    _stopsLeft = 0;
    // **同步发一拍全零。** 不走 [_stopNow]：那个会排上接下来几拍，而表马上
    // 就要停了，排了也发不出去。
    unawaited(_post(_stopBody()));
    _stopTimers();
  }

  /// 回前台：把表重新起起来。
  ///
  /// **不自动恢复控制权** —— 那是 [ControlPanel] 的事，它也是有意不恢复的。
  /// 这儿起的表只是让「狗现在还认不认我」这件事重新问得出来：杆此刻是灰的
  /// （[_held] 已经被弃租那一下翻成 false），人要重新取一次控制权才推得动。
  void _onResumed() {
    if (_awake) return;
    _awake = true;
    _startTimers();
  }

  // ------------------------------------------------------------ 画面那道闸

  /// 控制权到手 / 掉了。
  ///
  /// **掉了要跟画面掉了走同一条路**（[_disarmIfNeeded]）：人手里正推着杆而
  /// 控制权被别人接管走了，狗那头下一拍就是 409 —— 杆得立刻归零并主动发一次
  /// 全零停，不能等下一个周期。
  void _onHeld(bool held) {
    if (held == _held) return;
    setState(() => _held = held);
    _disarmIfNeeded();
  }

  /// 点 chip 换了人。**屏上先换，再往上报（落盘在名册那一屏）。**
  void _onOperatorChanged(String name) {
    if (name == _operator) return;
    setState(() => _operator = name);
    widget.onOperatorChanged?.call(name);
  }

  void _onHealth(VideoHealth h) {
    final bool live = h.anyLive;
    if (live == _live) return;
    setState(() {
      _live = live;
      if (live) {
        // 画面回来了，之前那句「看不见」就不该再挂着。
        _trouble = '';
      }
    });
    _disarmIfNeeded();
  }

  /// 杆刚变灰，而人手里还推着 —— 把杆归零，并**立刻**发一次全零停。
  ///
  /// **不能等下一个周期。** 变灰之后周期发拍就不发了，狗那头只剩守死人
  /// （0.6 秒）兜底：那半秒足够撞上东西。而全零那一拍在狗那头走的是
  /// `stop()`，看不见也允许 —— 「因为看不见所以不许停」是把狗锁在最后一个
  /// 动作上，掉线恰恰是最需要它停下来的时候。
  void _disarmIfNeeded() {
    if (_enabled || !_moving) return;
    setState(() {
      _left = Offset.zero;
      _right = Offset.zero;
    });
    _stopNow();
  }

  /// 立刻发一拍全零，并且**排上接下来那几拍**。见 [_stopBursts]。
  void _stopNow() {
    _stopsLeft = _stopBursts;
    unawaited(_post(_stopBody()));
  }

  // ------------------------------------------------------------ 两根杆

  void _onLeft(Offset v) => _onStick(() => _left = v);

  void _onRight(Offset v) => _onStick(() => _right = v);

  void _onStick(VoidCallback apply) {
    final bool was = _moving;
    setState(apply);
    if (was && !_moving) {
      // **松手主动发一次全零停，不等下一个周期**，而且接着再补两拍。
      // 守死人（0.6 秒）是兜底，不是停车方式：靠它停意味着松手之后狗还会
      // 往前走大半秒。而只发一拍的话，那一拍丢了就等于退回守死人。
      _stopNow();
    }
  }

  // ------------------------------------------------------------ 发拍与心跳

  void _tick() {
    if (_moving && _enabled) {
      _probeTick = 0;
      // 又推起来了：欠着的那几拍全零到此为止，不然会跟正推着的拍打架。
      _stopsLeft = 0;
      // **上一发还没回来就跳过这一拍**（必修 4）。狗那头 HTTP 线程卡住而
      // TCP 还接得上的时候（扫盘、写归档），不挡的话手机会在超时窗口里堆出
      // 几十条在途连接，把狗自己的热点上行挤死 —— 而屏上报的是「看不见就
      // 不许开狗」，人会去修相机。跳过这一拍不会让狗多走：狗那头一拍本来就
      // 有 `MAX_PULSE_S`，走完自己停。
      if (_posting) return;
      unawaited(_post(_pulseBody()));
      return;
    }
    if (_stopsLeft > 0) {
      // 松手/变灰之后补发的全零。**排在零位抑制和探针的前面**，
      // 而且不看杆是不是灰的 —— 停车那几拍在狗那头走的是 `stop()`。
      //
      // **这一支不看 [_posting]。** 在途就跳过那条闸是为了不让请求堆起来，
      // 而停车这件事说三遍不贵，说漏了要拿撞上去来还。
      _stopsLeft--;
      unawaited(_post(_stopBody()));
      return;
    }
    if (_fails >= _failsToGrey) {
      // 灰下去之后自己找回来。见 [_probeEvery]。
      _probeTick++;
      if (_probeTick % _probeEvery == 0 && !_posting) {
        unawaited(_post(_stopBody()));
      }
      return;
    }
    // 两个杆都在零位：**不发**。发零拍等于反复叫狗停，白占热点带宽。
  }

  Future<void> _beat() async {
    // 上一发还没回来就跳过这一拍（必修 4）。见 [_tick] 里那段。
    if (_beating) return;
    _beating = true;
    try {
      await widget.client
          .post('/api/teleop/heartbeat', null, teleopPeriodicDeadline);
      if (!mounted || _beatFails == 0) return;
      _beatFails = 0;
      // 自己挂上去的那句话自己收掉；发拍那条路的槽不碰。
      if (_beatTrouble.isNotEmpty) setState(() => _beatTrouble = '');
    } catch (e) {
      // **心跳失败不计进变灰的那三次。** 它 5 Hz 地跑，拿它去关控制通路是
      // 过敏；「狗不回话」该不该变灰由发拍那条路说了算。
      developer.log('$e', name: 'teleop/heartbeat');
      if (!mounted) return;
      _beatFails++;
      // **一次抖动不上屏，连着一秒才说话**（见 [beatTroubleHint]）。
      // 写的是自己那个槽：发拍那条路的话更急，两句话各占一行，谁也不盖谁。
      if (_beatFails >= _beatFailsToWarn && _beatTrouble.isEmpty) {
        setState(() => _beatTrouble = beatTroubleHint);
      }
    } finally {
      _beating = false;
    }
  }

  Map<String, dynamic> _pulseBody() => <String, dynamic>{
        'fwd': _left.dy,
        // **屏幕向右是 `dx` 为正，而狗那头 `lat`/`yaw` 为正是「左」**
        // （`app/static/index.html`：「左移」是 `lat=1`、「左转」是 `yaw=1`，
        // 就是 ROS 那套右手系）。这里翻号，摇杆那一层不认识狗。
        'lat': _flip(_left.dx),
        'yaw': _flip(_right.dx),
        'profile': _profile,
        // `seconds` 不传 —— 见 [_profile]。
      };

  Map<String, dynamic> _stopBody() => <String, dynamic>{
        'fwd': 0.0,
        'lat': 0.0,
        'yaw': 0.0,
        'profile': _profile,
      };

  /// 翻号，并且**不让 `-0.0` 上线**：`jsonEncode` 会把它写成 `-0.0`，
  /// 而「停」应该长得干干净净。
  static double _flip(double v) => v == 0.0 ? 0.0 : -v;

  Future<void> _post(Map<String, dynamic> body) async {
    _posting = true;
    try {
      await widget.client.post('/api/teleop', body, teleopPeriodicDeadline);
      if (!mounted) return;
      // 收掉的**只有发拍自己挂上去的那句**（[_trouble]）。心跳那句归心跳
      // 那条路管 —— 发拍通不代表心跳通，抹掉别人的话就是在骗人。
      if (_fails != 0 || _trouble.isNotEmpty) {
        setState(() {
          _fails = 0;
          _trouble = '';
        });
      }
    } catch (e) {
      // **原文只进日志。** 屏幕上要的是「该做什么」，Dart 的异常文本回答
      // 不了这个 —— 跟 `live_video.dart` 里 `_human` 一个规矩。
      developer.log('$e', name: 'teleop/pulse');
      if (!mounted) return;
      setState(() {
        _fails++;
        _trouble = _human(e);
      });
      _disarmIfNeeded();
    } finally {
      _posting = false;
    }
  }

  /// 把异常翻成现场看得懂的一句话。
  ///
  /// **不弹框**（§5.9 不设「确认后继续」的口子），就挂在顶部那条状态带上。
  String _human(Object e) {
    if (e is PatrolError && e.status == HttpStatus.conflict) {
      // **自己这头已经知道控制权不在手上，就不许再念那四个原因**（必修 6）。
      // 租约掉了的时候那四个一个都不是真的，而现场会照着其中一个去查一件
      // 根本不存在的事。
      if (!_held) return noControlPulseHint;
      // **「没有画面」也是 409。** 狗那头 §5.9 那道视频闸拒绝的时候回的是
      // 同一个码（`server.py` 的 `_video_gate` -> `TeleopBusy` -> 409）——
      // 不提这一种的话，屏幕上会同时挂着「看不见」和一句不相干的话，人会
      // 去找急停、找控制权，而该做的是把画面弄回来。
      return '狗那头现在不接遥控：任务在跑、急停按着、控制权在别人手里，'
          '或者没有画面';
    }
    if (e is PatrolError && e.status == HttpStatus.forbidden) {
      return '这个身份不许开狗。先取控制权';
    }
    if (e is PatrolError && e.status == HttpStatus.unauthorized) {
      return '狗不认这个身份了。退出去重新输 PIN';
    }
    return '这一拍没发到狗那儿，过几秒会自己再试。一直是这句就走过去看看那台狗';
  }

  // ------------------------------------------------------------ 现场 / 远程

  Future<void> _setMode(String name) async {
    if (name == _mode) return;
    if (name == 'remote' && !_remoteConfirmed) {
      final bool ok = await _askRemote();
      if (!mounted || !ok) return;
      _remoteConfirmed = true;
    }
    final String was = _mode;
    setState(() => _mode = name);
    try {
      await widget.client.post('/api/teleop/mode', <String, dynamic>{
        'mode': name,
        // 切回现场不用确认：往安全的方向走不该设卡，多设一道人就懒得切回来。
        if (name == 'remote') 'confirmed': true,
      });
    } catch (e) {
      developer.log('$e', name: 'teleop/mode');
      if (!mounted) return;
      setState(() {
        // **切失败就把屏幕退回上一档。**
        // 档位决定的是速度上限：屏幕上写着「远程」而狗那头还在「现场」，
        // 人会按远程那一档的手感去推，而狗按现场那一档收着走 —— 反过来更
        // 糟。一句话纠正不了一整屏的视觉，得把档退回去。
        _mode = was;
        _trouble = '档没切成，屏幕和狗都还按上一档算';
      });
    }
  }

  Future<bool> _askRemote() async {
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text('切到远程遥控'),
        content: const Text(remoteConfirmAsk),
        actions: <Widget>[
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('先不切')),
          TextButton(
              key: TeleopPage.confirmRemoteKey,
              onPressed: () => Navigator.of(ctx).pop(true),
              child: const Text('切过去')),
        ],
      ),
    );
    return ok ?? false;
  }

  // ------------------------------------------------------------ 画

  /// 人按了返回键/返回手势（必修 2）。
  ///
  /// **停止指令必须在路由弹走之前发出去，而且要等它落地。** 以前这条路上一个
  /// 字节都发不出去，两件事叠在一起：
  ///
  /// * `dispose()` 里压根没有停车那一拍；
  /// * 就算有也发不出去 —— `roster_page._open` 里 `client.close()` 排在
  ///   `TeleopPage.dispose()` **前面**（`await push` 在 `didPop` 那一刻就
  ///   完成，而 `dispose()` 要等退场动画跑完），发给的是一个已经被
  ///   `force: true` 关掉的 `HttpClient`。
  ///
  /// 于是操作员推着杆用返回手势退出遥控屏，狗要靠守死人在「一拍走完 +
  /// 0.6 秒守死」之后才停 —— 最坏接近一秒。**那是兜底，不是停车方式。**
  ///
  /// 第二件事从建议 11 那一轮起彻底没了：连接按狗缓存在
  /// `roster_page._clients` 里，**退屏不再收连接**。所以这条路上现在只剩
  /// 一件事要保证 —— 那一拍要在路由弹走之前发出去并且等它落地。
  Future<void> _leaveThenPop() async {
    _farewellSent = true;
    // 先停表：接下来这一拍是最后一拍，不该有别的拍跟它抢。
    _stopTimers();
    if (_moving) {
      setState(() {
        _left = Offset.zero;
        _right = Offset.zero;
      });
    }
    _stopsLeft = 0;
    try {
      // **等它落地再放行。** 超时窗口是周期路径那个短的：返回键不许被一条
      // 卡住的连接按住不放。
      await widget.client
          .post('/api/teleop', _stopBody(), teleopPeriodicDeadline);
    } catch (e) {
      // 发不出去也照样放人走：屏上再写什么都没人读了，而人正在离开这一屏。
      developer.log('$e', name: 'teleop/leave');
    }
    if (!mounted) return;
    // **这儿用 `pop()`，不是 `maybePop()`。**
    //
    // `maybePop()` 会再问一次 `PopScope` —— 而 `canPop` 那一位是随着下一帧
    // 才登记进路由的，这一刻它读到的还是「不放行」，于是这一屏永远退不出去
    // （本机真卡在这儿过：那一拍全零发得好好的，人却退不回名册）。既然
    // 该等的已经等完了，这里就是硬退。
    final NavigatorState nav = Navigator.of(context);
    if (nav.canPop()) nav.pop();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      // **一律不放行，退出这件事由 [_leaveThenPop] 自己完成。** 让它在发完
      // 那一拍之后翻成 `true` 再 `maybePop()` 是走不通的：那一位要等下一帧
      // 才登记进路由。
      canPop: false,
      onPopInvokedWithResult: (bool didPop, Object? result) {
        if (didPop || _farewellSent) return;
        unawaited(_leaveThenPop());
      },
      child: _body(),
    );
  }

  Widget _body() {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: LayoutBuilder(
          builder: (BuildContext ctx, BoxConstraints box) {
            final double side = _stickBoxFor(box.maxWidth);
            return Stack(
              children: <Widget>[
                // 视频铺满，垫在最底下。
                Positioned.fill(
                  child: LiveVideo(
                    baseUrl: widget.client.baseUrl,
                    camera: TeleopPage.camera,
                    health: _health,
                    token: widget.client.token ?? '',
                  ),
                ),
                Positioned(
                    top: 0,
                    left: 0,
                    right: 0,
                    child: bandShell(
                        maxHeight: _bandMaxFor(box.maxHeight), child: _band())),
                Positioned(
                  left: 0,
                  bottom: 0,
                  width: side,
                  height: side,
                  child: Joystick(
                    axis: JoystickAxis.translate,
                    enabled: _enabled,
                    onChanged: _onLeft,
                  ),
                ),
                Positioned(
                  right: 0,
                  bottom: 0,
                  width: side,
                  height: side,
                  child: Joystick(
                    axis: JoystickAxis.turn,
                    enabled: _enabled,
                    onChanged: _onRight,
                  ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }

  /// 一根杆占多大一块。两倍于 [Joystick.radius] 再宽一点：推到底的那一下
  /// 手指还在这块里，不至于滑出去把这一拍丢了。
  static const double _stickBoxMax = 200.0;

  /// 两块中间至少留这么宽。**两个拇指不能挨着。**
  static const double _stickGap = 24.0;

  /// 这么宽的屏上一根杆该占多大一块。
  ///
  /// **写死 200 的话，<400dp 的竖屏上两根杆是叠着的** —— 重叠那一块归
  /// `Stack` 里靠后的右杆，于是人按左下角想往前走，狗原地转弯。
  /// 这里不设下限：屏再窄，杆跟着变小也好过两根叠在一起（`Joystick` 的圆心
  /// 是按下那一点，盒子小一点只是量程短一点，不会按不准）。
  ///
  /// 横屏锁定不在这儿做，那一条记在真机清单上。
  static double _stickBoxFor(double width) {
    if (!width.isFinite) return _stickBoxMax;
    final double half = (width - _stickGap) / 2;
    return half < _stickBoxMax ? half : _stickBoxMax;
  }

  /// 这条带子最多占屏幕的多少。**不许无限往下长。**
  ///
  /// 带子最坏已经四五行（名字 / 控制权面板 / 现场提示 / 发拍那句 / 心跳那句 /
  /// 没画面那句），再往下长就盖掉画面顶部 —— 而正前方远处那一块恰恰是开狗的
  /// 人最需要看的。超了在带子里面滚：滚得到，又挡不住画面。
  static double _bandMaxFor(double height) =>
      height.isFinite ? height * 0.45 : 240.0;

  /// 顶部那条状态带。
  ///
  /// **Task 12 的控制权面板在这儿单占一行。**
  ///
  /// **不许塞进上面那个 `Row`**（Ruling 84）：那一行是
  /// `Expanded(Text(robot.label))` + 两个档位按钮，再往里加倒计时和按钮，
  /// `robot.label` 会被挤成省略号 —— 而 label 正是防「开错狗」的最后一道
  /// 视觉防线，现场有三台狗的时候，它是唯一能告诉人「你正在开的是哪一台」
  /// 的东西。
  Widget _band() => Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        color: Colors.black.withValues(alpha: 0.55),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Row(
              children: <Widget>[
                Expanded(
                  child: Text(widget.robot.label,
                      key: TeleopPage.labelKey,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          color: Colors.white, fontSize: 15)),
                ),
                _modeButton(TeleopPage.onsiteKey, 'onsite', '现场'),
                const SizedBox(width: 4),
                _modeButton(TeleopPage.remoteKey, 'remote', '远程'),
              ],
            ),
            // **署名 chip：同一个 `Column` 里自己一行，永远画得出来。**
            //
            // 不塞进上面那个 `Row`（Ruling 84 的同一条理由）：那一行是
            // `Expanded(Text(robot.label))` + 两个档位按钮，再挤进来会把
            // `robot.label` 压成省略号，而它是防「开错狗」的最后一道视觉防线。
            //
            // **也绝不许挪进任何要点开才看得见的地方**（人拍的板 3 的第一条
            // 补偿）：这一格存在的全部理由就是让开狗的人在**没有任何动作**的
            // 情况下看见账记在谁头上。挪进二级页之后，代价（乙的操作签在甲名
            // 下）照旧，补偿没了。
            Align(
              alignment: Alignment.centerLeft,
              child: OperatorChip(
                name: _operator,
                client: widget.client,
                dark: true,
                onChanged: _onOperatorChanged,
              ),
            ),
            // 控制权面板：**同一个 `Column` 里新起一行**，见上面的 Ruling 84。
            ControlPanel(client: widget.client, onHeld: _onHeld),
            if (_mode == 'onsite')
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text(onsiteHint,
                    key: TeleopPage.onsiteHintKey,
                    style: TextStyle(color: Colors.white70, fontSize: 13)),
              ),
            if (_trouble.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(_trouble,
                    key: TeleopPage.troubleKey,
                    style: const TextStyle(
                        color: Color(0xFFFFB4A9), fontSize: 13)),
              ),
            // 心跳那句话**自己一行**。跟发拍那句挤一个槽的话，两条路会互相
            // 抹，人看到的是一行在闪 —— 闪着的字比没有字更容易被当成花屏。
            if (_beatTrouble.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(_beatTrouble,
                    key: TeleopPage.beatTroubleKey,
                    style: const TextStyle(
                        color: Color(0xFFFFB4A9), fontSize: 13)),
              ),
            // **两道闸各说各的话。** 见 [noControlHint]：合成一句「不能操作」
            // 的话，人会朝错误的方向去排查。
            if (!_held)
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text(noControlHint,
                    key: TeleopPage.noControlKey,
                    style: TextStyle(color: Color(0xFFFFB4A9), fontSize: 13)),
              ),
            if (!_live)
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text(noVideoHint,
                    key: TeleopPage.noVideoKey,
                    style: TextStyle(color: Color(0xFFFFB4A9), fontSize: 13)),
              ),
          ],
        ),
      );

  Widget _modeButton(Key key, String name, String label) {
    final bool on = _mode == name;
    return TextButton(
      key: key,
      onPressed: () => unawaited(_setMode(name)),
      style: TextButton.styleFrom(
        minimumSize: const Size(56, 36),
        backgroundColor: on ? Colors.white24 : Colors.transparent,
        foregroundColor: on ? Colors.white : Colors.white60,
      ),
      child: Text(label),
    );
  }
}
