/// 狗的那几页（列表、单狗页、遥控）自己用的小件：狗此刻算哪种状态（药丸的字和颜色、列表的筛选）、
/// 列表上的筛选加狗卡、单狗页的信息格和操作格。
///
/// **状态只按站点给的那份狗的视图算**（`robots()`、`robot()`）：吊销、有没有状态、在不在线、急停、
/// 被叫停、在跑什么任务。视图里没有的不编 —— P1、跌倒不在这份视图里（在值守栏和告警里），这里不显示；
/// 电量现在站点也没放进这份视图（在值守汇总里），读得到 `battery_pct` 才显示、才判低电，读不到写「--」。
library;

import 'package:flutter/material.dart';

import '../../l10n.dart';
import 'nw_cards.dart';
import 'nw_theme.dart';
import 'nw_widgets.dart';

/// 狗此刻算哪一种（从上往下，先中的算）。
enum RobotPhase {
  /// 证书吊销了。
  revoked,

  /// 站点还没收到过它的状态。
  unknown,
  offline,

  /// 正在驱离（站点的驱离会话，P1 那一类）。
  deterring,

  /// 急停按着。
  estop,

  /// 被人叫停了，站点不再派它。
  held,
  charging,
  lowBattery,

  /// 在跑任务。
  working,
  idle,
}

Map? _taskOf(Map<String, dynamic> r) {
  final st = r['status'];
  final task = st is Map ? st['task'] : null;
  return task is Map ? task : null;
}

/// 在线：狗说自己在线，并且站点这边的状态是新鲜的（跟 `robotStatusLine` 一个口径）。
bool robotOnline(Map<String, dynamic> r) {
  final st = r['status'];
  return r['revoked'] != true && st is Map && st['online'] == true && r['fresh'] == true;
}

/// 在回充的那几趟里（对桩 / 去充电，跟 `taskLabel` 一个口径）。
bool robotCharging(Map<String, dynamic> r) {
  final task = _taskOf(r);
  if (task == null || !robotOnline(r)) return false;
  return task['kind'] == 'dock' || (task['kind'] == 'goto' && '${task['task_id'] ?? ''}'.startsWith('charge-'));
}

/// 在线、在跑任务（回充的不算）。
bool robotActive(Map<String, dynamic> r) => robotOnline(r) && _taskOf(r) != null && !robotCharging(r);

/// 电量百分数：视图里有 `battery_pct` 才有，没有是 null。
num? robotBattery(Map<String, dynamic>? r) {
  final b = r?['battery_pct'];
  return b is num ? b : null;
}

RobotPhase robotPhase(Map<String, dynamic> r, {bool deterring = false}) {
  if (r['revoked'] == true) return RobotPhase.revoked;
  final st = r['status'];
  if (st is! Map) return RobotPhase.unknown;
  if (!robotOnline(r)) return RobotPhase.offline;
  if (deterring) return RobotPhase.deterring;
  final ready = st['ready'];
  if (ready is Map && ready['estop_clear'] == false) return RobotPhase.estop;
  if (r['held'] is Map) return RobotPhase.held;
  if (robotCharging(r)) return RobotPhase.charging;
  final b = robotBattery(r);
  if (b != null && b < NwBattery.lowBelow) return RobotPhase.lowBattery;
  return _taskOf(r) != null ? RobotPhase.working : RobotPhase.idle;
}

/// 药丸的颜色：红只给驱离、急停、离线；橙只给低电；绿给在干活、在充电；别的不上色。
NwTone robotPhaseTone(RobotPhase p) => switch (p) {
      RobotPhase.deterring => nwToneOf('p1'),
      RobotPhase.estop => nwToneOf('estop'),
      RobotPhase.offline => nwToneOf('offline'),
      RobotPhase.lowBattery => nwToneOf('lowBattery'),
      RobotPhase.charging => nwToneOf('battery'),
      RobotPhase.working => nwToneOf('online'),
      RobotPhase.idle => nwToneOf('idle'),
      RobotPhase.revoked || RobotPhase.unknown || RobotPhase.held => NwTone.neutral,
    };

