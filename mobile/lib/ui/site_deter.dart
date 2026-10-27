/// 上装（W21，决策 36）：警灯、警笛、聚光灯、喇叭。
///
/// 每一路「开」都带时长，到点狗上自己关（站点、手机断了也一样）；「关」随时能按。喇叭放录好的话术，狗上配了 TTS 的
/// 还能现打一段字。喇叭有优先级：事件派遣、驱离（W22）放的比手动的高，那时候手动的会回「忙」。
/// 只列狗报了接上的那几路（`capabilities.tasks.deter.outputs`）。保安、管理员能用。
///
/// W40（上装 V1.2）：狗报了 `spotlight_sides` 就多两行「左聚光灯」「右聚光灯」（能只开一边）；报了
/// `strobe_colors` 警灯那一行多一个颜色下拉框（开的时候带上）。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

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

/// 警灯颜色名说人话（狗的配置里起的名字；不认识的原样显示）。
String strobeColorText(String c) => switch (c) {
      'red' => tr('Red', '红'),
      'yellow' => tr('Yellow', '黄'),
      'green' => tr('Green', '绿'),
      'blue' => tr('Blue', '蓝'),
      'white' => tr('White', '白'),
      'purple' || 'magenta' => tr('Purple', '紫'),
      'cyan' => tr('Cyan', '青'),
      _ => c,
    };

/// 页面上的一行：哪一路、哪一边（只给聚光灯）、名字。
typedef _Row = ({String key, String output, String? side, String name});

