/// 顶栏右边的文字按钮（App V2）：屏幕够宽（横屏、平板、桌面）就一个个摆出来；竖屏的手机摆不下，
/// 收进一个「⋮」菜单。按钮的 key 两种摆法都带着（测试、无障碍按 key 找）。
library;

import 'package:flutter/material.dart';

import '../../l10n.dart';

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
  if (MediaQuery.sizeOf(context).width >= barActionsCollapseBelow) {
    return [
      for (final a in actions) TextButton(key: a.key, onPressed: a.onPressed, child: Text(a.label)),
    ];
  }
  return [
    PopupMenuButton<int>(
        key: barMoreKey,
        tooltip: tr('More', '更多'),
        icon: const Icon(Icons.more_vert),
        onSelected: (i) => actions[i].onPressed(),
        itemBuilder: (_) => [
              for (var i = 0; i < actions.length; i++)
                PopupMenuItem<int>(key: actions[i].key, value: i, child: Text(actions[i].label)),
            ]),
  ];
}
