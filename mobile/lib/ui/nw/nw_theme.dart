/// 新设计系统（2026-10-14 按设计稿重做）的主题：颜色**从上下文取**、跟着浅色 / 深色走，不再是写死的常量。
/// 数值都来自 `design/mobile.json`（生成的 `design/nw_tokens.g.dart`），对比度的规矩在
/// `tests/test_design_mobile_tokens.py`。
///
/// 用法：`final c = Nw.of(context);` 然后 `c.surface`、`c.error`……字用 `NwText.bodyMedium`
/// （不带颜色，颜色自己配）。老的专业版主题和它写死的颜色常量 2026-10-14 收尾时删了。
library;

import 'package:flutter/material.dart';

import '../../design/nw_tokens.g.dart';

/// 一套颜色。浅色、深色各一份定值；挂在 `ThemeData.extensions` 上。
class NwColors extends ThemeExtension<NwColors> {
  final Color primary, onPrimary, secondary, tertiary;

  /// 卡片的边、分隔线（设计稿的 alternate）。压页面底只有 1.5:1 上下：**不拿来写字**。
  final Color outline;
  final Color text, textSecondary;

  /// 最淡的还读得出的字（设计稿的 accent3，调过深浅）：压 [bg]、[surface] 够 4.5，压 [surfaceVariant] 不够。
  final Color textMuted;
  final Color bg, surface, surfaceVariant;
  final Color success, onSuccess, warning, onWarning, error, onError, info, onInfo;

  /// 同色 12% 压在 [surface] 上的底：药丸、图标格、提示条。上面写同色的字或者正文色的字都够。
  final Color successSoft, warningSoft, errorSoft, infoSoft;

  /// P1 卡片的 2px 框。
  final Color errorFrame;

  /// 压在视频上的半透明底，和它上面的字。
  final Color scrim, onScrim;

  const NwColors({
    required this.primary,
    required this.onPrimary,
    required this.secondary,
    required this.tertiary,
    required this.outline,
    required this.text,
    required this.textSecondary,
    required this.textMuted,
    required this.bg,
    required this.surface,
    required this.surfaceVariant,
    required this.success,
    required this.onSuccess,
    required this.warning,
    required this.onWarning,
    required this.error,
    required this.onError,
    required this.info,
    required this.onInfo,
    required this.successSoft,
    required this.warningSoft,
    required this.errorSoft,
    required this.infoSoft,
    required this.errorFrame,
    required this.scrim,
    required this.onScrim,
  });

  static const NwColors light = NwColors(
    primary: NwLight.primary,
    onPrimary: NwLight.onPrimary,
    secondary: NwLight.secondary,
    tertiary: NwLight.tertiary,
    outline: NwLight.outline,
    text: NwLight.text,
    textSecondary: NwLight.textSecondary,
    textMuted: NwLight.textMuted,
    bg: NwLight.bg,
    surface: NwLight.surface,
    surfaceVariant: NwLight.surfaceVariant,
    success: NwLight.success,
    onSuccess: NwLight.onSuccess,
    warning: NwLight.warning,
    onWarning: NwLight.onWarning,
    error: NwLight.error,
    onError: NwLight.onError,
    info: NwLight.info,
    onInfo: NwLight.onInfo,
    successSoft: NwLight.successSoft,
    warningSoft: NwLight.warningSoft,
    errorSoft: NwLight.errorSoft,
    infoSoft: NwLight.infoSoft,
    errorFrame: NwLight.errorFrame,
    scrim: NwLight.scrim,
    onScrim: NwLight.onScrim,
  );

