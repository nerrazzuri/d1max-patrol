/// 站点模式的地图（W00c5d 第二部分，决策 8：站点是地图的唯一权威，狗上只有正在用的那一张）。
///
/// - 每张图一行（地图号:版本、从哪来、几个文件）；管理员点一张 → 选一台狗 → 下发。狗后台下载、核对、
///   载入，成了之后狗报的地图版本跟着变；没成站点出告警。
/// - 录包一行一个；管理员可以拿它在狗上重建一张新图（要给一个新的版本号）。
/// - 录包的开始、停止在单狗页（管理员）。
library;

import 'package:flutter/material.dart';

import '../design/tokens.g.dart';
import '../l10n.dart';
import '../net/site_client.dart';
import 'site_mapping.dart';
import 'widget/fields_dialog.dart';

class SiteMapsPage extends StatefulWidget {
  final SiteApi api;
  const SiteMapsPage({super.key, required this.api});

  static Key mapKey(String id, String version) => Key('map-$id:$version');
  static Key bagKey(String robot, String name) => Key('bag-$robot/$name');
  static Key previewKey(String id, String version) => Key('preview-$id:$version');
  static const Key versionKey = Key('build-version');

  @override
  State<SiteMapsPage> createState() => _SiteMapsPageState();
}

class _SiteMapsPageState extends State<SiteMapsPage> {
  late Future<Map<String, dynamic>> _data = widget.api.maps();

