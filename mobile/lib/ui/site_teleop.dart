/// 站点模式的遥控页（W00c5c，受决策 7 约束）。
///
/// - **连接就是租约**：进这一页开一条遥控连接；退出、断线都放租，狗停。
/// - **画面铺满整屏**（2026-10-30 用户）：两根半透明的杆浮在左下、右下，「停」在右上，状态在上边。
/// - 左杆上下是前后、左右是横移（能横移的狗才有，站点 `granted` 里给 `max_vy`；推斜了只走大的那一个轴）；
///   右杆只转向。按住每 100 ms 发一帧，松手发零速。站点和狗都会再夹限速。
/// - **大红「停」走站点的 halt，不走遥控连接**（连接卡住的时候正是最需要停车的时候）；同时发零速。
/// - 画面没了、租约结束、连接断了：屏上立刻说，杆变灰。
/// - **键盘**（W15 桌面版；手机接了蓝牙键盘也一样）：W/↑ 前进、S/↓ 后退、A/← 左转、D/→ 右转，
///   Q/E 往左、往右横移；**松开就停**；Esc 是「停」（同大红按钮）。跟摇杆一样每 100 ms 发一帧、站点和狗再夹限速。
///   **按键只在「能开」的那一段里算数**（W15 外审阻断）：系统连发一律不认；不能开的时候按下的不记；
///   每过一道边界 —— 拿到遥控、画面断了或回来、切走（失去焦点、inactive、后台）、结束、停车、退出 ——
///   都清空键盘输入、发一帧零速。之后要动，得**重新按下**：按着不放等授权到、等画面回来不会接着开。
/// - **切到后台就结束遥控**（零速、放租、关连接）；通知栏拉一下（inactive）只把杆值归零、
///   照发零速。后台里定时器还在跑的话，手指按着的那个杆值会一直发出去（W00c5e 内部评审）。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_robot.dart' show postureIcon, postureLabel;
import 'site_video.dart';
import 'orientation.dart';
import 'widget/joystick.dart';

/// 硬禁区守卫（决策 65）说的话：狗在续租回执里说、站点转过来的 `{state, label?, max_vx?}`。
/// 没什么要说的（`ok`、看不懂）是 null。
String? teleopZoneText(Map<String, dynamic>? zg) {
  if (zg == null) return null;
  final name = '${zg['label'] ?? ''}'.isNotEmpty ? '${zg['label']}' : '${zg['zone'] ?? ''}';
  final v = (zg['max_vx'] as num?)?.toStringAsFixed(1) ?? '0.2';
  return switch ('${zg['state']}') {
    'limited' => tr('Close to hard no-go zone "$name": slowing down', '接近硬禁区「$name」：减速'),
    'blocked' => tr('Hard no-go zone "$name" ahead: forward movement blocked. Back up or turn away',
        '前方为硬禁区「$name」：禁止继续前进，可后退或转向'),
    'inside' => tr('At or inside hard no-go zone "$name": only moves that lead out are allowed',
        '机器人位于硬禁区「$name」边缘或内部：只能向外移动'),
    'untrusted' when zg['in_sight'] == true => tr(
        'Position not trusted. You are on site watching the robot: speed allowed up to $v m/s. Hard no-go zones are not checked; middle gear needs a trusted position',
        '位置不可信，你在现场看着：放开到 $v m/s；不再自动避开硬禁区，中速档要位置可信'),
    'untrusted' => tr(
        'Position not trusted: hard no-go zones (water, drops) cannot be checked. Speed limited to $v m/s, watch the video',
        '位置不可信：无法保证避开硬禁区（水体、落差），限速 $v m/s，请看着画面操作'),
    'unsupported' => tr(
        'This robot\'s software does not keep remote control out of hard no-go zones: watch the video and update the robot',
        '此机器人软件版本过旧，遥控时不避开硬禁区：请看着画面操作并升级'),
    _ => null,
  };
}

/// 结束原因给人看的话。`by`：谁中止的（站点在 `aborted` 里说，现场 11-05）。
String teleopEndText(String reason, [String by = '']) => switch (reason) {
      'released' => tr('You released control', '你已退出遥控'),
      'disconnected' => tr('Remote control connection lost. The robot has stopped', '遥控连接已断开，机器人已停止'),
      'aborted' when by == 'site:swap' => tr(
          'The robot is missing a battery: the site stopped remote control. It resumes by itself once the battery is back',
          '机器人少一块电池，站点叫停了遥控；电池装好后自动恢复'),
      'aborted' when by.startsWith('site:') =>
        tr('The site stopped remote control ($by). The robot has stopped', '站点叫停了遥控（$by），机器人已停止'),
      'aborted' when by.isNotEmpty => tr('$by stopped remote control', '$by 中止了遥控'),
      'aborted' => tr('Remote control was stopped. The robot has stopped', '遥控被中止，机器人已停止'),
      'taken_over' => tr('An admin took control', '管理员已接管遥控'),
      'halt' => tr('Someone pressed stop', '有人按下了停车'),
      'robot_offline' => tr('The robot went offline', '机器人已离线'),
      'lease_lost' => tr('The control lease could not be renewed. Remote control ended', '租约续期失败，遥控已结束'),
      'logged_out' => tr('Your session has expired', '登录已过期'),
      _ => tr('Remote control ended ($reason)', '遥控结束（$reason）'),
    };

