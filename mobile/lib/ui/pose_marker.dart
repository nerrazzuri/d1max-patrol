import 'dart:math' as math;

import 'package:flutter/material.dart';

/// 地图上「一个点 + 朝向」的标记，跟网页值班台的 `posemarker.tsx` 一个样子（2026-10-15 用户：手机还是老
/// 图标——图钉 + 一根线连着的圆把手）：圆形底座 + 朝向那一侧伸出去的三角尖（导航软件那种定位箭头）。
/// 比例跟网页一样：三角尖底边压在底座边上（±42°、0.92 r），尖端伸到 2.15 r。
///
/// 画在 [at]（画布坐标）；[heading] 是画布里的方向角（弧度，y 往下，`atan2(dy, dx)`）。
/// [label] 有就写在底座中间（路线点的序号），没有画一个实心小圆点；[labelAngle] 让字转正（地图整体转过时
/// 传转角的相反数）。选中的底座涂满、外面一圈淡光晕。
class PoseMarkerPainter extends CustomPainter {
  final Offset at;
  final double heading;
  final double r;
  final String? label;
  final double labelAngle;
  final bool selected;
  /// 主色（正常 tertiary；有问题的点 error；别的路线 textSecondary）。
  final Color accent;
  final Color surface;
  final Color text;

  const PoseMarkerPainter(
      {required this.at,
      required this.heading,
      required this.r,
      required this.accent,
      required this.surface,
      required this.text,
      this.label,
      this.labelAngle = 0,
      this.selected = false});

  /// 三角尖的三个点（测试、点击区都用它）。
  static List<Offset> nose(Offset at, double heading, double r) {
    const half = 42 * math.pi / 180;
    Offset p(double a, double d) => at + Offset(math.cos(a), math.sin(a)) * d;
    return [p(heading - half, r * 0.92), p(heading, r * 2.15), p(heading + half, r * 0.92)];
  }

  @override
  void paint(Canvas canvas, Size size) {
    if (selected) {
      canvas.drawCircle(at, r + 7 * r / 11, Paint()..color = accent.withValues(alpha: 0.22));
    }
    final n = nose(at, heading, r);
    final tri = Path()
      ..moveTo(n[0].dx, n[0].dy)
      ..lineTo(n[1].dx, n[1].dy)
      ..lineTo(n[2].dx, n[2].dy)
      ..close();
    canvas.drawPath(tri, Paint()..color = accent);
    canvas.drawPath(
        tri,
        Paint()
          ..color = surface
          ..style = PaintingStyle.stroke
          ..strokeWidth = r * 1.5 / 11
          ..strokeJoin = StrokeJoin.round);
    canvas.drawCircle(at, r, Paint()..color = selected ? accent : surface);
    canvas.drawCircle(
        at,
        r,
        Paint()
          ..color = accent
          ..style = PaintingStyle.stroke
          ..strokeWidth = r * 2.5 / 11);
    final l = label;
    if (l == null) {
      canvas.drawCircle(at, r * 0.38, Paint()..color = selected ? surface : accent);
      return;
    }
    final tp = TextPainter(
        text: TextSpan(
            text: l,
            style: TextStyle(
                fontSize: r * (l.length > 2 ? 0.8 : 1.0),
                fontWeight: FontWeight.w700,
                fontFamily: 'JetBrains Mono',
                color: selected ? surface : text)),
        textDirection: TextDirection.ltr)
      ..layout();
    canvas.save();
    canvas.translate(at.dx, at.dy);
    canvas.rotate(labelAngle);
    tp.paint(canvas, Offset(-tp.width / 2, -tp.height / 2));
    canvas.restore();
  }

  @override
  bool shouldRepaint(PoseMarkerPainter old) =>
      old.at != at ||
      old.heading != heading ||
      old.r != r ||
      old.label != label ||
      old.labelAngle != labelAngle ||
      old.selected != selected ||
      old.accent != accent ||
      old.surface != surface ||
      old.text != text;
}
