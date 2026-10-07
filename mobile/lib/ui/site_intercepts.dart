/// 拦截点与防区（W16）。
///
/// 摄像头报「某个防区有入侵」→ 站点按防区找到**拦截点**，派最近的一只狗去。这一页列出拦截点与防区的对应；
/// 管理员能在某只狗**现在的位置**设拦截点（狗用此刻锚定后的位置、定位不好就拒，狗上什么都不改；拦截点记着
/// 狗现在用的那张图与版本）、把防区绑到拦截点、让防区不再派狗、删拦截点（还有防区指着它就删不了）。
/// 别的角色只看。
library;

import 'package:flutter/material.dart';

import '../net/site_client.dart';
import 'widget/fields_dialog.dart';

class SiteInterceptsPage extends StatefulWidget {
  final SiteApi api;
  const SiteInterceptsPage({super.key, required this.api});

  static const Key hereKey = Key('intercept-here');
  static const Key zoneKey = Key('zone-add');
  static const Key msgKey = Key('intercept-msg');

  @override
  State<SiteInterceptsPage> createState() => _SiteInterceptsPageState();
}

class _SiteInterceptsPageState extends State<SiteInterceptsPage> {
  List<Map<String, dynamic>> _points = const [];
  List<Map<String, dynamic>> _zones = const [];
  String _msg = '';
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _take(Map<String, dynamic> d) {
    _points = (d['intercepts'] as List? ?? const []).cast<Map<String, dynamic>>();
    _zones = (d['zones'] as List? ?? const []).cast<Map<String, dynamic>>();
  }

