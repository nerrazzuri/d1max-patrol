/// 建好的图怎么摆给人看（2026-10-14「地图给客户看得懂」）：地图栏、看图页、告警页小地图共用的几样。
///
/// - **摆正**：站点给 `rotation_deg`（[-45, 45)），整张图连同上面的东西绕图的中心在屏上顺时针转这么多度，
///   楼就是正的。只是显示：发给站点、狗的坐标一个不变 —— 点图的地方由 Flutter 按同一个变换反算
///   （[NwMapRotation]），换算公式还是 `MapGeo.toMap`。
/// - **平面图**：站点给 `plan: {url, scale}` 就画它（灰度 + 透明，没扫到的地方透明、露出主题的底），拉到预览
///   那么大；取不到退回栅格预览图。深色下跟栅格图过同一个滤镜。
/// - **片区**：站点给 `areas: [{name, points: [{x, y}]}]`，淡淡地画出来，名字正着摆在片区的视觉中心，
///   屏上放不下就不写。
/// - **比例尺**：1 / 2 / 5 / 10 / 20 m 里挑一段，跟着缩放走（转角不改长短）。
/// - **全图按有内容的范围**（地图台账 M7，2026-10-14）：站点给 `content_bbox`（预览像素 `[x0, y0, x1, y1]`：
///   地面 + 墙 + 「看到过」，去掉抹掉的），打开、回正时把它（四个角转过以后的外框）放进画布、居中
///   （[MapGeo.contentFit]），四周没建图的空白不算进去；没给按整张图。
/// - **看到过、狗没走过**（M9）：站点说 `seen: true` 就在图例里加一行（淡淡的地面是站点画进底图的）；
///   **抹掉区**（区域种类 `erase`，台账 44）：站点已经把底图那一块抹了，这边只描一道细边、图例加一行。
///
/// 站点没给这几个字段（老站点）一律按没有处理：不转、画栅格、没有片区。
library;

import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_map_raster.dart';
import 'nw/nw_p4_admin.dart' show NwLegendItem, NwMapOverlay;
import 'nw/nw_theme.dart';

/// 一片有名字的地方（站点给的 `areas` 里的一项）：名字 + 顶点（地图坐标，米）。
class MapArea {
  final String name;
  final List<Offset> points;
  const MapArea(this.name, this.points);
}

/// 建好的图的预览（`GET /api/maps/<id>/<ver>/preview` 那份 JSON）里跟「怎么摆」有关的几样。
///
/// 地图 (x, y) 在预览图上是 `((x − left_x) / m_per_px, (top_y − y) / m_per_px)`（图的像素，y 朝下）；
/// 屏上再整张绕图的中心顺时针转 [rotationDeg]。
class MapGeo {
  final double width;
  final double height;
  final double mPerPx;
  final double leftX;
  final double topY;

  /// 整张图在屏上顺时针转多少度（绕图的中心）；站点没给是 0。
  final double rotationDeg;

  /// 有名字的片区（名字是空的、不到三个点的不要）。
  final List<MapArea> areas;

  /// 平面图的地址（站点没给是 null）。
  final String? planUrl;

  /// 有内容的范围（预览像素，夹到图上；站点没给、坏的、没大小的是 null —— 按整张图）。
  final Rect? content;

  /// 这一版带「看到过、狗没走过」的淡色层（图例里加一行）。
  final bool seen;

  const MapGeo(
      {required this.width,
      required this.height,
      required this.mPerPx,
      required this.leftX,
      required this.topY,
      this.rotationDeg = 0,
      this.areas = const [],
      this.planUrl,
      this.content,
      this.seen = false});

