/// 新设计系统的几张大卡片：站点、狗、告警、一趟巡检、版本。只管摆，数据和动作由页面给。
///
/// 设计稿这几张卡自己的毛病没照抄：站点卡的 P1 徽标条件写反了；狗卡不认状态色、低电写死；告警卡的
/// 图标和底一个色。这里状态色都由调用方按真实状态给（[NwTone]）。
library;

import 'package:flutter/material.dart';

import '../../l10n.dart';
import 'nw_button.dart';
import 'nw_theme.dart';
import 'nw_widgets.dart';

/// 站点卡：名字、地址、（有数据才显示的）连接状态，下面一行是没处理的 P1 数目和「进入」。
/// 整张卡点了也是进入。
class NwSiteCard extends StatelessWidget {
  final String name;
  final String address;

  /// 连接状态的字（「CONNECTED」）；不给就不显示药丸（现在站点列表上没有这个数据）。
  final String? status;
  final bool connected;

  /// 没处理的 P1 数目；不给就不显示这一块。
  final int? p1Count;
  final VoidCallback? onEnter;

  /// 「进入」按钮的 Key（里面的 `TextButton`）。
  final Key? enterKey;

  /// 名字右边的小东西（菜单）。
  final Widget? menu;

  const NwSiteCard(
      {super.key,
      required this.name,
      required this.address,
      this.status,
      this.connected = true,
      this.p1Count,
      this.onEnter,
      this.enterKey,
      this.menu});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final p1 = p1Count;
    return NwCard(
        padding: const EdgeInsets.all(Nw.s24),
        onTap: onEnter,
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(children: [
            Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700)),
              const SizedBox(height: Nw.s4),
              Row(children: [
                Icon(Icons.location_on_rounded, size: 14, color: c.textSecondary),
                const SizedBox(width: Nw.s4),
                Expanded(
                    child: Text(address,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: NwText.bodySmall.copyWith(color: c.textSecondary))),
              ]),
            ])),
            if (status != null) ...[
              const SizedBox(width: Nw.s8),
              NwStatusPill(
                  label: status!, tone: nwToneOf(connected ? 'connected' : 'offline'), dot: true),
            ],
            ?menu,
          ]),
          const SizedBox(height: Nw.s16),
          Divider(height: 1, thickness: 1, color: c.outline),
          const SizedBox(height: Nw.s8),
          Row(children: [
            Expanded(
                child: p1 == null
                    ? const SizedBox.shrink()
                    : p1 > 0
                        ? Align(alignment: Alignment.centerLeft, child: _P1Badge(count: p1))
                        : Text(tr('NO ACTIVE P1 ALERTS', '没有未处理的 P1'),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: NwText.labelSmall
                                .copyWith(color: c.textSecondary, fontWeight: FontWeight.w700))),
            NwButton(
                buttonKey: enterKey,
                label: tr('Enter Station', '进入站点'),
                icon: Icons.arrow_forward_rounded,
                variant: NwButtonVariant.ghost,
                size: NwButtonSize.small,
                onPressed: onEnter),
          ]),
        ]));
  }
}

/// 「◆ P1 · 2 ALERTS」：红实底、白字，前面是 P1 的形状（菱形）。
class _P1Badge extends StatelessWidget {
  final int count;
  const _P1Badge({required this.count});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final style = NwText.labelSmall.copyWith(color: c.onError, fontWeight: FontWeight.w700);
    return Container(
        padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
        decoration: BoxDecoration(color: c.error, borderRadius: BorderRadius.circular(Nw.r8)),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Transform.rotate(angle: 0.785398, child: Container(width: 9, height: 9, color: c.onError)),
          const SizedBox(width: Nw.s8),
          Flexible(
              child: Text(tr('P1 · $count ALERTS', 'P1 · $count 条'),
                  maxLines: 1, overflow: TextOverflow.ellipsis, style: style)),
        ]));
  }
}

/// 狗卡：编号、状态药丸、在做什么、电量。[tone] 按真实状态给（P1 红、在线绿、空闲不上色、低电橙、
/// 离线红）；[p1] = 这只狗身上有没处理的 P1：红框、换成警告图标。
class NwDogCard extends StatelessWidget {
  final String id;
  final String status;
  final NwTone tone;
  final String activity;