/// POC 速度档（用户 11-05：按速度分）：上次选的速度（m/s；null = 没选过，按 0.6）。进程里记着，
/// [teleopTierFile] 设了（`main()` 里）就也存到本机文件，下次开 App 还是它。
double? teleopSpeedPick;
File? teleopTierFile;

Future<void> loadTeleopTier(File f) async {
  teleopTierFile = f;
  try {
    if (await f.exists()) {
      final v = jsonDecode(await f.readAsString());
      final sp = v is Map ? v['speed'] : null; // 改版前存的 {'tier': 'fast'} 不认，当没选过
      if (sp is num && sp > 0) teleopSpeedPick = sp.toDouble();
    }
  } catch (_) {
    // 文件坏了当没选过
  }
}

void _saveSpeed(double v) {
  teleopSpeedPick = v;
  final f = teleopTierFile;
  if (f == null) return;
  unawaited(f.writeAsString(jsonEncode(<String, double>{'speed': v})).then((_) {}, onError: (_) {}));
}

/// 没选过时的速度档。
const double kTeleopDefaultSpeed = 0.6;

/// 低速档最快（m/s）：超过它机器人要换 SDK 中速档（不能横移、转向变慢，换档要先松杆）。
const double kLowGearMax = 1.0;

/// 站点 `granted` 里的速度档：一组升序的数（改版前的站点给 `{slow, mid, fast}` 也认，取值排序）。
List<double> teleopSpeedsOf(Object? sp) {
  final raw = sp is List ? sp : (sp is Map ? sp.values.toList() : const <Object?>[]);
  final out = <double>{
    for (final v in raw)
      if (v is num && v > 0) v.toDouble(),
  }.toList()
    ..sort();
  return out;
}

/// 选哪一档：记住的那档在就用它；不在就用不超过它的最快那档；都没有用第一档。
double teleopSpeedFor(List<double> speeds, double? pick) {
  final want = pick ?? kTeleopDefaultSpeed;
  double? best;
  for (final v in speeds) {
    if (v <= want + 1e-9) best = v;
  }
  return best ?? speeds.first;
}

String teleopSpeedLabel(double v) => '${v.toStringAsFixed(1)} m/s';

/// POC 第 3 项：机器人为什么丢了遥控指令（站点转来的 `drops.last`），说人话。
String teleopDropText(String code) => switch (code) {
      'late' => tr('Command judged late: clock offset or network', '指令被判迟到：钟差或网络'),
      'video_recovered' => tr('Video just came back: release the sticks first', '画面刚恢复，请先松开摇杆'),
      'no_video' => tr('No video', '无画面'),
      'seq' || 'epoch' => tr('Commands out of order (reconnecting)', '指令顺序不对（重连中）'),
      'gear_wait' => tr('Release the stick to change gear', '换档要先松开摇杆'),
      'gear_switching' => tr('Changing gear, one moment', '正在换档，稍等'),
      _ => tr('Command dropped ($code)', '指令被丢弃（$code）'),
    };

class SiteTeleopPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  final String takeoverReason;

  /// 浮在画面右上、「停」下面的一块（W54 建图向导：小的建图预览和「走完了」）。没有就不摆。
  final Widget? overlay;
  const SiteTeleopPage(
      {super.key, required this.api, required this.robotId, this.takeoverReason = '', this.overlay});

  static const Key overlayKey = Key('teleop-overlay');

  static const Key stopKey = Key('teleop-stop');
  static const Key statusKey = Key('teleop-status');
  static const Key resumeKey = Key('teleop-resume');
  static const Key moveStickKey = Key('teleop-move');
  static const Key turnStickKey = Key('teleop-turn');
  static const Key keysKey = Key('teleop-keys');
  static const Key zoneKey = Key('teleop-zone');
  static Key speedKey(double v) => Key('teleop-speed-${v.toStringAsFixed(1)}');
  static const Key speedHintKey = Key('teleop-speed-hint');
  static const Key dropKey = Key('teleop-drop');
  static const Key inSightKey = Key('teleop-insight');
  static Key postureKey(String mode) => Key('teleop-posture-$mode');

  @override
  State<SiteTeleopPage> createState() => _SiteTeleopPageState();
}

class _SiteTeleopPageState extends State<SiteTeleopPage> with WidgetsBindingObserver {
  TeleopLink? _link;
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _tick;
  String _status = tr('Requesting control…', '正在请求控制权…');
  bool _granted = false;
  bool _video = true;
  bool _ended = false;
  double _maxVx = 0, _maxWz = 0, _maxVy = 0;

