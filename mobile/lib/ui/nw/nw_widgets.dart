/// 新设计系统的小件：卡片、小节标题、状态药丸、健康标签、分段、数值格、诊断行、操作格、电量、页头、
/// 空状态、提示条。样子（颜色、圆角、内距、字）取自设计稿的组件；设计稿里「同色字压同色底」的几处
/// 改成了**软底 + 同色字**（看得见）。
///
/// 颜色的规矩：红（[NwTone.error]）只给 P1、急停、离线、不可撤回的操作；橙（[NwTone.warning]）只给
/// P2、低电；绿（[NwTone.success]）给正常、在线、已连接。级别永远是「颜色 + 形状 + 文字」，不只靠颜色。
library;

import 'package:flutter/material.dart';

import '../../design/nw_tokens.g.dart';
import 'nw_theme.dart';

enum NwTone { neutral, success, warning, error, info }

/// 某种用途（`design/mobile.json` 的 usage：`p1`、`offline`、`lowBattery`、`online`……）该用哪种色调。
/// 不认得的用途不上色。
NwTone nwToneOf(String usage) => switch (nwUsage[usage]) {
      'success' => NwTone.success,
      'warning' => NwTone.warning,
      'error' => NwTone.error,
      'info' => NwTone.info,
      _ => NwTone.neutral,
    };

/// 色调落到具体的颜色上。
extension NwToneColors on NwColors {
  /// 当字、当图标用的那个色（压 `surface`、`bg`、自己的软底都够 4.5）。不上色的回正文色。
  Color fg(NwTone t) => switch (t) {
        NwTone.neutral => text,
        NwTone.success => success,
        NwTone.warning => warning,
        NwTone.error => error,
        NwTone.info => info,
      };

  /// 软底。不上色的回 `surfaceVariant`（上面写正文色的字）。
  Color soft(NwTone t) => switch (t) {
        NwTone.neutral => surfaceVariant,
        NwTone.success => successSoft,
        NwTone.warning => warningSoft,
        NwTone.error => errorSoft,
        NwTone.info => infoSoft,
      };

  /// 实底，和实底上的字。不上色的是 `surfaceVariant` + 正文色。
  (Color, Color) solid(NwTone t) => switch (t) {
        NwTone.neutral => (surfaceVariant, text),
        NwTone.success => (success, onSuccess),
        NwTone.warning => (warning, onWarning),
        NwTone.error => (error, onError),
        NwTone.info => (info, onInfo),
      };
}

/// 卡片：圆角 12、1px 边、没有阴影。[tone] 给了就是软底 + 同色的边（P1 = `error`，边 2px）。
class NwCard extends StatelessWidget {
  final Widget child;
  final EdgeInsetsGeometry padding;
  final NwTone tone;
  final VoidCallback? onTap;
  final double radius;

  const NwCard(
      {super.key,
      required this.child,
      this.padding = const EdgeInsets.all(Nw.s16),
      this.tone = NwTone.neutral,
      this.onTap,
      this.radius = Nw.r12});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final shape = RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(radius),
        side: switch (tone) {
          NwTone.neutral => BorderSide(color: c.outline),
          NwTone.error => BorderSide(color: c.errorFrame, width: 2),
          _ => BorderSide(color: c.fg(tone)),
        });
    return Material(
        color: tone == NwTone.neutral ? c.surface : c.soft(tone),
        shape: shape,
        clipBehavior: Clip.antiAlias,
        child: InkWell(onTap: onTap, child: Padding(padding: padding, child: child)));
  }
}

/// 小节标题：等宽小字、英文大写（「ACTIVE INCIDENTS」）。右边可以放一个小东西（数目、入口）。
class NwSectionLabel extends StatelessWidget {
  final String text;
  final Widget? trailing;
  const NwSectionLabel(this.text, {super.key, this.trailing});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Row(children: [
      Expanded(
          child: Text(text.toUpperCase(),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: NwText.labelMedium.copyWith(color: c.textSecondary))),
      ?trailing,
    ]);
  }
}