  static const NwColors dark = NwColors(
    primary: NwDark.primary,
    onPrimary: NwDark.onPrimary,
    secondary: NwDark.secondary,
    tertiary: NwDark.tertiary,
    outline: NwDark.outline,
    text: NwDark.text,
    textSecondary: NwDark.textSecondary,
    textMuted: NwDark.textMuted,
    bg: NwDark.bg,
    surface: NwDark.surface,
    surfaceVariant: NwDark.surfaceVariant,
    success: NwDark.success,
    onSuccess: NwDark.onSuccess,
    warning: NwDark.warning,
    onWarning: NwDark.onWarning,
    error: NwDark.error,
    onError: NwDark.onError,
    info: NwDark.info,
    onInfo: NwDark.onInfo,
    successSoft: NwDark.successSoft,
    warningSoft: NwDark.warningSoft,
    errorSoft: NwDark.errorSoft,
    infoSoft: NwDark.infoSoft,
    errorFrame: NwDark.errorFrame,
    scrim: NwDark.scrim,
    onScrim: NwDark.onScrim,
  );

  List<Color> get _all => <Color>[
        primary, onPrimary, secondary, tertiary, outline, text, textSecondary, textMuted, bg, surface,
        surfaceVariant, success, onSuccess, warning, onWarning, error, onError, info, onInfo,
        successSoft, warningSoft, errorSoft, infoSoft, errorFrame, scrim, onScrim, //
      ];

  factory NwColors._from(List<Color> c) => NwColors(
        primary: c[0],
        onPrimary: c[1],
        secondary: c[2],
        tertiary: c[3],
        outline: c[4],
        text: c[5],
        textSecondary: c[6],
        textMuted: c[7],
        bg: c[8],
        surface: c[9],
        surfaceVariant: c[10],
        success: c[11],
        onSuccess: c[12],
        warning: c[13],
        onWarning: c[14],
        error: c[15],
        onError: c[16],
        info: c[17],
        onInfo: c[18],
        successSoft: c[19],
        warningSoft: c[20],
        errorSoft: c[21],
        infoSoft: c[22],
        errorFrame: c[23],
        scrim: c[24],
        onScrim: c[25],
      );

  /// 两套都是定值，没有单改一个色的用法。
  @override
  NwColors copyWith() => this;

  /// 系统切浅色 / 深色时主题有一小段过渡：每个色各自渐变过去。
  @override
  NwColors lerp(NwColors? other, double t) {
    if (other == null) return this;
    final a = _all, b = other._all;
    return NwColors._from(<Color>[for (var i = 0; i < a.length; i++) Color.lerp(a[i], b[i], t)!]);
  }
}

/// 取颜色，外加间距、圆角那几档。
abstract final class Nw {
  /// 当前上下文的那一套颜色。**上下文里没有我们的主题也不抛**（大多数测试是光秃秃的
  /// `MaterialApp`）：按那个主题是亮是暗，回浅色或深色那一套。
  static NwColors of(BuildContext c) {
    final theme = Theme.of(c);
    return theme.extension<NwColors>() ??
        (theme.brightness == Brightness.dark ? NwColors.dark : NwColors.light);
  }

  static const double s4 = NwSpace.s4, s8 = NwSpace.s8, s16 = NwSpace.s16, s24 = NwSpace.s24, s32 = NwSpace.s32;
  static const double r2 = NwRadius.r2, r4 = NwRadius.r4, r8 = NwRadius.r8, r12 = NwRadius.r12, rPill = NwRadius.rPill;

  /// 手指点得准的最小尺寸。
  static const double touch = 44;
}

/// 有名字的几种字（设计稿的比例，最小 12）。**不带颜色**：用的地方 `.copyWith(color: …)`。
/// 标题 Space Grotesk，正文和小标题 Inter，标签（编号、状态、按钮）JetBrains Mono；中文落到系统字体。
abstract final class NwText {
  static const TextStyle headlineMedium = NwType.headlineMedium, headlineSmall = NwType.headlineSmall;
  static const TextStyle titleLarge = NwType.titleLarge, titleMedium = NwType.titleMedium, titleSmall = NwType.titleSmall;
  static const TextStyle labelLarge = NwType.labelLarge, labelMedium = NwType.labelMedium, labelSmall = NwType.labelSmall;
  static const TextStyle bodyLarge = NwType.bodyLarge, bodyMedium = NwType.bodyMedium, bodySmall = NwType.bodySmall;