  /// POC 速度档：站点给的这台能用的几档（m/s，升序）；老站点不给 = 空，照旧按 [_maxVx]。
  List<double> _speeds = const [];
  double _speed = 0;

  /// 推满前进多快：选的那档（不超过站点限速）；老站点 = 站点限速。
  double get _vTop => _speeds.isEmpty ? _maxVx : math.min(_speed, _maxVx);

  /// 转向、横移占站点上限的几成：跟着选的速度走，到 1.0 m/s 就满（中速档不更快）。
  double get _turnScale {
    if (_speeds.isEmpty || _maxVx <= 0) return 1.0;
    return (_speed / math.min(_maxVx, kLowGearMax)).clamp(0.0, 1.0);
  }
  double _fwd = 0, _turn = 0;

  /// 横移杆值（右为正，跟杆的 dx 一样；发出去换成机身系左为正）。
  double _lat = 0;

  /// 硬禁区守卫这会儿说的（决策 65）；null = 没什么要说的。
  String? _zoneNote;
  String _zoneState = '';

  /// POC 11-05「我在现场，看得见机器人」：站点回过来的为准；只管这一次遥控（重新拿到遥控就关）。
  bool _inSight = false;

  /// 这台机器人能换哪几种姿势（遥控页里的站起、趴下、蹲下；POC 11-05）。
  List<String> _postures = const [];
  bool _posturing = false;

  /// POC：机器人刚丢了指令的原因；几秒没新增就收起。
  String? _dropNote;
  Timer? _dropTimer;

  /// 现场 11-05：遥控被停了以后怎么接着来。不是自己退出、不是被接管、不是登录过期 → 有「继续遥控」；
  /// 中止、断线、掉线、续不上 → 还每 [_retryEvery] 自己再要一次（最多 [_retryFor]），要到了接着遥控。
  static const Duration _retryEvery = Duration(seconds: 3);
  static const Duration _retryFor = Duration(minutes: 2);
  static const Set<String> _autoReasons = {'aborted', 'disconnected', 'robot_offline', 'lease_lost'};
  static const Set<String> _resumeReasons = {..._autoReasons, 'halt', 'stopped_here'};
  String _endReason = '';
  Timer? _retryTimer;
  DateTime? _retryUntil;
  bool get _canResume => _ended && _resumeReasons.contains(_endReason);

