/// 布防模式（W20）。
///
/// 全站一个当前模式，决定入侵来了派不派狗、响不响铃（巡检、狗自己的告警不看模式）：
/// - **布防**：所有防区都布防。谁都能切到布防。
/// - **在家**：标了「在家时撤防」的防区撤防，别的照常。业主、管理员能切。
/// - **访客**：在「在家」的基础上再撤几个防区，必须有结束时间（默认 4 小时），到点自动回去。业主、管理员能切。
///
/// 下面列每个防区现在布不布防；管理员能改「在家时也布防」。撤防的防区来了入侵只记录（事件页看得见）。
library;

import 'package:flutter/material.dart';

import '../net/site_client.dart';

class SiteModePage extends StatefulWidget {
  final SiteApi api;
  const SiteModePage({super.key, required this.api});

  static const Key armKey = Key('mode-armed');
  static const Key homeKey = Key('mode-home');
  static const Key visitorKey = Key('mode-visitor');
  static const Key msgKey = Key('mode-msg');

  @override
  State<SiteModePage> createState() => _SiteModePageState();
}

/// 访客能挑的时长（小时）。
const List<int> visitorHours = <int>[1, 2, 4, 8, 12, 24];

class _SiteModePageState extends State<SiteModePage> {
  Map<String, dynamic>? _mode;
  String _msg = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final m = await widget.api.mode();
      if (mounted) setState(() => _mode = m);
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = e.status == 404 ? '这个站点没开布防模式' : '$e');
    }
  }

  Future<void> _run(Future<Map<String, dynamic>> Function() f, String what) async {
    String msg;
    try {
      final m = await f();
      if (mounted) setState(() => _mode = m);
      msg = '$what：好了';
    } on SiteError catch (e) {
      msg = '$what没成：$e';
    }
    if (mounted) setState(() => _msg = msg);
  }

  List<Map<String, dynamic>> get _zones =>
      ((_mode?['zones'] as List?) ?? const []).whereType<Map>().map((z) => z.cast<String, dynamic>()).toList();

  Future<void> _visitor() async {
    final picked = <String>{};
    var hours = 4;
    final zones = _zones.map((z) => '${z['zone']}').toList();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, set) => AlertDialog(
          scrollable: true, // 横屏
          title: const Text('访客模式：在「在家」的基础上，再撤哪几个防区？'),
          content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            if (zones.isEmpty) const Text('还没有防区（只按「在家」撤防）'),
            Wrap(spacing: 6, children: [
              for (final z in zones)
                FilterChip(
                  key: Key('visitor-zone-$z'),
                  label: Text(z),
                  selected: picked.contains(z),
                  onSelected: (v) => set(() => v ? picked.add(z) : picked.remove(z)),
                ),
            ]),
            Row(children: [
              const Text('多久：'),
              DropdownButton<int>(
                key: const Key('visitor-hours'),
                value: hours,
                items: [for (final h in visitorHours) DropdownMenuItem(value: h, child: Text('$h 小时'))],
                onChanged: (v) => set(() => hours = v ?? hours),
              ),
              const Text('，到点自动回去'),
            ]),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
            FilledButton(key: const Key('visitor-go'), onPressed: () => Navigator.pop(c, true), child: const Text('开访客')),
          ],
        ),
      ),
    );
    if (ok != true) return;
    final list = picked.toList()..sort();
    await _run(() => widget.api.setMode('visitor', zones: list, minutes: hours * 60), '开访客 $hours 小时');
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.api.session;
    final canArm = s?.canArm ?? false;
    final canSet = s?.canSetMode ?? false;
    final manage = s?.canManageMaps ?? false;
    final m = _mode;
    return Scaffold(
      appBar: AppBar(title: const Text('布防模式')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.all(8), children: [
          if (_msg.isNotEmpty) Text(_msg, key: SiteModePage.msgKey),
          if (m == null && _msg.isEmpty) const Center(child: CircularProgressIndicator()),
          if (m != null) ...[
            ListTile(
              key: const Key('mode-now'),
              leading: Icon(modeIcon('${m['mode']}'), color: modeColor('${m['mode']}')),
              title: Text('现在：${modeText(m)}'),
              subtitle: Text('${m['set_by'] == 'site:visitor_expired' ? '访客到点自动回来的' : '${m['set_by']} 切的'}'
                  '${(m['visitor_zones'] as List? ?? const []).isNotEmpty ? ' · 访客撤了 ${(m['visitor_zones'] as List).join('、')}' : ''}'),
            ),
            Wrap(spacing: 8, children: [
              if (canArm)
                FilledButton.icon(
                    key: SiteModePage.armKey,
                    onPressed: m['mode'] == 'armed' ? null : () => _run(() => widget.api.setMode('armed'), '切到布防'),
                    icon: const Icon(Icons.shield),
                    label: const Text('布防')),
              if (canSet) ...[
                OutlinedButton.icon(
                    key: SiteModePage.homeKey,
                    onPressed: m['mode'] == 'home' ? null : () => _run(() => widget.api.setMode('home'), '切到在家'),
                    icon: const Icon(Icons.home),
                    label: const Text('在家')),
                OutlinedButton.icon(
                    key: SiteModePage.visitorKey, onPressed: _visitor, icon: const Icon(Icons.people), label: const Text('访客…')),
              ],
              if (!canSet) const Text('撤防（在家、访客）要业主切'),
            ]),
            const Divider(),
            const Text('防区（没配过的防区一律布防）', style: TextStyle(fontWeight: FontWeight.bold)),
            if (_zones.isEmpty) const Text('还没有防区'),
            for (final z in _zones)
              ListTile(
                key: Key('mode-zone-${z['zone']}'),
                dense: true,
                leading: Icon(z['armed'] == true ? Icons.shield : Icons.shield_outlined,
                    color: z['armed'] == true ? Colors.red : Colors.grey),
                title: Text('${z['zone']} · ${z['armed'] == true ? '布防中' : '撤防中'}'),
                subtitle: Text(z['home_armed'] == true ? '在家时也布防' : '在家时撤防'),
                trailing: manage
                    ? Switch(
                        key: Key('mode-zone-home-${z['zone']}'),
                        value: z['home_armed'] == true,
                        onChanged: (v) => _run(() => widget.api.setZoneHome('${z['zone']}', v),
                            '${z['zone']} 在家时${v ? '布防' : '撤防'}'))
                    : null,
              ),
          ],
        ]),
      ),
    );
  }
}

IconData modeIcon(String mode) => switch (mode) {
      'home' => Icons.home,
      'visitor' => Icons.people,
      _ => Icons.shield,
    };

Color modeColor(String mode) => switch (mode) {
      'home' => Colors.blue,
      'visitor' => Colors.orange,
      _ => Colors.red,
    };
