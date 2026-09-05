/// 存取的测试。
///
/// 最要紧的一条是**掐电不能把名册掐没**：手机随时会没电、会被强杀，写到
/// 一半的 JSON 文件下次打开就是"我的狗全没了"。所以写是先写临时文件再改名。
library;

import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/model/registry.dart';
import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/store/pin_vault.dart';
import 'package:d1max_patrol/store/registry_store.dart';
import 'package:flutter_test/flutter_test.dart';

const _sanHao = Robot(
  sn: 'C40221',
  name: '三号',
  ssid: 'XG2WIFI_C40221',
  bssid: '1c:69:7a:8b:2f:30',
);

void main() {
  late Directory tmp;

  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('d1max_reg_');
  });

  tearDown(() async {
    if (await tmp.exists()) await tmp.delete(recursive: true);
  });

  File rosterFile() => File('${tmp.path}/robots.json');

  group('JSON 文件', () {
    test('第一次装 app 读出来是空名册，不是报错', () async {
      // 文件不存在是正常情况，不是异常。
      final reg = await JsonFileStore(rosterFile()).load();
      expect(reg.isEmpty, isTrue);
    });

    test('存了再读回来还是那几只', () async {
      final store = JsonFileStore(rosterFile());
      await store.save(RobotRegistry()..add(_sanHao));
      final back = await store.load();
      expect(back.bySn('C40221'), equals(_sanHao));
    });

    test('目录不存在会自己建出来', () async {
      // Android 上第一次写的时候那个目录可能还没有。
      final store = JsonFileStore(File('${tmp.path}/还没有的/robots.json'));
      await store.save(RobotRegistry()..add(_sanHao));
      expect((await store.load()).length, 1);
    });

    test('写完盘上没留下临时文件', () async {
      // 原子写留下的 .tmp 不清掉，下次读目录会看见一堆垃圾。
      final store = JsonFileStore(rosterFile());
      await store.save(RobotRegistry()..add(_sanHao));
      final leftOver = tmp.listSync().map((e) => e.path.split(RegExp(r'[\\/]')).last);
      expect(leftOver, ['robots.json']);
    });

    test('空文件当成空名册，不当成坏文件', () async {
      // 掐电有可能留下一个零字节的文件。这时候该说"没有狗"，不该说"文件坏了"
      // 然后把人挡在门外。
      await rosterFile().writeAsString('');
      expect((await JsonFileStore(rosterFile()).load()).isEmpty, isTrue);
    });

    test('文件真坏了要明说，不能装作是空的', () async {
      // 上一条的反面。半截 JSON 跟零字节不是一回事：前者说明有数据丢了，
      // 静默当成空名册，人就会以为自己从来没登记过，然后重新填一遍。
      await rosterFile().writeAsString('[{"sn": "C402');
      expect(() => JsonFileStore(rosterFile()).load(), throwsFormatException);
    });

    test('顶层不是数组也要明说', () async {
      await rosterFile().writeAsString('{"sn": "C40221"}');
      expect(() => JsonFileStore(rosterFile()).load(), throwsFormatException);
    });

    test('落到盘上的字节里一个 PIN 都搜不到', () async {
      // 前面几条测的是数据结构，这条测的是**真写到盘上的那串字节**。
      // 中间任何一层偷偷把 PIN 塞进去，都得在这里露出来。
      final store = JsonFileStore(rosterFile());
      await store.save(RobotRegistry()..add(_sanHao));
      final text = await rosterFile().readAsString();
      expect(text, isNot(contains('pin')));
      expect(text, isNot(contains('704311')));
      // 顺便确认存的确实是这只狗，别把上面两条测成"文件是空的"。
      expect(text, contains('C40221'));
      expect(jsonDecode(text), isA<List<dynamic>>());
    });
  });

  group('PIN 库', () {
    test('存了取得回来', () async {
      final vault = MemoryPinVault();
      await vault.write('C40221', '704311');
      expect(await vault.read('C40221'), '704311');
    });

    test('没存过取出来是 null', () async {
      expect(await MemoryPinVault().read('C40221'), isNull);
    });

    test('一只狗一个 PIN，互不串门', () async {
      // 串了的话，拿三号的 PIN 去解五号的锁，只会得到一句"PIN 不对"，
      // 人会以为自己记错了密码，而不会想到是 app 拿错了。
      final vault = MemoryPinVault();
      await vault.write('C40221', '704311');
      await vault.write('C40555', '428913');
      expect(await vault.read('C40221'), '704311');
      expect(await vault.read('C40555'), '428913');
    });

    test('忘掉之后取不到', () async {
      final vault = MemoryPinVault();
      await vault.write('C40221', '704311');
      await vault.forget('C40221');
      expect(await vault.read('C40221'), isNull);
    });

    test('忘掉没存过的不抛', () async {
      await MemoryPinVault().forget('C40221');
    });
  });

  group('名册和 PIN 库配合', () {
    test('从名册删一只，PIN 也得跟着没', () async {
      // 条目没了钥匙还留着，是一份谁也管不到的残留 —— 界面上看不见它，
      // 所以也没人会想起来去删。
      final reg = RobotRegistry()..add(_sanHao);
      final vault = MemoryPinVault();
      await vault.write('C40221', '704311');

      reg.remove('C40221');
      await vault.forget('C40221');

      expect(reg.bySn('C40221'), isNull);
      expect(await vault.read('C40221'), isNull);
    });
  });
}
