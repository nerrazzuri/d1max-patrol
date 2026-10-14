// 站点渲染的栅格底图（地图栏、告警页小地图）在深色下怎么压暗：同一个滤镜、同一个包法。
// 2026-10-14 地图好看一点：底图可以是平面图（灰度 + 透明），整张图可以转一个角度摆正（[NwMapRotation]）。
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

/// 深色下压暗栅格图用的滤镜：亮度反过来、再收一点（空地 254 → 27 接近深色底，没探过的 205 → 67，
/// 墙 0 → 235 浅色）。只滤站点给的底图；上面画的防区、待命点、狗都不过它。透明度不动：平面图透明的
/// 地方（没扫到）深色下还是透明的，露出底色。
const ColorFilter darkMapRaster = ColorFilter.matrix(<double>[
  -0.82, 0, 0, 0, 235, //
  0, -0.82, 0, 0, 235, //
  0, 0, -0.82, 0, 235, //
  0, 0, 0, 1, 0, //
]);

/// 站点渲染的栅格底图 [image]：浅色原样，深色包一层 [darkMapRaster]（`ColorFiltered` 带 [darkKey]）。
/// 只包底图本身 —— 叠在上面的标记别放进 [image] 里。
Widget nwMapRaster(BuildContext context, Widget image, {Key? darkKey}) =>
    Theme.of(context).brightness == Brightness.dark
        ? ColorFiltered(key: darkKey, colorFilter: darkMapRaster, child: image)
        : image;

/// 一张拿来画的底图（告警页小地图用）：字节（栅格预览图或者平面图）、整张图在屏上顺时针转多少度、
/// 图有多大（预览的像素；平面图分辨率高，也拉到这么大）。[size] 不知道（老站点、元数据取不到）就不转。
class NwMapPicture {
  final Uint8List bytes;
  final double rotationDeg;
  final Size? size;
  const NwMapPicture(this.bytes, {this.rotationDeg = 0, this.size});
}

/// [size] 那么大的一块绕中心转 [rad] 之后的外接框。
Size nwRotatedBounds(Size size, double rad) {
  final c = math.cos(rad).abs(), s = math.sin(rad).abs();
  return Size(size.width * c + size.height * s, size.width * s + size.height * c);
}

/// 屏幕坐标（y 朝下）里一个向量顺时针转 [rad]：`Transform.rotate(angle: rad)` 对它的孩子就是这么转的。
Offset nwRotate(Offset p, double rad) {
  final c = math.cos(rad), s = math.sin(rad);
  return Offset(p.dx * c - p.dy * s, p.dx * s + p.dy * c);
}

/// 整张图（[child]，[size] 那么大）绕自己的中心在屏上**顺时针**转 [rad] 摆着：自己的大小是转过之后的
/// 外接框，图在框的正中。点、拖落到 [child] 里时 Flutter 按同一个变换反算回 [child] 自己的坐标，
/// 所以点图换算地图坐标的地方不用自己管转角。图上的字要正着读，自己反着转回来（`-rad`）。
class NwMapRotation extends StatelessWidget {
  final double rad;
  final Size size;
  final Widget child;
  const NwMapRotation({super.key, required this.rad, required this.size, required this.child});

  @override
  Widget build(BuildContext context) {
    final outer = nwRotatedBounds(size, rad);
    return SizedBox(
        width: outer.width,
        height: outer.height,
        child: OverflowBox(
            minWidth: size.width,
            maxWidth: size.width,
            minHeight: size.height,
            maxHeight: size.height,
            child: Transform.rotate(angle: rad, child: child)));
  }
}