  /// 读预览 JSON。缺的、不是数的按老规矩取缺省（大小 1、一像素 1 m、边 0、不转）。
  factory MapGeo.fromPreview(Map<String, dynamic> meta) {
    double n(String k, double d) {
      final v = meta[k];
      return v is num && v.isFinite ? v.toDouble() : d;
    }

    final areas = <MapArea>[];
    final raw = meta['areas'];
    if (raw is List) {
      for (final a in raw) {
        if (a is! Map || a['name'] is! String) continue;
        final name = (a['name'] as String).trim();
        final pts = a['points'];
        if (name.isEmpty || pts is! List) continue;
        final poly = <Offset>[
          for (final p in pts)
            if (p is Map && p['x'] is num && p['y'] is num && (p['x'] as num).isFinite && (p['y'] as num).isFinite)
              Offset((p['x'] as num).toDouble(), (p['y'] as num).toDouble()),
        ];
        if (poly.length >= 3) areas.add(MapArea(name, poly));
      }
    }
    final plan = meta['plan'];
    final url = plan is Map && plan['url'] is String && (plan['url'] as String).isNotEmpty ? plan['url'] as String : null;
    final mpp = n('m_per_px', 1);
    final w = math.max(1.0, n('width', 1)), h = math.max(1.0, n('height', 1));
    Rect? content;
    final bb = meta['content_bbox'];
    if (bb is List && bb.length == 4 && bb.every((v) => v is num && v.isFinite)) {
      final v = [for (final e in bb) (e as num).toDouble()];
      final r = Rect.fromLTRB(math.max(0, math.min(v[0], v[2])), math.max(0, math.min(v[1], v[3])),
          math.min(w, math.max(v[0], v[2])), math.min(h, math.max(v[1], v[3])));
      if (r.width > 0 && r.height > 0) content = r;
    }
    return MapGeo(
        width: w,
        height: h,
        mPerPx: mpp > 0 ? mpp : 1,
        leftX: n('left_x', 0),
        topY: n('top_y', 0),
        rotationDeg: n('rotation_deg', 0),
        areas: areas,
        planUrl: url,
        content: content,
        seen: meta['seen'] == true);
  }

  double get radians => rotationDeg * math.pi / 180;

  Size get size => Size(width, height);

  /// 地图（米）→ 预览图上的像素（没转之前）。
  Offset toPx(double x, double y) => Offset((x - leftX) / mPerPx, (topY - y) / mPerPx);

  /// 预览图上的像素（没转之前）→ 地图（米）。点图的地方用它：点下去的位置 Flutter 已经反着转回来了。
  Offset toMap(Offset px) => Offset(leftX + px.dx * mPerPx, topY - px.dy * mPerPx);

  /// 转过的整张图放进 [box]：图的一像素画成多大（逻辑像素）。
  double fitScale(Size box) {
    final r = nwRotatedBounds(size, radians);
    return math.max(0.0, math.min(box.width / r.width, box.height / r.height));
  }

  /// 有内容的范围四个角绕图的中心顺时针转 [radians] 以后的外框（屏上方向、图的一像素 = 1、相对图的中心）；
  /// 站点没给范围就是整张图转过的外框。
  Rect contentBounds() {
    final b = content ?? Offset.zero & size;
    final c = size.center(Offset.zero);
    final q = [for (final p in [b.topLeft, b.topRight, b.bottomLeft, b.bottomRight]) nwRotate(p - c, radians)];
    return Rect.fromLTRB(q.map((p) => p.dx).reduce(math.min), q.map((p) => p.dy).reduce(math.min),
        q.map((p) => p.dx).reduce(math.max), q.map((p) => p.dy).reduce(math.max));
  }

  /// 「全图」：有内容的范围（[contentBounds]）放进 [box]（四边离 [pad]）、居中 —— 回（图的一像素画成多大，
  /// 范围的中心离图的中心多远：屏上的、已经乘过缩放）。图（绕自己的中心转过、按这个缩放画）往反方向挪这么多，
  /// 范围就在 [box] 正中。站点没给范围：整张图放进 [box]（[fitScale]，不留边、不挪），跟原来一样。
  (double, Offset) contentFit(Size box, {double pad = 8}) {
    if (content == null) return (fitScale(box), Offset.zero);
    final r = contentBounds();
    final k = math.max(0.0, math.min((box.width - 2 * pad) / r.width, (box.height - 2 * pad) / r.height));
    return (k, r.center * k);
  }
}