/// 状态药丸：软底 + 同色字（设计稿的 `RobotBadge` 是同色字压同色底，看不见）。[dot] 前面带个小圆点。
class NwStatusPill extends StatelessWidget {
  final String label;
  final NwTone tone;
  final bool dot;
  const NwStatusPill({super.key, required this.label, this.tone = NwTone.neutral, this.dot = false});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final fg = c.fg(tone);
    return Container(
        padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: Nw.s4),
        decoration: BoxDecoration(color: c.soft(tone), borderRadius: BorderRadius.circular(Nw.rPill)),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          if (dot) ...[
            Container(width: 8, height: 8, decoration: BoxDecoration(color: fg, shape: BoxShape.circle)),
            const SizedBox(width: Nw.s4),
          ],
          Flexible(
              child: Text(label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: NwText.labelSmall.copyWith(color: fg, fontWeight: FontWeight.w700))),
        ]));
  }
}

/// 健康标签（「Station Online」）：细边的药丸。[bad] = 这一项不行了（离线、没过）：红边、软红底、红字。
class NwHealthChip extends StatelessWidget {
  final IconData icon;
  final String label;
  final bool bad;
  const NwHealthChip({super.key, required this.icon, required this.label, this.bad = false});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final fg = bad ? c.error : c.text;
    return Container(
        padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
        decoration: BoxDecoration(
            color: bad ? c.errorSoft : c.surface,
            borderRadius: BorderRadius.circular(Nw.rPill),
            border: Border.all(color: bad ? c.error : c.outline)),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 16, color: bad ? c.error : c.success),
          const SizedBox(width: Nw.s4),
          Flexible(
              child: Text(label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: NwText.labelMedium.copyWith(color: fg))),
        ]));
  }
}

/// 分段（「ARMED / HOME / VISITOR」、前后摄像头）：一排等宽的格子，选中的那格有底。
/// [onChanged] 不给就是只读的（权限不够）。每一格的 Key 由 [keyOf] 给。
class NwSegment<T> extends StatelessWidget {
  final List<T> values;
  final T? selected;
  final ValueChanged<T>? onChanged;
  final String Function(T value) labelOf;
  final Key? Function(T value)? keyOf;

  const NwSegment(
      {super.key,
      required this.values,
      required this.selected,
      required this.onChanged,
      required this.labelOf,
      this.keyOf});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Container(
        padding: const EdgeInsets.all(Nw.s4),
        decoration: BoxDecoration(
            color: c.surface,
            borderRadius: BorderRadius.circular(Nw.r8),
            border: Border.all(color: c.outline)),
        child: Row(children: [
          for (final v in values)
            Expanded(
                child: Semantics(
                    button: true,
                    selected: v == selected,
                    child: Material(
                        color: v == selected ? c.surfaceVariant : Colors.transparent,
                        borderRadius: BorderRadius.circular(Nw.r4),
                        clipBehavior: Clip.antiAlias,
                        child: InkWell(
                            key: keyOf?.call(v),
                            onTap: onChanged == null ? null : () => onChanged!(v),
                            child: Container(
                                constraints: const BoxConstraints(minHeight: 40),
                                alignment: Alignment.center,
                                padding: const EdgeInsets.symmetric(horizontal: Nw.s8, vertical: Nw.s8),
                                child: Text(labelOf(v),
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: NwText.labelMedium.copyWith(
                                        color: v == selected ? c.text : c.textSecondary))))))),
        ]));
  }
}