/// 药丸的字。
String robotPhaseLabel(RobotPhase p) => switch (p) {
      RobotPhase.revoked => tr('REVOKED', '吊销'),
      RobotPhase.unknown => tr('NO DATA', '未上报'),
      RobotPhase.offline => tr('OFFLINE', '离线'),
      RobotPhase.deterring => tr('DETERRING', '驱离'),
      RobotPhase.estop => tr('E-STOP', '急停'),
      RobotPhase.held => tr('STOPPED', '叫停'),
      RobotPhase.charging => tr('CHARGING', '充电'),
      RobotPhase.lowBattery => tr('LOW BATTERY', '低电'),
      RobotPhase.working => tr('WORKING', '执行中'),
      RobotPhase.idle => tr('IDLE', '空闲'),
    };

/// 列表上的筛选（只筛已经读到的这一份，不另外问站点）。
enum FleetFilter { all, active, charging }

bool fleetFilterMatch(FleetFilter f, Map<String, dynamic> r) => switch (f) {
      FleetFilter.all => true,
      FleetFilter.active => robotActive(r),
      FleetFilter.charging => robotCharging(r),
    };

/// 狗列表的主体：上面一排筛选（全部 / 执行中 / 充电，带数目），下面一只狗一张卡。
/// 筛选选的哪一格是它自己的状态（不动页面的状态）。卡片的 Key 是 `robot-<编号>`。
class NwFleet extends StatefulWidget {
  final List<Map<String, dynamic>> robots;

  /// 正在驱离的狗的编号。
  final Set<String> deterring;

  /// 一只狗在做什么（页面给：`robotStatusLine`）。
  final String Function(Map<String, dynamic> robot) activityOf;
  final void Function(String robotId) onOpen;

  const NwFleet(
      {super.key,
      required this.robots,
      this.deterring = const <String>{},
      required this.activityOf,
      required this.onOpen});

  static Key filterKey(FleetFilter f) => Key('robots-filter-${f.name}');
  static const Key emptyKey = Key('robots-filter-empty');

  @override
  State<NwFleet> createState() => _NwFleetState();
}

class _NwFleetState extends State<NwFleet> {
  FleetFilter _filter = FleetFilter.all;

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    int count(FleetFilter f) => widget.robots.where((r) => fleetFilterMatch(f, r)).length;
    final shown = widget.robots.where((r) => fleetFilterMatch(_filter, r)).toList();
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
      NwSegment<FleetFilter>(
          values: FleetFilter.values,
          selected: _filter,
          onChanged: (f) => setState(() => _filter = f),
          keyOf: NwFleet.filterKey,
          labelOf: (f) => switch (f) {
                FleetFilter.all => tr('All ${count(f)}', '全部 ${count(f)}'),
                FleetFilter.active => tr('Active ${count(f)}', '执行中 ${count(f)}'),
                FleetFilter.charging => tr('Charging ${count(f)}', '充电 ${count(f)}'),
              }),
      const SizedBox(height: Nw.s16),
      if (shown.isEmpty && widget.robots.isNotEmpty)
        Padding(
            key: NwFleet.emptyKey,
            padding: const EdgeInsets.all(Nw.s16),
            child: Text(tr('No robots match this filter', '没有符合这一格的狗'),
                textAlign: TextAlign.center, style: NwText.bodyMedium.copyWith(color: c.textSecondary))),
      for (final r in shown)
        Padding(
            padding: const EdgeInsets.only(bottom: Nw.s8),
            child: Builder(builder: (_) {
              final id = '${r['robot_id']}';
              final phase = robotPhase(r, deterring: widget.deterring.contains(id));
              return NwDogCard(
                  key: Key('robot-$id'),
                  id: id,
                  status: robotPhaseLabel(phase),
                  tone: robotPhaseTone(phase),
                  activity: widget.activityOf(r),
                  battery: robotBattery(r),
                  p1: phase == RobotPhase.deterring,
                  onTap: () => widget.onOpen(id));
            })),
    ]);
  }
}