/// 底图的字节：站点给了平面图先取平面图，取不到（老站点、网断了一下、不在这个站点上）退回栅格预览图。
/// 回（字节，是不是平面图）。
Future<(Uint8List, bool)> loadMapBase(SiteApi api, String mapId, String version, MapGeo? geo) async {
  final url = geo?.planUrl;
  if (url != null) {
    try {
      final b = await api.mapPlanPng(url);
      if (b.isNotEmpty) return (b, true);
    } on SiteError {
      // 退回栅格
    }
  }
  return (await api.mapPreviewPng(mapId, version), false);
}

/// 告警页小地图要的那一张：元数据（转角、平面图）取得到就按它摆，取不到照旧只画栅格、不转。
Future<NwMapPicture> loadMapPicture(SiteApi api, String mapId, String version) async {
  MapGeo? geo;
  try {
    geo = MapGeo.fromPreview(await api.mapPreview(mapId, version));
  } on SiteError {
    // 没有元数据：不转
  }
  final (bytes, _) = await loadMapBase(api, mapId, version, geo);
  return NwMapPicture(bytes, rotationDeg: geo?.rotationDeg ?? 0, size: geo?.size);
}

/// 底图：栅格预览图或者平面图，拉满外面那一块；深色过 [darkMapRaster]（[darkKey] 安在那层滤镜上）。
/// 栅格图一格一格的，放大了不糊（不插值）；平面图本来就细，缩放时平滑一点。
Widget mapBaseImage(BuildContext context, Uint8List bytes, {required bool plan, Key? darkKey}) => nwMapRaster(
    context,
    Image.memory(bytes,
        fit: BoxFit.fill,
        filterQuality: plan ? FilterQuality.medium : FilterQuality.none,
        gaplessPlayback: true,
        errorBuilder: (_, _, _) => const SizedBox.shrink()),
    darkKey: darkKey);

// ------------------------------------------------------------ 片区

/// 点在不在多边形里（奇偶规则）。
bool _inside(Offset p, List<Offset> poly) {
  var inside = false;
  for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    final a = poly[i], b = poly[j];
    if ((a.dy > p.dy) != (b.dy > p.dy) && p.dx < (b.dx - a.dx) * (p.dy - a.dy) / (b.dy - a.dy) + a.dx) {
      inside = !inside;
    }
  }
  return inside;
}

double _segDist(Offset p, Offset a, Offset b) {
  final ab = b - a;
  final len2 = ab.dx * ab.dx + ab.dy * ab.dy;
  final t = len2 == 0 ? 0.0 : (((p.dx - a.dx) * ab.dx + (p.dy - a.dy) * ab.dy) / len2).clamp(0.0, 1.0);
  return (p - (a + ab * t)).distance;
}

/// 里面的点为正、外面为负：到最近一条边的距离。
double _depth(Offset p, List<Offset> poly) {
  var d = double.infinity;
  for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    d = math.min(d, _segDist(p, poly[j], poly[i]));
  }
  return _inside(p, poly) ? d : -d;
}