  /// 电量百分数；不知道给 null。
  final num? battery;
  final bool p1;
  final VoidCallback? onTap;

  const NwDogCard(
      {super.key,
      required this.id,
      required this.status,
      this.tone = NwTone.neutral,
      required this.activity,
      this.battery,
      this.p1 = false,
      this.onTap});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Material(
        color: c.surface,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Nw.r12),
            side: p1 ? BorderSide(color: c.errorFrame, width: 2) : BorderSide(color: c.outline)),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
            onTap: onTap,
            child: Padding(
                padding: const EdgeInsets.all(Nw.s16),
                child: Row(children: [
                  Container(
                      width: 48,
                      height: 48,
                      decoration: BoxDecoration(
                          color: p1 ? c.errorSoft : c.surfaceVariant, borderRadius: BorderRadius.circular(Nw.r8)),
                      child: Icon(p1 ? Icons.warning_rounded : Icons.smart_toy_rounded,
                          size: 28, color: p1 ? c.error : c.textSecondary)),
                  const SizedBox(width: Nw.s16),
                  Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                    Row(children: [
                      Flexible(
                          child: Text(id,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700))),
                      const SizedBox(width: Nw.s8),
                      Flexible(child: NwStatusPill(label: status, tone: tone)),
                    ]),
                    const SizedBox(height: Nw.s4),
                    Text(activity,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                  ])),
                  const SizedBox(width: Nw.s8),
                  Column(crossAxisAlignment: CrossAxisAlignment.end, mainAxisSize: MainAxisSize.min, children: [
                    NwBattery(percent: battery),
                    const SizedBox(height: Nw.s8),
                    Icon(Icons.chevron_right_rounded, size: 24, color: c.textSecondary),
                  ]),
                ]))));
  }
}

/// 告警的一行：左边软底的图标格、标题、级别标签，下面是哪只狗和什么时候。
/// [leading] 给了就放在图标格里代替 [icon]（放级别的形状：菱形、三角、圆）。
class NwAlertItem extends StatelessWidget {
  final String title;

  /// 级别的字（「P1」「P2」「P3」）。
  final String level;
  final NwTone tone;
  final IconData icon;
  final Widget? leading;

  /// 哪只狗（或者哪个摄像头）。
  final String source;
  final String time;
  final VoidCallback? onTap;
  final Widget? trailing;

  const NwAlertItem(
      {super.key,
      required this.title,
      required this.level,
      this.tone = NwTone.neutral,
      this.icon = Icons.notifications_rounded,
      this.leading,
      required this.source,
      required this.time,
      this.onTap,
      this.trailing});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final (tagBg, tagFg) = c.solid(tone);
    final small = NwText.labelSmall.copyWith(color: c.textSecondary);
    return NwCard(
        radius: Nw.r8,
        onTap: onTap,
        child: Row(children: [
          Container(
              width: 48,
              height: 48,
              alignment: Alignment.center,
              decoration: BoxDecoration(color: c.soft(tone), borderRadius: BorderRadius.circular(Nw.r4)),
              child: leading ??
                  Icon(icon, size: 24, color: tone == NwTone.neutral ? c.textSecondary : c.fg(tone))),
          const SizedBox(width: Nw.s16),
          Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
            Row(children: [
              Expanded(
                  child: Text(title,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: NwText.labelLarge.copyWith(color: c.text, fontWeight: FontWeight.w700))),
              const SizedBox(width: Nw.s8),
              Container(
                  padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: Nw.s4),
                  decoration: BoxDecoration(color: tagBg, borderRadius: BorderRadius.circular(Nw.r2)),
                  child: Text(level,
                      style: NwText.labelSmall.copyWith(color: tagFg, fontWeight: FontWeight.w700))),
            ]),
            const SizedBox(height: Nw.s4),
            Row(children: [
              Flexible(child: Text(source, maxLines: 1, overflow: TextOverflow.ellipsis, style: small)),
              Padding(
                  padding: const EdgeInsets.symmetric(horizontal: Nw.s8),
                  child: Text('•', style: small.copyWith(color: c.textMuted))),
              Flexible(child: Text(time, maxLines: 1, overflow: TextOverflow.ellipsis, style: small)),
            ]),
          ])),
          const SizedBox(width: Nw.s8),
          trailing ?? Icon(Icons.chevron_right_rounded, size: 24, color: c.textSecondary),
        ]));
  }
}

