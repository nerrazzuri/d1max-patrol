// W41：人员检测忽略区（区域种类 `ignore`）在手机上认得：标题写「人员忽略区」，不当成限速区；画成靛蓝色。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/ui/site_mapping.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('人员忽略区：标题、形状', () {
    final z = <String, dynamic>{'id': 'i2', 'kind': 'ignore', 'label': '展品', 'polygon': <dynamic>[]};
    appLang.value = AppLang.zh;
    expect(zoneTitle(z), contains('人员忽略区 · 展品'));
    expect(zoneTitle(z), isNot(contains('限速')));
    final s = ZoneShape.of(z, const <Offset>[]);
    expect(s.ignore, isTrue);
    expect(s.nogo, isFalse);
    expect(s.erase, isFalse);
  });
}
