/// 单狗页（W00c4）：状态、派巡检、叫停、回待命点、遥控、画面，管理员的录包、标原点。
///
/// **按钮按角色显示**：业主（owner）只有「叫停」；保安（guard）与管理员（admin）能派单。真正的
/// 权限在站点上（站点回 403），这里只是不给人一个注定被拒的按钮。
library;

import 'dart:math';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p3_robots.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
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

  /// 诊断卡的各行：只放狗自己报的（代理、适配器、当前版本、兼容级、传感器、上装）和站点估的钟差。
  /// 没报的那一行不出；一行都没有就不出这张卡。
  List<(IconData, String, String)> _diagnostics(Map<String, dynamic>? v) {
    final caps = v?['capabilities'];
    if (caps is! Map) return const [];
    final tasks = caps['tasks'];
    final rel = tasks is Map ? tasks['release_install'] : null;
    final compat = tasks is Map ? tasks['compat'] : null;
    String on(Object? m) =>
        m is Map ? [for (final e in m.entries) if (e.value == true) '${e.key}'].join(', ') : '';
    final skew = v?['clock_skew_s'];
    return [
      if (caps['agent'] is String) (Icons.memory_rounded, tr('Agent version', '代理版本'), '${caps['agent']}'),
      if (caps['adapter'] is String) (Icons.settings_input_component_rounded, tr('Adapter', '适配器'), '${caps['adapter']}'),
      if (rel is Map && rel['current'] is String)
        (Icons.system_update_rounded, tr('Installed release', '当前版本'), '${rel['current']}'),
      if (compat is Map && compat['level'] != null)
        (Icons.handshake_rounded, tr('Compatibility level', '兼容级'), '${compat['level']}'),
      if (on(caps['sensing']).isNotEmpty) (Icons.sensors_rounded, tr('Sensors', '传感器'), on(caps['sensing'])),
      if (on(caps['actuators']).isNotEmpty) (Icons.campaign_rounded, tr('Payload', '上装'), on(caps['actuators'])),
      if (skew is num)
        (Icons.schedule_rounded, tr('Clock offset', '钟差'), tr('${skew.toStringAsFixed(1)} s', '${skew.toStringAsFixed(1)} 秒')),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = widget.api.session;
    final v = _view;
    final st = v?['status'];
    final task = st is Map ? st['task'] : null;
    final taskId = task is Map && task['task_id'] is String ? task['task_id'] as String : null;
    final events = (v?['events'] as List? ?? const []).whereType<Map>().toList();
    final phase = v == null ? null : robotPhase(v);
    final dispatch = s?.canDispatch ?? false;
    final maps = s?.canManageMaps ?? false;
    void go(Widget Function() page) =>
        Navigator.push(context, MaterialPageRoute<void>(builder: (_) => page()));
    // 宽的（横屏手机、平板、桌面）：画面在左、操作在右，不用翻就按得到；竖屏手机一栏往下排。
    final wide = MediaQuery.sizeOf(context).width >= 700;

    final ops = <Widget>[
      if (dispatch)
        NwOpTile(
            tileKey: const Key('btn-patrol'),
            icon: Icons.explore_rounded,
            label: tr('Dispatch patrol', '派巡检'),
            onTap: _patrol),
      if (dispatch)
        NwOpTile(
            tileKey: const Key('btn-standby'),
            icon: Icons.home_rounded,
            label: tr('Return to standby', '回待命点'),
            onTap: () =>
                _do(() => widget.api.returnToStandby(widget.robotId), tr('Return to standby', '回待命点'))),
      // 连续录像（W18）：谁都能看
      NwOpTile(
          tileKey: const Key('btn-recordings'),
          icon: Icons.videocam_rounded,
          label: tr('Recordings', '录像'),
          onTap: () => go(() => SiteRecordingsPage(api: widget.api, robotId: widget.robotId))),
      if (s?.canTeleop ?? false)
        NwOpTile(
            tileKey: const Key('btn-teleop'),
            icon: Icons.sports_esports_rounded,
            label: tr('Remote control', '遥控'),
            onTap: () => go(() => SiteTeleopPage(api: widget.api, robotId: widget.robotId))),
      // 上装（W21）：警灯、警笛、聚光灯、喇叭。保安、管理员；狗报了 deter 才有
      if (dispatch && robotCan(v, 'deter'))
        NwOpTile(
            tileKey: const Key('btn-deter'),
            icon: Icons.volume_up_rounded,
            label: tr('Payload…', '上装…'),
            onTap: () => go(() => SiteDeterPage(
                api: widget.api,
                robotId: widget.robotId,
                caps: Map<String, dynamic>.from(
                    ((v?['capabilities'] as Map)['tasks'] as Map)['deter'] as Map)))),
      if (dispatch && robotCan(v, 'relocalize'))
        NwOpTile(
            tileKey: const Key('btn-relocalize'),
            icon: Icons.my_location_rounded,
            label: tr('Set position', '设位置'),
            onTap: _relocalize),
      // 待命点与原点（W13a）：列表、回指定的一个；管理员在这儿设、设默认、删
      if (dispatch || maps)
        NwOpTile(
            tileKey: const Key('btn-standby-list'),
            icon: Icons.flag_rounded,
            label: tr('Standby points…', '待命点…'),
            onTap: () => go(() => SiteStandbyPage(
                api: widget.api, robotId: widget.robotId, canMarkHere: robotCanStandbyHere(v)))),
    ];
    final admin = <Widget>[
      if (maps && robotCan(v, 'mapping')) ...[
        NwOpTile(
            tileKey: const Key('btn-record-start'),
            icon: Icons.fiber_manual_record_rounded,
            label: tr('Start bag recording', '开始录包'),
            onTap: _startRecord),
        NwOpTile(
            tileKey: const Key('btn-record-stop'),
            icon: Icons.stop_rounded,
            label: tr('Stop bag recording', '停止录包'),
            onTap: () => _do(
                () => widget.api.mapping(widget.robotId, 'stop'), tr('Stop bag recording', '停止录包'))),
      ],
      if (maps && robotCan(v, 'mark_home'))
        NwOpTile(
            tileKey: const Key('btn-mark-home'),
            icon: Icons.add_location_alt_rounded,
            label: tr('Mark origin here', '在这儿标原点'),
            onTap: _markHome),
      if (maps && robotCan(v, 'mapping_trail'))
        NwOpTile(
            tileKey: const Key('btn-mapping-trail'),
            icon: Icons.timeline_rounded,
            label: tr('Bag recording trail', '录包轨迹'),
            onTap: _openTrail),
      // 建图进程日志（W00c6g）：管理员看录包、重建子进程的日志，不用再 SSH 上狗。
      if (maps && robotCan(v, 'proc_log'))
        NwOpTile(
            tileKey: const Key('btn-proc-logs'),
            icon: Icons.article_rounded,
            label: tr('Mapping logs', '建图日志'),
            onTap: () => go(() => SiteProcLogsPage(api: widget.api, robotId: widget.robotId))),
    ];
    final diag = _diagnostics(v);
    final battery = robotBattery(v);
    final caps = v?['capabilities'] is Map ? (v!['capabilities'] as Map).cast<String, dynamic>() : null;

    // 最上面：一句话的状态、被叫停的说明、刚才那一下的结果 —— 两种排法都在最上面。
    final top = <Widget>[
      Text(v == null ? tr('Loading…', '加载中…') : robotStatusLine(v),
          key: const Key('robot-status'), style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
      // 叫停之后站点不再派它（W00c5e）：说清楚，给能派单的人一个「恢复」。
      if (v?['held'] is Map)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: NwCard(
              key: const Key('robot-held'),
              tone: NwTone.info,
              child: Row(children: [
                Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(tr('Stopped by ${(v!['held'] as Map)['by']}', '被 ${(v['held'] as Map)['by']} 叫停了'),
                      style: NwText.titleSmall.copyWith(color: c.text)),
                  const SizedBox(height: Nw.s4),
                  Text(
                      tr(
                          'The site will not dispatch it (schedule, incidents, return to standby, remote control) '
                              'until you tap Resume',
                          '站点不会再派它（排程、事件、回待命点、遥控都不派），点「恢复」之后才派'),
                      style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                ])),
                if (dispatch) ...[
                  const SizedBox(width: Nw.s8),
                  NwButton(
                      buttonKey: const Key('btn-resume'),
                      label: tr('Resume', '恢复'),
                      onPressed: () => _do(() => widget.api.resume(widget.robotId), tr('Resume', '恢复'))),
                ],
              ]),
            )),
      if (_msg != null)
        Padding(padding: const EdgeInsets.only(top: Nw.s8), child: NwNotice(text: _msg!, tone: NwTone.neutral)),
      const SizedBox(height: Nw.s16),
    ];
    // 画面经站点来（W00c5b）：谁都能看（业主也是），站点那头有人看才让狗推。
    final facts = <Widget>[
      SiteVideo(api: widget.api, robotId: widget.robotId),
      const SizedBox(height: Nw.s16),
      IntrinsicHeight(
          child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Expanded(
            flex: 2,
            child: NwInfoTile(
                icon: battery == null
                    ? Icons.battery_unknown_rounded
                    : (battery < NwBattery.lowBelow ? Icons.battery_alert_rounded : Icons.battery_full_rounded),
                label: tr('Battery', '电量'),
                value: battery == null ? '--' : '${battery.round()}%',
                tone: battery == null
                    ? NwTone.neutral
                    : nwToneOf(battery < NwBattery.lowBelow ? 'lowBattery' : 'battery'))),
        const SizedBox(width: Nw.s8),
        Expanded(
            flex: 3,
            child: v?['loc'] is Map
                ? NwInfoTile(
                    key: const Key('robot-loc'),
                    icon: Icons.location_on_rounded,
                    label: tr('Position', '定位'),
                    value: locText((v!['loc'] as Map).cast<String, dynamic>()))
                : NwInfoTile(
                    icon: Icons.location_on_rounded, label: tr('Position', '定位'), value: tr('Not reported', '没上报'))),
      ])),
      if (v?['rtk'] is Map)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: Text(rtkText((v!['rtk'] as Map).cast<String, dynamic>()),
                key: const Key('robot-rtk'), style: NwText.bodyMedium.copyWith(color: c.text))),
      if (caps != null && headText(caps).isNotEmpty)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: NwNotice(key: const Key('robot-head'), text: headText(caps), tone: NwTone.info)),
      if (caps != null && obstaclesText(caps).isNotEmpty)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: Text(obstaclesText(caps),
                key: const Key('robot-obstacles'), style: NwText.bodyMedium.copyWith(color: c.text))),
      const SizedBox(height: Nw.s16),
    ];
    final actions = <Widget>[
      if (ops.isNotEmpty) ...[
        NwSectionLabel(tr('Operations', '操作')),
        const SizedBox(height: Nw.s8),
        NwTileGrid(columns: wide ? 4 : 3, children: ops),
        const SizedBox(height: Nw.s16),
      ],
      if (admin.isNotEmpty) ...[
        NwSectionLabel(tr('Admin', '管理')),
        const SizedBox(height: Nw.s8),
        NwTileGrid(columns: wide ? 4 : 3, children: admin),
        const SizedBox(height: Nw.s16),
      ],
    ];
    final diagnostics = <Widget>[
      if (diag.isNotEmpty) ...[
        NwSectionLabel(tr('Diagnostics', '诊断')),
        const SizedBox(height: Nw.s8),
        NwCard(
            key: const Key('robot-diagnostics'),
            padding: EdgeInsets.zero,
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              for (final d in diag) NwDiagnosticRow(icon: d.$1, label: d.$2, value: d.$3),
            ])),
        const SizedBox(height: Nw.s16),
      ],
    ];
    return Scaffold(
      // 页头：返回、编号、状态，右上是红色的「叫停」—— 任何时候都在屏幕里，不跟着滚。没任务时置灰。
      body: Column(children: [
        NwHeader(
            title: widget.robotId,
            leading: Navigator.canPop(context) ? const BackButton() : null,
            trailing: [
              if (phase != null)
                ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 120),
                    child: NwStatusPill(
                        key: const Key('robot-phase'), label: robotPhaseLabel(phase), tone: robotPhaseTone(phase))),
              if (s?.canAbort ?? false) ...[
                const SizedBox(width: Nw.s8),
                // 没在跑任务：灰的、按不动，也不带 btn-abort 这个 Key（有 Key 的那个一定按得动）
                NwButton(
                    buttonKey: taskId != null ? const Key('btn-abort') : const Key('btn-abort-off'),
                    variant: NwButtonVariant.destructive,
                    size: NwButtonSize.large,
                    icon: Icons.stop_circle_rounded,
                    label: tr('Stop', '叫停'),
                    onPressed: taskId != null ? _abort : null),
                const SizedBox(width: Nw.s8),
              ],
            ]),
        // 「我在现场监护」钉在列表外面（W00c6i 内审）：放在按需构建的列表里，上面插一行提示或者往下一翻，
        // 它的状态就被换掉、发出放租 —— 刚派出去的任务随即被中止。
        if ((s?.canDispatch ?? false) && robotNeedsSupervision(v))
          SupervisionSwitch(
              key: ValueKey('supervise-${widget.robotId}'), api: widget.api, robotId: widget.robotId),
        Expanded(
          child: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(padding: const EdgeInsets.all(Nw.s16), children: [
          // 最上面那几行包成一个：多一行少一行，下面的画面在列表里的位置不变（位置一变它就得重连）
          Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: top),
          if (wide)
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(
                  flex: 5,
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch, children: [...facts, ...diagnostics])),
              const SizedBox(width: Nw.s16),
              Expanded(
                  flex: 6, child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: actions)),
            ])
          else ...[
            ...facts,
            ...actions,
            ...diagnostics,
          ],
          if (events.isNotEmpty) NwSectionLabel(tr('Recent events', '最近的事件')),
          for (final e in events.take(30))
            ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text('${e['kind']}'),
                subtitle: Text('${e['data']}')),
        ]),
          ),
        ),
      ]),
    );
  }
}