/// 多边形的视觉中心：里面离各边最远的那个点（凹的、L 形的片区，名字也落在片区里头，不落在拐角外面）；
/// 一样深的（长条的片区，中线上处处一样深）取离形心近的。粗格子找一遍，再在最好的那一格附近细找几轮；
/// 够写个名字用，不求极值。
Offset polygonLabelPoint(List<Offset> poly) {
  var minX = poly.first.dx, maxX = minX, minY = poly.first.dy, maxY = minY;
  for (final p in poly) {
    minX = math.min(minX, p.dx);
    maxX = math.max(maxX, p.dx);
    minY = math.min(minY, p.dy);
    maxY = math.max(maxY, p.dy);
  }
  var sum = Offset.zero;
  for (final p in poly) {
    sum += p;
  }
  final centroid = sum / poly.length.toDouble();
  double score(Offset p) => _depth(p, poly) - 0.05 * (p - centroid).distance;
  var best = centroid;
  var bestS = score(best);
  const n = 16;
  var cw = (maxX - minX) / n, ch = (maxY - minY) / n;
  if (cw <= 0 || ch <= 0) return best;
  void consider(Offset p) {
    final s = score(p);
    if (s > bestS) {
      best = p;
      bestS = s;
    }
  }

  for (var i = 0; i <= n; i++) {
    for (var j = 0; j <= n; j++) {
      consider(Offset(minX + i * cw, minY + j * ch));
    }
  }
  for (var round = 0; round < 4; round++) {
    final c = best;
    for (var i = -4; i <= 4; i++) {
      for (var j = -4; j <= 4; j++) {
        consider(Offset(c.dx + i * cw / 4, c.dy + j * ch / 4));
      }
    }
    cw /= 4;
    ch /= 4;
  }
  return best;
}

/// 片区淡淡的底和边（画布坐标）。颜色从主题来（[fill]、[stroke]），跟禁行区、限速区的红黄分开。
class AreasPainter extends CustomPainter {
  AreasPainter(this.shapes, {required this.fill, required this.stroke, this.strokeWidth = 1});
  final List<List<Offset>> shapes;
  final Color fill;
  final Color stroke;
  final double strokeWidth;

  @override
  void paint(Canvas canvas, Size size) {
    for (final s in shapes) {
      if (s.length < 3) continue;
      final path = Path()..addPolygon(s, true);
      canvas.drawPath(path, Paint()..color = fill);
      canvas.drawPath(
          path,
          Paint()
            ..color = stroke
            ..style = PaintingStyle.stroke
            ..strokeWidth = strokeWidth);
    }
  }

  @override
  bool shouldRepaint(AreasPainter old) =>
      old.shapes != shapes || old.fill != fill || old.stroke != stroke || old.strokeWidth != strokeWidth;
}

/// 片区底色、边色（浅色深色都从主题取）。
Color mapAreaFill(NwColors c) => c.tertiary.withValues(alpha: 0.10);
Color mapAreaStroke(NwColors c) => c.tertiary.withValues(alpha: 0.55);

/// 片区的名字：摆在画布里（跟图一起转），每个名字反着转回来、反着缩回来 —— 屏上永远是正的、12 号字；
/// 片区在屏上太小（缩小了看）就不写，放大了再出来（规矩见 [fits]）。不挡手势。
class MapAreaLabels extends StatelessWidget {
  /// 片区：名字 + 顶点（画布坐标）。
  final List<(String, List<Offset>)> areas;

  /// 图在屏上转了多少（弧度，顺时针）。
  final double rad;

  /// 缩放为 1 时画布的一个单位在屏上多少逻辑像素。
  final double unit;

  /// 缩放（`InteractiveViewer` 的变换）；没有就当 1。
  final ValueListenable<Matrix4>? zoom;
  final Key Function(String name)? labelKey;

  const MapAreaLabels(
      {super.key, required this.areas, required this.rad, required this.unit, this.zoom, this.labelKey});

  static const EdgeInsets _pad = EdgeInsets.symmetric(horizontal: 6, vertical: 2);

