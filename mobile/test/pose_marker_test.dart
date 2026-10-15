// 手机上的点位标记跟网页一个样子（2026-10-15 用户：手机还是老图标）：圆底座 + 朝向三角尖，比例同 posemarker.tsx。
import 'dart:math' as math;

import 'package:d1max_patrol/ui/pose_marker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('三角尖跟网页一样：底边在底座边上 ±42°、0.92 r，尖端 2.15 r，指着朝向', () {
    const at = Offset(100, 100);
    final n = PoseMarkerPainter.nose(at, 0, 10);
    expect(n[1].dx, closeTo(121.5, 1e-9));
    expect(n[1].dy, closeTo(100, 1e-9));
    for (final b in [n[0], n[2]]) {
      expect((b - at).distance, closeTo(9.2, 1e-9));
      expect(((b - at).direction).abs(), closeTo(42 * math.pi / 180, 1e-9));
    }
    // 画布 y 朝下：朝向 -π/2 = 屏上朝上
    final up = PoseMarkerPainter.nose(at, -math.pi / 2, 10)[1];
    expect(up.dx, closeTo(100, 1e-9));
    expect(up.dy, closeTo(78.5, 1e-9));
  });

  testWidgets('画得出来：有字、没字、选中', (t) async {
    for (final (label, sel) in [('3', false), (null, true), ('12', true)]) {
      await t.pumpWidget(Center(
          child: SizedBox(
              width: 80,
              height: 80,
              child: CustomPaint(
                  painter: PoseMarkerPainter(
                      at: const Offset(40, 40),
                      heading: 1,
                      r: 11,
                      label: label,
                      selected: sel,
                      accent: Colors.teal,
                      surface: Colors.white,
                      text: Colors.black)))));
      expect(t.takeException(), isNull);
    }
  });
}
