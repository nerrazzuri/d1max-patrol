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
import 'widget/joystick.dart';
import 'widget/live_video.dart';

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

class TeleopPage extends StatefulWidget {
  const TeleopPage({
    super.key,
    required this.client,
    required this.robot,
    this.health,
  });

  final PatrolClient client;

  /// **只用来显示名字**（[Robot.label]）。见文件头。
  final Robot robot;

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

  /// 这一屏画哪一路。
  ///
  /// 前置那一路是开狗时唯一看得见「正前方有没有东西」的画面 —— 换路子的开关
  /// 归后面的任务，这里不给，免得人在开着的时候把画面切走。
  static const String camera = 'front';

  @override
  State<TeleopPage> createState() => _TeleopPageState();
}

class _TeleopPageState extends State<TeleopPage> {
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

  bool get _moving => _left != Offset.zero || _right != Offset.zero;

  /// 杆是不是亮的。**两道闸，任何一道落下都变灰。**
  bool get _enabled => _live && _fails < _failsToGrey;

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
    _pulseTimer = Timer.periodic(_pulsePeriod, (_) => _tick());
    _beatTimer = Timer.periodic(_beatPeriod, (_) => _beat());
  }

  @override
  void dispose() {
    _pulseTimer?.cancel();
    _beatTimer?.cancel();
    _healthSub?.cancel();
    _poller?.stop();
    super.dispose();
  }

  // ------------------------------------------------------------ 画面那道闸

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
      unawaited(_post(_pulseBody()));
      return;
    }
    if (_stopsLeft > 0) {
      // 松手/变灰之后补发的全零。**排在零位抑制和探针的前面**，
      // 而且不看杆是不是灰的 —— 停车那几拍在狗那头走的是 `stop()`。
      _stopsLeft--;
      unawaited(_post(_stopBody()));
      return;
    }
    if (_fails >= _failsToGrey) {
      // 灰下去之后自己找回来。见 [_probeEvery]。
      _probeTick++;
      if (_probeTick % _probeEvery == 0) unawaited(_post(_stopBody()));
      return;
    }
    // 两个杆都在零位：**不发**。发零拍等于反复叫狗停，白占热点带宽。
  }

  Future<void> _beat() async {
    try {
      await widget.client.post('/api/teleop/heartbeat');
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
    try {
      await widget.client.post('/api/teleop', body);
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
    }
  }

  /// 把异常翻成现场看得懂的一句话。
  ///
  /// **不弹框**（§5.9 不设「确认后继续」的口子），就挂在顶部那条状态带上。
  String _human(Object e) {
    if (e is PatrolError && e.status == HttpStatus.conflict) {
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

  @override
  Widget build(BuildContext context) {
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
                Positioned(top: 0, left: 0, right: 0, child: _band()),
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

  /// 顶部那条状态带。
  ///
  /// **Task 12 的控制权面板挂在这儿**（下面那条注释标的位置）：租约倒计时、
  /// 谁拿着、抢过来的按钮，都在同一条带子上 —— 开狗的人眼睛在画面上，能顺
  /// 眼扫到的只有这一条。
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
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          color: Colors.white, fontSize: 15)),
                ),
                // ↓↓↓ Task 12 的控制权面板插在这儿（档位按钮的左边）↓↓↓
                _modeButton(TeleopPage.onsiteKey, 'onsite', '现场'),
                const SizedBox(width: 4),
                _modeButton(TeleopPage.remoteKey, 'remote', '远程'),
              ],
            ),
            if (_mode == 'onsite')
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text(onsiteHint,
                    style: TextStyle(color: Colors.white70, fontSize: 13)),
              ),
            if (_trouble.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(_trouble,
                    style: const TextStyle(
                        color: Color(0xFFFFB4A9), fontSize: 13)),
              ),
            // 心跳那句话**自己一行**。跟发拍那句挤一个槽的话，两条路会互相
            // 抹，人看到的是一行在闪 —— 闪着的字比没有字更容易被当成花屏。
            if (_beatTrouble.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(_beatTrouble,
                    style: const TextStyle(
                        color: Color(0xFFFFB4A9), fontSize: 13)),
              ),
            if (!_live)
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text('看不见就不许开狗：没有画面，两根杆是灰的',
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
