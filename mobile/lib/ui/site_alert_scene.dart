/// 告警现场（W17）：这条告警出事的时候狗在哪、拦截点在哪（在图上标出来）、到了拍的照片（那一趟的记录）。
///
/// 现场是站点记在告警上的（`context`）：狗最后一次报的地图位姿与收到的时刻、正在跑的那一趟、入侵的
/// 防区与拦截点。老站点不发：这一页只剩标题。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../model/alert.dart';
import '../net/site_client.dart';
import 'site_mapping.dart';
import 'site_recordings.dart';
import 'site_runs.dart';

class SiteAlertScenePage extends StatefulWidget {
  final SiteApi api;
  final Alert alert;
  final DateTime Function() now;
  const SiteAlertScenePage({super.key, required this.api, required this.alert, this.now = DateTime.now});

  static const Key mapKey = Key('scene-map');
  static const Key photoKey = Key('scene-photo');
  static const Key msgKey = Key('scene-msg');
  static const Key videoKey = Key('scene-video');
  static const Key deterKey = Key('scene-deter-here');

  @override
  State<SiteAlertScenePage> createState() => _SiteAlertScenePageState();
}

/// 告警现场里要在图上标的点：狗（红）、拦截点（紫）。没有位姿的不标；图不是同一张的（拦截点与狗报的
/// 不是一张图）各算各的。回 `(图 id, 版本, 点)`；一个都没有回 null。
(String, String, List<MapMark>)? sceneMarks(Alert a) {
  final ctx = a.context;
  final pose = ctx['pose'];
  final point = ctx['intercept'];
  String? mapId;
  String? version;
  final marks = <MapMark>[];
  void add(Object? p, String label, Color color) {
    if (p is! Map || p['x'] is! num || p['y'] is! num) return;
    final m = '${p['map_id'] ?? ''}';
    final v = '${p['map_version'] ?? ''}';
    if (m.isEmpty || v.isEmpty) return;
    if (mapId != null && (m != mapId || v != version)) return; // 别的图上的：不往这张图上画
    mapId = m;
    version = v;
    marks.add(MapMark(label, (p['x'] as num).toDouble(), (p['y'] as num).toDouble(), color));
  }

  add(pose, a.robot == 'site' ? tr('Robot', '狗') : tr('Robot ${a.robot}', '狗 ${a.robot}'), Colors.red);
  if (point is Map) add(point, tr('Intercept point ${point['name'] ?? ''}', '拦截点 ${point['name'] ?? ''}'), Colors.purple);
  if (marks.isEmpty) return null;
  return (mapId!, version!, marks);
}

class _SiteAlertScenePageState extends State<SiteAlertScenePage> {
  String _msg = '';

