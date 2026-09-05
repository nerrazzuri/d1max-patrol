/// 名册条目本身的测试。
///
/// 这一层看着简单，但它托着两条要命的约定：**PIN 不进名册文件**，以及
/// **没有 SN 的条目一律读不进来**。两条都失手过一次就没法补救 —— 前者是
/// 钥匙泄漏，后者是一堆分不清是谁的记录。
library;

import 'package:d1max_patrol/model/robot.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('落盘的形状', () {
    test('存下来的字段里没有 PIN', () {
      // 这条是硬约定。名册文件会被导出、会被贴进工单、会跟着备份走，
      // PIN 跟进去一次就收不回来了。
      const r = Robot(sn: 'C40221', name: '三号', ssid: 'XG2WIFI_C40221');
      final json = r.toJson();
      expect(json.keys, isNot(contains('pin')));
      expect(json.values.whereType<String>(), isNot(contains('704311')));
    });

    test('存下来再读回去还是同一只', () {
      const r = Robot(
        sn: 'C40221',
        name: '三号',
        ssid: 'XG2WIFI_C40221',
        bssid: '1c:69:7a:8b:2f:30',
        host: '192.168.168.100',
        port: 8095,
      );
      expect(Robot.fromJson(r.toJson()), equals(r));
    });

    test('没有 SN 的条目读不进来', () {
      // 空 SN 等于"不知道是哪只"。留在名册里只会让人按错。
      expect(() => Robot.fromJson({'name': '三号'}), throwsFormatException);
      expect(() => Robot.fromJson({'sn': '   ', 'name': '三号'}),
          throwsFormatException);
    });

    test('缺字段的旧文件照样读得动', () {
      // 名册是长期存的。日后加了新字段，旧手机上的旧文件还得打得开。
      final r = Robot.fromJson({'sn': 'C40221'});
      expect(r.sn, 'C40221');
      expect(r.name, '');
      expect(r.host, Robot.defaultHost);
      expect(r.port, Robot.defaultPort);
    });

    test('多出来的字段不当错处理', () {
      // 反过来也一样：新版本存的文件，装回旧 app 也不该炸。
      final r = Robot.fromJson({'sn': 'C40221', '将来才有的字段': 42});
      expect(r.sn, 'C40221');
    });

    test('host 是空串的时候退回默认地址', () {
      // 存成空串比缺字段更常见 —— 界面上把输入框清空就是这个。
      final r = Robot.fromJson({'sn': 'C40221', 'host': '  '});
      expect(r.host, Robot.defaultHost);
    });
  });

  group('兜底 SN', () {
    test('mac 开头的算兜底', () {
      // 服务端查不到真序列号时拿网卡 MAC 凑的，换主板就会变。
      const r = Robot(sn: 'mac:1c697a8b2f30');
      expect(r.provisional, isTrue);
    });

    test('厂商给的号不算兜底', () {
      const r = Robot(sn: 'C40221');
      expect(r.provisional, isFalse);
    });
  });

  group('BSSID 归一', () {
    test('大写和横杠都收成小写冒号', () {
      // Android 各家给的写法不一样。不统一的话同一个热点会被当成两个。
      expect(normalizeBssid('1C-69-7A-8B-2F-30'), '1c:69:7a:8b:2f:30');
      expect(normalizeBssid('  1C:69:7A:8B:2F:30 '), '1c:69:7a:8b:2f:30');
    });

    test('读进来的时候就归一了', () {
      final r = Robot.fromJson({'sn': 'C40221', 'bssid': '1C-69-7A-8B-2F-30'});
      expect(r.bssid, '1c:69:7a:8b:2f:30');
    });
  });

  group('界面上显示成什么', () {
    test('有名字就报名字带 SN', () {
      const r = Robot(sn: 'C40221', name: '三号');
      expect(r.label, '三号（C40221）');
    });

    test('没名字只能报 SN', () {
      const r = Robot(sn: 'C40221');
      expect(r.label, 'C40221');
    });

    test('地址拼出来是服务端那个口', () {
      const r = Robot(sn: 'C40221');
      expect(r.baseUrl, 'http://192.168.168.100:8095');
    });
  });
}