  /// 写不写名字（屏上 [w] × [h] 那么大一块，正着摆、中心在 [at]）：片区 [poly]（画布坐标）在屏上（转
  /// [rad]、乘 [scale]）**装得下一行字的高**（[at] 离边至少半个标签高），**也不比名字小太多**（屏上最长
  /// 的一边至少是标签宽的一半）。名字可以出片区的边一点（窄的长廊、小房间也写得上），小片区不写。
  static bool fits(List<Offset> poly, Offset at, double rad, double scale, double w, double h) {
    if (_depth(at, poly) * scale * 2 < h) return false;
    final q = [for (final p in poly) nwRotate(p, rad) * scale];
    var minX = q.first.dx, maxX = minX, minY = q.first.dy, maxY = minY;
    for (final p in q) {
      minX = math.min(minX, p.dx);
      maxX = math.max(maxX, p.dx);
      minY = math.min(minY, p.dy);
      maxY = math.max(maxY, p.dy);
    }
    return math.max(maxX - minX, maxY - minY) * 2 >= w;
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final style = NwText.labelSmall.copyWith(color: c.text, fontWeight: FontWeight.w600);
    final scaler = MediaQuery.textScalerOf(context);
    final dir = Directionality.of(context);
    final items = [
      for (final (name, poly) in areas)
        (name, poly, polygonLabelPoint(poly), () {
          final tp = TextPainter(
              text: TextSpan(text: name, style: style), textDirection: dir, textScaler: scaler, maxLines: 1)
            ..layout();
          final s = Size(tp.width + _pad.horizontal, tp.height + _pad.vertical);
          tp.dispose();
          return s;
        }()),
    ];
    Widget labels(double z) {
      final scale = unit * z;
      return Stack(clipBehavior: Clip.none, children: [
        for (final (name, poly, at, box) in items)
          if (scale > 0 && fits(poly, at, rad, scale, box.width, box.height))
            Positioned(
                left: at.dx,
                top: at.dy,
                child: FractionalTranslation(
                    translation: const Offset(-0.5, -0.5),
                    child: Transform.rotate(
                        angle: -rad,
                        child: Transform.scale(
                            scale: 1 / scale,
                            child: Container(
                                padding: _pad,
                                decoration: BoxDecoration(
                                    color: c.surface.withValues(alpha: 0.8),
                                    borderRadius: BorderRadius.circular(Nw.rPill)),
                                child: Text(name,
                                    key: labelKey?.call(name), maxLines: 1, softWrap: false, style: style)))))),
      ]);
    }

    final z = zoom;
    return IgnorePointer(
        child: z == null
            ? labels(1)
            : ValueListenableBuilder<Matrix4>(
                valueListenable: z, builder: (_, m, _) => labels(m.getMaxScaleOnAxis())));
  }
}

// ------------------------------------------------------------ 比例尺

/// 比例尺：1 / 2 / 5 / 10 / 20 m 里挑屏上不超过 [maxPx] 的最长一段（1 m 都超了就是 1 m），跟着缩放走。
/// 转角不改长短。[barKey] 安在那一段（`SizedBox`，宽就是这一段在屏上的长度）上。
class MapScaleBar extends StatelessWidget {
  /// 缩放为 1 时一米在屏上多少逻辑像素。
  final double pxPerMeter;
  final ValueListenable<Matrix4> zoom;
  final Key? barKey;
  final double maxPx;
  const MapScaleBar({super.key, required this.pxPerMeter, required this.zoom, this.barKey, this.maxPx = 96});

  static const List<int> steps = [1, 2, 5, 10, 20];

  /// 挑哪一段：（米，屏上多长）。
  static (int, double) pick(double pxPerMeter, {double maxPx = 96}) {
    for (final m in steps.reversed) {
      if (m * pxPerMeter <= maxPx) return (m, m * pxPerMeter);
    }
    return (steps.first, steps.first * pxPerMeter);
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return IgnorePointer(
        child: ValueListenableBuilder<Matrix4>(
            valueListenable: zoom,
            builder: (context, m, _) {
              final (meters, px) = pick(pxPerMeter * m.getMaxScaleOnAxis(), maxPx: maxPx);
              return NwMapOverlay(
                  child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text('$meters m', style: NwText.labelSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                const SizedBox(height: 2),
                SizedBox(
                    key: barKey,
                    width: px,
                    height: 6,
                    child: CustomPaint(painter: _ScaleBarPainter(c.text))),
              ]));
            }));
  }
}

