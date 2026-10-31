/// 派巡逻（W53）和「今天的巡逻」。
///
/// **派巡逻**是底部弹框：每条路线写预计多久（站点估，含走完回待命点那一段）；跟日程撞了先说清楚「日程优先，
/// 你这趟排到几点」。派出去会通知经理和管理员；这是临时巡逻，不进日程。被站点拒了弹「为什么 + 去处理」。
///
/// **今天的巡逻**：日程的每一轮和临时巡逻按时间排，一行一趟；没走完的点开看原因；排队中的临时巡逻派的人、
/// 经理、管理员能取消。值守页、日程页共用。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p4_admin.dart' show nwGap8;
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'refusal.dart';
import 'site_routes.dart' show SiteRouteEditorPage;

/// 时刻 `14:05`。给了 [offsetMin]（站点时区离 UTC 几分钟，站点回的 `utc_offset_min`）就按站点的钟，
/// 不按手机的（外审 W53-1 #12：人在异地、手机时区设错，巡逻时间就对不上）。
String hhmm(int ms, [int? offsetMin]) {
  final t = offsetMin == null
      ? DateTime.fromMillisecondsSinceEpoch(ms)
      : DateTime.fromMillisecondsSinceEpoch(ms, isUtc: true).add(Duration(minutes: offsetMin));
  return '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
}

String minutesText(num seconds) {
  final m = (seconds / 60).round().clamp(1, 24 * 60);
  return tr('about $m min', '约 $m 分钟');
}

/// 派出去以后（或排上了）给人看的一句。
String dispatchResultText(Map<String, dynamic> r, String label) {
  final off = (r['utc_offset_min'] as num?)?.toInt();
  if (r['queued'] == true) {
    final at = r['start_after_ms'];
    final why = '${r['because'] ?? ''}';
    return tr(
        'Queued: "$label" starts after ${at is num ? hhmm(at.toInt(), off) : '?'} (schedule first${why.isEmpty ? '' : ': $why'}). You\'ll be notified when it starts.',
        '已排队：「$label」将在 ${at is num ? hhmm(at.toInt(), off) : '?'} 之后开始（日程优先${why.isEmpty ? '' : '：$why'}），开始时通知你。');
  }
  final est = r['estimate_s'];
  return tr('Dispatched: "$label"${est is num ? ', ${minutesText(est)}' : ''}. The manager and administrator have been notified.',
      '已派出：「$label」${est is num ? '，${minutesText(est)}' : ''}，已通知经理和管理员。');
}

/// 弹派巡逻框。派出去（或排上了）回给人看的那一句；没派回 null。
Future<String?> showPatrolSheet(BuildContext context, SiteApi api, String robotId) =>
    showModalBottomSheet<String>(
        context: context,
        isScrollControlled: true,
        showDragHandle: true,
        builder: (_) => _PatrolSheet(api: api, robotId: robotId));

class _PatrolSheet extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  const _PatrolSheet({required this.api, required this.robotId});

  static Key routeKey(String id) => Key('patrol-route-$id');
  static const Key sendKey = Key('patrol-send');

  @override
  State<_PatrolSheet> createState() => _PatrolSheetState();
}

/// 测试找派巡逻框里的东西用。
Key patrolRouteKey(String id) => _PatrolSheet.routeKey(id);
const Key patrolSendKey = _PatrolSheet.sendKey;
const Key patrolQueuedKey = Key('patrol-queued');
const Key patrolCustomKey = Key('patrol-custom');

