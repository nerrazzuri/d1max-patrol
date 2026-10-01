/// 待命点与原点（W13a，决策 16）。
///
/// **原点**是安全返航、回充的语义，每张图一个（「在这儿标原点」改它）；**待命点**是运营调度的语义，可以
/// 多个：任务做完狗回默认的那一个，也可以叫它回指定的一个。这一页列出这台狗的原点和待命点；管理员能在狗现在
/// 的位置设待命点（不动原点）、设默认、删除；能派单的人能叫狗回某一个待命点。
library;

import 'package:flutter/material.dart';

import '../net/site_client.dart';

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
          ? '$what：好了'
          : '$what：${ackReasonText('${ack['reason'] ?? res}')}';
    } on SiteError catch (e) {
      msg = e.status == 409
          ? '$what没成：${ackReasonText(e.message.replaceFirst(RegExp(r'^狗没标[:：]'), ''))}'
          : '$what没成：$e';
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
          title: const Text('在这儿设待命点'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Text(
                '用狗现在的位置当一个待命点（原点不动）。狗会先核定位：没设位置、偏差大就不设；'
                '在跑任务也不设。',
              ),
              TextField(
                key: SiteStandbyPage.nameKey,
                controller: name,
                decoration: const InputDecoration(labelText: '名字（字母、数字、. _ -）'),
              ),
              CheckboxListTile(
                key: SiteStandbyPage.defaultKey,
                contentPadding: EdgeInsets.zero,
                value: isDefault,
                onChanged: (v) => set(() => isDefault = v ?? false),
                title: const Text('设成默认（任务做完回这儿）'),
              ),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
            FilledButton(
              key: SiteStandbyPage.goKey,
              onPressed: () => Navigator.pop(c, true),
              child: const Text('设'),
            ),
          ],
        ),
      ),
    );
    final n = name.text.trim();
    if (ok != true || n.isEmpty) return;
    await _run(() => widget.api.standbyHere(widget.robotId, n, isDefault: isDefault), '设待命点 $n');
  }

  Future<void> _remove(String n) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text('删掉待命点 $n？'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
          FilledButton(
            key: Key('standby-remove-go-$n'),
            onPressed: () => Navigator.pop(c, true),
            child: const Text('删'),
          ),
        ],
      ),
    );
    if (ok == true) await _run(() => widget.api.removeStandby(widget.robotId, n), '删待命点 $n');
  }

  static String _xy(Map<String, dynamic> p) =>
      '${p['map_id']}:${p['map_version']}  (${(p['x'] as num).toStringAsFixed(1)}, '
      '${(p['y'] as num).toStringAsFixed(1)})';

  @override
  Widget build(BuildContext context) {
    final s = widget.api.session;
    final manage = s?.canManageMaps ?? false;
    final dispatch = s?.canDispatch ?? false;
    return Scaffold(
      appBar: AppBar(
        title: Text('${widget.robotId} · 待命点与原点'),
        actions: [
          if (manage && widget.canMarkHere)
            TextButton(
              key: SiteStandbyPage.hereKey,
              onPressed: _here,
              child: const Text('在这儿设待命点'),
            ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(8),
              children: [
                if (_msg.isNotEmpty) Text(_msg, key: SiteStandbyPage.msgKey),
                const Text(
                  '原点（安全返航、回充；每张图一个，在狗页「在这儿标原点」改）',
                  style: TextStyle(fontWeight: FontWeight.bold),
                ),
                if (_homes.isEmpty) const Text('还没标过原点'),
                for (final h in _homes)
                  Text(
                    '${h['name']}  ${_xy(h)}',
                    key: Key('home-${h['map_id']}-${h['map_version']}'),
                  ),
                const Divider(),
                const Text('待命点（任务做完回默认的那一个）', style: TextStyle(fontWeight: FontWeight.bold)),
                if (_points.isEmpty) const Text('还没有待命点'),
                for (final p in _points)
                  ListTile(
                    key: Key('standby-${p['name']}'),
                    dense: true,
                    title: Text('${p['name']}${p['default'] == true ? '（默认）' : ''}'),
                    subtitle: Text(_xy(p)),
                    trailing: Wrap(
                      spacing: 4,
                      children: [
                        if (dispatch)
                          OutlinedButton(
                            key: Key('standby-go-${p['name']}'),
                            onPressed: () => _run(
                              () =>
                                  widget.api.returnToStandby(widget.robotId, name: '${p['name']}'),
                              '回待命点 ${p['name']}',
                            ),
                            child: const Text('回这儿'),
                          ),
                        if (manage && p['default'] != true)
                          OutlinedButton(
                            key: Key('standby-default-${p['name']}'),
                            onPressed: () => _run(
                              () => widget.api.setStandbyDefault(widget.robotId, p),
                              '设 ${p['name']} 为默认',
                            ),
                            child: const Text('设默认'),
                          ),
                        if (manage)
                          IconButton(
                            key: Key('standby-remove-${p['name']}'),
                            tooltip: '删掉',
                            onPressed: () => _remove('${p['name']}'),
                            icon: const Icon(Icons.delete_outline),
                          ),
                      ],
                    ),
                  ),
              ],
            ),
    );
  }
}
