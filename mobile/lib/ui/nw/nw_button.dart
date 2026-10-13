/// 新设计系统的按钮：五种样子 × 三种大小。里面是**标准按钮**（`FilledButton` / `OutlinedButton` /
/// `TextButton`），测试和无障碍按类型、按 Key 认它 —— 要给里面那个按钮安 Key 用 [NwButton.buttonKey]，
/// `key` 是安在外面这一层上的。
library;

import 'package:flutter/material.dart';

import 'nw_theme.dart';

enum NwButtonVariant {
  /// 实底、主色：这一屏最要紧的那一个。
  primary,

  /// 实底、次色。
  secondary,

  /// 透明底、细边。
  outline,

  /// 透明底、没边。
  ghost,

  /// 实底、红：只给急停、叫停、删除这一类。
  destructive,
}

enum NwButtonSize { small, medium, large }

class NwButton extends StatelessWidget {
  /// 安在里面那个标准按钮上的 Key（primary / secondary / destructive 是 `FilledButton`，
  /// outline 是 `OutlinedButton`，ghost 是 `TextButton`）。
  final Key? buttonKey;
  final String label;
  final IconData? icon;

  /// 不给就是禁用的样子。
  final VoidCallback? onPressed;
  final NwButtonVariant variant;
  final NwButtonSize size;

  /// 撑满所在的那一行。
  final bool fullWidth;

  const NwButton(
      {super.key,
      this.buttonKey,
      required this.label,
      this.icon,
      this.onPressed,
      this.variant = NwButtonVariant.primary,
      this.size = NwButtonSize.medium,
      this.fullWidth = false});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    // 设计稿的内距（横 / 竖）：16/4、24/8、32/16；高度另外给个下限，小号也不至于点不中。
    final (double padH, double padV, double radius, double minH, double iconSize, TextStyle text) = switch (size) {
      NwButtonSize.small => (Nw.s16, Nw.s4, Nw.r4, 32, 16, NwText.labelMedium),
      NwButtonSize.medium => (Nw.s24, Nw.s8, Nw.r8, 40, 18, NwText.labelMedium),
      NwButtonSize.large => (Nw.s32, Nw.s16, Nw.r12, 52, 20, NwText.labelLarge),
    };
    final shape = RoundedRectangleBorder(borderRadius: BorderRadius.circular(radius));
    final padding = EdgeInsets.symmetric(horizontal: padH, vertical: padV);
    final minimum = Size(Nw.touch, minH);
    final child = Row(mainAxisSize: MainAxisSize.min, children: [
      if (icon != null) ...[Icon(icon, size: iconSize), const SizedBox(width: Nw.s8)],
      // 中文、长文案：摆不下就截，不把按钮撑出屏幕
      Flexible(child: Text(label, maxLines: 1, overflow: TextOverflow.ellipsis)),
    ]);
    ButtonStyle filled(Color bg, Color fg) => FilledButton.styleFrom(
        backgroundColor: bg,
        foregroundColor: fg,
        disabledBackgroundColor: c.surfaceVariant,
        disabledForegroundColor: c.textMuted,
        padding: padding,
        minimumSize: minimum,
        textStyle: text,
        shape: shape);
    final Widget button = switch (variant) {
      NwButtonVariant.primary =>
        FilledButton(key: buttonKey, onPressed: onPressed, style: filled(c.primary, c.onPrimary), child: child),
      NwButtonVariant.secondary =>
        FilledButton(key: buttonKey, onPressed: onPressed, style: filled(c.secondary, c.onPrimary), child: child),
      NwButtonVariant.destructive =>
        FilledButton(key: buttonKey, onPressed: onPressed, style: filled(c.error, c.onError), child: child),
      NwButtonVariant.outline => OutlinedButton(
          key: buttonKey,
          onPressed: onPressed,
          style: OutlinedButton.styleFrom(
              foregroundColor: c.text,
              disabledForegroundColor: c.textMuted,
              side: BorderSide(color: c.outline),
              padding: padding,
              minimumSize: minimum,
              textStyle: text,
              shape: shape),
          child: child),
      NwButtonVariant.ghost => TextButton(
          key: buttonKey,
          onPressed: onPressed,
          style: TextButton.styleFrom(
              foregroundColor: c.primary,
              disabledForegroundColor: c.textMuted,
              padding: padding,
              minimumSize: minimum,
              textStyle: text,
              shape: shape),
          child: child),
    };
    return fullWidth ? SizedBox(width: double.infinity, child: button) : button;
  }
}
