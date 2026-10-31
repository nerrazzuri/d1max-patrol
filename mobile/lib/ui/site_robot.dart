/// 单狗页（W00c4）：状态、派巡检、叫停、回待命点、遥控、画面，管理员的录包、标原点。
///
/// **按钮按角色显示**：经理（owner）只有「叫停」；保安（guard）与管理员（admin）能派单。真正的
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
import 'robot_events.dart';
import 'robot_bind_page.dart';
import 'patrol_dispatch.dart';
import 'refusal.dart';
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
  if (st is! Map) return tr('No status', '无状态');
  final online = st['online'] == true && r['fresh'] == true;
  final task = st['task'];
  final taskText = task is Map ? taskLabel(task) : tr('Idle', '空闲');
  final held = r['held'] is Map ? tr('Stopped · ', '已叫停 · ') : '';
  // W33：只许手动派的狗（站点自己不让它动）；狗确认看见人（决策 48）
  final manual = r['manual_only'] is Map ? tr('Manual only · ', '仅限手动 · ') : '';
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
  if (task['kind'] == 'dock') return tr('Charging (docking, charging, undocking)', '充电中（对桩、充电、离桩）');
  if (task['kind'] == 'goto' && id.startsWith('charge-')) return tr('Going to charge', '去充电');
  return '${task['kind']} ${task['state']}';
}

