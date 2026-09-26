/// 「我在现场监护」（W00c6i，W08 决定 11）。
///
/// 避障真机验收之前，真狗的自主移动是直线、没有避障 —— 只许在有人在现场时动。这个开关开着的时候每 1 s
/// 向站点续一次监护（站点转给狗，狗按 3 s 有效期记）；关掉、离开这一页、切到后台就放掉 —— 放的请求发不出去
/// 也没关系，狗那边 3 s 后自己过期，正在跑的 goto、巡检当场中止。
library;

import 'dart:async';

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
  Timer? _tick;
  bool _on = false;
  String _msg = '';

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

  Future<void> _renew() async {
    try {
      final r = await widget.api.supervise(widget.robotId, 'renew');
      final ack = r['ack'];
      if (ack is Map && ack['result'] != 'accepted') {
        _stop('狗没接监护：${ackReasonText('${ack['reason'] ?? ack['result']}')}');
      }
    } on SiteError catch (e) {
      // 续不上：狗那边几秒内自己过期、停下。这边如实说，不假装还在监护。
      _stop('监护续不上：$e（狗会停下）');
    }
  }

  void _start() {
    if (_on) return;
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
    unawaited(_release());
    if (mounted) setState(() => _msg = why);
  }

  Future<void> _release() async {
    try {
      await widget.api.supervise(widget.robotId, 'release');
    } on SiteError {
      // 放不掉也没关系：狗那边 3 s 后自己过期。
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _tick?.cancel();
    if (_on) {
      _on = false;
      unawaited(_release());
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Card(
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
