/// 顶栏右边的文字按钮（App V2）：屏幕够宽（横屏、平板、桌面）就一个个摆出来；竖屏的手机摆不下，
/// 收进一个「⋮」菜单。按钮的 key 两种摆法都带着（测试、无障碍按 key 找）。
library;

import 'package:flutter/material.dart';

import '../../l10n.dart';
import '../nw/nw_theme.dart';

class BarAction {
  final Key? key;
  final String label;
  final VoidCallback onPressed;
  const BarAction({this.key, required this.label, required this.onPressed});
}

/// 窄于这个宽度就收进菜单（竖屏手机 360–430；横屏手机最窄 568）。
const double barActionsCollapseBelow = 520;

const Key barMoreKey = Key('bar-more');

List<Widget> barActions(BuildContext context, List<BarAction> actions) {
  if (actions.isEmpty) return const <Widget>[];
  final c = Nw.of(context);
  if (MediaQuery.sizeOf(context).width >= barActionsCollapseBelow) {
    // 顶栏上的字：正文色的等宽标签（顶栏的底是卡片色，不用强调色）
    final style = TextButton.styleFrom(
        foregroundColor: c.text, textStyle: NwText.labelMedium, minimumSize: const Size(Nw.touch, Nw.touch));
    return [
      for (final a in actions) TextButton(key: a.key, style: style, onPressed: a.onPressed, child: Text(a.label)),
    ];
  }
  return [
    PopupMenuButton<int>(
        key: barMoreKey,
        tooltip: tr('More', '更多'),
        icon: Icon(Icons.more_vert_rounded, color: c.text),
        color: c.surface,
        elevation: 0,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Nw.r12), side: BorderSide(color: c.outline)),
        onSelected: (i) => actions[i].onPressed(),
        itemBuilder: (_) => [
              for (var i = 0; i < actions.length; i++)
                PopupMenuItem<int>(
                    key: actions[i].key,
                    value: i,
                    child: Text(actions[i].label, style: NwText.bodyMedium.copyWith(color: c.text))),
            ]),
  ];
}