class _PatrolSheetState extends State<_PatrolSheet> {
  List<String>? _routes;
  final Map<String, Map<String, dynamic>> _plans = <String, Map<String, dynamic>>{};
  String? _pick;
  String? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final s = await widget.api.schedule();
      final rs = (s['missions'] as List? ?? const []).map((e) => '$e').toList();
      if (!mounted) return;
      setState(() {
        _routes = rs;
        _pick = rs.length == 1 ? rs.first : null;
      });
      for (final r in rs) {
        unawaited(widget.api.patrolPlan(widget.robotId, r).then((p) {
          if (mounted) setState(() => _plans[r] = p);
        }, onError: (Object _) {}));
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _error = tr("Can't load routes: ${refusalText(e)}", '无法加载路线：${refusalText(e)}'));
    }
  }

  Future<void> _send() async {
    final r = _pick;
    if (r == null) return;
    setState(() => _busy = true);
    try {
      final got = await widget.api.patrol(widget.robotId, r);
      final ack = got['ack'];
      if (got['queued'] != true && ack is Map && ack['result'] != 'accepted' && ack['result'] != 'duplicate') {
        // 机器人明确没收（忙、电量低……）：不说「派出去了」（外审 W53-1 #4）
        if (mounted) {
          setState(() {
            _busy = false;
            _error = tr('The robot did not accept: ${ack['result']} ${ack['reason'] ?? ''}',
                '机器人未接受：${ack['result']} ${ack['reason'] ?? ''}');
          });
        }
        return;
      }
      if (mounted) Navigator.pop(context, dispatchResultText(got, r));
    } on SiteError catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      final nav = Navigator.of(context);
      final host = nav.context;
      nav.pop();
      if (host.mounted) await showRefusal(host, widget.api, e, robotId: widget.robotId);
    }
  }

  /// 在图上点几个点（W53，用户 10-31）：路线编辑页的临时模式，派出去不存。
  Future<void> _custom() async {
    Map<String, dynamic> v;
    try {
      v = await widget.api.robot(widget.robotId);
    } on SiteError catch (e) {
      if (mounted) setState(() => _error = refusalText(e));
      return;
    }
    final caps = v['capabilities'];
    final tasks = caps is Map ? caps['tasks'] : null;
    final p = tasks is Map ? tasks['patrol'] : null;
    if (p is! Map || p['map_id'] is! String || p['map_version'] is! String) {
      if (mounted) {
        setState(() => _error = tr("${widget.robotId} hasn't reported its loaded map",
            '${widget.robotId} 未上报已加载的地图'));
      }
      return;
    }
    if (!mounted) return;
    final msg = await Navigator.push<String>(
        context,
        MaterialPageRoute<String>(
            builder: (_) => SiteRouteEditorPage(
                api: widget.api,
                mapId: '${p['map_id']}',
                version: '${p['map_version']}',
                newName: '',
                tempFor: widget.robotId)));
    if (msg != null && mounted) Navigator.pop(context, msg);
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final rs = _routes;
    final plan = _pick == null ? null : _plans[_pick];
    final queued = plan != null && plan['queued'] == true;
    final startAt = plan != null && plan['start_ms'] is num
        ? hhmm((plan['start_ms'] as num).toInt(), (plan['utc_offset_min'] as num?)?.toInt())
        : '?';
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(Nw.s16, 0, Nw.s16, Nw.s16 + MediaQuery.viewInsetsOf(context).bottom),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Text(tr('Dispatch ${widget.robotId} on patrol', '派 ${widget.robotId} 巡逻'),
              style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700)),
          nwGap8,
          if (_error != null)
            NwNotice(text: _error!, tone: NwTone.error)
          else if (rs == null)
            const Padding(padding: EdgeInsets.all(Nw.s16), child: Center(child: CircularProgressIndicator()))
          else if (rs.isEmpty)
            NwNotice(
                text: tr('No patrol routes yet. An administrator adds them under Admin → Patrol routes.',
                    '暂无巡逻路线，请管理员在「管理 → 巡逻路线」中添加。'))
          else
            ConstrainedBox(
              constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * 0.45),
              child: ListView(shrinkWrap: true, children: [
                for (final r in rs)
                  Padding(
                    padding: const EdgeInsets.only(bottom: Nw.s8),
                    child: NwCard(
                      key: _PatrolSheet.routeKey(r),
                      tone: _pick == r ? NwTone.info : NwTone.neutral,
                      onTap: () => setState(() => _pick = r),
                      child: Row(children: [
                        Icon(_pick == r ? Icons.radio_button_checked_rounded : Icons.radio_button_off_rounded,
                            color: _pick == r ? c.primary : c.textSecondary),
                        const SizedBox(width: Nw.s8),
                        Expanded(child: Text(r, style: NwText.bodyLarge.copyWith(color: c.text))),
                        Text(_plans[r]?['estimate_s'] is num ? minutesText(_plans[r]!['estimate_s'] as num) : '…',
                            style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                      ]),
                    ),
                  ),
              ]),
            ),
          nwGap8,
          NwButton(
              buttonKey: patrolCustomKey,
              variant: NwButtonVariant.outline,
              icon: Icons.touch_app_rounded,
              label: tr('Pick points on the map (one-off route)', '在地图上选点（临时路线）'),
              onPressed: _busy ? null : _custom),
          if (queued) ...[
            nwGap8,
            NwNotice(
                key: patrolQueuedKey,
                tone: NwTone.warning,
                text: tr(
                    'Clashes with the schedule (${plan['because']}). The schedule takes priority: this patrol starts after $startAt.',
                    '与日程冲突（${plan['because']}），日程优先：本次巡逻将在 $startAt 之后开始。')),
          ],
          nwGap8,
          Text(
              tr("One-off patrols are not saved to the schedule. The manager and administrator are notified; the robot returns to standby afterwards.",
                  '临时巡逻不保存到日程，派出后通知经理和管理员，结束后机器人返回待命点。'),
              style: NwText.bodySmall.copyWith(color: c.textSecondary)),
          nwGap8,
          Row(children: [
            Expanded(
                child: NwButton(
                    variant: NwButtonVariant.outline,
                    label: tr('Cancel', '取消'),
                    onPressed: _busy ? null : () => Navigator.pop(context))),
            const SizedBox(width: Nw.s8),
            Expanded(
                child: NwButton(
                    buttonKey: _PatrolSheet.sendKey,
                    label: queued ? tr('Queue it', '排队派出') : tr('Send', '派出'),
                    onPressed: _pick == null || _busy ? null : _send)),
          ]),
        ]),
      ),
    );
  }
}

// ------------------------------------------------------------ 今天的巡逻

