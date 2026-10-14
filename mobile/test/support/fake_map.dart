/// 地图给客户看得懂（2026-10-14）：一张转了 23° 的真图当夹具（出图、测试都用）。
///
/// `test/fixtures/map_rot23_preview.png` 是仓库里那张真栅格图 `maps/newdog2_floor.pgm`（0.05 m）按站点
/// 的三值规矩（占据 0 / 空闲 254 / 未知 205）缩到 0.1 m、**逆时针**转 23° 存的 —— App 顺时针转 23° 摆回来
/// 楼就是正的；`map_rot23_plan.png` 是同一张图 2 倍分辨率的平面图（灰度 + 透明：地面 248、墙 52、没扫到
/// 全透明），平滑过。下面的坐标（片区、狗、待命点、原点、区域）是从正着的原图上挑的点按同一个转法换算的。
library;

import 'dart:io';
import 'dart:typed_data';

import '../site_page_test.dart' show FakeApi;
import 'fake_site.dart' show siteFixture;

/// 预览 JSON（站点那份的形状，带 `rotation_deg`、`areas`、`plan`）。
Map<String, dynamic> rot23Preview({bool rotation = true, bool areas = true, bool plan = true}) => <String, dynamic>{
      'width': 301, 'height': 468, 'm_per_px': 0.1, 'left_x': -15.05, 'top_y': 23.4, 'source': 'newdog2_floor.yaml',
      if (rotation) 'rotation_deg': 23.0,
      if (areas)
        'areas': <dynamic>[
          <String, dynamic>{'name': 'Reception hall', 'points': _pts([(-12.06, 16.5), (-4.69, 19.63), (-1.37, 11.8), (-8.73, 8.68)])},
          <String, dynamic>{'name': 'Gallery', 'points': _pts([(-1.58, -2.41), (4.4, 0.13), (11.63, -16.9), (4.95, -19.73)])},
        ],
      if (plan) 'plan': <String, dynamic>{'url': '/api/maps/estate-1/8/plan.png', 'scale': 2},
      'standby': <dynamic>[
        <String, dynamic>{'robot_id': 'A', 'name': 'dock', 'x': -0.88, 'y': 1.69, 'yaw': 0.0, 'default': true},
      ],
      'homes': <dynamic>[
        <String, dynamic>{'robot_id': 'A', 'name': 'origin', 'x': 6.4, 'y': -18.03, 'yaw': 0.0},
      ],
    };

List<Map<String, dynamic>> _pts(List<(double, double)> p) => [for (final (x, y) in p) <String, dynamic>{'x': x, 'y': y}];

/// 这张图上的禁行区、限速区（`GET …/zones` 的形状）。
Map<String, dynamic> rot23Zones() => <String, dynamic>{
      'revision': 3,
      'zones': <dynamic>[
        <String, dynamic>{'id': 'fountain', 'kind': 'nogo', 'label': 'Fountain',
          'polygon': [[4.23, -9.72], [6.54, -8.74], [7.9, -11.96], [5.6, -12.94]]},
        <String, dynamic>{'id': 'doorway', 'kind': 'slow', 'label': 'Doorway', 'max_speed_mps': 0.3,
          'polygon': [[-5.65, 7.81], [-0.59, 9.96], [0.39, 7.66], [-4.68, 5.51]]},
      ],
      'confirmed': <String, dynamic>{'revision': 3, 'by': 'admin', 'at_ms': 1, 'current': true},
      'confirm_text': 'x',
    };

/// 狗 A 在这张图上的位置（米）。
const (double, double) rot23Robot = (-3.29, 6.1);

/// 夹具里的那只狗，改成报着 estate-1:[version] 这一版图、在线、站在 ([x], [y])。
List<Map<String, dynamic>> robotsOnMap(String version, double x, double y) => [
      for (final r in (siteFixture('site_robots')['robots'] as List).cast<Map<String, dynamic>>())
        <String, dynamic>{
          ...r,
          'loc': <String, dynamic>{
            ...(r['loc'] as Map).cast<String, dynamic>(),
            'pose': <String, dynamic>{'x': x, 'y': y, 'yaw': 0.0},
          },
          'status': <String, dynamic>{...(r['status'] as Map).cast<String, dynamic>(), 'online': true},
          'capabilities': <String, dynamic>{
            ...(r['capabilities'] as Map).cast<String, dynamic>(),
            'tasks': <String, dynamic>{
              ...((r['capabilities'] as Map)['tasks'] as Map).cast<String, dynamic>(),
              'patrol': <String, dynamic>{'autonomy': 'autonomous', 'map_id': 'estate-1', 'map_version': version},
            },
          },
        },
    ];

Uint8List rot23RasterPng() => File('test/fixtures/map_rot23_preview.png').readAsBytesSync();
Uint8List rot23PlanPng() => File('test/fixtures/map_rot23_plan.png').readAsBytesSync();

/// 站点那头是这张转了 23° 的图：狗 A 报着 estate-1:8、站在 [rot23Robot]；[plan] 假 = 站点没给平面图。
class Rot23Api extends FakeApi {
  Rot23Api(super.role, {this.plan = true});
  final bool plan;
  @override
  Future<List<Map<String, dynamic>>> robots() async => robotsOnMap('8', rot23Robot.$1, rot23Robot.$2);
  @override
  Future<Map<String, dynamic>> mapPreview(String mapId, String version) async {
    calls.add('mapPreview $mapId:$version');
    return rot23Preview(plan: plan);
  }

  @override
  Future<Uint8List> mapPreviewPng(String mapId, String version) async {
    calls.add('mapPreviewPng $mapId:$version');
    return rot23RasterPng();
  }

  @override
  Future<Uint8List> mapPlanPng(String url) async {
    calls.add('mapPlanPng $url');
    return rot23PlanPng();
  }

  @override
  Future<Map<String, dynamic>> mapZones(String mapId, String version) async => rot23Zones();
}