  void _snack(String t) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar() // 新的一条顶掉旧的：不让人等旧的那条 4 秒过完才看到结果
      ..showSnackBar(SnackBar(content: Text(t)));
  }

  String _ackText(Map<String, dynamic> r, String ok) {
    final ack = (r['ack'] as Map?) ?? const {};
    return ack['result'] == 'accepted'
        ? ok
        : tr('The robot did not accept: ${ack['result']} ${ack['reason'] ?? ''}',
            '狗没接：${ack['result']} ${ack['reason'] ?? ''}');
  }

  /// 选一台**报了这种能力**的狗（没这能力的狗会回 unsupported）。一台都没有就说一声、返回 null。
  Future<String?> _pickRobot(String task, String what) async {
    List<Map<String, dynamic>> robots;
    try {
      robots = (await widget.api.robots()).where((r) => robotCan(r, task)).toList();
    } on SiteError catch (e) {
      _snack(tr('Robot list unavailable: $e', '拿不到狗的列表：$e'));
      return null;
    }
    if (!mounted) return null;
    if (robots.isEmpty) {
      _snack(tr('No robot can $what (none is online reporting this capability, or it is not supported)',
          '没有能$what的狗（狗没在线报能力，或者这台狗不支持）'));
      return null;
    }
    return showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: Text(tr('Send to which robot', '下发给哪台狗')), children: [
        for (final r in robots)
          SimpleDialogOption(
              key: Key('pick-${r['robot_id']}'),
              onPressed: () => Navigator.pop(c, '${r['robot_id']}'),
              child: Text('${r['robot_id']}')),
      ]),
    );
  }

  Future<void> _activate(Map<String, dynamic> m) async {
    final robot = await _pickRobot('map_activate', tr('switch maps', '换图'));
    if (robot == null || !mounted) return;
    final label = '${m['map_id']}:${m['version']}';
    // 换图会换坐标系：点位、待命点都得是这张图上的。点错了狗就在错的图上走，所以要再确认一次。
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Switch $robot to $label?', '让 $robot 换成 $label？')),
        content: Text(tr(
            'The robot downloads and verifies the map first, then switches coordinate frames once its '
                'current task is done. It accepts no new tasks during the switch. '
                'Task packages and standby points must be on this map.',
            '狗先下载、核对，等手上的任务做完再切换坐标系；切换那一小会儿不接新任务。'
                '任务包、待命点要是这张图上的。')),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
          FilledButton(
              key: const Key('activate-go'),
              onPressed: () => Navigator.pop(c, true),
              child: Text(tr('Switch', '换'))),
        ],
      ),
    );
    if (ok != true) return;
    try {
      Map<String, dynamic> r;
      try {
        r = await widget.api.activateMap(robot, '${m['map_id']}', '${m['version']}');
      } on SiteError catch (e) {
        // W13a：这台狗在这张图上还没有原点。说清楚后果，管理员确认了才照样下发（标原点要狗先载上这张图）。
        if (e.status != 409 || e.body['reason'] != 'no_home' || !mounted) rethrow;
        final go = await showDialog<bool>(
          context: context,
          builder: (c) => AlertDialog(
            title: Text(tr('$robot has no origin on $label', '$robot 在 $label 上还没有原点')),
            content: Text(tr(
                'If you send it anyway, the robot accepts no goto or patrol after loading this map '
                    '(without an origin there is no safe place to return to). Remote control and '
                    'set position still work. Drive the robot to the origin (in front of the dock), '
                    'set its position, then tap "Mark origin here".',
                '照样下发的话，狗载上这张图之后不接 goto、巡检（没有原点就没有安全返航的目标）；'
                    '遥控、设位置照常。要先把狗开到原点（充电桩前），设好位置，再点「在这儿标原点」。')),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
              FilledButton(
                  key: const Key('activate-without-home'),
                  onPressed: () => Navigator.pop(c, true),
                  child: Text(tr('Send anyway', '照样下发'))),
            ],
          ),
        );
        if (go != true) {
          _snack(tr('Not sent: $robot has no origin on $label', '没下发：$robot 在 $label 上还没有原点'));
          return;
        }
        r = await widget.api.activateMap(robot, '${m['map_id']}', '${m['version']}',
            withoutHome: true);
      }
      _snack(_ackText(
          r,
          tr('$robot is downloading and loading $label. Its reported map version changes when done',
              '$robot 在下载、载入 $label，好了它报的地图版本会变')));
    } on SiteError catch (e) {
      _snack(tr('Send failed: $e', '下发没成：$e'));
    }
  }

  Future<void> _build(Map<String, dynamic> b) async {
    final robot = '${b['robot_id']}';
    try {
      if (!robotCan(await widget.api.robot(robot), 'map_build')) {
        _snack(tr('$robot cannot rebuild now (not online reporting this capability, or not supported)',
            '$robot 现在不能重建（没在线报能力，或者不支持）'));
        return;
      }
    } on SiteError catch (e) {
      _snack(tr('Status of $robot unavailable: $e', '拿不到 $robot 的状态：$e'));
      return;
    }
    if (!mounted) return;
    final got = await showFieldsDialog(context,
        title: tr('Rebuild from ${b['name']}', '拿 ${b['name']} 重建'),
        fields: [
          DialogField(tr('Map ID', '地图号'), initial: 'estate-1'),
          DialogField(tr('New version (must differ from existing ones)', '新的版本号（不能跟已有的重）'),
              key: SiteMapsPage.versionKey),
        ],
        confirm: tr('Rebuild', '重建'),
        confirmKey: const Key('build-go'));
    if (got == null || got[0].isEmpty || got[1].isEmpty) return;
    try {
      final r = await widget.api.buildMap(robot, '${b['name']}', got[0], got[1]);
      _snack(_ackText(
          r,
          tr('$robot started rebuilding. The new map appears here when done',
              '$robot 开始重建，好了新图会出现在这里')));
    } on SiteError catch (e) {
      _snack(tr('Rebuild failed: $e', '重建没成：$e'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final admin = widget.api.session?.canManageMaps ?? false;
    return Scaffold(
      appBar: AppBar(title: Text(tr('Maps', '地图'))),
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
                    title: Text(tr('Map list unavailable: ${snap.error}', '地图目录拿不到：${snap.error}'),
                        style: const TextStyle(color: D1Color.p1Text))),
              ]);
            }
            if (!snap.hasData) return const Center(child: CircularProgressIndicator());
            final maps = (snap.data!['maps'] as List).whereType<Map<String, dynamic>>();
            final bags = (snap.data!['bags'] as List).whereType<Map<String, dynamic>>();
            return ListView(children: [
              ListTile(title: Text(tr('Maps', '图'))),
              if (maps.isEmpty) ListTile(title: Text(tr('No maps on the site yet', '站点上还没有图'))),
              for (final m in maps)
                ListTile(
                  key: SiteMapsPage.mapKey('${m['map_id']}', '${m['version']}'),
                  title: Text('${m['map_id']}:${m['version']}'),
                  subtitle: Text(tr(
                      'From ${m['source']} · ${(m['files'] as List).length} files'
                          '${(m['note'] ?? '') != '' ? ' · ${m['note']}' : ''}',
                      '来自 ${m['source']} · ${(m['files'] as List).length} 个文件'
                          '${(m['note'] ?? '') != '' ? ' · ${m['note']}' : ''}')),
                  // 看图（W00c6h）谁都能点；点这一行是下发（管理员）。
                  trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                    IconButton(
                        key: SiteMapsPage.previewKey('${m['map_id']}', '${m['version']}'),
                        tooltip: tr('View map', '看图'),
                        icon: const Icon(Icons.map_outlined),
                        onPressed: () => Navigator.push(
                            context,
                            MaterialPageRoute<void>(
                                builder: (_) => SiteMapPreviewPage(
                                    api: widget.api,
                                    mapId: '${m['map_id']}',
                                    version: '${m['version']}')))),
                    if (admin) const Icon(Icons.send_to_mobile),
                  ]),
                  onTap: admin ? () => _activate(m) : null,
                ),
              const Divider(),
              ListTile(title: Text(tr('Mapping recordings', '录包'))),
              if (bags.isEmpty) ListTile(title: Text(tr('No mapping recordings yet', '还没有录包'))),
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