class _ScaleBarPainter extends CustomPainter {
  _ScaleBarPainter(this.color);
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..color = color
      ..strokeWidth = 2
      ..strokeCap = StrokeCap.square;
    final y = size.height - 1;
    canvas.drawLine(Offset(1, y), Offset(size.width - 1, y), p);
    canvas.drawLine(const Offset(1, 0), Offset(1, y), p);
    canvas.drawLine(Offset(size.width - 1, 0), Offset(size.width - 1, y), p);
  }

  @override
  bool shouldRepaint(_ScaleBarPainter old) => old.color != color;
}

// ------------------------------------------------------------ 图例里底图那三样

/// 栅格图上地面、墙、没扫到是哪三个灰（站点 `map_preview.py`：空闲 254、占据 0、未知 205）；平面图上地面、
/// 墙大致是这两个灰，没扫到是透明的（露出画布的底）。
const int _rasterFloor = 254, _rasterWall = 0, _rasterUnknown = 205;
const int _planFloor = 248, _planWall = 52;

/// 预览图上「看到过、狗没走过」的灰（站点 `SEEN_GRAY`）；平面图上是这么透明的白（站点 `PLAN_SEEN_ALPHA`）。
const int _rasterSeen = 232;
const double _planSeenAlpha = 0.35;

/// 图例里底图那三样：地面、墙和障碍物、没扫到；[seen]（站点说这一版有）再加一样「看到过、狗没走过」。色块
/// 跟图过同一个深色滤镜，跟图上看到的一个色。
List<NwLegendItem> mapBaseLegend(BuildContext context, {required bool plan, bool seen = false}) {
  final c = Nw.of(context);
  Widget gray(int v) => Container(
      width: 12,
      height: 12,
      decoration: BoxDecoration(border: Border.all(color: c.textSecondary)),
      child: nwMapRaster(context, ColoredBox(color: Color.fromARGB(255, v, v, v))));
  return [
    NwLegendItem(swatch: gray(plan ? _planFloor : _rasterFloor), label: tr('Floor', '地面')),
    NwLegendItem(swatch: gray(plan ? _planWall : _rasterWall), label: tr('Wall, obstacle', '墙、障碍物')),
    if (seen)
      NwLegendItem(
          swatch: Container(
              width: 12,
              height: 12,
              decoration: BoxDecoration(color: c.surfaceVariant, border: Border.all(color: c.textSecondary)),
              child: nwMapRaster(
                  context,
                  ColoredBox(
                      color: plan
                          ? Color.fromARGB((_planSeenAlpha * 255).round(), 255, 255, 255)
                          : const Color.fromARGB(255, _rasterSeen, _rasterSeen, _rasterSeen)))),
          label: tr('Seen, not mapped for the robot', '看到过、机器人没走过')),
    NwLegendItem(
        swatch: plan
            ? Container(
                width: 12,
                height: 12,
                decoration: BoxDecoration(color: c.surfaceVariant, border: Border.all(color: c.textSecondary)))
            : gray(_rasterUnknown),
        label: tr('Not mapped', '没扫到')),
  ];
}

/// 抹掉区（台账 44）在图上的边：底图那一块站点已经抹了，这里只描一道细边。
Color mapEraseStroke(NwColors c) => c.textSecondary;

/// 图例里「已抹掉」那一样：跟图上一样只有边。
NwLegendItem mapEraseLegend(BuildContext context) {
  final c = Nw.of(context);
  return NwLegendItem(
      swatch: Container(
          width: 12, height: 10, decoration: BoxDecoration(border: Border.all(color: mapEraseStroke(c), width: 1))),
      label: tr('Erased (robots never go there)', '已抹掉（机器人不去）'));
}

/// 图例里「片区」那一样：跟图上画的一个底、一个边。
NwLegendItem mapAreaLegend(BuildContext context) {
  final c = Nw.of(context);
  return NwLegendItem(
      swatch: Container(
          width: 12,
          height: 10,
          decoration: BoxDecoration(color: mapAreaFill(c), border: Border.all(color: mapAreaStroke(c), width: 1.5))),
      label: tr('Named area', '片区'));
}