/// 数值格（电量、定位、今天几趟）：小标签在上、数值在下，左边一个图标。
class NwStatTile extends StatelessWidget {
  final String label;
  final String value;
  final IconData? icon;
  final NwTone tone;
  const NwStatTile(
      {super.key, required this.label, required this.value, this.icon, this.tone = NwTone.neutral});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return NwCard(
        child: Row(children: [
      if (icon != null) ...[
        Icon(icon, size: 24, color: tone == NwTone.neutral ? c.textSecondary : c.fg(tone)),
        const SizedBox(width: Nw.s8),
      ],
      Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        Text(label.toUpperCase(),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: NwText.labelSmall.copyWith(color: c.textSecondary)),
        const SizedBox(height: Nw.s4),
        Text(value,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: NwText.titleMedium.copyWith(color: c.fg(tone), fontWeight: FontWeight.w700)),
      ])),
    ]));
  }
}

/// 诊断行（「System Clock … Synchronized」）：左边图标和名字，右边加粗的值，下面一条线。
class NwDiagnosticRow extends StatelessWidget {
  final IconData? icon;
  final String label;
  final String value;
  final NwTone tone;
  const NwDiagnosticRow(
      {super.key, this.icon, required this.label, required this.value, this.tone = NwTone.neutral});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Container(
        padding: const EdgeInsets.all(Nw.s16),
        decoration: BoxDecoration(border: Border(bottom: BorderSide(color: c.outline))),
        child: Row(children: [
          if (icon != null) ...[
            Icon(icon, size: 20, color: c.textSecondary),
            const SizedBox(width: Nw.s8),
          ],
          Expanded(child: Text(label, style: NwText.bodyMedium.copyWith(color: c.text))),
          const SizedBox(width: Nw.s16),
          // 值可能很长（站点给的明细）：最多占一半，换行
          Flexible(
              child: Text(value,
                  textAlign: TextAlign.end,
                  style: NwText.bodyMedium.copyWith(color: c.fg(tone), fontWeight: FontWeight.w700))),
        ]));
  }
}

/// 操作格（单狗页「Operations」、管理页的入口）：一个图标、下面一行字。[onTap] 不给就置灰。
/// [tileKey] 安在能点的那一层（`InkWell`）上。[tone] 只给不可撤回的操作（红）。
class NwActionTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback? onTap;
  final Key? tileKey;
  final NwTone tone;
  const NwActionTile(
      {super.key, required this.icon, required this.label, this.onTap, this.tileKey, this.tone = NwTone.neutral});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final fg = onTap == null ? c.textMuted : (tone == NwTone.neutral ? c.primary : c.fg(tone));
    return Semantics(
        button: true,
        enabled: onTap != null,
        child: Material(
            color: c.surface,
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Nw.r12), side: BorderSide(color: c.outline)),
            clipBehavior: Clip.antiAlias,
            child: InkWell(
                key: tileKey,
                onTap: onTap,
                child: Padding(
                    padding: const EdgeInsets.all(Nw.s16),
                    child: Column(mainAxisSize: MainAxisSize.min, mainAxisAlignment: MainAxisAlignment.center, children: [
                      Icon(icon, size: 24, color: fg),
                      const SizedBox(height: Nw.s4),
                      Text(label,
                          maxLines: 2,
                          textAlign: TextAlign.center,
                          overflow: TextOverflow.ellipsis,
                          style: NwText.labelSmall.copyWith(
                              color: onTap == null ? c.textMuted : (tone == NwTone.neutral ? c.text : fg))),
                    ])))));
  }
}

/// 电量：图标 + 「42%」。低于 [lowBelow] 是橙色（低电）加另一个图标，别的是绿色；不知道（null）写「--」、不上色。
class NwBattery extends StatelessWidget {
  final num? percent;

  /// 低于这么多算低电（设计稿的线）。
  static const num lowBelow = 20;
  const NwBattery({super.key, required this.percent});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final p = percent;
    final low = p != null && p < lowBelow;
    final tone = p == null ? NwTone.neutral : nwToneOf(low ? 'lowBattery' : 'battery');
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(
          p == null
              ? Icons.battery_unknown_rounded
              : (low ? Icons.battery_alert_rounded : Icons.battery_full_rounded),
          size: 16,
          color: p == null ? c.textSecondary : c.fg(tone)),
      const SizedBox(width: Nw.s4),
      Text(p == null ? '--' : '${p.round()}%',
          style: NwText.labelMedium.copyWith(color: c.text, fontWeight: FontWeight.w700)),
    ]);
  }
}

