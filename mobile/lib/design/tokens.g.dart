// 生成的文件,别手改:改 design/ 下的 JSON,再跑 python3 tools/gen_mobile_design.py。

import 'dart:ui' show Color;

const int tokensVersion = 2;

/// 颜色(``design/tokens.json`` 的 color)。
abstract final class D1Color {
  static const Color well = Color(0xFF000000);
  static const Color bg = Color(0xFF1C1D1F);
  static const Color surface = Color(0xFF252628);
  static const Color raised = Color(0xFF303134);
  static const Color rule = Color(0xFF3C3D40);
  static const Color hatch = Color(0xFF2C2D30);
  static const Color borderControl = Color(0xFF85878C);
  static const Color text = Color(0xFFE2E2E2);
  static const Color textSecondary = Color(0xFFC0C0C0);
  static const Color textMuted = Color(0xFFA0A0A0);
  static const Color textDisabled = Color(0xFF6A6A6A);
  static const Color primary = Color(0xFFE2E2E2);
  static const Color onPrimary = Color(0xFF1C1D1F);
  static const Color onSolid = Color(0xFFFFFFFF);
  static const Color select = Color(0xFF8FBAF5);
  static const Color selectBg = Color(0xFF18273D);
  static const Color p1 = Color(0xFFC42B2B);
  static const Color p1Text = Color(0xFFFF8073);
  static const Color p1Frame = Color(0xFFFF5A4C);
  static const Color p1Bg = Color(0xFF3A1618);
  static const Color p1Soft = Color(0xFFFFC9C2);
  static const Color p2 = Color(0xFFEDB443);
  static const Color p2Bg = Color(0xFF33290F);
  static const Color noGo = Color(0xFF3A1C20);
  static const Color noGoBorder = Color(0xFFAD4A52);
  static const Color noGoText = Color(0xFFC77B80);
}

/// 尺寸(size),逻辑像素。
abstract final class D1Size {
  static const double touch = 44.0;
  static const double button = 40.0;
  static const double buttonPhone = 52.0;
  static const double topbar = 56.0;
  static const double jumpStrip = 40.0;
  static const double unitStrip = 32.0;
  static const double unitOffline = 48.0;
  static const double alarmHeader = 40.0;
  static const double alarmActions = 56.0;
  static const double alarmFrame = 3.0;
  static const double alarmFrameReduced = 4.0;
  static const double queueOpen = 168.0;
  static const double queueClosed = 40.0;
  static const double unitMinWidth = 640.0;
  static const double gutter = 16.0;
  static const double stopAllRing = 2.0;
}

/// 圆角(radius)。
abstract final class D1Radius {
  static const double video = 0.0;
  static const double unit = 4.0;
  static const double control = 4.0;
  static const double dialog = 8.0;
}

/// 字号、行高、字重(type);family 是 mono 的用等宽字体。
abstract final class D1Type {
  static const ({double size, double line, int weight, bool mono}) osd = (size: 14.0, line: 18.0, weight: 600, mono: true);
  static const ({double size, double line, int weight, bool mono}) caption = (size: 14.0, line: 20.0, weight: 400, mono: false);
  static const ({double size, double line, int weight, bool mono}) label = (size: 14.0, line: 20.0, weight: 600, mono: false);
  static const ({double size, double line, int weight, bool mono}) body = (size: 16.0, line: 24.0, weight: 400, mono: false);
  static const ({double size, double line, int weight, bool mono}) heading = (size: 18.0, line: 24.0, weight: 600, mono: false);
  static const ({double size, double line, int weight, bool mono}) unitId = (size: 18.0, line: 24.0, weight: 700, mono: false);
  static const ({double size, double line, int weight, bool mono}) title = (size: 21.0, line: 28.0, weight: 700, mono: false);
  static const ({double size, double line, int weight, bool mono}) clock = (size: 21.0, line: 28.0, weight: 600, mono: true);
  static const ({double size, double line, int weight, bool mono}) phoneTitle = (size: 24.0, line: 30.0, weight: 700, mono: false);
}

/// 动效(motion),毫秒。
abstract final class D1Motion {
  static const int fast = 120;
  static const int p1FlashMs = 1600;
  static const double p1FlashMinOpacity = 0.4;
}
