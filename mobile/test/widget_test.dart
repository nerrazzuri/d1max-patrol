/// 名册这一屏的整屏测试。
///
/// 存取全是传进去的内存实现，所以这些用例不碰手机、不碰插件，`flutter test`
/// 直接跑。盯的是三件在现场会出事的事：**重复登记不能静默覆盖**、**删一只
/// 要连 PIN 一起没**、以及 **PIN 从头到尾不能落进名册文件**。
library;

import 'package:d1max_patrol/main.dart';
import 'package:d1max_patrol/model/registry.dart';
import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/store/pin_vault.dart';
import 'package:d1max_patrol/store/registry_store.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

const _sanHao = Robot(sn: 'C40221', name: '三号', ssid: 'XG2WIFI_C40221');

/// 读的时候直接抛。用来测"名册坏了"那条路。
class _BrokenStore implements RegistryStore {
  @override
  Future<RobotRegistry> load() async => throw const FormatException('半截 JSON');
  @override
  Future<void> save(RobotRegistry registry) async {}
  // 姓名那两口子也一起坏掉：读不动就当没记过（见 `roster_page.dart`
  // 的 `_readKept`），写不进去不打断人。
  @override
  Future<String> readOperator({required String dog}) async =>
      throw const FormatException('半截 JSON');
  @override
  Future<void> saveOperator({required String dog, required String name}) async {
    throw const FormatException('盘满了');
  }
}

Future<void> _pump(
  WidgetTester tester,
  RegistryStore store,
  PinVault vault,
) async {
  await tester.pumpWidget(PatrolApp(store: store, vault: vault));
  await tester.pumpAndSettle();
}

/// 把对话框里的四个框填上。按 label 找，顺序换了也不会填错格。
Future<void> _fill(
  WidgetTester tester, {
  String? sn,
  String? name,
  String? ssid,
  String? pin,
}) async {
  Future<void> put(String label, String? text) async {
    if (text == null) return;
    await tester.enterText(
      find.ancestor(
        of: find.text(label),
        matching: find.byType(TextField),
      ),
      text,
    );
  }

  await put('机身序列号 SN', sn);
  await put('名字', name);
  await put('热点名 SSID', ssid);
  await put('PIN', pin);
}

