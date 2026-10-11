/// 上装（W21，决策 36）：警灯、警笛、聚光灯、喇叭。
///
/// 每一路「开」都带时长，到点狗上自己关（站点、手机断了也一样）；「关」随时能按。喇叭放录好的话术，狗上配了 TTS 的
/// 还能现打一段字。喇叭有优先级：事件派遣、驱离（W22）放的比手动的高，那时候手动的会回「忙」。
/// 只列狗报了接上的那几路（`capabilities.tasks.deter.outputs`）。保安、管理员能用。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';

class SiteDeterPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;

  /// 狗报的 `capabilities.tasks.deter`：`outputs`、`clips`、`tts`、`max_s`。
  final Map<String, dynamic> caps;
  const SiteDeterPage({super.key, required this.api, required this.robotId, required this.caps});

  static const Key msgKey = Key('deter-msg');

  @override
  State<SiteDeterPage> createState() => _SiteDeterPageState();
}

/// 每一路的名字、默认开多久（秒）。
Map<String, (String, double)> get deterOutputs => <String, (String, double)>{
      'strobe': (tr('Strobe light', '警灯'), 60),
      'siren': (tr('Siren', '警笛'), 10),
      'spotlight': (tr('Spotlight', '聚光灯'), 120),
      'speaker': (tr('Speaker', '喇叭'), 30),
    };

/// 狗回的拒绝理由说人话。
String deterReasonText(String r) => switch (r) {
      'busy' => tr('The speaker is playing something more urgent (incident, deterrence). Wait for it to finish',
          '喇叭正在放更要紧的（事件、驱离），等它放完'),
      'unsupported' => tr('This output is not fitted on the robot', '狗上没接这一路'),
      _ when r.startsWith('device:') =>
        tr('Device failed: ${r.substring(7).trim()}', '设备没做成：${r.substring(7).trim()}'),
      _ => ackReasonText(r),
    };

class _SiteDeterPageState extends State<SiteDeterPage> {
  String _msg = '';
  String? _clip;
  final TextEditingController _tts = TextEditingController();
  String _lang = 'zh';

  List<String> get _outputs => ((widget.caps['outputs'] as List?) ?? const []).map((e) => '$e').toList();
  List<String> get _clips => ((widget.caps['clips'] as List?) ?? const []).map((e) => '$e').toList();

  @override
  void initState() {
    super.initState();
    _clip = _clips.isEmpty ? null : _clips.first;
  }

  @override
  void dispose() {
    _tts.dispose();
    super.dispose();
  }

  Future<void> _go(String output, bool on, {String? clip}) async {
    final name = deterOutputs[output]?.$1 ?? output;
    final secs = deterOutputs[output]?.$2 ?? 10;
    final what = on
        ? tr('$name on for ${secs.toStringAsFixed(0)} s', '开$name ${secs.toStringAsFixed(0)} 秒')
        : tr('$name off', '关$name');
    String msg;
    try {
      final r = await widget.api.deter(widget.robotId, output, on, maxS: on ? secs : null, clip: clip);
      final ack = r['ack'];
      final res = ack is Map ? '${ack['result']}' : 'accepted';
      if (res == 'accepted') {
        msg = tr('$what: done', '$what：好了');
      } else {
        final why = deterReasonText(ack is Map ? '${ack['reason'] ?? res}' : res);
        msg = tr('$what failed: $why', '$what没成：$why');
      }
    } on SiteError catch (e) {
      msg = tr('$what failed: $e', '$what没成：$e');
    }
    if (mounted) setState(() => _msg = msg);
  }

  @override
  Widget build(BuildContext context) {
    final outs = _outputs;
    return Scaffold(
      appBar: AppBar(title: Text(tr('${widget.robotId} · Payload', '${widget.robotId} · 上装'))),
      body: ListView(padding: const EdgeInsets.all(8), children: [
        if (_msg.isNotEmpty) Text(_msg, key: SiteDeterPage.msgKey),
        if (outs.isEmpty) Text(tr('No payload fitted on this robot', '这只狗没接上装')),
        for (final o in outs.where((o) => o != 'speaker'))
          ListTile(
            key: Key('deter-$o'),
            dense: true,
            title: Text(tr(
                '${deterOutputs[o]?.$1 ?? o} (on for ${deterOutputs[o]?.$2.toStringAsFixed(0)} s, then switches off by itself)',
                '${deterOutputs[o]?.$1 ?? o}（开 ${deterOutputs[o]?.$2.toStringAsFixed(0)} 秒，到点自己关）')),
            trailing: Wrap(spacing: 8, children: [
              FilledButton(key: Key('deter-on-$o'), onPressed: () => _go(o, true), child: Text(tr('On', '开'))),
              OutlinedButton(key: Key('deter-off-$o'), onPressed: () => _go(o, false), child: Text(tr('Off', '关'))),
            ]),
          ),
        if (outs.contains('speaker')) ...[
          const Divider(),
          Row(children: [
            Text(tr('Speaker: ', '喇叭：')),
            if (_clips.isEmpty) Text(tr('No recorded clips on the robot', '狗上没有录好的话术')),
            if (_clips.isNotEmpty)
              DropdownButton<String>(
                key: const Key('deter-clip'),
                value: _clip,
                items: [for (final c in _clips) DropdownMenuItem(value: c, child: Text(c))],
                onChanged: (v) => setState(() => _clip = v),
              ),
            const SizedBox(width: 8),
            FilledButton(
                key: const Key('deter-play'),
                onPressed: _clip == null ? null : () => _go('speaker', true, clip: _clip),
                child: Text(tr('Play', '放'))),
            const SizedBox(width: 8),
            OutlinedButton(key: const Key('deter-off-speaker'), onPressed: () => _go('speaker', false), child: Text(tr('Stop', '停'))),
          ]),
          if (widget.caps['tts'] == true)
            Row(children: [
              DropdownButton<String>(
                key: const Key('deter-lang'),
                value: _lang,
                items: const [
                  DropdownMenuItem(value: 'zh', child: Text('中文')),
                  DropdownMenuItem(value: 'en', child: Text('English')),
                  DropdownMenuItem(value: 'ms', child: Text('Melayu')),
                ],
                onChanged: (v) => setState(() => _lang = v ?? _lang),
              ),
              const SizedBox(width: 8),
              Expanded(
                  child: TextField(
                      key: const Key('deter-tts'),
                      controller: _tts,
                      maxLength: 300,
                      decoration: InputDecoration(
                          labelText: tr('Type a message (synthesized on the robot, then played)',
                              '现打一段（狗上合成后放）')))),
              const SizedBox(width: 8),
              FilledButton(
                  key: const Key('deter-say'),
                  onPressed: () {
                    final t = _tts.text.trim();
                    if (t.isEmpty) return;
                    _go('speaker', true, clip: 'tts:$_lang:$t');
                  },
                  child: Text(tr('Say', '说'))),
            ]),
        ],
      ]),
    );
  }
}
