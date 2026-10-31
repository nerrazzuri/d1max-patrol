/// 运行可视化（运营可视化）：地图栏上叠的规划路径（虚线）、最近轨迹（细线）、被挡的位置、定位出过问题的点。
///
/// 数据来自站点：`GET /api/runviz`（路径、轨迹、被挡；站点内存里的）、`GET /api/loc-episodes`（定位历史）。
/// 这里只画：坐标已经是「图的像素 × 放进框的比例」（跟地图栏上待命点、狗用同一个 `px()`），整层在
/// [NwMapRotation] 里，所以转角、缩放跟底图一致；线宽、点的大小除以当前缩放，放大了不变粗。
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../l10n.dart';
import 'nw/nw_p4_admin.dart' show NwLegendItem;
import 'nw/nw_theme.dart';

/// 一台狗在这张图上要画的东西（已经换成画布像素）。
class RunVizShape {
  final List<Offset> path;
  final List<Offset> trail;
  final Offset? blocked;
  const RunVizShape({this.path = const [], this.trail = const [], this.blocked});
}

/// 读 `/api/runviz` 的一份，挑出报着这张图（[mapId]:[version]）的狗，坐标换成画布像素（[px]：地图米 → 画布）。
List<RunVizShape> runVizShapes(Map<String, dynamic>? viz, String mapId, String version,
    Offset Function(double x, double y) px) {
  final robots = viz?['robots'];
  if (robots is! Map) return const [];
  Offset? pt(Object? a) => a is List && a.length >= 2 && a[0] is num && a[1] is num &&
          (a[0] as num).isFinite && (a[1] as num).isFinite
      ? px((a[0] as num).toDouble(), (a[1] as num).toDouble())
      : null;
  List<Offset> line(Object? raw) => raw is List ? [for (final p in raw) if (pt(p) != null) pt(p)!] : const [];
  final out = <RunVizShape>[];
  for (final r in robots.values) {
    if (r is! Map) continue;
    final m = r['map'];
    if (m is! Map || m['map_id'] != mapId || m['version'] != version) continue;
    final b = r['blocked'];
    Offset? blocked;
    if (b is Map && b['x'] is num && b['y'] is num) {
      blocked = px((b['x'] as num).toDouble(), (b['y'] as num).toDouble());
    }
    out.add(RunVizShape(path: line(r['path']), trail: line(r['trail']), blocked: blocked));
  }
  return out;
}

/// 定位历史里画得出来的点（有位置的）：(画布像素, 是不是「丢了」[否则「变差」])。
List<(Offset, bool)> episodeDots(List<Map<String, dynamic>> episodes, Offset Function(double x, double y) px) => [
      for (final e in episodes)
        if (e['x'] is num && e['y'] is num)
          (px((e['x'] as num).toDouble(), (e['y'] as num).toDouble()), e['kind'] == 'lost'),
    ];

class RunVizPainter extends CustomPainter {
  final List<RunVizShape> shapes;
  final List<(Offset, bool)> episodes;

  /// 当前缩放倍数（线宽、点大小除以它）。
  final double zoom;
  final Color pathColor, trailColor, warnColor, edgeColor;

  const RunVizPainter(
      {required this.shapes,
      required this.episodes,
      required this.zoom,
      required this.pathColor,
      required this.trailColor,
      required this.warnColor,
      required this.edgeColor});

  @override
  void paint(Canvas canvas, Size size) {
    final u = 1 / math.max(zoom, 1);
    final trail = Paint()
      ..color = trailColor
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.5 * u
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final path = Paint()
      ..color = pathColor
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.5 * u
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    for (final s in shapes) {
      if (s.trail.length >= 2) canvas.drawPath(Path()..addPolygon(s.trail, false), trail);
    }
    for (final s in shapes) {
      if (s.path.length >= 2) {
        _dashed(canvas, s.path, path, 7 * u, 5 * u);
        final g = s.path.last, r = 5 * u;
        canvas.drawLine(g + Offset(-r, -r), g + Offset(r, r), path);
        canvas.drawLine(g + Offset(r, -r), g + Offset(-r, r), path);
      }
    }
    final ep = Paint()
      ..color = warnColor
      ..strokeWidth = 2 * u;
    for (final (at, lost) in episodes) {
      if (lost) {
        canvas.drawCircle(at, 4 * u, ep..style = PaintingStyle.fill);
      } else {
        canvas.drawCircle(at, 4 * u, ep..style = PaintingStyle.stroke);
      }
    }
    for (final s in shapes) {
      final b = s.blocked;
      if (b == null) continue;
      final h = 9 * u;
      final tri = Path()
        ..moveTo(b.dx, b.dy - h)
        ..lineTo(b.dx + h, b.dy + h * 0.8)
        ..lineTo(b.dx - h, b.dy + h * 0.8)
        ..close();
      canvas.drawPath(tri, Paint()..color = warnColor);
      canvas.drawPath(
          tri,
          Paint()
            ..color = edgeColor
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.5 * u);
    }
  }

  static void _dashed(Canvas canvas, List<Offset> pts, Paint paint, double dash, double gap) {
    final metrics = (Path()..addPolygon(pts, false)).computeMetrics();
    for (final m in metrics) {
      for (var d = 0.0; d < m.length; d += dash + gap) {
        canvas.drawPath(m.extractPath(d, math.min(d + dash, m.length)), paint);
      }
    }
  }

  @override
  bool shouldRepaint(RunVizPainter old) =>
      old.shapes != shapes || old.episodes != episodes || old.zoom != zoom;
}

/// 图例里的几项（只列图上有的）。
List<NwLegendItem> runVizLegend(BuildContext context,
    {required bool path, required bool trail, required bool blocked, required bool episodes}) {
  final c = Nw.of(context);
  Widget line(Color color, {bool dashed = false}) => SizedBox(
      width: 14,
      height: 10,
      child: CustomPaint(painter: _LineSwatch(color, dashed)));
  return [
    if (path) NwLegendItem(swatch: line(c.tertiary, dashed: true), label: tr('Planned path', '规划路径')),
    if (trail) NwLegendItem(swatch: line(c.textSecondary), label: tr('Recent trail', '最近走过')),
    if (blocked)
      NwLegendItem(
          swatch: Icon(Icons.warning_rounded, size: 12, color: c.warning),
          label: tr('Blocked here', '被挡的地方')),
    if (episodes)
      NwLegendItem(
          swatch: Container(
              width: 10,
              height: 10,
              decoration: BoxDecoration(shape: BoxShape.circle, color: c.warning)),
          label: tr('Localization problem', '定位异常')),
  ];
}

class _LineSwatch extends CustomPainter {
  final Color color;
  final bool dashed;
  const _LineSwatch(this.color, this.dashed);

  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..color = color
      ..strokeWidth = 2
      ..strokeCap = StrokeCap.round;
    final y = size.height / 2;
    if (!dashed) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), p);
      return;
    }
    for (var x = 0.0; x < size.width; x += 5) {
      canvas.drawLine(Offset(x, y), Offset(math.min(x + 3, size.width), y), p);
    }
  }

  @override
  bool shouldRepaint(_LineSwatch old) => old.color != color || old.dashed != dashed;
}