/// 狗回的拒绝理由说人话。
String deterReasonText(String r) => switch (r) {
      'busy' => tr('The speaker is playing something more urgent (incident, deterrence). Wait for it to finish',
          '喇叭正在放更要紧的（事件、驱离），等它放完'),
      'unsupported' => tr('This output is not fitted on the robot', '机器人上没接这一路'),
      _ when r.startsWith('unsupported:') => tr('Not supported by the robot: ${r.substring(12).trim()}',
          '机器人上不支持：${r.substring(12).trim()}'),
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
  List<String> get _sides => ((widget.caps['spotlight_sides'] as List?) ?? const []).map((e) => '$e').toList();
  List<String> get _colors => ((widget.caps['strobe_colors'] as List?) ?? const []).map((e) => '$e').toList();
  String? _color;

  /// 喇叭以外的每一行。聚光灯分左右的：「两边」一行，再加左、右各一行。
  List<_Row> get _rows => <_Row>[
        for (final o in _outputs.where((o) => o != 'speaker')) ...[
          (key: o, output: o, side: null, name: deterOutputs[o]?.$1 ?? o),
          if (o == 'spotlight' && _sides.contains('left'))
            (key: 'spotlight-left', output: o, side: 'left', name: tr('Left spotlight', '左聚光灯')),
          if (o == 'spotlight' && _sides.contains('right'))
            (key: 'spotlight-right', output: o, side: 'right', name: tr('Right spotlight', '右聚光灯')),
        ],
      ];

  @override
  void initState() {
    super.initState();
    _clip = _clips.isEmpty ? null : _clips.first;
    final def = '${widget.caps['strobe_default'] ?? ''}';
    _color = _colors.isEmpty ? null : (_colors.contains(def) ? def : _colors.first);
  }

  @override
  void dispose() {
    _tts.dispose();
    super.dispose();
  }

  Future<void> _go(String output, bool on, {String? clip, String? side, String? name}) async {
    name ??= deterOutputs[output]?.$1 ?? output;
    final secs = deterOutputs[output]?.$2 ?? 10;
    final color = output == 'strobe' && on ? _color : null;
    final what = on
        ? tr('$name on for ${secs.toStringAsFixed(0)} s', '开$name ${secs.toStringAsFixed(0)} 秒')
        : tr('$name off', '关$name');
    String msg;
    try {
      final r = await widget.api.deter(widget.robotId, output, on,
          maxS: on ? secs : null, clip: clip, side: side, color: color);
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
    final c = Nw.of(context);
    final outs = _outputs;
    return Scaffold(
      appBar: AppBar(title: Text(tr('${widget.robotId} · Payload', '${widget.robotId} · 上装'))),
      body: ListView(
          // 横屏的手机只有 360 高：内距、间距收紧一点，常用的那几样不用滚就点得到
          padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
          children: [
        if (_msg.isNotEmpty)
          Container(
              margin: const EdgeInsets.only(bottom: Nw.s8),
              padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
              decoration: BoxDecoration(color: c.infoSoft, borderRadius: BorderRadius.circular(Nw.r8)),
              child: Text(_msg, key: SiteDeterPage.msgKey, style: NwText.bodyMedium.copyWith(color: c.text))),
        if (outs.isEmpty)
          Text(tr('No payload fitted on this robot', '这台机器人没接上装'),
              style: NwText.bodyLarge.copyWith(color: c.text)),
        for (final r in _rows)
          Padding(
              padding: const EdgeInsets.only(bottom: Nw.s8),
              child: NwCard(
                  padding: EdgeInsets.zero,
                  // 竖屏的手机一行摆不下「名字 + 开 + 关」（名字会被挤成几个字一行）：按钮放到下面一行
                  child: LayoutBuilder(builder: (context, box) {
                    final narrow = box.maxWidth < 520;
                    final o = r.output;
                    final buttons = Wrap(spacing: Nw.s8, crossAxisAlignment: WrapCrossAlignment.center, children: [
                      if (o == 'strobe' && _colors.isNotEmpty)
                        DropdownButton<String>(
                          key: const Key('deter-color'),
                          value: _color,
                          items: [
                            for (final k in _colors) DropdownMenuItem(value: k, child: Text(strobeColorText(k))),
                          ],
                          onChanged: (v) => setState(() => _color = v ?? _color),
                        ),
                      NwButton(
                          buttonKey: Key('deter-on-${r.key}'),
                          onPressed: () => _go(o, true, side: r.side, name: r.name),
                          label: tr('On', '开')),
                      NwButton(
                          buttonKey: Key('deter-off-${r.key}'),
                          variant: NwButtonVariant.outline,
                          onPressed: () => _go(o, false, side: r.side, name: r.name),
                          label: tr('Off', '关')),
                    ]);
                    final secs = deterOutputs[o]?.$2.toStringAsFixed(0);
                    return ListTile(
                      key: Key('deter-${r.key}'),
                      dense: true,
                      title: Text(
                          r.side == null && o == 'spotlight' && _sides.isNotEmpty
                              ? tr('Both spotlights (on for $secs s, then switch off by themselves)',
                                  '两边聚光灯（开 $secs 秒，到点自己关）')
                              : tr('${r.name} (on for $secs s, then switches off by itself)',
                                  '${r.name}（开 $secs 秒，到点自己关）'),
                          style: NwText.bodyMedium.copyWith(color: c.text)),
                      subtitle: narrow ? Align(alignment: Alignment.centerRight, child: buttons) : null,
                      trailing: narrow ? null : buttons,
                    );
                  }))),
        if (outs.contains('speaker')) ...[
          NwCard(
              padding: const EdgeInsets.all(12),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
            // 话术的名字可能很长：下拉框占剩下的宽度、名字摆不下就截。竖屏的手机一行摆不下
            // 「喇叭 + 话术 + 放 + 停」：话术一行，放、停在下面一行。
            LayoutBuilder(builder: (context, box) {
              final narrow = box.maxWidth < 520;
              final name = Text(tr('Speaker: ', '喇叭：'), style: NwText.titleSmall.copyWith(color: c.text));
              final clip = _clips.isEmpty
                  ? Text(tr('No recorded clips on the robot', '机器人上没有录好的话术'),
                      style: NwText.bodyMedium.copyWith(color: c.textSecondary))
                  : DropdownButton<String>(
                      key: const Key('deter-clip'),
                      isExpanded: true,
                      value: _clip,
                      items: [
                        for (final c in _clips)
                          DropdownMenuItem(value: c, child: Text(c, maxLines: 1, overflow: TextOverflow.ellipsis)),
                      ],
                      onChanged: (v) => setState(() => _clip = v),
                    );
              final play = NwButton(
                  buttonKey: const Key('deter-play'),
                  onPressed: _clip == null ? null : () => _go('speaker', true, clip: _clip),
                  label: tr('Play', '放'));
              final stop = NwButton(
                  buttonKey: const Key('deter-off-speaker'),
                  variant: NwButtonVariant.outline,
                  onPressed: () => _go('speaker', false),
                  label: tr('Stop', '停'));
              const gap = SizedBox(width: Nw.s8);
              return narrow
                  ? Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
                      name,
                      clip,
                      const SizedBox(height: Nw.s8),
                      Row(mainAxisAlignment: MainAxisAlignment.end, children: [play, gap, stop]),
                    ])
                  : Row(children: [name, Expanded(child: clip), gap, play, gap, stop]);
            }),
            if (widget.caps['tts'] == true) ...[
              const SizedBox(height: Nw.s8),
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
                const SizedBox(width: Nw.s8),
                Expanded(
                    child: TextField(
                        key: const Key('deter-tts'),
                        controller: _tts,
                        maxLength: 300,
                        decoration: InputDecoration(
                            labelText: tr('Type a message (synthesized on the robot, then played)',
                                '现打一段（机器人上合成后放）')))),
                const SizedBox(width: Nw.s8),
                NwButton(
                    buttonKey: const Key('deter-say'),
                    onPressed: () {
                      final t = _tts.text.trim();
                      if (t.isEmpty) return;
                      _go('speaker', true, clip: 'tts:$_lang:$t');
                    },
                    label: tr('Say', '说')),
              ]),
            ],
          ])),
        ],
      ]),
    );
  }
}
