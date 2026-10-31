/// 待命点与原点（W13a，决策 16）。
///
/// **原点**是安全返航、回充的语义，每张图一个（「在这儿标原点」改它）；**待命点**是运营调度的语义，可以
/// 多个：任务做完狗回默认的那一个，也可以叫它回指定的一个。这一页列出这台狗的原点和待命点；管理员能在狗现在
/// 的位置设待命点（不动原点）、设默认、删除；能派单的人能叫狗回某一个待命点。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'widget/bar_actions.dart';

class SiteStandbyPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;

  /// 这台狗能不能「在这儿设待命点」（代理报了 `mark_home.standby`）。
  final bool canMarkHere;
  const SiteStandbyPage({
    super.key,
    required this.api,
    required this.robotId,
    this.canMarkHere = false,
  });

  static const Key hereKey = Key('standby-here');
  static const Key nameKey = Key('standby-here-name');
  static const Key defaultKey = Key('standby-here-default');
  static const Key goKey = Key('standby-here-go');
  static const Key msgKey = Key('standby-msg');
  static const Key chargerKey = Key('standby-charger-here');

  @override
  State<SiteStandbyPage> createState() => _SiteStandbyPageState();
}

class _SiteStandbyPageState extends State<SiteStandbyPage> {
  List<Map<String, dynamic>> _points = const [];
  List<Map<String, dynamic>> _homes = const [];
  String _msg = '';
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final r = await widget.api.standbyPoints(widget.robotId);
      if (!mounted) return;
      setState(() {
        _points = (r['points'] as List? ?? const []).cast<Map<String, dynamic>>();
        _homes = (r['homes'] as List? ?? const []).cast<Map<String, dynamic>>();
        _loading = false;
      });
    } on SiteError catch (e) {
      if (mounted) {
        setState(() {
          _msg = e.toString();
          _loading = false;
        });
      }
    }
  }

  Future<void> _run(Future<Map<String, dynamic>> Function() f, String what) async {
    String msg;
    try {
      final r = await f();
      final ack = r['ack'];
      final res = ack is Map ? ack['result'] : null;
      msg = (res == null || res == 'accepted')
          ? tr('$what: done', '$what：完成')
          : tr('$what: ${ackReasonText('${ack['reason'] ?? res}')}',
              '$what：${ackReasonText('${ack['reason'] ?? res}')}');
    } on SiteError catch (e) {
      msg = e.status == 409
          ? tr('$what failed: ${ackReasonText(e.message.replaceFirst(RegExp(r'^机器人标记失败[:：]'), ''))}',
              '$what失败：${ackReasonText(e.message.replaceFirst(RegExp(r'^机器人标记失败[:：]'), ''))}')
          : tr('$what failed: $e', '$what失败：$e');
    }
    if (!mounted) return;
    setState(() => _msg = msg);
    await _load();
  }

  Future<void> _here() async {
    final name = TextEditingController();
    var isDefault = _points.isEmpty;
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, set) => AlertDialog(
          scrollable: true, // 横屏时键盘占掉一大半高：整个对话框能滚（同 fields_dialog）
          title: Text(tr('Set standby point here', '在这儿设待命点')),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(tr(
                "Sets the robot's current position as a standby point (origin unchanged). Not possible if the position isn't set, the error is large, or a task is running.",
                '将机器人当前位置设为待命点（原点不变）。未设置位置、偏差过大或执行任务时无法设置。',
              )),
              TextField(
                key: SiteStandbyPage.nameKey,
                controller: name,
                decoration: InputDecoration(
                    labelText: tr('Name (letters, digits, . _ -)', '名字（字母、数字、. _ -）')),
              ),
              CheckboxListTile(
                key: SiteStandbyPage.defaultKey,
                contentPadding: EdgeInsets.zero,
                value: isDefault,
                onChanged: (v) => set(() => isDefault = v ?? false),
                title: Text(tr('Make default (the robot returns here after a task)', '设为默认（任务结束后返回此处）')),
              ),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
            FilledButton(
              key: SiteStandbyPage.goKey,
              onPressed: () => Navigator.pop(c, true),
              child: Text(tr('Set', '设置')),
            ),
          ],
        ),
      ),
    );
    final n = name.text.trim();
    if (ok != true || n.isEmpty) return;
    await _run(() => widget.api.standbyHere(widget.robotId, n, isDefault: isDefault),
        tr('Set standby point $n', '设待命点 $n'));
  }

  Future<void> _remove(String n) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Delete standby point $n?', '删除待命点 $n？')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
          FilledButton(
            key: Key('standby-remove-go-$n'),
            onPressed: () => Navigator.pop(c, true),
            child: Text(tr('Delete', '删除')),
          ),
        ],
      ),
    );
    if (ok == true) {
      await _run(() => widget.api.removeStandby(widget.robotId, n),
          tr('Delete standby point $n', '删除待命点 $n'));
    }
  }

  static String _xy(Map<String, dynamic> p) =>
      '${p['map_id']}:${p['map_version']}  (${(p['x'] as num).toStringAsFixed(1)}, '
      '${(p['y'] as num).toStringAsFixed(1)})';

  /// W13：狗现在站的地方登记成充电桩对准点（领到桩前约 1.5 米、正对桩）。
  Future<void> _charger() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        scrollable: true,
        title: Text(tr("Set the robot's current position as the dock alignment point?",
            '把机器人现在的位置设成充电桩对准点？')),
        content: Text(tr(
            'Lead the robot to about 1.5 m in front of the dock, facing it. Below 30% battery it docks to charge automatically.',
            '请将机器人引导到充电桩正前方约 1.5 米处，朝向充电桩。电量低于 30% 时将自动回充。')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
          FilledButton(
              key: const Key('standby-charger-go'),
              onPressed: () => Navigator.pop(c, true),
              child: Text(tr('Set', '设置'))),
        ],
      ),
    );
    if (ok != true) return;
    String msg;
    try {
      final d = await widget.api.setChargerHere(widget.robotId);
      msg = tr(
          'Dock alignment point set (${(d['x'] as num).toStringAsFixed(1)}, ${(d['y'] as num).toStringAsFixed(1)})',
          '充电桩对准点已设置（${(d['x'] as num).toStringAsFixed(1)}, ${(d['y'] as num).toStringAsFixed(1)}）');
    } on SiteError catch (e) {
      msg = tr('Not set: $e', '设置失败：$e');
    }
    if (mounted) setState(() => _msg = msg);
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = widget.api.session;
    final manage = s?.canManageMaps ?? false;
    final dispatch = s?.canDispatch ?? false;
    final head = NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.bold);
    final body = NwText.bodyMedium.copyWith(color: c.text);
    return Scaffold(
      appBar: AppBar(
        title: Text(tr('${widget.robotId} · Standby points and origin', '${widget.robotId} · 待命点与原点')),
        actions: barActions(context, [
          if (manage && widget.canMarkHere)
            BarAction(
                key: SiteStandbyPage.hereKey,
                onPressed: _here,
                label: tr('Set standby point here', '在这儿设待命点')),
          if (manage)
            BarAction(
                key: SiteStandbyPage.chargerKey,
                onPressed: _charger,
                label: tr('Set dock here', '在这儿设充电桩')),
        ]),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(Nw.s16),
              children: [
                if (_msg.isNotEmpty)
                  Padding(
                      padding: const EdgeInsets.only(bottom: Nw.s16),
                      child: Text(_msg, key: SiteStandbyPage.msgKey, style: body)),
                Text(
                  tr(
                      'Origin (safe return and charging; one per map; change it with '
                          '"Mark origin here" on the robot page)',
                      '原点（用于安全返航和回充；每张地图一个，在机器人页「在这儿标原点」修改）'),
                  style: head,
                ),
                const SizedBox(height: Nw.s8),
                NwCard(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  if (_homes.isEmpty)
                    Text(tr('No origin marked yet', '尚未标原点'), style: body.copyWith(color: c.textSecondary)),
                  for (final h in _homes)
                    Text(
                      '${h['name']}  ${_xy(h)}',
                      key: Key('home-${h['map_id']}-${h['map_version']}'),
                      style: body,
                    ),
                ])),
                const SizedBox(height: Nw.s24),
                Text(
                    tr('Standby points (the robot returns to the default one after a task)',
                        '待命点（任务结束后返回默认待命点）'),
                    style: head),
                const SizedBox(height: Nw.s8),
                if (_points.isEmpty)
                  Text(tr('No standby points yet', '暂无待命点'), style: body.copyWith(color: c.textSecondary)),
                if (_points.isNotEmpty)
                  NwCard(
                      padding: EdgeInsets.zero,
                      child: Column(mainAxisSize: MainAxisSize.min, children: [
                        for (final p in _points)
                          ListTile(
                            key: Key('standby-${p['name']}'),
                            dense: true,
                            shape: Border(bottom: BorderSide(color: c.outline)),
                            title: Text(
                                '${p['name']}${p['default'] == true ? tr(' (default)', '（默认）') : ''}'),
                            subtitle: Text(_xy(p)),
                            trailing: Wrap(
                              spacing: 4,
                              children: [
                                if (dispatch)
                                  OutlinedButton(
                                    key: Key('standby-go-${p['name']}'),
                                    onPressed: () => _run(
                                      () => widget.api
                                          .returnToStandby(widget.robotId, name: '${p['name']}'),
                                      tr('Return to standby ${p['name']}', '回待命点 ${p['name']}'),
                                    ),
                                    child: Text(tr('Return here', '返回此处')),
                                  ),
                                if (manage && p['default'] != true)
                                  OutlinedButton(
                                    key: Key('standby-default-${p['name']}'),
                                    onPressed: () => _run(
                                      () => widget.api.setStandbyDefault(widget.robotId, p),
                                      tr('Make ${p['name']} default', '设 ${p['name']} 为默认'),
                                    ),
                                    child: Text(tr('Make default', '设默认')),
                                  ),
                                if (manage)
                                  IconButton(
                                    key: Key('standby-remove-${p['name']}'),
                                    tooltip: tr('Delete', '删除'),
                                    onPressed: () => _remove('${p['name']}'),
                                    icon: const Icon(Icons.delete_outline_rounded),
                                  ),
                              ],
                            ),
                          ),
                      ])),
              ],
            ),
    );
  }
}