  Future<void> _load() async {
    try {
      final d = await widget.api.intercepts();
      if (!mounted) return;
      setState(() {
        _take(d);
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
      final d = await f();
      _take(d);
      msg = '$what：好了';
    } on SiteError catch (e) {
      msg = e.status == 409
          ? '$what没成：${ackReasonText(e.message.replaceFirst(RegExp(r'^狗没标[:：]'), ''))}'
          : '$what没成：$e';
    }
    if (mounted) setState(() => _msg = msg);
  }

  Future<void> _here() async {
    List<Map<String, dynamic>> robots;
    try {
      robots = (await widget.api.robots()).where(robotCanStandbyHere).toList();
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = '拿不到狗的列表：$e');
      return;
    }
    if (!mounted) return;
    if (robots.isEmpty) {
      setState(() => _msg = '没有能报位置的狗（不在线，或者狗上的版本太老）');
      return;
    }
    final robot = robots.length == 1
        ? '${robots.first['robot_id']}'
        : await showDialog<String>(
            context: context,
            builder: (c) => SimpleDialog(title: const Text('用哪只狗现在的位置？'), children: [
              for (final r in robots)
                SimpleDialogOption(
                    key: Key('intercept-robot-${r['robot_id']}'),
                    onPressed: () => Navigator.pop(c, '${r['robot_id']}'),
                    child: Text('${r['robot_id']}')),
            ]),
          );
    if (robot == null || !mounted) return;
    final got = await showFieldsDialog(context,
        title: '在 $robot 现在的位置设拦截点',
        fields: const [DialogField('拦截点名字（字母、数字、. _ -）')],
        confirm: '设');
    final name = got?.first.trim() ?? '';
    if (name.isEmpty) return;
    await _run(() => widget.api.interceptHere(robot, name), '设拦截点 $name');
  }

  Future<void> _addZone() async {
    if (_points.isEmpty) {
      setState(() => _msg = '先设一个拦截点');
      return;
    }
    String? point = '${_points.first['name']}';
    final zone = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, set) => AlertDialog(
          scrollable: true, // 横屏弹键盘
          title: const Text('防区绑到拦截点'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(
                key: const Key('zone-name'),
                controller: zone,
                decoration: const InputDecoration(labelText: '防区名（跟摄像头、NVR 报的一样）')),
            DropdownButton<String>(
              key: const Key('zone-intercept'),
              isExpanded: true,
              value: point,
              items: [
                for (final p in _points)
                  DropdownMenuItem(value: '${p['name']}', child: Text('${p['name']}')),
              ],
              onChanged: (v) => set(() => point = v),
            ),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
            FilledButton(
                key: const Key('zone-go'),
                onPressed: () => Navigator.pop(c, true),
                child: const Text('绑')),
          ],
        ),
      ),
    );
    final z = zone.text.trim();
    if (ok != true || z.isEmpty || point == null) return;
    await _run(() => widget.api.mapZone(z, point!), '防区 $z 绑到 $point');
  }

  Future<void> _confirm(String title, Future<void> Function() go, Key key) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(title),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
          FilledButton(key: key, onPressed: () => Navigator.pop(c, true), child: const Text('是')),
        ],
      ),
    );
    if (ok == true) await go();
  }

  @override
  Widget build(BuildContext context) {
    final manage = widget.api.session?.canManageMaps ?? false;
    return Scaffold(
      appBar: AppBar(title: const Text('拦截点与防区'), actions: [
        if (manage) ...[
          TextButton(
              key: SiteInterceptsPage.hereKey, onPressed: _here, child: const Text('在狗这儿设拦截点')),
          TextButton(key: SiteInterceptsPage.zoneKey, onPressed: _addZone, child: const Text('绑防区')),
        ],
      ]),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(8), children: [
              if (_msg.isNotEmpty) Text(_msg, key: SiteInterceptsPage.msgKey),
              const Text('防区 → 拦截点（摄像头报这个防区有入侵，派狗去那个拦截点）',
                  style: TextStyle(fontWeight: FontWeight.bold)),
              if (_zones.isEmpty) const Text('还没有防区：这时候来的入侵都记「没映射」，没狗去'),
              for (final z in _zones)
                ListTile(
                  key: Key('zone-${z['zone']}'),
                  dense: true,
                  title: Text('${z['zone']} → ${z['intercept']}'),
                  trailing: manage
                      ? IconButton(
                          key: Key('zone-remove-${z['zone']}'),
                          tooltip: '不再派狗',
                          icon: const Icon(Icons.link_off),
                          onPressed: () => _confirm('防区 ${z['zone']} 不再派狗？（之后的入侵照样报告警）',
                              () => _run(() => widget.api.unmapZone('${z['zone']}'), '取消防区 ${z['zone']}'),
                              Key('zone-remove-go-${z['zone']}')))
                      : null,
                ),
              const Divider(),
              const Text('拦截点', style: TextStyle(fontWeight: FontWeight.bold)),
              if (_points.isEmpty) const Text('还没有拦截点'),
              for (final p in _points)
                ListTile(
                  key: Key('intercept-${p['name']}'),
                  dense: true,
                  title: Text('${p['name']}'),
                  subtitle: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('${p['map_id']}:${p['map_version']}  (${(p['x'] as num).toStringAsFixed(1)}, '
                        '${(p['y'] as num).toStringAsFixed(1)})'),
                    // W23：走不到（入侵来了不派狗）、没查成的、图有新版本要重设
                    if ('${p['reach'] ?? ''}'.isNotEmpty)
                      Text('走不到，入侵来了不派狗：${p['reach']}',
                          key: Key('intercept-reach-${p['name']}'), style: const TextStyle(color: Colors.red)),
                    if ('${p['reach_note'] ?? ''}'.isNotEmpty)
                      Text('${p['reach_note']}', style: const TextStyle(color: Colors.grey)),
                    if (p['newer_version'] is String)
                      Text('图有新版本 ${p['newer_version']}：要在新图上重设',
                          key: Key('intercept-newer-${p['name']}'), style: const TextStyle(color: Colors.orange)),
                  ]),
                  trailing: manage
                      ? IconButton(
                          key: Key('intercept-remove-${p['name']}'),
                          tooltip: '删掉',
                          icon: const Icon(Icons.delete_outline),
                          onPressed: () => _confirm('删掉拦截点 ${p['name']}？',
                              () => _run(() => widget.api.removeIntercept('${p['name']}'), '删拦截点 ${p['name']}'),
                              Key('intercept-remove-go-${p['name']}')))
                      : null,
                ),
            ]),
    );
  }
}