/// 一趟巡检：编号、时间、类型标签，缩略图（站点给的照片，没有就不摆这一行），下面是结果。
/// [anomaly] = 这一趟有异常：图标换成警告；颜色按 [tone]（调用方按这趟的级别给，缺省不上色）。
class NwPatrolCard extends StatelessWidget {
  final String id;
  final String time;
  final String? type;
  final String status;
  final bool anomaly;
  final NwTone tone;

  /// 缩略图（已经是做好的小部件：站点的照片）。每张摆成 80×60。
  final List<Widget> thumbs;

  /// 没摆出来的还有几张。
  final int more;
  final VoidCallback? onTap;

  const NwPatrolCard(
      {super.key,
      required this.id,
      required this.time,
      this.type,
      required this.status,
      this.anomaly = false,
      this.tone = NwTone.neutral,
      this.thumbs = const <Widget>[],
      this.more = 0,
      this.onTap});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final mark = anomaly ? c.fg(tone) : c.success;
    Widget box(Widget child) => ClipRRect(
        borderRadius: BorderRadius.circular(Nw.r8),
        child: Container(width: 80, height: 60, color: c.surfaceVariant, alignment: Alignment.center, child: child));
    return NwCard(
        onTap: onTap,
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Icon(anomaly ? Icons.warning_amber_rounded : Icons.check_circle_outline_rounded,
                    size: 16, color: mark),
                const SizedBox(width: Nw.s4),
                Expanded(
                    child: Text(id,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: NwText.labelLarge.copyWith(color: c.text, fontWeight: FontWeight.w700))),
              ]),
              const SizedBox(height: Nw.s4),
              Text(time,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: NwText.bodySmall.copyWith(color: c.textSecondary)),
            ])),
            if (type != null) ...[
              const SizedBox(width: Nw.s8),
              Container(
                  padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: Nw.s4),
                  decoration: BoxDecoration(color: c.surfaceVariant, borderRadius: BorderRadius.circular(Nw.r4)),
                  child: Text(type!, style: NwText.labelSmall.copyWith(color: c.textSecondary))),
            ],
          ]),
          if (thumbs.isNotEmpty) ...[
            const SizedBox(height: Nw.s16),
            SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Row(children: [
                  for (final t in thumbs)
                    Padding(padding: const EdgeInsets.only(right: Nw.s8), child: box(t)),
                  if (more > 0)
                    box(Text('+$more', style: NwText.labelMedium.copyWith(color: c.textSecondary))),
                ])),
          ],
          const SizedBox(height: Nw.s16),
          Divider(height: 1, thickness: 1, color: c.outline),
          const SizedBox(height: Nw.s8),
          Row(children: [
            Expanded(
                child: Text(status,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: NwText.bodySmall.copyWith(
                        color: anomaly ? c.fg(tone) : c.textSecondary,
                        fontWeight: anomaly ? FontWeight.w700 : FontWeight.w400))),
            Icon(Icons.arrow_forward_rounded, size: 24, color: c.text),
          ]),
        ]));
  }
}

/// 版本格：是谁（站点、哪只狗、哪台手机）、哪一版，可以带一个小标（「有新版」「狗太老」）。
class NwVersionCard extends StatelessWidget {
  final String entity;
  final String version;
  final String? note;
  final NwTone noteTone;

  const NwVersionCard(
      {super.key, required this.entity, required this.version, this.note, this.noteTone = NwTone.info});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return NwCard(
        radius: Nw.r8,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
          Text(entity.toUpperCase(),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: NwText.labelSmall.copyWith(color: c.textSecondary)),
          const SizedBox(height: Nw.s4),
          Row(children: [
            Expanded(
                child: Text(version,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700))),
            if (note != null) ...[
              const SizedBox(width: Nw.s8),
              Flexible(child: NwStatusPill(label: note!, tone: noteTone)),
            ],
          ]),
        ]));
  }
}
