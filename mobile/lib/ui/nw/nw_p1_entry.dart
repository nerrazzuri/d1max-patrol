/// 进入的几页（选站点、登录、加站点、扫码）合用的小件：带标签的输入框、放进 `Scaffold.appBar` 的页头。
/// 输入框的样子取自设计稿的 `TextField` 组件（标签在框上面、等宽小字；框有底、细边），颜色从上下文取
/// —— 不带我们的主题也是这个样子。
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'nw_theme.dart';
import 'nw_widgets.dart';

/// 把 [NwHeader] 放进 `Scaffold.appBar`（要一个事先知道的高度）。放在那儿而不是放在 body 的 `Column`
/// 顶上，是因为横屏弹键盘时 body 可以只剩零高：`Column` 里的页头会溢出，`appBar` 那一格不会。
/// 高度按页头有几行字、系统字号放大了多少算。
PreferredSizeWidget nwHeaderBar(BuildContext context, NwHeader header) {
  final scaler = MediaQuery.textScalerOf(context);
  double line(TextStyle s) => (scaler.scale(s.fontSize!) * s.height!).ceilToDouble();
  final text = (header.overline == null ? 0.0 : line(NwText.labelSmall)) +
      line(NwText.headlineSmall) +
      (header.subtitle == null ? 0.0 : line(NwText.bodySmall));
  // 上下各 8 的内距、底下 1 的线（见 NwHeader）
  return PreferredSize(preferredSize: Size.fromHeight(2 * Nw.s8 + math.max(Nw.touch, text) + 1), child: header);
}

/// 标签在上、输入框在下。里面是**标准的 `TextField`**（测试和无障碍按类型、按 Key 认它）：
/// 要给它安 Key 用 [fieldKey]，`key` 是安在外面这一层上的。
class NwField extends StatelessWidget {
  final Key? fieldKey;

  /// 框上面那一行字；不给就只有框（对话框里标题已经说了是什么）。
  final String? label;
  final TextEditingController controller;
  final String? hint;
  final IconData? icon;
  final bool obscureText;
  final bool autofocus;
  final bool autocorrect;
  final bool enableSuggestions;
  final bool enabled;

  /// 等宽字（指纹、配对码这种要一个字一个字对的）。
  final bool mono;

  /// 这一栏有错：红边。
  final bool error;
  final TextInputType? keyboardType;
  final TextInputAction? textInputAction;
  final int minLines;
  final int maxLines;
  final ValueChanged<String>? onSubmitted;

  const NwField(
      {super.key,
      this.fieldKey,
      this.label,
      required this.controller,
      this.hint,
      this.icon,
      this.obscureText = false,
      this.autofocus = false,
      this.autocorrect = true,
      this.enableSuggestions = true,
      this.enabled = true,
      this.mono = false,
      this.error = false,
      this.keyboardType,
      this.textInputAction,
      this.minLines = 1,
      this.maxLines = 1,
      this.onSubmitted});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    // 框的边要认得出是个框（非文字 3:1）：用最淡的字色，不用卡片那条淡边（跟 nwTheme 一个规矩）。
    OutlineInputBorder border(Color color, [double width = 1]) => OutlineInputBorder(
        borderRadius: BorderRadius.circular(Nw.r8), borderSide: BorderSide(color: color, width: width));
    final idle = border(error ? c.error : c.textMuted);
    final label = this.label;
    return Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (label != null) ...[
        // 标签念给读屏的那一份挂在输入框上（下面的 Semantics），这一行只是给眼睛看的
        ExcludeSemantics(
            child: Text(label,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: NwText.labelMedium.copyWith(color: error ? c.error : c.textSecondary))),
        const SizedBox(height: Nw.s8),
      ],
      Semantics(
          label: label,
          child: TextField(
              key: fieldKey,
              controller: controller,
              obscureText: obscureText,
              autofocus: autofocus,
              autocorrect: autocorrect,
              enableSuggestions: enableSuggestions,
              enabled: enabled,
              keyboardType: keyboardType,
              textInputAction: textInputAction,
              minLines: minLines,
              maxLines: maxLines,
              onSubmitted: onSubmitted,
              cursorColor: c.tertiary,
              style: (mono ? NwText.labelLarge : NwText.bodyLarge).copyWith(color: c.text),
              decoration: InputDecoration(
                  isDense: true,
                  filled: true,
                  fillColor: c.surface,
                  hintText: hint,
                  hintStyle: NwText.bodyMedium.copyWith(color: c.textMuted),
                  prefixIcon: icon == null ? null : Icon(icon, size: 20, color: c.textSecondary),
                  contentPadding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: 12),
                  border: idle,
                  enabledBorder: idle,
                  disabledBorder: border(c.outline),
                  focusedBorder: border(error ? c.error : c.tertiary, 2)))),
    ]);
  }
}