/// 列表上的一条横幅（布防模式、天气、正在驱离）：左边图标、中间一两行字、右边箭头，整条能点。
/// [tone] 只给驱离（红）。
class NwBanner extends StatelessWidget {
  final IconData icon;
  final String title;
  final String? subtitle;
  final NwTone tone;
  final VoidCallback? onTap;
  const NwBanner(
      {super.key, required this.icon, required this.title, this.subtitle, this.tone = NwTone.neutral, this.onTap});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return NwCard(
        tone: tone,
        onTap: onTap,
        padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
        child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: Nw.touch - Nw.s16),
            child: Row(children: [
              Icon(icon, size: 24, color: tone == NwTone.neutral ? c.textSecondary : c.fg(tone)),
              const SizedBox(width: Nw.s16),
              Expanded(
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisSize: MainAxisSize.min,
                      children: [
                    Text(title, style: NwText.titleSmall.copyWith(color: c.text)),
                    if (subtitle != null)
                      Text(subtitle!, style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                  ])),
              const SizedBox(width: Nw.s8),
              Icon(Icons.chevron_right_rounded, size: 24, color: c.textSecondary),
            ])));
  }
}

/// 单狗页的信息格（电量、定位）：小标签在上，下面一段话**整段显示、不截**（定位那一行是给人看着办的）。
class NwInfoTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final String value;
  final NwTone tone;
  const NwInfoTile(
      {super.key, required this.icon, required this.label, required this.value, this.tone = NwTone.neutral});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return NwCard(
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Icon(icon, size: 24, color: tone == NwTone.neutral ? c.textSecondary : c.fg(tone)),
      const SizedBox(width: Nw.s8),
      Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        Text(label.toUpperCase(),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: NwText.labelSmall.copyWith(color: c.textSecondary)),
        const SizedBox(height: Nw.s4),
        Text(value, style: NwText.titleSmall.copyWith(color: c.text)),
      ])),
    ]));
  }
}

/// 单狗页「Operations」的一格：图标在上、字在下（最多三行，英文长的也放得下）。[onTap] 不给就置灰。
/// [tileKey] 安在能点的那一层上。
class NwOpTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback? onTap;
  final Key? tileKey;
  const NwOpTile({super.key, required this.icon, required this.label, this.onTap, this.tileKey});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final off = onTap == null;
    return Semantics(
        button: true,
        enabled: !off,
        child: Material(
            color: c.surface,
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Nw.r12), side: BorderSide(color: c.outline)),
            clipBehavior: Clip.antiAlias,
            child: InkWell(
                key: tileKey,
                onTap: onTap,
                child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: 12),
                    child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
                      Icon(icon, size: 24, color: off ? c.textMuted : c.primary),
                      const SizedBox(height: Nw.s4),
                      Text(label,
                          maxLines: 3,
                          textAlign: TextAlign.center,
                          overflow: TextOverflow.ellipsis,
                          style: NwText.labelSmall.copyWith(color: off ? c.textMuted : c.text)),
                    ])))));
  }
}

/// 一排几格的格子：同一排的格子一样高，最后一排不满的空着。
class NwTileGrid extends StatelessWidget {
  final int columns;
  final List<Widget> children;
  const NwTileGrid({super.key, required this.columns, required this.children});

  @override
  Widget build(BuildContext context) {
    return Column(mainAxisSize: MainAxisSize.min, children: [
      for (var i = 0; i < children.length; i += columns)
        Padding(
            padding: EdgeInsets.only(top: i == 0 ? 0 : Nw.s8),
            child: IntrinsicHeight(
                child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              for (var j = i; j < i + columns; j++) ...[
                if (j > i) const SizedBox(width: Nw.s8),
                Expanded(child: j < children.length ? children[j] : const SizedBox.shrink()),
              ],
            ]))),
    ]);
  }
}
