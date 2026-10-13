/// 单狗页（W00c4）：状态、派巡检、叫停、回待命点、遥控、画面，管理员的录包、标原点。
///
/// **按钮按角色显示**：业主（owner）只有「叫停」；保安（guard）与管理员（admin）能派单。真正的
/// 权限在站点上（站点回 403），这里只是不给人一个注定被拒的按钮。
library;

import 'dart:math';

import 'package:flutter/material.dart';

import '../design/tokens.g.dart';
import '../l10n.dart';
import '../net/site_client.dart';
import 'site_deter.dart';
import 'site_logs.dart';
import 'site_mapping.dart';
import 'site_recordings.dart';
import 'site_standby.dart';
import 'site_supervise.dart';
import 'site_teleop.dart';
import 'site_video.dart';
import 'widget/fields_dialog.dart';

/// 一只狗的状态说成一行（狗列表、单狗页共用）。
String robotStatusLine(Map<String, dynamic> r) {
  final st = r['status'];
  if (r['revoked'] == true) return tr('Revoked', '已吊销');
  if (st is! Map) return tr('No status', '没有状态');
  final online = st['online'] == true && r['fresh'] == true;
  final task = st['task'];
  final taskText = task is Map ? taskLabel(task) : tr('Idle', '空闲');
  final held = r['held'] is Map ? tr('Stopped · ', '已叫停 · ') : '';
  // W33：只许手动派的狗（站点自己不让它动）；狗确认看见人（决策 48）
  final manual = r['manual_only'] is Map ? tr('Manual only · ', '只许手动 · ') : '';
  final caps = r['capabilities'];
  final tasks = caps is Map ? caps['tasks'] : null;
  final p = tasks is Map ? tasks['persons'] : null;
  final seen = online && p is Map && p['state'] == 'ok' && p['present'] == true
      ? tr(' · sees ${p['count'] ?? 1} person(s)', ' · 看见 ${p['count'] ?? 1} 人')
      : '';
  return held +
      manual +
      (online ? tr('Online · $taskText$seen', '在线 · $taskText$seen') : tr('Offline', '离线'));
}

/// 一趟任务说人话（W13：回充那几趟）；别的照原样 `kind state`。
String taskLabel(Map task) {
  final id = '${task['task_id'] ?? ''}';
  if (task['kind'] == 'dock') return tr('Charging (docking, charging, undocking)', '充电中（对桩、充电、出桩）');
  if (task['kind'] == 'goto' && id.startsWith('charge-')) return tr('Going to charge', '去充电');
  return '${task['kind']} ${task['state']}';
}

class SiteRobotPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  const SiteRobotPage({super.key, required this.api, required this.robotId});

  @override
  State<SiteRobotPage> createState() => _SiteRobotPageState();
}

