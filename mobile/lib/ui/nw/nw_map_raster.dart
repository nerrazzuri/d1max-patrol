// 站点渲染的栅格底图（地图栏、告警页小地图）在深色下怎么压暗：同一个滤镜、同一个包法。
import 'package:flutter/material.dart';

/// 深色下压暗栅格图用的滤镜：亮度反过来、再收一点（空地 254 → 27 接近深色底，没探过的 205 → 67，
/// 墙 0 → 235 浅色）。只滤站点给的底图；上面画的防区、待命点、狗都不过它。
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
