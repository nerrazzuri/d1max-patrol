/// 上装（W21，决策 36）：警灯、警笛、聚光灯、喇叭。
///
/// 每一路「开」都带时长，到点狗上自己关（站点、手机断了也一样）；「关」随时能按。喇叭放录好的话术，狗上配了 TTS 的
/// 还能现打一段字。喇叭有优先级：事件派遣、驱离（W22）放的比手动的高，那时候手动的会回「忙」。
/// 只列狗报了接上的那几路（`capabilities.tasks.deter.outputs`）。保安、管理员能用。
library;

import 'package:flutter/material.dart';

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
const Map<String, (String, double)> deterOutputs = <String, (String, double)>{
  'strobe': ('警灯', 60),
  'siren': ('警笛', 10),
  'spotlight': ('聚光灯', 120),
  'speaker': ('喇叭', 30),
};

/// 狗回的拒绝理由说人话。
String deterReasonText(String r) => switch (r) {
      'busy' => '喇叭正在放更要紧的（事件、驱离），等它放完',
      'unsupported' => '狗上没接这一路',
      _ when r.startsWith('device:') => '设备没做成：${r.substring(7).trim()}',
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
    final what = on ? '开$name ${secs.toStringAsFixed(0)} 秒' : '关$name';
    String msg;
    try {
      final r = await widget.api.deter(widget.robotId, output, on, maxS: on ? secs : null, clip: clip);
      final ack = r['ack'];
      final res = ack is Map ? '${ack['result']}' : 'accepted';
      msg = res == 'accepted' ? '$what：好了' : '$what没成：${deterReasonText(ack is Map ? '${ack['reason'] ?? res}' : res)}';
    } on SiteError catch (e) {
      msg = '$what没成：$e';
    }
    if (mounted) setState(() => _msg = msg);
  }

  @override
  Widget build(BuildContext context) {
    final outs = _outputs;
    return Scaffold(
      appBar: AppBar(title: Text('${widget.robotId} · 上装')),
      body: ListView(padding: const EdgeInsets.all(8), children: [
        if (_msg.isNotEmpty) Text(_msg, key: SiteDeterPage.msgKey),
        if (outs.isEmpty) const Text('这只狗没接上装'),
        for (final o in outs.where((o) => o != 'speaker'))
          ListTile(
            key: Key('deter-$o'),
            dense: true,
            title: Text('${deterOutputs[o]?.$1 ?? o}（开 ${deterOutputs[o]?.$2.toStringAsFixed(0)} 秒，到点自己关）'),
            trailing: Wrap(spacing: 8, children: [
              FilledButton(key: Key('deter-on-$o'), onPressed: () => _go(o, true), child: const Text('开')),
              OutlinedButton(key: Key('deter-off-$o'), onPressed: () => _go(o, false), child: const Text('关')),
            ]),
          ),
        if (outs.contains('speaker')) ...[
          const Divider(),
          Row(children: [
            const Text('喇叭：'),
            if (_clips.isEmpty) const Text('狗上没有录好的话术'),
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
                child: const Text('放')),
            const SizedBox(width: 8),
            OutlinedButton(key: const Key('deter-off-speaker'), onPressed: () => _go('speaker', false), child: const Text('停')),
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
                      decoration: const InputDecoration(labelText: '现打一段（狗上合成后放）'))),
              const SizedBox(width: 8),
              FilledButton(
                  key: const Key('deter-say'),
                  onPressed: () {
                    final t = _tts.text.trim();
                    if (t.isEmpty) return;
                    _go('speaker', true, clip: 'tts:$_lang:$t');
                  },
                  child: const Text('说')),
            ]),
        ],
      ]),
    );
  }
}
