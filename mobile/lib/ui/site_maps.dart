/// 站点模式的地图（W00c5d 第二部分，决策 8：站点是地图的唯一权威，狗上只有正在用的那一张）。
///
/// - 每张图一行（地图号:版本、从哪来、几个文件）；管理员点一张 → 选一台狗 → 下发。狗后台下载、核对、
///   载入，成了之后狗报的地图版本跟着变；没成站点出告警。
/// - 录包一行一个；管理员可以拿它在狗上重建一张新图（要给一个新的版本号）。
/// - 录包的开始、停止在单狗页（管理员）。
library;

import 'package:flutter/material.dart';

import '../net/site_client.dart';

class SiteMapsPage extends StatefulWidget {
  final SiteApi api;
  const SiteMapsPage({super.key, required this.api});

  static Key mapKey(String id, String version) => Key('map-$id:$version');
  static Key bagKey(String robot, String name) => Key('bag-$robot/$name');
  static const Key versionKey = Key('build-version');

  @override
  State<SiteMapsPage> createState() => _SiteMapsPageState();
}

class _SiteMapsPageState extends State<SiteMapsPage> {
  late Future<Map<String, dynamic>> _data = widget.api.maps();

  void _snack(String t) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));
  }

  String _ackText(Map<String, dynamic> r, String ok) {
    final ack = (r['ack'] as Map?) ?? const {};
    return ack['result'] == 'accepted' ? ok : '狗没接：${ack['result']} ${ack['reason'] ?? ''}';
  }

  Future<String?> _pickRobot() async {
    final robots = await widget.api.robots();
    if (!mounted) return null;
    return showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: const Text('下发给哪台狗'), children: [
        for (final r in robots)
          SimpleDialogOption(
              key: Key('pick-${r['robot_id']}'),
              onPressed: () => Navigator.pop(c, '${r['robot_id']}'),
              child: Text('${r['robot_id']}')),
      ]),
    );
  }

  Future<void> _activate(Map<String, dynamic> m) async {
    final robot = await _pickRobot();
    if (robot == null) return;
    try {
      final r = await widget.api.activateMap(robot, '${m['map_id']}', '${m['version']}');
      _snack(_ackText(r, '$robot 在下载、载入 ${m['map_id']}:${m['version']}，好了它报的地图版本会变'));
    } on SiteError catch (e) {
      _snack('下发没成：$e');
    }
  }

  Future<void> _build(Map<String, dynamic> b) async {
    final ctl = TextEditingController();
    final mapId = TextEditingController(text: 'estate-1');
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text('拿 ${b['name']} 重建'),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(controller: mapId, decoration: const InputDecoration(labelText: '地图号')),
          TextField(
              key: SiteMapsPage.versionKey,
              controller: ctl,
              decoration: const InputDecoration(labelText: '新的版本号（不能跟已有的重）')),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
          FilledButton(
              key: const Key('build-go'),
              onPressed: () => Navigator.pop(c, true),
              child: const Text('重建')),
        ],
      ),
    );
    if (ok != true || ctl.text.trim().isEmpty) return;
    try {
      final r = await widget.api
          .buildMap('${b['robot_id']}', '${b['name']}', mapId.text.trim(), ctl.text.trim());
      _snack(_ackText(r, '${b['robot_id']} 开始重建，好了新图会出现在这里'));
    } on SiteError catch (e) {
      _snack('重建没成：$e');
    }
  }

  @override
  Widget build(BuildContext context) {
    final admin = widget.api.session?.canManageMaps ?? false;
    return Scaffold(
      appBar: AppBar(title: const Text('地图')),
      body: RefreshIndicator(
        onRefresh: () async {
          setState(() {
            _data = widget.api.maps();
          });
          await _data.catchError((Object _) => <String, dynamic>{});
        },
        child: FutureBuilder<Map<String, dynamic>>(
          future: _data,
          builder: (c, snap) {
            if (snap.hasError) {
              return ListView(children: [
                ListTile(
                    title: Text('地图目录拿不到：${snap.error}',
                        style: const TextStyle(color: Colors.red))),
              ]);
            }
            if (!snap.hasData) return const Center(child: CircularProgressIndicator());
            final maps = (snap.data!['maps'] as List).whereType<Map<String, dynamic>>();
            final bags = (snap.data!['bags'] as List).whereType<Map<String, dynamic>>();
            return ListView(children: [
              const ListTile(title: Text('图')),
              if (maps.isEmpty) const ListTile(title: Text('站点上还没有图')),
              for (final m in maps)
                ListTile(
                  key: SiteMapsPage.mapKey('${m['map_id']}', '${m['version']}'),
                  title: Text('${m['map_id']}:${m['version']}'),
                  subtitle: Text('来自 ${m['source']} · ${(m['files'] as List).length} 个文件'
                      '${(m['note'] ?? '') != '' ? ' · ${m['note']}' : ''}'),
                  trailing: admin ? const Icon(Icons.send_to_mobile) : null,
                  onTap: admin ? () => _activate(m) : null,
                ),
              const Divider(),
              const ListTile(title: Text('录包')),
              if (bags.isEmpty) const ListTile(title: Text('还没有录包')),
              for (final b in bags)
                ListTile(
                  key: SiteMapsPage.bagKey('${b['robot_id']}', '${b['name']}'),
                  title: Text('${b['name']}'),
                  subtitle: Text('${b['robot_id']} · ${((b['bytes'] as num? ?? 0) / 1048576).toStringAsFixed(1)} MB'),
                  trailing: admin ? const Icon(Icons.build) : null,
                  onTap: admin ? () => _build(b) : null,
                ),
            ]);
          },
        ),
      ),
    );
  }
}
