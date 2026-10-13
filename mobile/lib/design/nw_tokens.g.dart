// 生成的文件,别手改:改 design/ 下的 JSON,再跑 python3 tools/gen_mobile_design.py。

import 'package:flutter/painting.dart' show Color, FontWeight, TextStyle;

const int nwTokensVersion = 1;

/// 浅色那一套颜色(``design/mobile.json`` 的 color)。
abstract final class NwLight {
  static const Color primary = Color(0xFF1A1C1E);
  static const Color onPrimary = Color(0xFFFFFFFF);
  static const Color secondary = Color(0xFF44474E);
  static const Color tertiary = Color(0xFF005FB7);
  static const Color outline = Color(0xFFC4C6D0);
  static const Color text = Color(0xFF1A1C1E);
  static const Color textSecondary = Color(0xFF44474E);
  static const Color textMuted = Color(0xFF6F727A);
  static const Color bg = Color(0xFFFDFDFD);
  static const Color surface = Color(0xFFFFFFFF);
  static const Color surfaceVariant = Color(0xFFE1E2EC);
  static const Color success = Color(0xFF2D7A31);
  static const Color onSuccess = Color(0xFFFFFFFF);
  static const Color warning = Color(0xFFAD4F01);
  static const Color onWarning = Color(0xFFFFFFFF);
  static const Color error = Color(0xFFC82D2D);
  static const Color onError = Color(0xFFFFFFFF);
  static const Color info = Color(0xFF0270AB);
  static const Color onInfo = Color(0xFFFFFFFF);
  static const Color successSoft = Color(0xFFE6EFE6);
  static const Color warningSoft = Color(0xFFF5EAE1);
  static const Color errorSoft = Color(0xFFF8E6E6);
  static const Color infoSoft = Color(0xFFE1EEF5);
  static const Color errorFrame = Color(0xFFC82D2D);
  static const Color scrim = Color(0xB3000000);
  static const Color onScrim = Color(0xFFFFFFFF);
}

/// 深色那一套颜色(``design/mobile.json`` 的 color)。
abstract final class NwDark {
  static const Color primary = Color(0xFFD1E4FF);
  static const Color onPrimary = Color(0xFF000000);
  static const Color secondary = Color(0xFFBEC6DC);
  static const Color tertiary = Color(0xFF4796E6);
  static const Color outline = Color(0xFF30353F);
  static const Color text = Color(0xFFE2E2E6);
  static const Color textSecondary = Color(0xFFA1A1A6);
  static const Color textMuted = Color(0xFF8A8D93);
  static const Color bg = Color(0xFF0A0C10);
  static const Color surface = Color(0xFF1B2028);
  static const Color surfaceVariant = Color(0xFF282E38);
  static const Color success = Color(0xFF4CAF50);
  static const Color onSuccess = Color(0xFF0A0C10);
  static const Color warning = Color(0xFFFFA726);
  static const Color onWarning = Color(0xFF0A0C10);
  static const Color error = Color(0xFFFF5757);
  static const Color onError = Color(0xFF0A0C10);
  static const Color info = Color(0xFF4FC3F7);
  static const Color onInfo = Color(0xFF0A0C10);
  static const Color successSoft = Color(0xFF21312D);
  static const Color warningSoft = Color(0xFF363028);
  static const Color errorSoft = Color(0xFF36272E);
  static const Color infoSoft = Color(0xFF213441);
  static const Color errorFrame = Color(0xFFFF5757);
  static const Color scrim = Color(0xB3000000);
  static const Color onScrim = Color(0xFFFFFFFF);
}

/// 间距(space),逻辑像素。
abstract final class NwSpace {
  static const double s4 = 4.0;
  static const double s8 = 8.0;
  static const double s16 = 16.0;
  static const double s24 = 24.0;
  static const double s32 = 32.0;
}

/// 圆角(radius)。
abstract final class NwRadius {
  static const double r2 = 2.0;
  static const double r4 = 4.0;
  static const double r8 = 8.0;
  static const double r12 = 12.0;
  static const double rPill = 9999.0;
}

/// 字体(font):标题、正文、等宽;中文落到系统字体。
abstract final class NwFont {
  static const String display = 'Space Grotesk';
  static const String sans = 'Inter';
  static const String mono = 'JetBrains Mono';
  static const List<String> fallback = <String>['Noto Sans SC', 'Microsoft YaHei UI', 'PingFang SC'];
}

/// 字(type):不带颜色。
abstract final class NwType {
  static const TextStyle headlineMedium = TextStyle(
      fontFamily: NwFont.display,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 28.0,
      height: 1.25,
      fontWeight: FontWeight.w600,
      letterSpacing: 0.0);
  static const TextStyle headlineSmall = TextStyle(
      fontFamily: NwFont.display,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 24.0,
      height: 1.3,
      fontWeight: FontWeight.w600,
      letterSpacing: 0.0);
  static const TextStyle titleLarge = TextStyle(
      fontFamily: NwFont.sans,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 20.0,
      height: 1.3,
      fontWeight: FontWeight.w600,
      letterSpacing: 0.0);
  static const TextStyle titleMedium = TextStyle(
      fontFamily: NwFont.sans,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 16.0,
      height: 1.4,
      fontWeight: FontWeight.w600,
      letterSpacing: 0.0);
  static const TextStyle titleSmall = TextStyle(
      fontFamily: NwFont.sans,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 14.0,
      height: 1.4,
      fontWeight: FontWeight.w600,
      letterSpacing: 0.0);
  static const TextStyle labelLarge = TextStyle(
      fontFamily: NwFont.mono,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 14.0,
      height: 1.2,
      fontWeight: FontWeight.w600,
      letterSpacing: 0.0);
  static const TextStyle labelMedium = TextStyle(
      fontFamily: NwFont.mono,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 12.0,
      height: 1.2,
      fontWeight: FontWeight.w600,
      letterSpacing: 0.0);
  static const TextStyle labelSmall = TextStyle(
      fontFamily: NwFont.mono,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 12.0,
      height: 1.2,
      fontWeight: FontWeight.w500,
      letterSpacing: 0.0);
  static const TextStyle bodyLarge = TextStyle(
      fontFamily: NwFont.sans,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 16.0,
      height: 1.5,
      fontWeight: FontWeight.w400,
      letterSpacing: 0.0);
  static const TextStyle bodyMedium = TextStyle(
      fontFamily: NwFont.sans,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 14.0,
      height: 1.5,
      fontWeight: FontWeight.w400,
      letterSpacing: 0.0);
  static const TextStyle bodySmall = TextStyle(
      fontFamily: NwFont.sans,
      fontFamilyFallback: NwFont.fallback,
      fontSize: 12.0,
      height: 1.4,
      fontWeight: FontWeight.w400,
      letterSpacing: 0.0);
}

/// 哪种状态用哪种色调(usage);红只给 P1 和急停类,橙只给 P2 和低电。
const Map<String, String> nwUsage = <String, String>{
  'p1': 'error',
  'estop': 'error',
  'offline': 'error',
  'destructive': 'error',
  'p2': 'warning',
  'lowBattery': 'warning',
  'online': 'success',
  'connected': 'success',
  'battery': 'success',
  'synced': 'success',
  'ok': 'success',
  'p3': 'neutral',
  'idle': 'neutral',
  'info': 'info',
};
