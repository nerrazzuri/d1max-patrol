/// 站点模式的遥控页（W00c5c，受决策 7 约束）。
///
/// - **连接就是租约**：进这一页开一条遥控连接；退出、断线都放租，狗停。
/// - 左杆前后（狗不横移），右杆只转向；按住每 100 ms 发一帧，松手发零速。站点和狗都会再夹限速。
/// - **大红「停」走站点的 halt，不走遥控连接**（连接卡住的时候正是最需要停车的时候）；同时发零速。
/// - 画面没了、租约结束、连接断了：屏上立刻说，杆变灰。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../net/site_client.dart';
import 'site_video.dart';
import 'widget/joystick.dart';

/// 结束原因给人看的话。
String teleopEndText(String reason) => switch (reason) {
      'released' => '你放开了遥控',
      'disconnected' => '遥控连接断了，狗已经停下',
      'taken_over' => '管理员接管了遥控',
      'halt' => '有人按了停车',
      'robot_offline' => '狗掉线了',
      'lease_lost' => '续不上租约，遥控结束',
      'logged_out' => '登录过期了',
      _ => '遥控结束（$reason）',
    };

class SiteTeleopPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  final String takeoverReason;
  const SiteTeleopPage(
      {super.key, required this.api, required this.robotId, this.takeoverReason = ''});

  static const Key stopKey = Key('teleop-stop');
  static const Key statusKey = Key('teleop-status');
  static const Key moveStickKey = Key('teleop-move');
  static const Key turnStickKey = Key('teleop-turn');

  @override
  State<SiteTeleopPage> createState() => _SiteTeleopPageState();
}

class _SiteTeleopPageState extends State<SiteTeleopPage> {
  TeleopLink? _link;
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _tick;
  String _status = '正在拿遥控……';
  bool _granted = false;
  bool _video = true;
  bool _ended = false;
  double _maxVx = 0, _maxWz = 0;
  double _fwd = 0, _turn = 0;

  @override
  void initState() {
    super.initState();
    unawaited(_open());
  }

  Future<void> _open() async {
    try {
      final link = await widget.api.teleop(widget.robotId, takeoverReason: widget.takeoverReason);
      if (!mounted) {
        await link.close();
        return;
      }
      _link = link;
      _sub = link.messages.listen(_onMsg);
    } on SiteError catch (e) {
      if (mounted) {
        setState(() {
          _status = '遥控开不了：${e.message}';
          _ended = true;
        });
      }
    }
  }

  void _onMsg(Map<String, dynamic> m) {
    if (!mounted) return;
    switch (m['kind']) {
      case 'granted':
        setState(() {
          _granted = true;
          _maxVx = (m['max_vx'] as num?)?.toDouble() ?? 0;
          _maxWz = (m['max_wz'] as num?)?.toDouble() ?? 0;
          _status = '你在遥控（限速 ${_maxVx.toStringAsFixed(2)} m/s）';
        });
        _tick ??= Timer.periodic(const Duration(milliseconds: 100), (_) => _sendNow());
      case 'video':
        final ok = m['ok'] == true;
        if (!ok) {
          // 画面没了：杆变灰，手指抬起会被灰掉的杆吞掉 —— 这里当场归零并发一帧零速，
          // 不让断之前的杆值卡着、等画面回来就开走（W00c5c 内部评审）。
          _fwd = _turn = 0;
          _link?.send(0, 0);
        }
        setState(() => _video = ok);
      case 'ended':
        _stopTicking();
        _fwd = _turn = 0;
        setState(() {
          _ended = true;
          _granted = false;
          _status = teleopEndText('${m['reason']}');
        });
    }
  }

  bool get _canDrive => _granted && !_ended && _video;

  void _sendNow() {
    final link = _link;
    if (link == null || !_granted || _ended || !_video) return;
    link.send(_fwd * _maxVx, -_turn * _maxWz); // 右推 = 顺时针 = wz 为负
  }

  void _stopTicking() {
    _tick?.cancel();
    _tick = null;
  }

  Future<void> _halt() async {
    // 先在本机收手：不再发杆值、放租。halt 万一没发出去，手指还按在杆上也不会接着开。
    _stopTicking();
    _fwd = _turn = 0;
    final link = _link;
    if (link != null) {
      link.send(0, 0);
      link.release();
    }
    if (mounted) {
      setState(() {
        _ended = true;
        _granted = false;
        _status = '你按了停车';
      });
    }
    try {
      await widget.api.halt(widget.robotId);
    } on SiteError catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('停车命令没发出去：$e —— 按机身急停！')));
      }
    }
  }

  @override
  void dispose() {
    _stopTicking();
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
    return Scaffold(
      appBar: AppBar(title: Text('遥控 ${widget.robotId}')),
      // **状态钉在顶上,「停」和摇杆钉在底下**,只有画面在中间滚:停车按钮和「现在能不能动」
      // 任何时候都要一眼看得见。
      body: Column(children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: Text(
            !_video && !_ended ? '没有画面：不许动（画面回来之前摇杆不起作用）' : _status,
            key: SiteTeleopPage.statusKey,
            style: TextStyle(color: bad ? Colors.red : null, fontWeight: FontWeight.bold),
          ),
        ),
        Expanded(
          child: ListView(padding: const EdgeInsets.all(12), children: [
            SiteVideo(api: widget.api, robotId: widget.robotId),
          ]),
        ),
        // 摇杆铺满给它的那块地方:要给定大小。
        SizedBox(
          height: 160,
          child: Row(children: [
            Expanded(
              child: Joystick(
                key: SiteTeleopPage.moveStickKey,
                axis: JoystickAxis.translate,
                enabled: _canDrive,
                onChanged: (v) => _fwd = v.dy,
              ),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Joystick(
                key: SiteTeleopPage.turnStickKey,
                axis: JoystickAxis.turn,
                enabled: _canDrive,
                onChanged: (v) => _turn = v.dx,
              ),
            ),
          ]),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: SizedBox(
              height: 64,
              width: double.infinity,
              child: FilledButton(
                key: SiteTeleopPage.stopKey,
                style: FilledButton.styleFrom(backgroundColor: Colors.red),
                onPressed: _halt,
                child: const Text('停', style: TextStyle(fontSize: 28)),
              ),
            ),
          ),
        ),
      ]),
    );
  }
}