  /// 通知栏拉下来、来电盖住这一类：手指多半被系统收走了，这段时间只发零速。
  bool _inactive = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // App V2：别的页面竖屏横屏都行，遥控只许横屏（两根杆在两边、画面在中间）；离开放开。
    unawaited(lockLandscape());
    unawaited(_open());
    unawaited(_loadPostures());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      case AppLifecycleState.inactive:
        _inactive = true;
        _clearKeys();
      case AppLifecycleState.resumed:
        _inactive = false;
      case AppLifecycleState.hidden:
      case AppLifecycleState.paused:
      case AppLifecycleState.detached:
        _clearKeys();
        _endLocally(tr(
            'App went to the background. Remote control ended and the robot stopped',
            'App 已切到后台，遥控结束，机器人已停止'));
    }
  }

  /// 本机收手：不再发杆值、零速、放租、关连接、杆变灰。
  void _endLocally(String why) {
    if (_ended) return;
    _endReason = 'background';
    _retryTimer?.cancel();
    _stopTicking();
    _clearKeys();
    final link = _link;
    if (link != null) {
      link.send(0, 0);
      link.release();
      unawaited(link.close());
    }
    if (mounted) {
      setState(() {
        _ended = true;
        _granted = false;
        _status = why;
      });
    }
  }

  /// 画面还没起来被拒（站点回 `no_video`）时再要几次：这一页一开就去拉画面，机器人推上来要几秒（W54）。
  static const int _videoRetries = 6;
  int _noVideo = 0;

  Future<void> _open() async {
    try {
      final link = await widget.api.teleop(widget.robotId, takeoverReason: widget.takeoverReason);
      if (!mounted || _ended) {
        // 连接还在路上的时候本机已经收手了（切后台、按了停、退出这一页）：刚回来的这条不许留下，
        // 零速、放租、关掉 —— 不然站点上的租约一直占着，自动巡检、事件派遣、别人接管都派不了
        // （W00c5 外审阻断 2）。
        link.send(0, 0);
        link.release();
        await link.close();
        return;
      }
      _link = link;
      _sub = link.messages.listen(_onMsg);
    } on SiteError catch (e) {
      if (e.status == 409 && e.body['reason'] == 'no_video' && _noVideo < _videoRetries && mounted && !_ended) {
        _noVideo++;
        setState(() => _status = tr('Waiting for the video to start…', '正在等待画面…'));
        await Future<void>.delayed(const Duration(seconds: 2));
        if (mounted && !_ended) await _open();
        return;
      }
      final until = _retryUntil;
      if (mounted && until != null && DateTime.now().isBefore(until)) {
        // 自己再要的那几次：还不能遥控就说站点给的原因，接着等
        setState(() {
          _status = tr('Cannot drive yet: ${e.message}. Retrying…', '暂时还不能遥控：${e.message}；稍后自动重试');
          _ended = true;
        });
        _retryTimer = Timer(_retryEvery, _resume);
        return;
      }
      if (mounted) {
        setState(() {
          _status = tr("Can't start remote control: ${e.message}", '无法开始遥控：${e.message}');
          _ended = true;
        });
      }
    }
  }

  /// 「继续遥控」、自己再要：丢掉结束了的那条连接，重新要一次（拿到之前杆是灰的；拿到后要重新按下）。
  Future<void> _resume() async {
    _retryTimer?.cancel();
    _retryTimer = null;
    if (!mounted || !_ended) return;
    unawaited(_sub?.cancel());
    _sub = null;
    final old = _link;
    _link = null;
    if (old != null) unawaited(old.close());
    _clearKeys();
    setState(() {
      _ended = false;
      _granted = false;
      _video = true;
      _noVideo = 0;
      _status = tr('Requesting control again…', '正在重新请求控制权…');
    });
    await _open();
  }

  void _onMsg(Map<String, dynamic> m) {
    // 本机收过手（按了停、切后台、站点说结束）之后，晚到的一概不理：晚到的 granted 会把定时器
    // 点起来、把屏上翻回「你在遥控」（W00c5 外审阻断 2）。
    if (!mounted || _ended) return;
    switch (m['kind']) {
      case 'granted':
        _retryTimer?.cancel();
        _retryUntil = null;
        _endReason = '';
        _clearKeys(); // 授权前按着的键不算：拿到遥控之后要重新按下
        setState(() {
          _granted = true;
          _maxVx = (m['max_vx'] as num?)?.toDouble() ?? 0;
          _maxWz = (m['max_wz'] as num?)?.toDouble() ?? 0;
          _maxVy = (m['max_vy'] as num?)?.toDouble() ?? 0;
          _speeds = teleopSpeedsOf(m['speeds']);
          if (_speeds.isNotEmpty) _speed = teleopSpeedFor(_speeds, teleopSpeedPick);
          _status = tr('You are in control (speed limit ${_maxVx.toStringAsFixed(2)} m/s)',
              '遥控中（限速 ${_maxVx.toStringAsFixed(2)} m/s）');
          _zoneNote = teleopZoneText(m['zone_guard'] as Map<String, dynamic>?);
          _zoneState = '${(m['zone_guard'] as Map?)?['state'] ?? ''}';
          _inSight = false;
        });
        _tick ??= Timer.periodic(const Duration(milliseconds: 100), (_) => _sendNow());
      case 'drops':
        // 站点只在累计数有新增时才转：来一条就是这几秒里又丢了
        _dropTimer?.cancel();
        setState(() => _dropNote = teleopDropText('${m['last'] ?? ''}'));
        _dropTimer = Timer(const Duration(seconds: 4), () {
          if (mounted) setState(() => _dropNote = null);
        });
      case 'video':
        final ok = m['ok'] == true;
        _clearKeys(); // 画面断了、回来了：按着的键都不算，回来之后要重新按下
        if (!ok) {
          // 画面没了：杆变灰，手指抬起会被灰掉的杆吞掉 —— 这里当场归零并发一帧零速，
          // 不让断之前的杆值卡着、等画面回来就开走（W00c5c 内部评审）。
          _fwd = _turn = _lat = 0;
          _link?.send(0, 0);
        }
        setState(() => _video = ok);
      case 'zone_guard':
        // 只是告诉人为什么截了（截速度是狗那头的事）：杆、定时器、键盘一概不动
        setState(() {
          _zoneNote = teleopZoneText(m);
          _zoneState = '${m['state'] ?? ''}';
        });
      case 'in_sight':
        setState(() => _inSight = m['on'] == true);
      case 'ended':
        _stopTicking();
        _clearKeys();
        final reason = '${m['reason']}';
        final auto = _autoReasons.contains(reason);
        setState(() {
          _ended = true;
          _granted = false;
          _endReason = reason;
          _status = teleopEndText(reason, '${m['by'] ?? ''}') +
              (auto ? tr('. Retrying automatically…', '；正在自动重试…') : '');
        });
        if (auto) {
          _retryUntil = DateTime.now().add(_retryFor);
          _retryTimer = Timer(_retryEvery, _resume);
        }
    }
  }

  bool get _canDrive => _granted && !_ended && _video;

  Future<void> _loadPostures() async {
    try {
      final v = await widget.api.robot(widget.robotId);
      if (mounted) setState(() => _postures = robotPostures(v));
    } on Object {
      return; // 读不到就不摆按钮（不挡遥控）
    }
  }

  bool get _sticksIdle => _fwd == 0 && _turn == 0 && _lat == 0;

  /// 屏上姿势按钮按的「松着杆」（杆、键盘改杆值不重画；发帧那一拍对一下）。
  bool _idleShown = true;

  /// 「我在现场」：开要两次确认（用户 11-05）；关直接关。
  Future<void> _toggleInSight() async {
    final link = _link;
    if (link == null) return;
    if (_inSight) {
      link.setInSight(false);
      return;
    }
    Future<bool> ask(String title, String body, String okKey, String ok) async =>
        await showDialog<bool>(
            context: context,
            builder: (ctx) => AlertDialog(
                  title: Text(title),
                  content: Text(body),
                  actions: [
                    TextButton(onPressed: () => Navigator.pop(ctx, false), child: Text(tr('Cancel', '取消'))),
                    FilledButton(key: Key(okKey), onPressed: () => Navigator.pop(ctx, true), child: Text(ok)),
                  ],
                )) ==
        true;
    if (!await ask(
        tr('Are you on site and can you see the robot?', '你在现场、看得见机器人吗？'),
        tr('Only turn this on when you are next to the robot and can see it with your own eyes. The robot does not know where it is right now.',
            '只有你本人就在机器人旁边、亲眼看得见它的时候才打开。机器人现在不知道自己在哪。'),
        'teleop-insight-ok1',
        tr('I am on site', '我在现场'))) {
      return;
    }
    if (!mounted) return;
    if (!await ask(
        tr('Confirm again', '再确认一次'),
        tr('Speed goes up to 1.0 m/s and hard no-go zones (water, drops) are no longer avoided automatically. Only for this remote-control session; your name is recorded.',
            '限速放开到 1.0 m/s，不再自动避开硬禁区（水体、落差）。只管这一次遥控，会记下是你打开的。'),
        'teleop-insight-ok2',
        tr('Turn on', '确认打开'))) {
      return;
    }
    if (mounted && !_ended) _link?.setInSight(true);
  }

  /// 遥控页里换姿势（用户 11-05）：确认才发；推着杆不能按（机器人那头也拒）。
  Future<void> _posture(String mode) async {
    final what = postureLabel(mode);
    final yes = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
              title: Text(what),
              content: Text(tr('The robot will move its body right away. Make sure nobody is next to it.',
                  '机器人会马上动起来，先确认旁边没有人。')),
              actions: [
                TextButton(onPressed: () => Navigator.pop(ctx, false), child: Text(tr('Cancel', '取消'))),
                FilledButton(
                    key: const Key('posture-confirm'),
                    onPressed: () => Navigator.pop(ctx, true),
                    child: Text(what)),
              ],
            ));
    if (yes != true || !mounted) return;
    setState(() => _posturing = true);
    String? bad;
    try {
      final r = await widget.api.posture(widget.robotId, mode);
      final ack = r['ack'];
      if (ack is Map && ack['result'] != 'accepted' && ack['result'] != 'duplicate') {
        bad = ackReasonText('${ack['reason'] ?? ack['result']}');
      }
    } on SiteError catch (e) {
      bad = refusalText(e);
    }
    if (!mounted) return;
    setState(() => _posturing = false);
    if (bad != null) {
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(tr('$what failed: $bad', '$what 失败：$bad'))));
    }
  }

  /// 键盘（W15）：按着的方向键 → 杆值。只认按下、松开（系统连发不算）；松开就是零。
  final Set<LogicalKeyboardKey> _keys = <LogicalKeyboardKey>{};
  static final _fwdKeys = {LogicalKeyboardKey.keyW, LogicalKeyboardKey.arrowUp};
  static final _backKeys = {LogicalKeyboardKey.keyS, LogicalKeyboardKey.arrowDown};
  static final _leftKeys = {LogicalKeyboardKey.keyA, LogicalKeyboardKey.arrowLeft};
  static final _rightKeys = {LogicalKeyboardKey.keyD, LogicalKeyboardKey.arrowRight};
  static final _latLeftKeys = {LogicalKeyboardKey.keyQ};
  static final _latRightKeys = {LogicalKeyboardKey.keyE};

  KeyEventResult _onKey(FocusNode _, KeyEvent e) {
    final k = e.logicalKey;
    if (e is KeyDownEvent && k == LogicalKeyboardKey.escape) {
      unawaited(_halt());
      return KeyEventResult.handled;
    }
    final mine = _fwdKeys.contains(k) || _backKeys.contains(k) || _leftKeys.contains(k) ||
        _rightKeys.contains(k) || _latLeftKeys.contains(k) || _latRightKeys.contains(k);
    if (!mine) return KeyEventResult.ignored;
    if (e is KeyRepeatEvent) return KeyEventResult.handled; // 系统连发：不认，杆值不动
    if (e is KeyDownEvent) {
      if (!_canDrive) return KeyEventResult.handled; // 不能开的时候按下的不记
      _keys.add(k);
    }
    if (e is KeyUpEvent) _keys.remove(k);
    double axis(Set<LogicalKeyboardKey> plus, Set<LogicalKeyboardKey> minus) =>
        (_keys.any(plus.contains) ? 1.0 : 0.0) - (_keys.any(minus.contains) ? 1.0 : 0.0);
    if (_canDrive) {
      _fwd = axis(_fwdKeys, _backKeys);
      _turn = axis(_rightKeys, _leftKeys);
      _lat = axis(_latRightKeys, _latLeftKeys);
    } else {
      _keys.clear();
      _fwd = _turn = _lat = 0;
    }
    return KeyEventResult.handled;
  }

  /// 清空键盘输入、杆值归零、发一帧零速（W15 外审阻断）。之后要动得重新按下。
  void _clearKeys() {
    _keys.clear();
    _fwd = _turn = _lat = 0;
    if (!_ended) _link?.send(0, 0);
  }

  void _lostFocus(bool has) {
    if (!has) _clearKeys(); // 切到别的窗口：键松开的事件收不到了，当场归零
  }

  void _sendNow() {
    if (_postures.isNotEmpty && _sticksIdle != _idleShown && mounted) {
      setState(() => _idleShown = _sticksIdle);
    }
    final link = _link;
    if (link == null || !_granted || _ended || !_video) return;
    if (_inactive) {
      link.send(0, 0);
      return;
    }
    // 右推 = 顺时针 = wz 为负；横移右推 = 往右 = vy 为负（机身系左为正）。前后、横移只走大的那一个。
    final fwd = _fwd.abs() >= _lat.abs() ? _fwd : 0.0;
    final lat = _fwd.abs() >= _lat.abs() ? 0.0 : _lat;
    final k = _turnScale;
    link.send(fwd * _vTop, -_turn * _maxWz * k,
        vy: -lat * _maxVy * k, vmax: _speeds.isEmpty ? null : _speed);
  }

  void _stopTicking() {
    _tick?.cancel();
    _tick = null;
  }

  Future<void> _halt() async {
    // 先在本机收手：不再发杆值、放租。halt 万一没发出去，手指还按在杆上也不会接着开。
    _stopTicking();
    _clearKeys();
    final link = _link;
    if (link != null) {
      link.send(0, 0);
      link.release();
    }
    if (mounted) {
      setState(() {
        _ended = true;
        _granted = false;
        _endReason = 'stopped_here';
        _status = tr('You pressed stop', '你已按下停车');
      });
    }
    try {
      await widget.api.halt(widget.robotId);
    } on SiteError catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(
                content: Text(tr('Stop not confirmed: $e. Press the E-stop on the robot',
                    '停车未确认：$e —— 请按机身急停！'))));
      }
    }
  }

  @override
  void dispose() {
    unawaited(unlockOrientation());
    WidgetsBinding.instance.removeObserver(this);
    _stopTicking();
    _dropTimer?.cancel();
    _retryTimer?.cancel();
    _keys.clear();
    _sub?.cancel();
    final link = _link;
    if (link != null) {
      link.send(0, 0);
      link.release();
      unawaited(link.close());
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final bad = _ended || !_video;
    // 2026-10-30（用户）：画面铺满整屏；两根半透明的杆浮在左下、右下（拇指底下），大红「停」在右上，
    // 关闭、哪台、状态在上边。字都压在半透明的黑底上，看得清又不挡多少画面。
    // 杆的那一块边长：短边的一半，夹在 168（圈半径 72，放得下一整个圈）到 240 之间。
    final size = MediaQuery.sizeOf(context);
    final stick = (size.shortestSide * 0.5).clamp(168.0, 240.0);
    final desktop = {TargetPlatform.linux, TargetPlatform.windows, TargetPlatform.macOS}
        .contains(defaultTargetPlatform);
    final lateral = _maxVy > 0;
    return Focus(
      autofocus: true,
      onKeyEvent: _onKey,
      onFocusChange: _lostFocus,
      // 这一页不管系统是浅色深色都是深色的：它就是一块画面，上面的东西都按「压在画面上」配色。
      child: Theme(
        data: nwTheme(Brightness.dark),
        child: Builder(builder: (context) {
          final c = Nw.of(context);
          final scrim = Colors.black.withValues(alpha: 0.5);
          return Scaffold(
            backgroundColor: Colors.black,
            body: Stack(fit: StackFit.expand, children: [
              // 画面铺满（不管安全区：刘海那一块也是画面）；前后切换浮在底边正中、两根杆中间
              Positioned.fill(
                  child: SiteVideo(
                      api: widget.api, robotId: widget.robotId, fullscreen: true, sideInset: stick + Nw.s8)),
              SafeArea(
                child: Stack(children: [
                  // 上边：左关闭和哪台、中间状态、右「停」
                  Positioned(
                    left: Nw.s8,
                    right: Nw.s8,
                    top: Nw.s8,
                    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Container(
                        decoration: BoxDecoration(color: scrim, borderRadius: BorderRadius.circular(Nw.r12)),
                        padding: const EdgeInsets.fromLTRB(0, 0, Nw.s16, 0),
                        child: Row(mainAxisSize: MainAxisSize.min, children: [
                          IconButton(
                              key: const Key('teleop-close'),
                              tooltip: MaterialLocalizations.of(context).backButtonTooltip,
                              icon: Icon(Icons.close_rounded, color: c.onScrim),
                              onPressed: () => Navigator.maybePop(context)),
                          Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Text(widget.robotId,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: NwText.labelLarge.copyWith(color: c.onScrim)),
                                const SizedBox(height: 2),
                                // 这里说的是**遥控权**，不是画面（App 重做外审 M1）。画面活不活看画面自己。
                                NwStatusPill(
                                    label: _ended
                                        ? tr('ENDED', '已结束')
                                        : !_video
                                            ? tr('NO VIDEO', '无画面')
                                            : (_granted ? tr('IN CONTROL', '已获控制') : tr('WAITING', '等待')),
                                    tone: _canDrive ? NwTone.success : NwTone.neutral,
                                    dot: _canDrive),
                              ]),
                        ]),
                      ),
                      const SizedBox(width: Nw.s8),
                      Expanded(
                        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                          Container(
                            padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: Nw.s4),
                            decoration: BoxDecoration(
                                color: bad ? c.error.withValues(alpha: 0.85) : scrim,
                                borderRadius: BorderRadius.circular(Nw.r8)),
                            child: Text(
                              !_video && !_ended
                                  ? tr('No video: do not drive (the joysticks are disabled until video returns)',
                                      '无画面：禁止操作（画面恢复前摇杆不可用）')
                                  : _status,
                              key: SiteTeleopPage.statusKey,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: NwText.titleSmall.copyWith(color: Colors.white, fontWeight: FontWeight.bold),
                            ),
                          ),
                          if (_canResume)
                            Padding(
                              padding: const EdgeInsets.only(top: Nw.s4),
                              child: Align(
                                alignment: Alignment.centerLeft,
                                child: FilledButton.icon(
                                  key: SiteTeleopPage.resumeKey,
                                  onPressed: _resume,
                                  icon: const Icon(Icons.replay_rounded),
                                  label: Text(tr('Resume remote control', '继续遥控')),
                                ),
                              ),
                            ),
                          if (_zoneNote != null && !_ended)
                            Padding(
                              padding: const EdgeInsets.only(top: Nw.s4),
                              child: Container(
                                padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: Nw.s4),
                                decoration: BoxDecoration(
                                    color: c.fg(NwTone.warning).withValues(alpha: 0.85),
                                    borderRadius: BorderRadius.circular(Nw.r8)),
                                child: Text(_zoneNote!,
                                    key: SiteTeleopPage.zoneKey,
                                    maxLines: 3,
                                    overflow: TextOverflow.ellipsis,
                                    style: NwText.bodySmall.copyWith(color: Colors.black, fontWeight: FontWeight.w600)),
                              ),
                            ),
                          if (_dropNote != null && !_ended)
                            Padding(
                              padding: const EdgeInsets.only(top: Nw.s4),
                              child: Container(
                                key: SiteTeleopPage.dropKey,
                                padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: Nw.s4),
                                decoration: BoxDecoration(
                                    color: c.fg(NwTone.warning).withValues(alpha: 0.85),
                                    borderRadius: BorderRadius.circular(Nw.r8)),
                                child: Text(_dropNote!,
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                    style: NwText.bodySmall.copyWith(color: Colors.black, fontWeight: FontWeight.w600)),
                              ),
                            ),
                          if (_speeds.isNotEmpty && !_ended)
                            Padding(
                              padding: const EdgeInsets.only(top: Nw.s4),
                              child: Wrap(spacing: Nw.s4, runSpacing: Nw.s4, children: [
                                for (final v in _speeds)
                                  ChoiceChip(
                                    key: SiteTeleopPage.speedKey(v),
                                    visualDensity: VisualDensity.compact,
                                    label: Text(teleopSpeedLabel(v)),
                                    selected: _speed == v,
                                    onSelected: (_) => setState(() {
                                      _speed = v;
                                      _saveSpeed(v);
                                    }),
                                  ),
                              ]),
                            ),
                          if (_speeds.isNotEmpty && !_ended && _speed > kLowGearMax + 1e-9)
                            Padding(
                              key: SiteTeleopPage.speedHintKey,
                              padding: const EdgeInsets.only(top: Nw.s4),
                              child: Text(
                                  tr('Above 1.0 m/s the robot uses its middle gear: no sideways, slower turning; release the stick to change gear',
                                      '超过 1.0 m/s 用中速档：不能横移、转向变慢；换档要先松开摇杆'),
                                  style: NwText.bodySmall.copyWith(
                                      color: Colors.white, shadows: const [Shadow(blurRadius: 3)])),
                            ),
                          if (desktop)
                            Padding(
                              key: SiteTeleopPage.keysKey,
                              padding: const EdgeInsets.only(top: Nw.s4),
                              child: Text(
                                  lateral
                                      ? tr('Keyboard: W/S forward/back, Q/E sideways, A/D turn, release to stop; Esc to stop',
                                          '键盘：W/S 前后，Q/E 横移，A/D 转向，松开即停；Esc 停车')
                                      : tr('Keyboard: W/S forward/back, A/D turn, release to stop; Esc to stop',
                                          '键盘：W/S 前后，A/D 转向，松开即停；Esc 停车'),
                                  style: NwText.bodySmall.copyWith(
                                      color: Colors.white,
                                      shadows: const [Shadow(blurRadius: 3)])),
                            ),
                        ]),
                      ),
                      const SizedBox(width: Nw.s8),
                      // 大红「停」：实心、带红光（整个 App 只有这一处有光晕），右手拇指一抬就按到
                      SizedBox(
                        width: (stick * 0.8).clamp(120.0, 180.0),
                        height: 64,
                        child: DecoratedBox(
                          decoration: BoxDecoration(borderRadius: BorderRadius.circular(Nw.r12), boxShadow: [
                            BoxShadow(color: c.error.withValues(alpha: 0.35), blurRadius: 20, offset: const Offset(0, 8)),
                          ]),
                          child: FilledButton(
                            key: SiteTeleopPage.stopKey,
                            style: FilledButton.styleFrom(
                                backgroundColor: c.error,
                                foregroundColor: c.onError,
                                padding: const EdgeInsets.symmetric(horizontal: Nw.s8),
                                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(Nw.r12))),
                            onPressed: _halt,
                            child: FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Row(mainAxisSize: MainAxisSize.min, children: [
                                const Icon(Icons.front_hand_rounded, size: 28),
                                const SizedBox(width: Nw.s8),
                                Text(tr('Stop', '停'), style: const TextStyle(fontSize: 28, fontWeight: FontWeight.w900)),
                              ]),
                            ),
                          ),
                        ),
                      ),
                    ]),
                  ),
                  if (widget.overlay != null)
                    Positioned(
                        key: SiteTeleopPage.overlayKey,
                        right: stick + Nw.s16,
                        top: 64 + Nw.s16,
                        child: widget.overlay!),
                  // 底下两根杆中间：「我在现场」、站起 / 趴下 / 蹲下（POC 11-05）
                  Positioned(
                    left: stick + Nw.s16,
                    right: stick + Nw.s16,
                    bottom: Nw.s8,
                    child: Wrap(
                        alignment: WrapAlignment.center,
                        spacing: Nw.s4,
                        runSpacing: Nw.s4,
                        verticalDirection: VerticalDirection.up,
                        children: [
                          if (_postures.isNotEmpty && !_ended)
                            for (final p in _postures)
                              FilledButton.tonalIcon(
                                key: SiteTeleopPage.postureKey(p),
                                style: FilledButton.styleFrom(
                                    visualDensity: VisualDensity.compact,
                                    padding: const EdgeInsets.symmetric(horizontal: Nw.s8)),
                                icon: Icon(postureIcon(p), size: 18),
                                label: Text(postureLabel(p)),
                                onPressed: _posturing || !_idleShown || !_sticksIdle ? null : () => unawaited(_posture(p)),
                              ),
                          if (_granted && !_ended && (_zoneState == 'untrusted' || _inSight))
                            FilterChip(
                              key: SiteTeleopPage.inSightKey,
                              visualDensity: VisualDensity.compact,
                              avatar: const Icon(Icons.visibility_rounded, size: 18),
                              label: Text(tr('I am on site, I can see the robot', '我在现场，看得见机器人')),
                              selected: _inSight,
                              onSelected: (_) => unawaited(_toggleInSight()),
                            ),
                        ]),
                  ),
                  // 左下：前后 + 横移（能横移的狗）；右下：转向。半透明，浮在画面上
                  Positioned(
                    left: Nw.s8,
                    bottom: Nw.s8,
                    width: stick,
                    height: stick,
                    child: Joystick(
                      key: SiteTeleopPage.moveStickKey,
                      axis: JoystickAxis.translate,
                      enabled: _canDrive,
                      label: lateral ? tr('Move · sideways', '前后 · 横移') : tr('Move', '前后'),
                      icon: lateral ? Icons.open_with_rounded : null,
                      onChanged: (v) {
                        _fwd = v.dy;
                        _lat = lateral ? v.dx : 0;
                      },
                    ),
                  ),
                  Positioned(
                    right: Nw.s8,
                    bottom: Nw.s8,
                    width: stick,
                    height: stick,
                    child: Joystick(
                      key: SiteTeleopPage.turnStickKey,
                      axis: JoystickAxis.turn,
                      enabled: _canDrive,
                      label: tr('Turn', '转向'),
                      onChanged: (v) => _turn = v.dx,
                    ),
                  ),
                ]),
              ),
            ]),
          );
        }),
      ),
    );
  }
}
