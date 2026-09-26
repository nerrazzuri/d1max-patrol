/// 「我在现场监护」（W00c6i，W08 决定 11）。
///
/// 避障真机验收之前，真狗的自主移动是直线、没有避障 —— 只许在有人在现场时动。这个开关开着的时候每 1 s
/// 向站点续一次监护（站点转给狗，狗按 3 s 有效期记）；关掉、离开这一页、切到后台就放掉 —— 放的请求发不出去
/// 也没关系，狗那边 3 s 后自己过期，正在跑的 goto、巡检当场中止、停车。
///
/// 每次打开新起一个**会话号**，心跳带单调递增的**序号**：狗不认同一会话里序号不增的心跳（慢网里迟到的旧续约、
/// 断线补投的，都比放租那一条小）。同一时间只有一条在路上；连续两次续不上才自己关（丢一次还在）。
library;

import 'dart:async';
import 'dart:math';

import 'package:flutter/material.dart';

import '../net/site_client.dart';

class SupervisionSwitch extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  const SupervisionSwitch({super.key, required this.api, required this.robotId});

  static const Key switchKey = Key('supervise-switch');
  static const Key statusKey = Key('supervise-status');

  @override
  State<SupervisionSwitch> createState() => _SupervisionSwitchState();
}

class _SupervisionSwitchState extends State<SupervisionSwitch> with WidgetsBindingObserver {
  static final Random _rand = Random.secure();

  Timer? _tick;
  bool _on = false;
  String _msg = '';
  String _session = '';
  int _seq = 0;
  bool _inFlight = false;
  int _failures = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.hidden ||
        state == AppLifecycleState.paused ||
        state == AppLifecycleState.detached) {
      _stop('切到后台了，监护已放（狗会停下）');
    }
  }

  static String _newSession() =>
      List<String>.generate(12, (_) => _rand.nextInt(16).toRadixString(16)).join();

  Future<void> _renew() async {
    if (!_on || _inFlight) return; // 同一时间只有一条在路上
    final session = _session;
    _inFlight = true;
    String? why;
    try {
      final r = await widget.api.supervise(widget.robotId, 'renew', session: session, seq: ++_seq);
      final ack = r['ack'];
      if (ack is Map && ack['result'] != 'accepted') {
        why = '狗没接监护：${ackReasonText('${ack['reason'] ?? ack['result']}')}';
      }
    } catch (e) {
      why = '监护续不上：$e';
    } finally {
      _inFlight = false;
    }
    if (!_on || session != _session) return; // 上一个会话迟到的结果，不管
    if (why == null) {
      _failures = 0;
      return;
    }
    _failures++;
    if (_failures >= 2) {
      _stop('$why（狗会停下）');
    } else if (mounted) {
      setState(() => _msg = '$why，再试一次');
    }
  }

  void _start() {
    if (_on) return;
    _session = _newSession();
    _seq = 0;
    _failures = 0;
    setState(() {
      _on = true;
      _msg = '';
    });
    unawaited(_renew());
    _tick = Timer.periodic(const Duration(seconds: 1), (_) => unawaited(_renew()));
  }

  void _stop(String why) {
    if (!_on) return;
    _tick?.cancel();
    _tick = null;
    _on = false;
    unawaited(_release(_session, ++_seq));
    if (mounted) setState(() => _msg = why);
  }

  Future<void> _release(String session, int seq) async {
    try {
      await widget.api.supervise(widget.robotId, 'release', session: session, seq: seq);
    } catch (_) {
      // 放不掉也没关系：狗那边 3 s 后自己过期。
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _tick?.cancel();
    if (_on) {
      _on = false;
      unawaited(_release(_session, ++_seq));
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.fromLTRB(12, 8, 12, 0),
      color: _on ? Colors.red.shade50 : null,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SwitchListTile(
          key: SupervisionSwitch.switchKey,
          title: Text(_on ? '监护中' : '我在现场监护'),
          subtitle: const Text('这台狗要人现场监护才执行派单（避障还没验收）。开着的时候狗会执行下发的任务；'
              '关掉、离开这一页、切到后台就停。'),
          value: _on,
          onChanged: (v) => v ? _start() : _stop('监护已放'),
        ),
        if (_msg.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: Text(_msg, key: SupervisionSwitch.statusKey),
          ),
      ]),
    );
  }
}
