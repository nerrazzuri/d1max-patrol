/// 专业版主题（视觉规范 B0 修订版）：中性深灰，**颜色只给报警**（P1 红、P2 琥珀）和选中（蓝）。
/// 没有品牌色、渐变、阴影；正常状态不上色。颜色、尺寸都来自 `design/tokens.json`（生成的常量）。
library;

import 'package:flutter/material.dart';

import '../design/tokens.g.dart';

const String fontSans = 'Atkinson Hyperlegible Next';
const String fontMono = 'Atkinson Hyperlegible Mono';

/// 中文字形不在 Atkinson 里：落到系统的中文字体。
const List<String> fontFallback = <String>['Noto Sans SC', 'Microsoft YaHei UI', 'PingFang SC'];

TextStyle _t(({double size, double line, int weight, bool mono}) t, {Color color = D1Color.text}) => TextStyle(
    fontFamily: t.mono ? fontMono : fontSans,
    fontFamilyFallback: fontFallback,
    fontSize: t.size,
    height: t.line / t.size,
    fontWeight: FontWeight.values[(t.weight ~/ 100) - 1],
    color: color);

/// 规范里有名字的几种字（页面里直接用）。
abstract final class D1Text {
  static final TextStyle osd = _t(D1Type.osd);
  static final TextStyle caption = _t(D1Type.caption, color: D1Color.textSecondary);
  static final TextStyle label = _t(D1Type.label);
  static final TextStyle body = _t(D1Type.body);
  static final TextStyle heading = _t(D1Type.heading);
  static final TextStyle unitId = _t(D1Type.unitId);
  static final TextStyle title = _t(D1Type.title);
  static final TextStyle clock = _t(D1Type.clock);
  static final TextStyle phoneTitle = _t(D1Type.phoneTitle);
}

