// 运营可视化（手机）：两个新接口的请求（真 HTTPS 的假站点；widget 测试的绑定会把网络换成 400，所以单独一个文件）。
import 'package:d1max_patrol/net/site_client.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_site.dart';

final List<Map<String, dynamic>> _episodes = [
  {'kind': 'lost', 'x': -5.0, 'y': 3.0, 'cause': '雷达没数据', 'robot_id': 'A'},
  {'kind': 'degraded', 'x': 2.0, 'y': -2.0, 'cause': '在推算', 'robot_id': 'A'},
  {'kind': 'lost', 'x': null, 'y': null, 'cause': '没位置的不画', 'robot_id': 'A'},
];

void main() {
  group('请求', () {
    late FakeSite site;
    setUp(() async {
      site = FakeSite();
      await site.start();
    });
    tearDown(() async => site.close());

    test('runViz 取 /api/runviz；locEpisodes 把图、机器人、起点、条数编进查询串', () async {
      site.bodies['/api/runviz'] = <String, dynamic>{'robots': <String, dynamic>{'A': <String, dynamic>{'path': <dynamic>[]}}};
      site.bodies['/api/loc-episodes'] = <String, dynamic>{'episodes': _episodes};
      final c = SiteClient(site.url, testCertFingerprint());
      await c.login('alice', 'x');
      expect((await c.runViz())['robots'], isA<Map>());
      final got = await c.locEpisodes(mapId: 'estate-1', mapVersion: '8', robot: 'A b', sinceMs: 123, limit: 50);
      expect(got.length, 3);
      final q = Uri.parse(site.rawPaths.last).queryParameters;
      expect(q, {'map_id': 'estate-1', 'map_version': '8', 'robot': 'A b', 'since_ms': '123', 'limit': '50'});
      expect(Uri.parse(site.rawPaths.last).path, '/api/loc-episodes');
      await c.locEpisodes();
      expect(Uri.parse(site.rawPaths.last).queryParameters, {'limit': '200'});
      c.close();
    });
  });
}