/// 机器人连的 WiFi 一行：`Estate · 走 4G`（W42）。
String robotWifiLine(Map w) {
  final link = switch (w['link']) {
    'wifi' => tr('on WiFi', 'WiFi 连接'),
    '4g' => tr('on 4G', '4G 连接'),
    _ => tr('no network', '无网络'),
  };
  final ssid = '${w['ssid'] ?? ''}';
  return ssid.isEmpty ? link : '$ssid · $link';
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
      msg = tr('$what failed: ${refusalText(e)}', '$what 失败：${refusalText(e)}');
      if (e.status == 409 && e.body['reason_code'] is String && mounted) {
        // W53：常见的拒绝说清楚、给去处理的按钮
        await showRefusal(context, widget.api, e, robotId: widget.robotId, title: what, onChanged: _reload);
      }
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
          '边走边建：地图号和版本需同时填写（只录包则都留空）'));
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
      builder: (c) => SimpleDialog(title: Text(tr('Where is the robot now', '机器人当前位置')), children: [
        SimpleDialogOption(
            key: const Key('reloc-home'),
            onPressed: () => Navigator.pop(c, 'home'),
            child: Text(tr('At the origin (dock)', '在原点（充电桩）'))),
        SimpleDialogOption(
            key: const Key('reloc-xy'),
            onPressed: () => Navigator.pop(c, 'xy'),
            child: Text(tr('Enter map coordinates', '输地图坐标'))),
        if (robotCanFindPose(_view))
          SimpleDialogOption(
              key: const Key('reloc-find'),
              onPressed: () => Navigator.pop(c, 'find'),
              child: Text(tr('Auto-localize (match the scanned walls to the map)', '自动定位（用扫描到的墙体匹配地图）'))),
      ]),
    );
    if (how == null || !mounted) return;
    if (how == 'find') {
      // W41：狗拿看到的墙在整张图上套；认不准它会拒、说原因（有两处差不多像……），那时再手动设。
      await _do(() => widget.api.relocalize(widget.robotId, global: true), tr('Auto-localize', '自动定位'));
      return;
    }
    if (how == 'home') {
      // 点错了狗就按错的位置走（W00c6e 内审）：再确认一次。
      final sure = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
          title: Text(tr('Is the robot at the origin now?', '机器人当前在原点？')),
          content: Text(tr(
              'The robot must be on the origin (dock), facing as when the origin was marked; otherwise navigation will be wrong.',
              '机器人须站在原点（充电桩）上，朝向与标原点时一致，否则导航会出错。')),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
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
        title: tr('Where is the robot on the map', '机器人在地图上的当前位置'),
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
        confirm: tr('Set', '设置'),
        confirmKey: const Key('reloc-go'));
    if (got == null) return;
    final x = double.tryParse(got[0]), y = double.tryParse(got[1]), deg = double.tryParse(got[2]);
    if (x == null || y == null || deg == null || !x.isFinite || !y.isFinite || !deg.isFinite) {
      if (mounted) setState(() => _msg = tr('Coordinates and heading must be numbers', '坐标和朝向须为数字'));
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
            'Set the origin of ${widget.robotId} on this map to its current position? Standby points are unchanged.\nThe robot verifies its localization first and won\'t mark it if the position isn\'t set, the error is large, or it is busy or switching maps.',
            '将 ${widget.robotId} 在此地图上的原点改为当前位置？待命点不变。\n机器人会先核验定位：未设置位置、偏差过大、正在执行任务或换图时不会标记。')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
          FilledButton(
              key: const Key('mark-home-go'),
              onPressed: () => Navigator.pop(c, true),
              child: Text(tr('Mark', '标记'))),
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
                '待命点 home 目前登记在 $where 上。是否移到机器人当前使用的地图？（$where 上将删除）')),
            actions: [
              TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
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
                '未标记：待命点 home 仍在 $where 上'));
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
            '原点已标记：($x, $y)，偏差约 $sigma m');
      } else {
        msg = tr('Origin marked', '原点已标记');
      }
    } on SiteError catch (e) {
      final d = e.body['data'];
      if (e.status == 500 && d is Map && d['x'] is num && d['y'] is num) {
        // 狗上已经换了、站点没登记上：给坐标，好手工登记待命点。
        final at = '${d['map_id']}:${d['map_version']}';
        final x = (d['x'] as num).toStringAsFixed(1), y = (d['y'] as num).toStringAsFixed(1);
        msg = tr("${e.message}: the robot's origin is at ($x, $y) on $at",
            '${e.message}：机器人上的原点位于 $at 的 ($x, $y)');
      } else if (e.status == 409) {
        // 站点回的是「狗没标:<狗的原因>」：原因照拒收那一套说人话。
        final why = ackReasonText(e.message.replaceFirst(RegExp(r'^机器人没标[:：]'), ''));
        msg = tr('Not marked: $why', '未标记：$why');
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
      if (mounted) setState(() => _msg = tr('Stop failed: $e', '叫停失败：$e'));
      return;
    }
    final st = v['status'];
    final task = st is Map ? st['task'] : null;
    final tid = task is Map ? task['task_id'] : null;
    if (tid is! String || tid.isEmpty) {
      if (mounted) {
        setState(() {
          _view = v;
          _msg = tr('The robot isn\'t running a task', '机器人当前无任务');
        });
      }
      return;
    }
    await _do(() => widget.api.abort(widget.robotId, tid), tr('Stop', '叫停'));
  }

  /// 派巡逻（W53）：底部弹框，带估时、撞日程排队、被拒说原因。
  Future<void> _patrol() async {
    final msg = await showPatrolSheet(context, widget.api, widget.robotId);
    if (msg != null && mounted) setState(() => _msg = msg);
    await _reload();
  }

  /// W42 §6：机器人上有人提了换 WiFi —— 说清楚，给有管理权限的人「确认 / 不换」。
  Widget? _wifiChange(NwColors c, Map<String, dynamic>? v, bool manage) {
    final w = v?['wifi'];
    final ch = w is Map ? w['change'] : null;
    if (ch is! Map) return null;
    final trial = ch['mode'] == 'trial';
    return Padding(
      padding: const EdgeInsets.only(top: Nw.s8),
      child: NwCard(
        key: const Key('robot-wifi-change'),
        tone: NwTone.warning,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(
              trial
                  ? tr("The robot lost the site and is trying WiFi “${ch['ssid']}”. Approve within 30 minutes to keep it, otherwise it reverts.",
                      '机器人连不上站点，正在尝试 WiFi「${ch['ssid']}」。30 分钟内确认则保留，否则恢复原网络。')
                  : tr("Someone at the robot asked to switch its WiFi to “${ch['ssid']}”. Nothing changes until it is approved.",
                      '请确认机器人端申请将 WiFi 切换为「${ch['ssid']}」'),
              style: NwText.bodyMedium.copyWith(color: c.text)),
          if (manage) ...[
            const SizedBox(height: Nw.s8),
            Wrap(spacing: Nw.s8, children: [
              NwButton(
                  buttonKey: const Key('btn-wifi-approve'),
                  size: NwButtonSize.small,
                  label: tr('Approve WiFi change', '确认换 WiFi'),
                  onPressed: () => _do(() => widget.api.confirmRobotWifi(widget.robotId, '${ch['id']}', true),
                      tr('Approve WiFi change', '确认换 WiFi'))),
              NwButton(
                  buttonKey: const Key('btn-wifi-reject'),
                  size: NwButtonSize.small,
                  variant: NwButtonVariant.ghost,
                  label: tr("Reject", '拒绝'),
                  onPressed: () => _do(() => widget.api.confirmRobotWifi(widget.robotId, '${ch['id']}', false),
                      tr("Reject WiFi change", '拒绝更换 WiFi'))),
            ]),
          ],
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = widget.api.session;
    final v = _view;
    final events = (v?['events'] as List? ?? const []).whereType<Map>().toList();
    final phase = v == null ? null : robotPhase(v);
    final dispatch = s?.canDispatch ?? false;
    final maps = s?.canManageMaps ?? false;
    void go(Widget Function() page) =>
        Navigator.push(context, MaterialPageRoute<void>(builder: (_) => page()));
    // 宽的（横屏手机、平板、桌面）：画面在左、操作在右，不用翻就按得到；竖屏手机一栏往下排。
    final wide = MediaQuery.sizeOf(context).width >= 700;

    final ops = <Widget>[
      if (s?.canPatrol ?? false)
        NwOpTile(
            tileKey: const Key('btn-patrol'),
            icon: Icons.explore_rounded,
            label: tr('Patrol', '派巡逻'),
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
            label: tr('Standby', '待命点'),
            onTap: () => go(() => SiteStandbyPage(
                api: widget.api, robotId: widget.robotId, canMarkHere: robotCanStandbyHere(v)))),
    ];
    final admin = <Widget>[
      // W42：换 WiFi（站在机器人旁边扫贴纸；站点上确认了才生效）。机器人报了 wifi_confirm 才有
      if ((s?.canManageRobots ?? false) && robotCan(v, 'wifi_confirm'))
        NwOpTile(
            tileKey: const Key('btn-change-wifi'),
            icon: Icons.wifi_rounded,
            label: tr('Change WiFi', '换 WiFi'),
            onTap: () => go(() => RobotWifiPage(api: widget.api, robotId: widget.robotId))),
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
    final battery = robotBattery(v);
    final caps = v?['capabilities'] is Map ? (v!['capabilities'] as Map).cast<String, dynamic>() : null;

    // 最上面：一句话的状态、被叫停的说明、刚才那一下的结果 —— 两种排法都在最上面。
    final top = <Widget>[
      Text(v == null ? tr('Loading…', '加载中…') : robotStatusLine(v),
          key: const Key('robot-status'), style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
      ?_wifiChange(c, v, s?.canManageRobots ?? false),
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
                  Text(tr('Stopped by ${(v!['held'] as Map)['by']}', '已被 ${(v['held'] as Map)['by']} 叫停'),
                      style: NwText.titleSmall.copyWith(color: c.text)),
                  const SizedBox(height: Nw.s4),
                  Text(
                      tr(
                          'Until you tap Resume, the site won\'t dispatch it (schedule, incidents, return to standby, remote control)',
                          '点「恢复」前，站点不会再派出此机器人（日程、事件、回待命点、遥控均不派）'),
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
    // 画面经站点来（W00c5b）：谁都能看（经理也是），站点那头有人看才让狗推。
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
                    icon: Icons.location_on_rounded, label: tr('Position', '定位'), value: tr('Not reported', '未上报'))),
      ])),
      if (v?['nav'] is Map && navText((v!['nav'] as Map).cast<String, dynamic>()).isNotEmpty)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: Text(navText((v['nav'] as Map).cast<String, dynamic>()),
                key: const Key('robot-nav'), style: NwText.bodyMedium.copyWith(color: c.warning))),
      if (v?['wifi'] is Map)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: Row(key: const Key('robot-wifi'), children: [
              Icon(Icons.wifi_rounded, size: 18, color: c.textSecondary),
              const SizedBox(width: Nw.s8),
              Expanded(child: Text(robotWifiLine(v!['wifi'] as Map), style: NwText.bodyMedium.copyWith(color: c.text))),
            ])),
      if (v?['rtk'] is Map)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: Text(rtkText((v!['rtk'] as Map).cast<String, dynamic>()),
                key: const Key('robot-rtk'), style: NwText.bodyMedium.copyWith(color: c.text))),
      // W40：设备健康（上装 V1.2 的继电器板、喇叭、RTK、相机、存储盘、路由器……每台在线 / 离线 / 降级 / 故障）
      if (v?['devices'] is Map &&
          (v!['devices'] as Map)['items'] is List &&
          ((v['devices'] as Map)['items'] as List).isNotEmpty)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s8),
            child: _Devices(view: (v['devices'] as Map).cast<String, dynamic>())),
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
                // **有叫停权限就一直按得动**，不看这一页手上那份状态里有没有任务（App 重做外审 I1）：那份
                // 状态是进来时、手动刷新时读的，之后狗被排程、入侵、另一台手机派走，这一页不知道 ——
                // 拿它把叫停置灰，人就停不了狗。按下去 `_abort` 自己现问站点现在跑的是哪一趟；真没在跑
                // 就说没在跑。
                NwButton(
                    buttonKey: const Key('btn-abort'),
                    variant: NwButtonVariant.destructive,
                    size: NwButtonSize.large,
                    icon: Icons.stop_circle_rounded,
                    label: tr('Stop', '叫停'),
                    onPressed: _abort),
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
                      crossAxisAlignment: CrossAxisAlignment.stretch, children: facts)),
              const SizedBox(width: Nw.s16),
              Expanded(
                  flex: 6, child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: actions)),
            ])
          else ...[
            ...facts,
            ...actions,
          ],
          if (events.isNotEmpty) NwSectionLabel(tr('Recent events', '最近的事件')),
          for (final e in events.take(30))
            ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text(robotEventTitle('${e['kind']}', e['data'] is Map ? (e['data'] as Map).cast<String, dynamic>() : null)),
                subtitle: _eventSub(e),
                trailing: Text(robotEventTime(e['stamp']), style: NwText.labelSmall)),
        ]),
          ),
        ),
      ]),
    );
  }
}