class _SiteRobotPageState extends State<SiteRobotPage> {
  Map<String, dynamic>? _view;
  String? _msg;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    try {
      final v = await widget.api.robot(widget.robotId);
      if (mounted) setState(() => _view = v);
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = e.toString());
    }
  }

  /// 发一条、把结果写在页面上。狗收下了（或站点没带回执）回真。
  Future<bool> _do(Future<Map<String, dynamic>> Function() f, String what) async {
    String msg;
    var ok = false;
    try {
      final r = await f();
      final ack = r['ack'];
      final reason = ack is Map ? '${ack['reason'] ?? ''}' : '';
      ok = ack is! Map || ack['result'] == 'accepted' || ack['result'] == 'duplicate';
      final res = ack is Map ? '${ack['result']}' : 'ok';
      msg = reason.isEmpty
          ? tr('$what: $res', '$what：$res')
          : tr('$what: $res (${ackReasonText(reason)})', '$what：$res（${ackReasonText(reason)}）');
    } on SiteError catch (e) {
      msg = tr('$what failed: $e', '$what 没成：$e');
    }
    if (!mounted) return ok; // 派单要等回执，人可能早就退出这一页了
    setState(() => _msg = msg);
    await _reload();
    return ok;
  }

  void _openTrail() => Navigator.push(context,
      MaterialPageRoute<void>(builder: (_) => SiteMappingTrailPage(api: widget.api, robotId: widget.robotId)));

  /// 录包（W00c5d 第二部分，管理员）：给包起个名，之后经站点遥控开着狗走一圈，再停。
  /// 填了地图号与版本就边走边建（W09c2）：停下之后狗上直接打包成这一版，不用再点「重建」。
  Future<void> _startRecord() async {
    final got = await showFieldsDialog(context,
        title: tr('Start bag recording', '开始录包'),
        fields: [
          DialogField(tr('Bag name (letters, digits, . _ -)', '包名（字母、数字、. _ -）'),
              key: const Key('record-name'),
              initial:
                  'rec-${DateTime.now().toUtc().toIso8601String().substring(0, 16).replaceAll(':', '')}'),
          DialogField(
              tr('Map ID (build while driving; leave empty to record only)', '地图号（边走边建；空着只录包）'),
              key: const Key('record-map')),
          DialogField(tr('Version', '版本'), key: const Key('record-version')),
        ],
        confirm: tr('Start', '开始'),
        confirmKey: const Key('record-go'));
    if (got == null || got[0].isEmpty) return;
    final mapId = got[1].trim(), version = got[2].trim();
    if (mapId.isEmpty != version.isEmpty) {
      setState(() => _msg = tr(
          'Build while driving: fill in both map ID and version (leave both empty to record only)',
          '边走边建：地图号与版本要一起填（只录包就都空着）'));
      return;
    }
    final ok = await _do(
        () => widget.api.mapping(widget.robotId, 'start', name: got[0], mapId: mapId, version: version),
        mapId.isEmpty
            ? tr('Start bag recording', '开始录包')
            : tr('Start bag recording and mapping $mapId:$version', '开始录包并建图 $mapId:$version'));
    // 收下了就打开录包轨迹（W00c6h）：边开边看哪儿走过了。录包在狗上后台起，起来之后才开始记点。
    if (ok && mounted && robotCan(_view, 'mapping_trail')) _openTrail();
  }

  /// 设位置（W00c6e）：「狗在原点」一键，或者输地图坐标（朝向按度输）。
  Future<void> _relocalize() async {
    final how = await showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: Text(tr('Where is the robot now', '狗现在在哪')), children: [
        SimpleDialogOption(
            key: const Key('reloc-home'),
            onPressed: () => Navigator.pop(c, 'home'),
            child: Text(tr('At the origin (dock)', '在原点（充电桩）'))),
        SimpleDialogOption(
            key: const Key('reloc-xy'),
            onPressed: () => Navigator.pop(c, 'xy'),
            child: Text(tr('Enter map coordinates', '输地图坐标'))),
      ]),
    );
    if (how == null || !mounted) return;
    if (how == 'home') {
      // 点错了狗就按错的位置走（W00c6e 内审）：再确认一次。
      final sure = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
          title: Text(tr('Is the robot at the origin now?', '狗现在就在原点？')),
          content: Text(tr(
              'It must stand on the origin (dock), facing the same way as when the origin was marked. '
                  'If it is anywhere else, the robot will navigate from the wrong position.',
              '要站在原点（充电桩）上、朝向跟标原点时一样。站在别处点了，狗会按错的位置走。')),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
            FilledButton(
                key: const Key('reloc-home-go'),
                onPressed: () => Navigator.pop(c, true),
                child: Text(tr('At the origin', '在原点'))),
          ],
        ),
      );
      if (sure != true) return;
      await _do(() => widget.api.relocalize(widget.robotId, atHome: true), tr('Set position', '设位置'));
      return;
    }
    final got = await showFieldsDialog(context,
        title: tr('Where is the robot on the map', '狗现在在地图上哪儿'),
        fields: [
          DialogField(tr('x (m)', 'x（米）'), key: const Key('reloc-x')),
          DialogField(tr('y (m)', 'y（米）'), key: const Key('reloc-y')),
          // 地图的 x 轴不一定朝东（雷达建的图按建图起点定轴，W00c6e 内审）。
          DialogField(
              tr('Heading (degrees; 0 = map x axis, counter-clockwise positive)',
                  '朝向（度，地图 x 轴方向为 0，逆时针为正）'),
              key: const Key('reloc-yaw'),
              initial: '0'),
        ],
        confirm: tr('Set', '设'),
        confirmKey: const Key('reloc-go'));
    if (got == null) return;
    final x = double.tryParse(got[0]), y = double.tryParse(got[1]), deg = double.tryParse(got[2]);
    if (x == null || y == null || deg == null || !x.isFinite || !y.isFinite || !deg.isFinite) {
      if (mounted) setState(() => _msg = tr('Coordinates and heading must be numbers', '坐标、朝向要是数字'));
      return;
    }
    await _do(() => widget.api.relocalize(widget.robotId, x: x, y: y, yaw: deg * pi / 180),
        tr('Set position', '设位置'));
  }

  /// 在当前位置标原点（W00c6f，管理员）：确认之后狗用它此刻的位置当这张图上的原点（W13a：原点跟待命点
  /// 分开了 —— 这张图上还没有待命点时，站点顺手用它建一个默认的）。
  Future<void> _markHome() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Mark origin here', '在这儿标原点')),
        content: Text(tr(
            'Move the origin of ${widget.robotId} on this map (used for safe return and charging, named home) '
                'to where it is now? Standby points stay as they are (if this map has none yet, a default '
                'one is created).\n'
                'The robot checks its localization first: it will not mark if the position is not set or the '
                'error is large, or while it is running a task or switching maps.',
            '把 ${widget.robotId} 在这张图上的原点（安全返航、回充用，名字叫 home）换成它现在的'
                '位置？待命点不动（这张图上还没有待命点的话，顺手建一个默认的）。\n'
                '狗会先核定位：没设位置、偏差大就不标；在跑任务、在换图也不标。')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
          FilledButton(
              key: const Key('mark-home-go'),
              onPressed: () => Navigator.pop(c, true),
              child: Text(tr('Mark', '标'))),
        ],
      ),
    );
    if (ok != true) return;
    String msg;
    try {
      Map<String, dynamic> r;
      try {
        r = await widget.api.markHome(widget.robotId);
      } on SiteError catch (e) {
        final taken = e.body['name_taken'];
        if (e.status != 409 || taken is! Map || !mounted) rethrow;
        // 同名的点登记在别的图上（比如重建之前那一版）：问清楚再搬（W00c6f 内审）。
        final where = '${taken['map_id']}:${taken['map_version']}';
        final move = await showDialog<bool>(
          context: context,
          builder: (c) => AlertDialog(
            title: Text(tr('Replace the old standby point?', '替换旧的待命点？')),
            content: Text(tr(
                'Standby point home is registered on $where. Move it to the map the robot is using now '
                    '(it is removed from $where)?',
                '待命点 home 现在登记在 $where 上。换到狗现在用的这张图上（$where 上就没有它了）？')),
            actions: [
              TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
              FilledButton(
                  key: const Key('mark-home-replace'),
                  onPressed: () => Navigator.pop(c, true),
                  child: Text(tr('Replace', '替换'))),
            ],
          ),
        );
        if (move != true) {
          if (mounted) {
            setState(() => _msg = tr('Not marked: standby point home is still on $where',
                '没标：待命点 home 还在 $where 上'));
          }
          return;
        }
        r = await widget.api.markHome(widget.robotId, replace: true);
      }
      final d = (r['ack'] as Map?)?['data'];
      if (d is Map) {
        final x = (d['x'] as num).toStringAsFixed(1), y = (d['y'] as num).toStringAsFixed(1);
        final sigma = (d['sigma_m'] as num? ?? 0).toStringAsFixed(1);
        msg = tr('Origin marked: ($x, $y), error about $sigma m',
            '原点标好了：($x, $y)，偏差约 $sigma m');
      } else {
        msg = tr('Origin marked', '原点标好了');
      }
    } on SiteError catch (e) {
      final d = e.body['data'];
      if (e.status == 500 && d is Map && d['x'] is num && d['y'] is num) {
        // 狗上已经换了、站点没登记上：给坐标，好手工登记待命点。
        final at = '${d['map_id']}:${d['map_version']}';
        final x = (d['x'] as num).toStringAsFixed(1), y = (d['y'] as num).toStringAsFixed(1);
        msg = tr("${e.message}: the robot's origin is at ($x, $y) on $at",
            '${e.message}：狗上的原点在 $at 的 ($x, $y)');
      } else if (e.status == 409) {
        // 站点回的是「狗没标:<狗的原因>」：原因照拒收那一套说人话。
        final why = ackReasonText(e.message.replaceFirst(RegExp(r'^狗没标[:：]'), ''));
        msg = tr('Not marked: $why', '没标：$why');
      } else {
        msg = e.toString(); // 504「狗可能已经标了」等：照站点说的
      }
    }
    if (!mounted) return;
    setState(() => _msg = msg);
    await _reload();
  }

  /// 叫停之前**现取**正在跑的任务：页面上的可能是几秒前的，排程早换了一趟。
  Future<void> _abort() async {
    Map<String, dynamic> v;
    try {
      v = await widget.api.robot(widget.robotId);
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr('Stop failed: $e', '叫停没成：$e'));
      return;
    }
    final st = v['status'];
    final task = st is Map ? st['task'] : null;
    final tid = task is Map ? task['task_id'] : null;
    if (tid is! String || tid.isEmpty) {
      if (mounted) {
        setState(() {
          _view = v;
          _msg = tr('It is not running a task now', '它现在没在跑任务');
        });
      }
      return;
    }
    await _do(() => widget.api.abort(widget.robotId, tid), tr('Stop', '叫停'));
  }

  Future<void> _patrol() async {
    List<String> missions;
    try {
      final s = await widget.api.schedule();
      missions = (s['missions'] as List? ?? const []).map((e) => '$e').toList();
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr("Can't load tasks: $e", '拿不到任务：$e'));
      return;
    }
    if (!mounted) return;
    final pick = await showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: Text(tr('Choose a patrol', '派哪趟巡检')), children: [
        if (missions.isEmpty)
          Padding(
              padding: const EdgeInsets.all(16),
              child: Text(tr('No task bundle on the site yet', '站点上还没有任务包'))),
        for (final m in missions)
          SimpleDialogOption(onPressed: () => Navigator.pop(c, m), child: Text(m)),
      ]),
    );
    if (pick == null) return;
    await _do(() => widget.api.patrol(widget.robotId, pick),
        tr('Dispatch patrol $pick', '派巡检 $pick'));
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.api.session;
    final v = _view;
    final st = v?['status'];
    final task = st is Map ? st['task'] : null;
    final taskId = task is Map && task['task_id'] is String ? task['task_id'] as String : null;
    final events = (v?['events'] as List? ?? const []).whereType<Map>().toList();
    return Scaffold(
      appBar: AppBar(title: Text(widget.robotId)),
      // 「我在现场监护」钉在列表外面（W00c6i 内审）：放在按需构建的列表里，上面插一行提示或者往下一翻，
      // 它的状态就被换掉、发出放租 —— 刚派出去的任务随即被中止。
      body: Column(children: [
        if ((s?.canDispatch ?? false) && robotNeedsSupervision(v))
          SupervisionSwitch(
              key: ValueKey('supervise-${widget.robotId}'), api: widget.api, robotId: widget.robotId),
        Expanded(
          child: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(padding: const EdgeInsets.all(12), children: [
          Text(v == null ? tr('Loading…', '加载中…') : robotStatusLine(v), key: const Key('robot-status')),
          if (v?['loc'] is Map)
            Text(locText((v!['loc'] as Map).cast<String, dynamic>()), key: const Key('robot-loc')),
          if (v?['rtk'] is Map)
            Text(rtkText((v!['rtk'] as Map).cast<String, dynamic>()), key: const Key('robot-rtk')),
          if (v?['capabilities'] is Map &&
              headText((v!['capabilities'] as Map).cast<String, dynamic>()).isNotEmpty)
            Text(headText((v['capabilities'] as Map).cast<String, dynamic>()),
                key: const Key('robot-head'), style: const TextStyle(color: D1Color.p2)),
          if (v?['capabilities'] is Map &&
              obstaclesText((v!['capabilities'] as Map).cast<String, dynamic>()).isNotEmpty)
            Text(obstaclesText((v['capabilities'] as Map).cast<String, dynamic>()),
                key: const Key('robot-obstacles')),
          // 叫停之后站点不再派它（W00c5e）：说清楚，给能派单的人一个「恢复」。
          if (v?['held'] is Map)
            Card(
              key: const Key('robot-held'),
              color: D1Color.p2Bg,
              child: ListTile(
                title: Text(tr('Stopped by ${(v!['held'] as Map)['by']}',
                    '被 ${(v['held'] as Map)['by']} 叫停了')),
                subtitle: Text(tr(
                    'The site will not dispatch it (schedule, incidents, return to standby, remote control) '
                        'until you tap Resume',
                    '站点不会再派它（排程、事件、回待命点、遥控都不派），点「恢复」之后才派')),
                trailing: (s?.canDispatch ?? false)
                    ? FilledButton(
                        key: const Key('btn-resume'),
                        onPressed: () => _do(() => widget.api.resume(widget.robotId), tr('Resume', '恢复')),
                        child: Text(tr('Resume', '恢复')))
                    : null,
              ),
            ),
          if (_msg != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_msg!)),
          const SizedBox(height: 12),
          Wrap(spacing: 8, runSpacing: 8, children: [
            if (s?.canDispatch ?? false)
              FilledButton(
                  key: const Key('btn-patrol'), onPressed: _patrol, child: Text(tr('Dispatch patrol', '派巡检'))),
            // 连续录像（W18）：谁都能看
            OutlinedButton(
                key: const Key('btn-recordings'),
                onPressed: () => Navigator.push(
                    context,
                    MaterialPageRoute<void>(
                        builder: (_) => SiteRecordingsPage(api: widget.api, robotId: widget.robotId))),
                child: Text(tr('Recordings', '录像'))),
            if (s?.canTeleop ?? false)
              FilledButton.tonal(
                  key: const Key('btn-teleop'),
                  onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) =>
                              SiteTeleopPage(api: widget.api, robotId: widget.robotId))),
                  child: Text(tr('Remote control', '遥控'))),
            if ((s?.canManageMaps ?? false) && robotCan(v, 'mapping')) ...[
              OutlinedButton(
                  key: const Key('btn-record-start'),
                  onPressed: _startRecord,
                  child: Text(tr('Start bag recording', '开始录包'))),
              OutlinedButton(
                  key: const Key('btn-record-stop'),
                  onPressed: () => _do(() => widget.api.mapping(widget.robotId, 'stop'),
                      tr('Stop bag recording', '停止录包')),
                  child: Text(tr('Stop bag recording', '停止录包'))),
            ],
            if ((s?.canManageMaps ?? false) && robotCan(v, 'mark_home'))
              OutlinedButton(
                  key: const Key('btn-mark-home'),
                  onPressed: _markHome,
                  child: Text(tr('Mark origin here', '在这儿标原点'))),
            if ((s?.canManageMaps ?? false) && robotCan(v, 'mapping_trail'))
              OutlinedButton(
                  key: const Key('btn-mapping-trail'), onPressed: _openTrail,
                  child: Text(tr('Bag recording trail', '录包轨迹'))),
            // 建图进程日志（W00c6g）：管理员看录包、重建子进程的日志，不用再 SSH 上狗。
            if ((s?.canManageMaps ?? false) && robotCan(v, 'proc_log'))
              OutlinedButton(
                  key: const Key('btn-proc-logs'),
                  onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) =>
                              SiteProcLogsPage(api: widget.api, robotId: widget.robotId))),
                  child: Text(tr('Mapping logs', '建图日志'))),
            // 上装（W21）：警灯、警笛、聚光灯、喇叭。保安、管理员；狗报了 deter 才有
            if ((s?.canDispatch ?? false) && robotCan(v, 'deter'))
              OutlinedButton(
                  key: const Key('btn-deter'),
                  onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) => SiteDeterPage(
                              api: widget.api,
                              robotId: widget.robotId,
                              caps: Map<String, dynamic>.from(
                                  ((v?['capabilities'] as Map)['tasks'] as Map)['deter'] as Map)))),
                  child: Text(tr('Payload…', '上装…'))),
            if ((s?.canDispatch ?? false) && robotCan(v, 'relocalize'))
              OutlinedButton(
                  key: const Key('btn-relocalize'),
                  onPressed: _relocalize,
                  child: Text(tr('Set position', '设位置'))),
            if (s?.canDispatch ?? false)
              OutlinedButton(
                  key: const Key('btn-standby'),
                  onPressed: () => _do(() => widget.api.returnToStandby(widget.robotId),
                      tr('Return to standby', '回待命点')),
                  child: Text(tr('Return to standby', '回待命点'))),
            // 待命点与原点（W13a）：列表、回指定的一个；管理员在这儿设、设默认、删
            if ((s?.canDispatch ?? false) || (s?.canManageMaps ?? false))
              OutlinedButton(
                  key: const Key('btn-standby-list'),
                  onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) => SiteStandbyPage(
                              api: widget.api,
                              robotId: widget.robotId,
                              canMarkHere: robotCanStandbyHere(v)))),
                  child: Text(tr('Standby points…', '待命点…'))),
            if ((s?.canAbort ?? false) && taskId != null)
              FilledButton(
                  key: const Key('btn-abort'),
                  style: FilledButton.styleFrom(
                      backgroundColor: D1Color.p1,
                      foregroundColor: D1Color.onSolid,
                      side: const BorderSide(color: D1Color.p1Frame, width: D1Size.stopAllRing)),
                  onPressed: _abort,
                  child: Text(tr('Stop', '叫停'))),
          ]),
          const SizedBox(height: 12),
          // 画面经站点来（W00c5b）：谁都能看（业主也是），站点那头有人看才让狗推。
          // **放在按钮下面**：叫停要一眼就按得到，不许被画面挤出屏幕。
          SiteVideo(api: widget.api, robotId: widget.robotId),
          const Divider(),
          for (final e in events.take(30))
            ListTile(dense: true, title: Text('${e['kind']}'), subtitle: Text('${e['data']}')),
        ]),
          ),
        ),
      ]),
    );
  }
}