ThemeData proTheme() {
  const scheme = ColorScheme(
    brightness: Brightness.dark,
    primary: D1Color.primary,
    onPrimary: D1Color.onPrimary,
    secondary: D1Color.select,
    onSecondary: D1Color.onPrimary,
    secondaryContainer: D1Color.selectBg,
    onSecondaryContainer: D1Color.text,
    error: D1Color.p1Text,
    onError: D1Color.onPrimary,
    errorContainer: D1Color.p1Bg,
    onErrorContainer: D1Color.text,
    surface: D1Color.bg,
    onSurface: D1Color.text,
    onSurfaceVariant: D1Color.textSecondary,
    surfaceContainerLowest: D1Color.well,
    surfaceContainerLow: D1Color.surface,
    surfaceContainer: D1Color.surface,
    surfaceContainerHigh: D1Color.raised,
    surfaceContainerHighest: D1Color.raised,
    outline: D1Color.borderControl,
    outlineVariant: D1Color.rule,
    inverseSurface: D1Color.raised,
    onInverseSurface: D1Color.text,
    inversePrimary: D1Color.select,
    surfaceTint: Colors.transparent,
    shadow: Colors.transparent,
  );
  final control = RoundedRectangleBorder(borderRadius: BorderRadius.circular(D1Radius.control));
  const minButton = Size(D1Size.touch, D1Size.touch);
  final text = TextTheme(
    bodyLarge: D1Text.body,
    bodyMedium: D1Text.body,
    bodySmall: D1Text.caption,
    titleLarge: D1Text.title,
    titleMedium: D1Text.heading,
    titleSmall: D1Text.label,
    labelLarge: D1Text.label,
    labelMedium: D1Text.label,
    labelSmall: D1Text.caption,
    headlineSmall: D1Text.phoneTitle,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: D1Color.bg,
    fontFamily: fontSans,
    fontFamilyFallback: fontFallback,
    textTheme: text,
    splashFactory: NoSplash.splashFactory,
    dividerTheme: const DividerThemeData(color: D1Color.rule, thickness: 1, space: 1),
    appBarTheme: AppBarTheme(
        backgroundColor: D1Color.surface,
        foregroundColor: D1Color.text,
        elevation: 0,
        scrolledUnderElevation: 0,
        toolbarHeight: D1Size.topbar,
        titleTextStyle: D1Text.title),
    cardTheme: CardThemeData(
        color: D1Color.surface,
        elevation: 0,
        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(D1Radius.unit), side: const BorderSide(color: D1Color.rule))),
    listTileTheme: ListTileThemeData(
        iconColor: D1Color.textSecondary,
        textColor: D1Color.text,
        titleTextStyle: D1Text.body,
        subtitleTextStyle: D1Text.caption),
    filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
            backgroundColor: D1Color.primary,
            foregroundColor: D1Color.onPrimary,
            disabledBackgroundColor: D1Color.raised,
            disabledForegroundColor: D1Color.textDisabled,
            minimumSize: minButton,
            textStyle: D1Text.label,
            shape: control)),
    outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
            foregroundColor: D1Color.text,
            disabledForegroundColor: D1Color.textDisabled,
            side: const BorderSide(color: D1Color.borderControl),
            minimumSize: minButton,
            textStyle: D1Text.label,
            shape: control)),
    textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
            foregroundColor: D1Color.select, minimumSize: minButton, textStyle: D1Text.label, shape: control)),
    iconButtonTheme: IconButtonThemeData(style: IconButton.styleFrom(foregroundColor: D1Color.text)),
    floatingActionButtonTheme: FloatingActionButtonThemeData(
        backgroundColor: D1Color.primary, foregroundColor: D1Color.onPrimary, elevation: 0, shape: control),
    inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: D1Color.surface,
        labelStyle: D1Text.caption,
        floatingLabelStyle: D1Text.caption.copyWith(color: D1Color.select),
        border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(D1Radius.control),
            borderSide: const BorderSide(color: D1Color.borderControl)),
        enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(D1Radius.control),
            borderSide: const BorderSide(color: D1Color.borderControl)),
        focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(D1Radius.control),
            borderSide: const BorderSide(color: D1Color.select, width: 2))),
    dialogTheme: DialogThemeData(
        backgroundColor: D1Color.raised,
        elevation: 0,
        titleTextStyle: D1Text.title,
        contentTextStyle: D1Text.body,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(D1Radius.dialog))),
    snackBarTheme: SnackBarThemeData(
        backgroundColor: D1Color.raised,
        contentTextStyle: D1Text.body,
        behavior: SnackBarBehavior.floating,
        elevation: 0,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(D1Radius.control), side: const BorderSide(color: D1Color.rule))),
    chipTheme: ChipThemeData(
        backgroundColor: D1Color.surface,
        selectedColor: D1Color.selectBg,
        labelStyle: D1Text.label,
        side: const BorderSide(color: D1Color.borderControl),
        shape: control),
    switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
            (s) => s.contains(WidgetState.selected) ? D1Color.onPrimary : D1Color.textMuted),
        trackColor: WidgetStateProperty.resolveWith(
            (s) => s.contains(WidgetState.selected) ? D1Color.select : D1Color.raised),
        trackOutlineColor: const WidgetStatePropertyAll(D1Color.borderControl)),
    progressIndicatorTheme:
        const ProgressIndicatorThemeData(color: D1Color.text, linearTrackColor: D1Color.raised),
    popupMenuTheme: PopupMenuThemeData(color: D1Color.raised, elevation: 0, textStyle: D1Text.body),
    bottomSheetTheme: const BottomSheetThemeData(backgroundColor: D1Color.raised, elevation: 0),
    tabBarTheme: TabBarThemeData(
        labelColor: D1Color.text,
        unselectedLabelColor: D1Color.textMuted,
        indicatorColor: D1Color.select,
        labelStyle: D1Text.label,
        unselectedLabelStyle: D1Text.label),
  );
}
