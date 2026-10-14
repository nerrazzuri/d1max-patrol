/// 值守首页、告警页、布防、驱离这一批（App 重做 P2）用的几样小件：级别的色调和图标格、能单独置灰
/// 某一格的分段、小地图。只管摆，数据和动作由页面给。
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../widget/alarm_widgets.dart';
import 'nw_map_raster.dart';
import 'nw_theme.dart';
import 'nw_widgets.dart';

/// 告警级别的色调：P1 红、P2 橙、别的不上色。
NwTone nwLevelTone(String level) => switch (level) {
      'P1' => nwToneOf('p1'),
      'P2' => nwToneOf('p2'),
      _ => NwTone.neutral,
    };

/// 级别的图标格：软底 + 同色的形状（P1 菱形、P2 三角、别的空心圆）。级别的字由旁边的标题给。
class NwLevelBox extends StatelessWidget {
  final String level;
  final double size;
  const NwLevelBox({super.key, required this.level, this.size = 40});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final tone = nwLevelTone(level);
    return Container(
        width: size,
        height: size,
        alignment: Alignment.center,
        decoration: BoxDecoration(color: c.soft(tone), borderRadius: BorderRadius.circular(Nw.r4)),
        child: PriorityShape(
            level: level, color: tone == NwTone.neutral ? c.textSecondary : c.fg(tone), size: 18));
  }
}

/// [NwChoiceBar] 的一格。[onTap] 不给就是置灰的（没权限，或者已经是这一格）。
class NwChoice {
  final Key? key;
  final String label;
  final IconData? icon;
  final bool selected;
  final VoidCallback? onTap;
  const NwChoice({this.key, required this.label, this.icon, this.selected = false, this.onTap});
}

/// 分段（「ARMED / HOME / VISITOR」），跟 `NwSegment` 一个样子，但**每一格各自能不能点**：
/// 选中的那格有底、加粗；点不了又没选中的是最淡的字。每格高 48。Key 安在能点的那一层（`InkWell`）上。
class NwChoiceBar extends StatelessWidget {
  final List<NwChoice> choices;
  const NwChoiceBar({super.key, required this.choices});

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
          for (final v in choices)
            Expanded(
                child: Semantics(
                    button: true,
                    selected: v.selected,
                    enabled: v.onTap != null,
                    child: Material(
                        color: v.selected ? c.surfaceVariant : Colors.transparent,
                        borderRadius: BorderRadius.circular(Nw.r4),
                        clipBehavior: Clip.antiAlias,
                        child: InkWell(
                            key: v.key,
                            onTap: v.onTap,
                            child: Container(
                                constraints: const BoxConstraints(minHeight: 48),
                                alignment: Alignment.center,
                                padding: const EdgeInsets.symmetric(horizontal: Nw.s4, vertical: Nw.s8),
                                child: Builder(builder: (context) {
                                  final fg = v.selected
                                      ? c.text
                                      : (v.onTap == null ? c.textMuted : c.textSecondary);
                                  return Row(mainAxisSize: MainAxisSize.min, children: [
                                    if (v.icon != null) ...[
                                      Icon(v.icon, size: 16, color: fg),
                                      const SizedBox(width: Nw.s4),
                                    ],
                                    Flexible(
                                        child: Text(v.label,
                                            maxLines: 1,
                                            overflow: TextOverflow.ellipsis,
                                            style: NwText.labelMedium.copyWith(
                                                color: fg,
                                                fontWeight:
                                                    v.selected ? FontWeight.w700 : FontWeight.w600))),
                                  ]);
                                })))))),
        ]));
  }
}

/// 小地图：站点渲染的那张栅格图的缩略，下面一行说图上标了什么。整块能点（[tapKey] 安在 `InkWell` 上）。
/// 图取不到、读不出就只剩下面那一行（入口还在）。**换了一张图要换 `key`**：这里只取一次。
class NwMapThumb extends StatefulWidget {
  final Future<Uint8List> Function() load;

  /// 图上标了什么（「狗 A · 拦截点 gate」）。
  final String label;

  /// 点了去哪（「在图上看」）。
  final String action;
  final VoidCallback onTap;
  final Key? tapKey;
  final double height;

  /// 深色下包住缩略图底图的那层 `ColorFiltered`（[darkMapRaster]）。
  static const Key darkRasterKey = Key('map-thumb-dark-raster');

  const NwMapThumb(
      {super.key,
      required this.load,
      required this.label,
      required this.action,
      required this.onTap,
      this.tapKey,
      this.height = 120});

  @override
  State<NwMapThumb> createState() => _NwMapThumbState();
}

class _NwMapThumbState extends State<NwMapThumb> {
  Uint8List? _png;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final png = await widget.load();
      if (mounted && png.isNotEmpty) setState(() => _png = png);
    } on Object {
      // 没有图：不摆缩略，入口照旧
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final png = _png;
    return Material(
        color: c.surface,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Nw.r12), side: BorderSide(color: c.outline)),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
            key: widget.tapKey,
            onTap: widget.onTap,
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              if (png != null)
                Container(
                    height: widget.height,
                    color: c.surfaceVariant,
                    // 只有栅格底图过深色滤镜（跟地图栏同一个）；浅色原样
                    child: nwMapRaster(
                        context,
                        Image.memory(png,
                            fit: BoxFit.cover,
                            gaplessPlayback: true,
                            filterQuality: FilterQuality.medium,
                            errorBuilder: (_, _, _) => const SizedBox.shrink()),
                        darkKey: NwMapThumb.darkRasterKey)),
              ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: 48),
                  child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
                      child: Row(children: [
                        Icon(Icons.location_on_rounded, size: 20, color: c.textSecondary),
                        const SizedBox(width: Nw.s8),
                        Expanded(
                            child: Text(widget.label,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: NwText.bodySmall.copyWith(color: c.textSecondary))),
                        const SizedBox(width: Nw.s8),
                        Text(widget.action, style: NwText.labelLarge.copyWith(color: c.text)),
                        Icon(Icons.chevron_right_rounded, size: 24, color: c.textSecondary),
                      ]))),
            ])));
  }
}
