// 版本兼容（商业化 A7）：站点登录回复里的级别配不配这个 App。
import 'package:d1max_patrol/net/site_client.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('老站点不带级别：都算 1，配得上', () {
    expect(compatProblem(<String, dynamic>{'token': 't'}), isNull);
  });
  test('站点要更新的 App：说「升级 App」', () {
    final why = compatProblem(<String, dynamic>{'api_level': 1, 'min_app_level': appApiLevel + 1});
    expect(why, contains('升级 App'));
  });
  test('站点接口级别低于 App 要的：说「先升级站点」', () {
    final why = compatProblem(<String, dynamic>{'api_level': minSiteApiLevel - 1, 'min_app_level': 1});
    expect(why, contains('先升级站点'));
  });
  test('现在的站点配现在的 App', () {
    expect(compatProblem(<String, dynamic>{'api_level': 1, 'min_app_level': 1}), isNull);
  });
}