  Future<void> _photo(String taskId) async {
    List<Map<String, dynamic>> runs;
    try {
      runs = await widget.api.runs(mission: taskId);
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr("Can't find the record for that task: $e", '找不到那一趟的记录：$e'));
      return;
    }
    if (!mounted) return;
    if (runs.isEmpty) {
      setState(() => _msg = tr(
          'The record for that task has not reached the site yet (it uploads after the robot arrives and takes photos)',
          '那一趟的记录还没传到站点（狗到了、拍完会传上来）'));
      return;
    }
    await Navigator.push(
        context,
        MaterialPageRoute<void>(
            builder: (_) => SiteRunPage(api: widget.api, runId: (runs.first['id'] as num).toInt())));
  }

  /// 就地驱离（W33，决策 48）：狗在跑的任务先撤掉，就在它这儿开一场、从 L1（灯光）起。
  Future<void> _deterHere() async {
    try {
      final s = await widget.api.deterStart(widget.alert.robot);
      final lv = s['session'] is Map ? (s['session'] as Map)['level'] : null;
      if (mounted) {
        setState(() => _msg = tr(
            'Deterrence started at ${widget.alert.robot} (L${lv ?? 1}). Escalate or end it on the deterrence page',
            '已在 ${widget.alert.robot} 这儿开驱离（L${lv ?? 1}），驱离页可以升级、解除'));
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr("Can't start deterrence: $e", '开不了：$e'));
    }
  }

  String _ago(int ms) {
    final s = widget.now().difference(DateTime.fromMillisecondsSinceEpoch(ms)).inSeconds;
    if (s < 60) return tr('$s s ago', '$s 秒前');
    if (s < 3600) return tr('${s ~/ 60} min ago', '${s ~/ 60} 分钟前');
    return tr('${s ~/ 3600} h ago', '${s ~/ 3600} 小时前');
  }

  @override
  Widget build(BuildContext context) {
    final a = widget.alert;
    final ctx = a.context;
    final pose = ctx['pose'] is Map ? ctx['pose'] as Map : null;
    final point = ctx['intercept'] is Map ? ctx['intercept'] as Map : null;
    final taskId = ctx['task_id'] is String ? ctx['task_id'] as String : null;
    final marks = sceneMarks(a);
    return Scaffold(
      appBar: AppBar(title: Text(tr('${a.level} ${a.robot} · Scene', '${a.level} ${a.robot} · 现场'))),
      body: ListView(padding: const EdgeInsets.all(12), children: [
        Text(a.title, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
        if (a.detail.isNotEmpty) Text(a.detail),
        const Divider(),
        if (ctx['zone'] != null) Text(tr('Zone: ${ctx['zone']}', '防区：${ctx['zone']}')),
        if (point != null)
          Text(tr(
              'Intercept point: ${point['name']}  (${(point['x'] as num).toStringAsFixed(1)}, '
                  '${(point['y'] as num).toStringAsFixed(1)})',
              '拦截点：${point['name']}  (${(point['x'] as num).toStringAsFixed(1)}, '
                  '${(point['y'] as num).toStringAsFixed(1)})')),
        if (pose != null)
          Text(tr(
              'Robot last at: (${(pose['x'] as num).toStringAsFixed(1)}, '
                  '${(pose['y'] as num).toStringAsFixed(1)})  map ${pose['map_id']}:${pose['map_version']}'
                  '${pose['at_ms'] is num ? '  · reported ${_ago((pose['at_ms'] as num).toInt())}' : ''}',
              '狗最后在：(${(pose['x'] as num).toStringAsFixed(1)}, '
                  '${(pose['y'] as num).toStringAsFixed(1)})  图 ${pose['map_id']}:${pose['map_version']}'
                  '${pose['at_ms'] is num ? '  · ${_ago((pose['at_ms'] as num).toInt())}报的' : ''}')),
        if (taskId != null) Text(tr('Task: $taskId', '那一趟：$taskId')),
        if (ctx.isEmpty) Text(tr('This alarm has no scene data (older site, or a site-level alarm)',
              '这条告警没带现场（老站点，或站点那一行的告警）')),
        const SizedBox(height: 8),
        Wrap(spacing: 8, runSpacing: 8, children: [
          if (marks != null)
            FilledButton.icon(
                key: SiteAlertScenePage.mapKey,
                icon: const Icon(Icons.map_outlined),
                label: Text(tr('Show on map', '在图上看')),
                onPressed: () => Navigator.push(
                    context,
                    MaterialPageRoute<void>(
                        builder: (_) => SiteMapPreviewPage(
                            api: widget.api, mapId: marks.$1, version: marks.$2, marks: marks.$3)))),
          if (a.robot != 'site')
            OutlinedButton.icon(
                key: SiteAlertScenePage.videoKey,
                icon: const Icon(Icons.videocam_outlined),
                label: Text(tr('Scene recording', '现场录像')),
                onPressed: () => Navigator.push(
                    context,
                    MaterialPageRoute<void>(
                        builder: (_) => SiteRecordingsPage(
                            api: widget.api, robotId: a.robot, aroundMs: a.firstMs)))),
          if (a.kind == 'dog_sees_person' && a.robot != 'site')
            FilledButton.icon(
                key: SiteAlertScenePage.deterKey,
                icon: const Icon(Icons.campaign_outlined),
                label: Text(tr('Deter here', '就地驱离')),
                onPressed: _deterHere),
          if (taskId != null)
            OutlinedButton.icon(
                key: SiteAlertScenePage.photoKey,
                icon: const Icon(Icons.photo_camera_outlined),
                label: Text(tr('Scene photos', '现场照片')),
                onPressed: () => _photo(taskId)),
        ]),
        if (_msg.isNotEmpty) Padding(
            padding: const EdgeInsets.only(top: 8), child: Text(_msg, key: SiteAlertScenePage.msgKey)),
      ]),
    );
  }
}