  static const List<TextStyle> all = <TextStyle>[
    headlineMedium, headlineSmall, titleLarge, titleMedium, titleSmall, labelLarge, labelMedium, labelSmall,
    bodyLarge, bodyMedium, bodySmall, //
  ];
}

/// 整个 App 的主题：浅色、深色各一份（`MaterialApp.theme` / `darkTheme`，跟随系统）。
/// 没有阴影、没有水波；标准控件（按钮、输入框、对话框……）也照设计稿的颜色和圆角，
/// 这样还没换成新组件的老页面先有个大样。
ThemeData nwTheme(Brightness b) {
  final c = b == Brightness.dark ? NwColors.dark : NwColors.light;
  final scheme = ColorScheme(
    brightness: b,
    primary: c.primary,
    onPrimary: c.onPrimary,
    secondary: c.secondary,
    onSecondary: c.onPrimary,
    secondaryContainer: c.surfaceVariant,
    onSecondaryContainer: c.text,
    tertiary: c.tertiary,
    onTertiary: c.onPrimary,
    error: c.error,
    onError: c.onError,
    errorContainer: c.errorSoft,
    onErrorContainer: c.text,
    surface: c.bg,
    onSurface: c.text,
    onSurfaceVariant: c.textSecondary,
    surfaceContainerLowest: c.bg,
    surfaceContainerLow: c.surface,
    surfaceContainer: c.surface,
    surfaceContainerHigh: c.surface,
    surfaceContainerHighest: c.surfaceVariant,
    outline: c.textMuted,
    outlineVariant: c.outline,
    inverseSurface: c.text,
    onInverseSurface: c.bg,
    inversePrimary: c.tertiary,
    surfaceTint: Colors.transparent,
    shadow: Colors.transparent,
  );
  TextStyle on(TextStyle s, [Color? color]) => s.copyWith(color: color ?? c.text);
  final control = RoundedRectangleBorder(borderRadius: BorderRadius.circular(Nw.r8));
  final card = RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(Nw.r12), side: BorderSide(color: c.outline));
  const minButton = Size(Nw.touch, Nw.touch);
  // 输入框的边要认得出是个框（非文字 3:1）：用最淡的字色，不用卡片那条淡边。
  OutlineInputBorder field(Color color, [double width = 1]) => OutlineInputBorder(
      borderRadius: BorderRadius.circular(Nw.r8), borderSide: BorderSide(color: color, width: width));
  return ThemeData(
    useMaterial3: true,
    brightness: b,
    colorScheme: scheme,
    extensions: <ThemeExtension<dynamic>>[c],
    scaffoldBackgroundColor: c.bg,
    fontFamily: NwFont.sans,
    fontFamilyFallback: NwFont.fallback,
    textTheme: TextTheme(
      headlineMedium: on(NwText.headlineMedium),
      headlineSmall: on(NwText.headlineSmall),
      titleLarge: on(NwText.titleLarge),
      titleMedium: on(NwText.titleMedium),
      titleSmall: on(NwText.titleSmall),
      labelLarge: on(NwText.labelLarge),
      labelMedium: on(NwText.labelMedium, c.textSecondary),
      labelSmall: on(NwText.labelSmall, c.textSecondary),
      bodyLarge: on(NwText.bodyLarge),
      bodyMedium: on(NwText.bodyMedium),
      bodySmall: on(NwText.bodySmall, c.textSecondary),
    ),
    splashFactory: NoSplash.splashFactory,
    dividerTheme: DividerThemeData(color: c.outline, thickness: 1, space: 1),
    iconTheme: IconThemeData(color: c.text, size: 24),
    appBarTheme: AppBarTheme(
        backgroundColor: c.surface,
        foregroundColor: c.text,
        elevation: 0,
        scrolledUnderElevation: 0,
        toolbarHeight: 56,
        shape: Border(bottom: BorderSide(color: c.outline)),
        titleTextStyle: on(NwText.titleLarge)),
    cardTheme: CardThemeData(
        color: c.surface,
        elevation: 0,
        margin: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s4),
        shape: card),
    listTileTheme: ListTileThemeData(
        iconColor: c.textSecondary,
        textColor: c.text,
        titleTextStyle: on(NwText.bodyLarge),
        subtitleTextStyle: on(NwText.bodyMedium, c.textSecondary)),
    filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
            backgroundColor: c.primary,
            foregroundColor: c.onPrimary,
            disabledBackgroundColor: c.surfaceVariant,
            disabledForegroundColor: c.textMuted,
            minimumSize: minButton,
            textStyle: NwText.labelLarge,
            shape: control)),
    outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
            foregroundColor: c.text,
            disabledForegroundColor: c.textMuted,
            side: BorderSide(color: c.outline),
            minimumSize: minButton,
            textStyle: NwText.labelLarge,
            shape: control)),
    textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
            foregroundColor: c.tertiary, minimumSize: minButton, textStyle: NwText.labelLarge, shape: control)),
    iconButtonTheme: IconButtonThemeData(style: IconButton.styleFrom(foregroundColor: c.text)),
    floatingActionButtonTheme: FloatingActionButtonThemeData(
        backgroundColor: c.primary,
        foregroundColor: c.onPrimary,
        elevation: 0,
        focusElevation: 0,
        hoverElevation: 0,
        highlightElevation: 0,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(Nw.r12))),
    inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: c.surface,
        labelStyle: on(NwText.bodyMedium, c.textSecondary),
        floatingLabelStyle: on(NwText.bodyMedium, c.tertiary),
        hintStyle: on(NwText.bodyMedium, c.textMuted),
        border: field(c.textMuted),
        enabledBorder: field(c.textMuted),
        focusedBorder: field(c.tertiary, 2),
        errorBorder: field(c.error),
        focusedErrorBorder: field(c.error, 2)),
    dialogTheme: DialogThemeData(
        backgroundColor: c.surface,
        elevation: 0,
        titleTextStyle: on(NwText.titleLarge),
        contentTextStyle: on(NwText.bodyMedium),
        shape: card),
    snackBarTheme: SnackBarThemeData(
        backgroundColor: c.text,
        contentTextStyle: on(NwText.bodyMedium, c.bg),
        actionTextColor: c.bg,
        behavior: SnackBarBehavior.floating,
        elevation: 0,
        shape: control),
    chipTheme: ChipThemeData(
        backgroundColor: c.surface,
        selectedColor: c.surfaceVariant,
        labelStyle: on(NwText.labelMedium),
        side: BorderSide(color: c.outline),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(Nw.rPill))),
    segmentedButtonTheme: SegmentedButtonThemeData(
        style: ButtonStyle(
            shape: WidgetStatePropertyAll(control),
            side: WidgetStatePropertyAll(BorderSide(color: c.outline)),
            textStyle: const WidgetStatePropertyAll(NwText.labelMedium),
            foregroundColor: WidgetStateProperty.resolveWith(
                (s) => s.contains(WidgetState.selected) ? c.text : c.textSecondary),
            backgroundColor: WidgetStateProperty.resolveWith(
                (s) => s.contains(WidgetState.selected) ? c.surfaceVariant : Colors.transparent))),
    switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
            (s) => s.contains(WidgetState.selected) ? c.onPrimary : c.textMuted),
        trackColor: WidgetStateProperty.resolveWith(
            (s) => s.contains(WidgetState.selected) ? c.primary : c.surfaceVariant),
        trackOutlineColor: WidgetStatePropertyAll(c.textMuted)),
    progressIndicatorTheme: ProgressIndicatorThemeData(color: c.primary, linearTrackColor: c.surfaceVariant),
    popupMenuTheme: PopupMenuThemeData(
        color: c.surface, elevation: 0, textStyle: on(NwText.bodyMedium), shape: card),
    bottomSheetTheme: BottomSheetThemeData(backgroundColor: c.surface, elevation: 0),
    tabBarTheme: TabBarThemeData(
        labelColor: c.text,
        unselectedLabelColor: c.textSecondary,
        indicatorColor: c.primary,
        labelStyle: NwText.labelMedium,
        unselectedLabelStyle: NwText.labelMedium),
  );
}