Widget? _eventSub(Map e) {
  final d = e['data'];
  final text = robotEventDetail('${e['kind']}', d is Map ? d.cast<String, dynamic>() : const <String, dynamic>{});
  return text.isEmpty ? null : Text(text, maxLines: 2, overflow: TextOverflow.ellipsis);
}

/// 设备健康（W40）：一台一行，圆点按状态上色；不在线的排前面；好一会儿没更新整块标出来。
class _Devices extends StatelessWidget {
  final Map<String, dynamic> view;
  const _Devices({required this.view});

  static const Key listKey = Key('robot-devices');

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    const order = <String, int>{'error': 0, 'offline': 1, 'degraded': 2, 'online': 3};
    final items = <Map<String, dynamic>>[
      for (final x in (view['items'] as List)) if (x is Map) x.cast<String, dynamic>(),
    ]..sort((a, b) => (order['${a['state']}'] ?? 9).compareTo(order['${b['state']}'] ?? 9));
    Color dot(String st) => switch (st) {
          'online' => c.success,
          'degraded' => c.warning,
          _ => c.error,
        };
    return Column(key: listKey, crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text(
          view['stale'] == true
              ? tr('Devices (stale)', '设备（长时间未更新）')
              : tr('Devices', '设备'),
          style: NwText.titleSmall.copyWith(color: view['stale'] == true ? c.warning : c.text)),
      for (final d in items)
        Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Row(key: Key('device-${d['id']}'), crossAxisAlignment: CrossAxisAlignment.start, children: [
              Padding(
                  padding: const EdgeInsets.only(top: 5, right: Nw.s8),
                  child: Icon(Icons.circle, size: 10, color: dot('${d['state']}'))),
              Expanded(
                  child: Text(
                      '${deviceName('${d['id']}')}${tr(': ', '：')}${deviceStateText('${d['state']}')}'
                      '${'${d['detail'] ?? ''}'.isEmpty ? '' : ' · ${d['detail']}'}',
                      style: NwText.bodyMedium.copyWith(color: c.text))),
            ])),
    ]);
  }
}