/// 一行的状态说人话 + 颜色。
(String, NwTone) patrolStateText(String state) => switch (state) {
      'done' => (tr('Done', '完成'), NwTone.success),
      'failed' => (tr('Not finished', '未完成'), NwTone.error),
      'aborted' => (tr('Stopped', '已叫停'), NwTone.neutral),
      'running' => (tr('In progress', '进行中'), NwTone.info),
      'waiting' => (tr('Waiting for a robot', '等待机器人'), NwTone.warning),
      'missed' => (tr('Missed', '未执行'), NwTone.error),
      'next' => (tr('Next', '下一次'), NwTone.neutral),
      'later' => (tr('Later', '稍后'), NwTone.neutral),
      'queued' => (tr('Queued', '排队中'), NwTone.warning),
      'cancelled' => (tr('Cancelled', '已取消'), NwTone.neutral),
      'expired' => (tr('Expired', '已作废'), NwTone.neutral),
      _ => (state, NwTone.neutral),
    };

/// 这台机器人正在走的那一趟（今天的巡逻里 `running`、`task_id` 对得上的）。
Map<String, dynamic>? runningItem(List<Map<String, dynamic>> items, String robotId, String? taskId) {
  for (final it in items) {
    if (it['state'] == 'running' && it['robot'] == robotId && (taskId == null || it['task_id'] == taskId)) {
      return it;
    }
  }
  return null;
}

Key todayRowKey(int i) => Key('today-row-$i');

class TodayPatrolList extends StatelessWidget {
  final SiteApi api;
  final List<Map<String, dynamic>> items;
  /// 取消了一条以后调（页面重读）。
  final VoidCallback? onChanged;

  /// 站点时区离 UTC 几分钟（今天的巡逻回的 `utc_offset_min`）；老站点没有就按手机的钟。
  final int? offsetMin;
  const TodayPatrolList({super.key, required this.api, required this.items, this.onChanged, this.offsetMin});

  static const Key emptyKey = Key('today-empty');

  Future<void> _open(BuildContext context, Map<String, dynamic> it) async {
    final s = api.session;
    final reason = '${it['reason'] ?? ''}';
    final adhoc = it['kind'] == 'adhoc';
    final canCancel = adhoc && it['state'] == 'queued' && (s?.canPatrol ?? false) &&
        (it['by'] == s?.name || (s?.canSchedule ?? false));
    final (st, _) = patrolStateText('${it['state']}');
    final lines = <String>[
      '${hhmm((it['at_ms'] as num).toInt(), offsetMin)} · ${it['label']}${'${it['robot'] ?? ''}'.isEmpty ? '' : ' · ${it['robot']}'}',
      st,
      if (adhoc) tr('One-off patrol sent by ${it['by']}', '${it['by']} 派的临时巡逻'),
      if (it['end_ms'] is num) tr('Ends around ${hhmm((it['end_ms'] as num).toInt(), offsetMin)}', '预计 ${hhmm((it['end_ms'] as num).toInt(), offsetMin)} 结束'),
      if (reason.isNotEmpty) reason,
    ];
    final cancel = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              content: Text(lines.join('\n')),
              actions: [
                if (canCancel)
                  TextButton(
                      key: const Key('today-cancel'),
                      onPressed: () => Navigator.pop(c, true),
                      child: Text(tr('Cancel this patrol', '取消本次巡逻'))),
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Close', '关闭'))),
              ],
            ));
    if (cancel != true) return;
    try {
      await api.cancelAdhoc('${it['adhoc_id']}');
    } on SiteError catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(refusalText(e))));
      }
    }
    onChanged?.call();
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    if (items.isEmpty) {
      return Padding(
          key: emptyKey,
          padding: const EdgeInsets.symmetric(vertical: Nw.s8),
          child: Text(tr('No patrols today.', '今天没有巡逻。'),
              style: NwText.bodyMedium.copyWith(color: c.textSecondary)));
    }
    return NwCard(
      padding: EdgeInsets.zero,
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        for (var i = 0; i < items.length; i++)
          InkWell(
            key: todayRowKey(i),
            onTap: () => _open(context, items[i]),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: Nw.s8),
              child: Row(children: [
                SizedBox(
                    width: 48,
                    child: Text(hhmm((items[i]['at_ms'] as num).toInt(), offsetMin),
                        style: NwText.labelLarge.copyWith(color: c.text, fontFeatures: const [FontFeature.tabularFigures()]))),
                Expanded(
                    child: Text(
                        [
                          if (items[i]['kind'] == 'adhoc') tr('One-off', '临时'),
                          '${items[i]['label']}',
                          if ('${items[i]['robot'] ?? ''}'.isNotEmpty) '${items[i]['robot']}',
                          if (items[i]['kind'] == 'adhoc' && '${items[i]['by'] ?? ''}'.isNotEmpty) '${items[i]['by']}',
                        ].join(' · '),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: NwText.bodyMedium.copyWith(color: c.text))),
                const SizedBox(width: Nw.s8),
                Builder(builder: (_) {
                  final (label, tone) = patrolStateText('${items[i]['state']}');
                  return NwStatusPill(label: label, tone: tone);
                }),
              ]),
            ),
          ),
      ]),
    );
  }
}