/// 页头（没有 `AppBar` 的页面用）：上面一行小标（「NEXUS WARDEN」）、大标题、下面可以再一行说明；
/// 左边放返回之类，右边放按钮。下面一条线。自己躲开刘海。
class NwHeader extends StatelessWidget {
  final String title;
  final String? overline;
  final String? subtitle;
  final Widget? leading;
  final List<Widget> trailing;

  const NwHeader(
      {super.key,
      required this.title,
      this.overline,
      this.subtitle,
      this.leading,
      this.trailing = const <Widget>[]});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Container(
        decoration: BoxDecoration(color: c.surface, border: Border(bottom: BorderSide(color: c.outline))),
        child: SafeArea(
            bottom: false,
            child: Padding(
                padding: EdgeInsets.fromLTRB(leading == null ? Nw.s24 : Nw.s8, Nw.s8, Nw.s8, Nw.s8),
                child: Row(children: [
                  ?leading,
                  Expanded(
                      child: ConstrainedBox(
                          constraints: const BoxConstraints(minHeight: Nw.touch),
                          child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              mainAxisAlignment: MainAxisAlignment.center,
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                if (overline != null)
                                  Text(overline!.toUpperCase(),
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: NwText.labelSmall.copyWith(color: c.textSecondary)),
                                Text(title,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: NwText.headlineSmall.copyWith(color: c.text)),
                                if (subtitle != null)
                                  Text(subtitle!,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                              ]))),
                  ...trailing,
                ]))));
  }
}

/// 空状态（「还没有…」）：居中的图标、一句话、可以再一句引导和一个按钮。
/// 「没有」「还没读到」「读不到」是三种话，调用方分开给，这里只管摆。
class NwEmpty extends StatelessWidget {
  final IconData? icon;
  final String text;
  final String? hint;
  final Widget? action;
  const NwEmpty({super.key, this.icon, required this.text, this.hint, this.action});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Center(
        child: SingleChildScrollView(
            padding: const EdgeInsets.all(Nw.s24),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              if (icon != null) ...[
                Icon(icon, size: 48, color: c.textMuted),
                const SizedBox(height: Nw.s16),
              ],
              Text(text, textAlign: TextAlign.center, style: NwText.titleSmall.copyWith(color: c.text)),
              if (hint != null) ...[
                const SizedBox(height: Nw.s4),
                Text(hint!,
                    textAlign: TextAlign.center, style: NwText.bodySmall.copyWith(color: c.textSecondary)),
              ],
              if (action != null) ...[const SizedBox(height: Nw.s16), action!],
            ])));
  }
}

/// 提示条（实时更新断了、读不到、没权限）：软底、左边同色的图标、正文色的字。
class NwNotice extends StatelessWidget {
  final String text;
  final NwTone tone;
  final IconData? icon;
  const NwNotice({super.key, required this.text, this.tone = NwTone.info, this.icon});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Container(
        padding: const EdgeInsets.all(Nw.s16),
        decoration: BoxDecoration(color: c.soft(tone), borderRadius: BorderRadius.circular(Nw.r8)),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(
              icon ??
                  switch (tone) {
                    NwTone.error => Icons.error_outline_rounded,
                    NwTone.warning => Icons.warning_amber_rounded,
                    NwTone.success => Icons.check_circle_outline_rounded,
                    _ => Icons.info_outline_rounded,
                  },
              size: 20,
              color: tone == NwTone.neutral ? c.textSecondary : c.fg(tone)),
          const SizedBox(width: Nw.s8),
          Expanded(child: Text(text, style: NwText.bodyMedium.copyWith(color: c.text))),
        ]));
  }
}
