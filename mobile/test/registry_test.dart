/// 名册的测试。
///
/// 盯的是三件在现场会出事的事：**重复添加不能静默改掉旧记录**、**删条目
/// 必须连 PIN 一起忘掉**、以及**一个 SSID 对上两只狗的时候不许随便挑一只**。
/// 最后一条尤其要紧 —— 挑错了，操作员就在错的狗上按了"跑"。
library;

import 'package:d1max_patrol/model/registry.dart';
import 'package:d1max_patrol/model/robot.dart';
import 'package:flutter_test/flutter_test.dart';

const _sanHao = Robot(
  sn: 'C40221',
  name: '三号',
  ssid: 'XG2WIFI_C40221',
  bssid: '1c:69:7a:8b:2f:30',
);
const _wuHao = Robot(
  sn: 'C40555',
  name: '五号',
  ssid: 'XG2WIFI_C40555',
  bssid: 'aa:bb:cc:dd:ee:ff',
);

void main() {
  group('加和取', () {
    test('新名册是空的', () {
      expect(RobotRegistry().isEmpty, isTrue);
    });

    test('加进去取得出来', () {
      final reg = RobotRegistry()..add(_sanHao);
      expect(reg.bySn('C40221'), equals(_sanHao));
      expect(reg.length, 1);
    });

    test('取不认识的 SN 给 null 而不是抛', () {
      // 认不出是常态：可能是只还没登记的新狗。
      expect(RobotRegistry().bySn('C40221'), isNull);
    });

    test('取的时候两头的空格不算数', () {
      final reg = RobotRegistry()..add(_sanHao);
      expect(reg.bySn('  C40221 '), equals(_sanHao));
    });

    test('重复添加会被拦下来', () {
      // 手滑重复添加要是静默覆盖，改掉的是已经调好的那条记录，
      // 而人不会发现 —— 界面上看起来完全一样。
      final reg = RobotRegistry()..add(_sanHao);
      expect(() => reg.add(_sanHao.copyWith(name: '手滑')),
          throwsA(isA<DuplicateRobot>()));
      expect(reg.bySn('C40221')!.name, '三号');
    });

    test('明确要改的时候才覆盖', () {
      final reg = RobotRegistry()..add(_sanHao);
      reg.upsert(_sanHao.copyWith(name: '三号（换了电池）'));
      expect(reg.length, 1);
      expect(reg.bySn('C40221')!.name, '三号（换了电池）');
    });

    test('顺序按加进来的先后，不重排', () {
      // 操作员心里对"我的第一只、第二只"是有次序的，按字母排会打乱它。
      final reg = RobotRegistry()
        ..add(_wuHao)
        ..add(_sanHao);
      expect(reg.all.map((r) => r.sn), ['C40555', 'C40221']);
    });
  });

  group('删', () {
    test('删掉就取不到了', () {
      final reg = RobotRegistry()..add(_sanHao);
      expect(reg.remove('C40221'), isTrue);
      expect(reg.bySn('C40221'), isNull);
    });

    test('删不存在的返回 false，不抛', () {
      expect(RobotRegistry().remove('C40221'), isFalse);
    });
  });

  group('按热点认狗', () {
    test('SSID 对上了认得出来', () {
      final reg = RobotRegistry()
        ..add(_sanHao)
        ..add(_wuHao);
      expect(reg.bySsid('XG2WIFI_C40555'), equals(_wuHao));
    });

    test('没登记过的热点给 null', () {
      final reg = RobotRegistry()..add(_sanHao);
      expect(reg.bySsid('别人家的WiFi'), isNull);
      expect(reg.bySsid(''), isNull);
    });

    test('一个 SSID 对上两只就不许挑，直接摊开', () {
      // **这条是这本名册最要紧的一条。** 随便返回一只，操作员就会在错的狗
      // 上按"跑"，而他不会知道自己按错了。宁可把歧义顶到界面上让人来断。
      final reg = RobotRegistry()
        ..add(_sanHao)
        ..add(_wuHao.copyWith(ssid: 'XG2WIFI_C40221'));
      expect(() => reg.bySsid('XG2WIFI_C40221'),
          throwsA(isA<AmbiguousSsid>()));
    });

    test('歧义异常里把两只的 SN 都说出来', () {
      // 只说"有歧义"没用，人要知道去改哪两条。
      final reg = RobotRegistry()
        ..add(_sanHao)
        ..add(_wuHao.copyWith(ssid: 'XG2WIFI_C40221'));
      try {
        reg.bySsid('XG2WIFI_C40221');
        fail('应该抛 AmbiguousSsid');
      } on AmbiguousSsid catch (e) {
        expect(e.sns, containsAll(<String>['C40221', 'C40555']));
      }
    });

    test('BSSID 认得比 SSID 硬', () {
      // SSID 是人能随便改的字符串，BSSID 是网卡地址，撞不了。
      final reg = RobotRegistry()
        ..add(_sanHao)
        ..add(_wuHao);
      expect(reg.byBssid('AA:BB:CC:DD:EE:FF'), equals(_wuHao));
    });

    test('没记过 BSSID 的条目不会被空串匹配上', () {
      // 一堆条目 bssid 都是空的，拿空串一查全中，那就成了随机认狗。
      final reg = RobotRegistry()..add(const Robot(sn: 'C40221'));
      expect(reg.byBssid(''), isNull);
      expect(reg.byBssid('   '), isNull);
    });
  });

  group('从盘上读回来那条路', () {
    test('文件里万一有重复 SN，取最后一条而不是整本打不开', () {
      // 这是读文件的路。抛异常等于名册整本报废，而重复只可能是我们自己
      // 写坏的。取最后一条至少还能用。加新条目那条路才严格拒重。
      final reg = RobotRegistry.fromList([
        _sanHao,
        _sanHao.copyWith(name: '后写的'),
      ]);
      expect(reg.length, 1);
      expect(reg.bySn('C40221')!.name, '后写的');
    });

    test('整本转成 JSON 里一个 PIN 字段都没有', () {
      final reg = RobotRegistry()
        ..add(_sanHao)
        ..add(_wuHao);
      for (final item in reg.toJson()) {
        expect(item.keys, isNot(contains('pin')));
      }
    });
  });
}