void main() {
  testWidgets('一只都没登记时告诉人该干嘛', (tester) async {
    await _pump(tester, MemoryRegistryStore(), MemoryPinVault());
    expect(find.text('还没有登记过狗'), findsOneWidget);
    // 空屏上必须解释清楚"为什么要按 SN 登记"，不然人只会觉得这一步多余。
    expect(find.textContaining('内网地址都一样'), findsOneWidget);
  });

  testWidgets('已经有的狗列出来', (tester) async {
    final store = MemoryRegistryStore();
    await store.save(RobotRegistry()..add(_sanHao));
    await _pump(tester, store, MemoryPinVault());
    expect(find.text('三号（C40221）'), findsOneWidget);
    expect(find.textContaining('XG2WIFI_C40221'), findsOneWidget);
  });

  testWidgets('兜底 SN 在列表上标出来', (tester) async {
    // 不标的话人会把 mac: 开头那串当成厂商给的号，换块主板就对不上了。
    final store = MemoryRegistryStore();
    await store.save(RobotRegistry()..add(const Robot(sn: 'mac:1c697a8b2f30')));
    await _pump(tester, store, MemoryPinVault());
    expect(find.textContaining('序列号是兜底的'), findsOneWidget);
  });

  testWidgets('登记一只之后列表上有了，而且落了盘', (tester) async {
    final store = MemoryRegistryStore();
    final vault = MemoryPinVault();
    await _pump(tester, store, vault);

    await tester.tap(find.byType(FloatingActionButton));
    await tester.pumpAndSettle();
    await _fill(tester,
        sn: 'C40221', name: '三号', ssid: 'XG2WIFI_C40221', pin: '704311');
    await tester.tap(find.text('存下'));
    await tester.pumpAndSettle();

    expect(find.text('三号（C40221）'), findsOneWidget);
    // 只在界面上出现不算数 —— 得真存下去，不然一退出就没了。
    expect((await store.load()).bySn('C40221')?.name, '三号');
    expect(await vault.read('C40221'), '704311');
  });

  testWidgets('PIN 没跟着名册一起落盘', (tester) async {
    // 这条盯的是那条硬约定。名册会被导出、会被贴进工单。
    final store = MemoryRegistryStore();
    await _pump(tester, store, MemoryPinVault());

    await tester.tap(find.byType(FloatingActionButton));
    await tester.pumpAndSettle();
    await _fill(tester, sn: 'C40221', pin: '704311');
    await tester.tap(find.text('存下'));
    await tester.pumpAndSettle();

    final saved = (await store.load()).toJson();
    expect(saved.single.keys, isNot(contains('pin')));
    expect(saved.single.values.whereType<String>(), isNot(contains('704311')));
  });

  testWidgets('没填 SN 存不下去', (tester) async {
    await _pump(tester, MemoryRegistryStore(), MemoryPinVault());
    await tester.tap(find.byType(FloatingActionButton));
    await tester.pumpAndSettle();
    await _fill(tester, name: '三号');
    await tester.tap(find.text('存下'));
    await tester.pumpAndSettle();

    // 对话框不能关，而且要说清楚卡在哪。
    expect(find.text('没有 SN 就分不出是哪只狗'), findsOneWidget);
    expect(find.text('存下'), findsOneWidget);
  });

  testWidgets('重复登记被拦住，旧记录不动', (tester) async {
    // 静默覆盖改掉的是已经调好的那条，而界面上看起来一模一样。
    final store = MemoryRegistryStore();
    await store.save(RobotRegistry()..add(_sanHao));
    await _pump(tester, store, MemoryPinVault());

    await tester.tap(find.byType(FloatingActionButton));
    await tester.pumpAndSettle();
    await _fill(tester, sn: 'C40221', name: '手滑');
    await tester.tap(find.text('存下'));
    await tester.pumpAndSettle();

    expect(find.textContaining('已经有 C40221'), findsOneWidget);
    expect((await store.load()).bySn('C40221')?.name, '三号');
  });

  testWidgets('点开一条能改，改完存得下', (tester) async {
    final store = MemoryRegistryStore();
    await store.save(RobotRegistry()..add(_sanHao));
    await _pump(tester, store, MemoryPinVault());

    await tester.tap(find.text('三号（C40221）'));
    await tester.pumpAndSettle();
    await _fill(tester, name: '三号（换了电池）');
    await tester.tap(find.text('存下'));
    await tester.pumpAndSettle();

    expect((await store.load()).length, 1);
    expect((await store.load()).bySn('C40221')?.name, '三号（换了电池）');
  });

  testWidgets('改 SN 等于换一只，旧钥匙不许留下', (tester) async {
    // 旧 SN 的 PIN 留在密钥库里，界面上再也看不见它，也就没人会想起来删。
    final store = MemoryRegistryStore();
    final vault = MemoryPinVault();
    await store.save(RobotRegistry()..add(_sanHao));
    await vault.write('C40221', '704311');
    await _pump(tester, store, vault);

    await tester.tap(find.text('三号（C40221）'));
    await tester.pumpAndSettle();
    await _fill(tester, sn: 'C40999');
    await tester.tap(find.text('存下'));
    await tester.pumpAndSettle();

    expect((await store.load()).bySn('C40221'), isNull);
    expect(await vault.read('C40221'), isNull);
    expect(await vault.read('C40999'), '704311');
  });

  testWidgets('删要先问一句', (tester) async {
    final store = MemoryRegistryStore();
    await store.save(RobotRegistry()..add(_sanHao));
    await _pump(tester, store, MemoryPinVault());

    await tester.tap(find.byIcon(Icons.delete_outline));
    await tester.pumpAndSettle();
    expect(find.textContaining('只动手机上的名册'), findsOneWidget);

    await tester.tap(find.text('算了'));
    await tester.pumpAndSettle();
    expect((await store.load()).length, 1);
  });

  testWidgets('删掉之后名册和钥匙一起没', (tester) async {
    final store = MemoryRegistryStore();
    final vault = MemoryPinVault();
    await store.save(RobotRegistry()..add(_sanHao));
    await vault.write('C40221', '704311');
    await _pump(tester, store, vault);

    await tester.tap(find.byIcon(Icons.delete_outline));
    await tester.pumpAndSettle();
    await tester.tap(find.text('去掉'));
    await tester.pumpAndSettle();

    expect((await store.load()).isEmpty, isTrue);
    expect(await vault.read('C40221'), isNull);
  });

  testWidgets('名册坏了要明说，不能装成一本空的', (tester) async {
    // 装成空的，人就会以为自己从来没登记过，然后重新填一遍，
    // 而坏掉的文件还躺在盘上。
    await _pump(tester, _BrokenStore(), MemoryPinVault());
    expect(find.text('名册读不出来'), findsOneWidget);
    expect(find.text('还没有登记过狗'), findsNothing);
    // 坏的时候不给"加一只"——加了也存不进去。
    expect(find.byType(FloatingActionButton), findsNothing);
  });
}
