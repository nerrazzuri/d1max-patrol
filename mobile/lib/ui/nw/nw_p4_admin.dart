/// 地图、记录、管理这几页（重做的第四批）共用的小件：居中限宽的滚动列表、一组行装一张卡、格子、
/// 图例的一行、压在图上的方按钮、图上的狗。样子取自设计稿的 `legend_item`、`robot_marker`、
/// System Health 那一页；颜色都从 [Nw.of] 取。
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'nw_theme.dart';
import 'nw_widgets.dart';

/// 小节之间、小节标题和内容之间的空。
const SizedBox nwGap4 = SizedBox(height: Nw.s4);
const SizedBox nwGap8 = SizedBox(height: Nw.s8);
const SizedBox nwGap16 = SizedBox(height: Nw.s16);
const SizedBox nwGap24 = SizedBox(height: Nw.s24);

/// 页面内容最宽这么多（桌面的大窗口上不把卡片拉成一长条），多出来的两边留白。
const double nwContentMaxWidth = 960;

/// 这么宽的地方（[width]）两边各留多少：窄屏是页边距，宽屏把内容收到 [nwContentMaxWidth] 居中。
double nwSideInset(double width) => math.max(Nw.s16, (width - nwContentMaxWidth) / 2);

/// 一页的滚动内容：页边距、宽屏限宽居中。跟 `ListView` 一样用（下拉刷新、`controller` 都照旧）。
class NwListView extends StatelessWidget {
  final List<Widget> children;
  final ScrollController? controller;
  const NwListView({super.key, required this.children, this.controller});

  @override
  Widget build(BuildContext context) => LayoutBuilder(builder: (context, box) {
        final side = nwSideInset(box.maxWidth);
        return ListView(
            controller: controller,
            padding: EdgeInsets.fromLTRB(side, Nw.s16, side, Nw.s24),
            children: children);
      });
}

/// 一组行装在一张卡片里：行还是原来的 `ListTile`（测试、无障碍按它认），行和行之间一条线。
class NwListCard extends StatelessWidget {
  final List<Widget> children;
  final NwTone tone;
  const NwListCard({super.key, required this.children, this.tone = NwTone.neutral});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return NwCard(
        padding: EdgeInsets.zero,
        tone: tone,
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          for (var i = 0; i < children.length; i++) ...[
            if (i > 0) Divider(height: 1, thickness: 1, color: c.outline),
            children[i],
          ],
        ]));
  }
}

/// 卡片里的一句话（「还没有…」之类）：次要色，跟行一样的内距。
class NwCardText extends StatelessWidget {
  final String text;
  const NwCardText(this.text, {super.key});

  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.all(Nw.s16),
      child: Text(text, style: NwText.bodyMedium.copyWith(color: Nw.of(context).textSecondary)));
}

/// 格子：一行摆几格看有多宽（每格至少 [minTile] 宽），格与格之间 8。版本格子、入口、数值格都用它。
class NwGrid extends StatelessWidget {
  final List<Widget> children;
  final double minTile;
  final int maxColumns;
  const NwGrid({super.key, required this.children, this.minTile = 280, this.maxColumns = 4});

  @override
  Widget build(BuildContext context) => LayoutBuilder(builder: (context, box) {
        final cols = math.max(1, math.min(maxColumns, (box.maxWidth + Nw.s8) ~/ (minTile + Nw.s8)));
        // 少算一丁点：小数宽度加起来不至于刚好超过一行、把最后一格挤到下一行
        final w = (box.maxWidth - Nw.s8 * (cols - 1)) / cols - 0.01;
        return Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
          for (final ch in children) SizedBox(width: w, child: ch),
        ]);
      });
}

/// 图例的一行：左边一个小样子（色块、形状），右边名字。
class NwLegendItem extends StatelessWidget {
  final Widget swatch;
  final String label;
  const NwLegendItem({super.key, required this.swatch, required this.label});

  @override
  Widget build(BuildContext context) => Row(mainAxisSize: MainAxisSize.min, children: [
        SizedBox(width: 16, height: 16, child: Center(child: swatch)),
        const SizedBox(width: Nw.s4),
        Flexible(
            child: Text(label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: NwText.labelSmall.copyWith(color: Nw.of(context).textSecondary))),
      ]);
}

/// 压在图上的浮层（图例）：实底的卡片 —— 字压的是卡片的底，不是底下那张栅格图，浅色深色都读得出。
class NwMapOverlay extends StatelessWidget {
  final Widget child;

  /// 安在卡片那一层（`Material`）上的 Key。
  final Key? overlayKey;
  const NwMapOverlay({super.key, required this.child, this.overlayKey});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Material(
        key: overlayKey,
        color: c.surface,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Nw.r8), side: BorderSide(color: c.outline)),
        child: Padding(padding: const EdgeInsets.all(Nw.s8), child: child));
  }
}

/// 压在图上的方按钮（回正、放大、缩小）：实底、细边、够手指点。[buttonKey] 安在能点的那一层上。
class NwMapButton extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;
  final Key? buttonKey;
  const NwMapButton({super.key, required this.icon, required this.tooltip, this.onPressed, this.buttonKey});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Tooltip(
        message: tooltip,
        child: Material(
            color: c.surface,
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Nw.r8), side: BorderSide(color: c.outline)),
            clipBehavior: Clip.antiAlias,
            child: InkWell(
                key: buttonKey,
                onTap: onPressed,
                child: SizedBox(
                    width: Nw.touch,
                    height: Nw.touch,
                    child: Icon(icon, size: 24, color: onPressed == null ? c.textMuted : c.text)))));
  }
}

/// 图上的一只狗：实底的小圆牌，里面是它的编号；边的颜色是状态（在线绿、离线红）。牌子自己有底，
/// 压在什么颜色的图上都读得出。[markerKey] 安在牌子那一层（`Container`）上。
class NwRobotMarker extends StatelessWidget {
  final String id;
  final NwTone tone;
  final Key? markerKey;

  /// 牌子的高；宽跟着编号长短走，最窄也是这么多。
  static const double size = 28;
  const NwRobotMarker({super.key, required this.id, this.tone = NwTone.neutral, this.markerKey});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Container(
        key: markerKey,
        height: size,
        constraints: const BoxConstraints(minWidth: size),
        padding: const EdgeInsets.symmetric(horizontal: Nw.s4),
        alignment: Alignment.center,
        decoration: BoxDecoration(
            color: c.surface,
            borderRadius: BorderRadius.circular(Nw.rPill),
            border: Border.all(color: tone == NwTone.neutral ? c.textSecondary : c.fg(tone), width: 2)),
        child: Text(id,
            maxLines: 1,
            softWrap: false,
            style: NwText.labelSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)));
  }
}
