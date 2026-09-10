/// 名册怎么存。
///
/// 分成两个口子，是因为存的是两种东西：
///
/// * **名册本身**（名字、SSID、地址）—— 丢了只是要重填一遍，用普通 JSON 文件。
/// * **PIN** —— 丢了等于把狗交出去，必须进系统密钥库。
///
/// 两者分开还有一个好处：名册文件可以随便导出、随便贴到工单里，里面不会
/// 夹带钥匙。
library;

import 'dart:convert';
import 'dart:io';

import '../model/registry.dart';
import '../model/robot.dart';

/// 名册的存取口。
abstract class RegistryStore {
  /// 读出来。文件不存在就给一本空名册 —— 第一次装 app 是正常情况，不是错误。
  Future<RobotRegistry> load();

  /// 整本写回去。
  Future<void> save(RobotRegistry registry);

  /// 这只狗上一次是谁在开。没记过就是空串 —— **不是 `null`，也不编一个**。
  ///
  /// **按狗分别记**（人拍的板 3）：换一只狗常常就是换一个班。全局记一个的话，
  /// 现场同时开着两只狗的那位，在第二只上看到的是第一只上那个班的名字。
  Future<String> readOperator({required String dog});

  /// 记下这只狗此刻是谁在开。**落盘，重开 app 还在。**
  Future<void> saveOperator({required String dog, required String name});
}

/// 存成一个 JSON 文件。
///
/// 写的时候**先写临时文件再改名**：手机随时可能被掐电，半截的 JSON 文件
/// 下次读出来就是"名册没了"。改名在同一个文件系统上是原子的，掐在中间要么
/// 是旧的整本，要么是新的整本。
class JsonFileStore implements RegistryStore {
  final File file;

  const JsonFileStore(this.file);

  @override
  Future<RobotRegistry> load() async {
    if (!await file.exists()) {
      return RobotRegistry();
    }
    final text = await file.readAsString();
    if (text.trim().isEmpty) {
      return RobotRegistry();
    }
    final raw = jsonDecode(text);
    if (raw is! List) {
      throw const FormatException('名册文件的顶层应该是个数组');
    }
    final robots = <Robot>[];
    for (final item in raw) {
      if (item is! Map) {
        throw const FormatException('名册里有条目不是对象');
      }
      robots.add(Robot.fromJson(Map<String, dynamic>.from(item)));
    }
    return RobotRegistry.fromList(robots);
  }

  @override
  Future<void> save(RobotRegistry registry) async {
    final dir = file.parent;
    if (!await dir.exists()) {
      await dir.create(recursive: true);
    }
    final text = const JsonEncoder.withIndent('  ').convert(registry.toJson());
    final tmp = File('${file.path}.tmp');
    await tmp.writeAsString('$text\n', flush: true);
    await tmp.rename(file.path);
  }

  /// 姓名记在名册**旁边的另一个文件**里，不并进名册。
  ///
  /// 名册那份文件的用法写在文件头：可以随便导出、随便贴到工单里。值班人的
  /// 姓名不该跟着工单出门 —— 它不是秘密，但它也不该在一份要发出去的附件里
  /// 躺着。分开存，导出名册这件事就永远带不上它。
  File get operatorFile => File('${file.path}.operators.json');

  /// 盘上那份「哪只狗上一次是谁在开」。读不动、坏了都当成一本空的。
  ///
  /// **这一份跟名册的处置不一样。** 名册读坏了要喊出来（人会以为自己没登记
  /// 过，然后重填一遍，而坏文件还躺在盘上）；姓名读坏了只是要再问一句「你是
  /// 谁」，把人挡在名册屏外面反而更坏。
  Future<Map<String, String>> _operators() async {
    try {
      if (!await operatorFile.exists()) return <String, String>{};
      final text = await operatorFile.readAsString();
      if (text.trim().isEmpty) return <String, String>{};
      final raw = jsonDecode(text);
      if (raw is! Map) return <String, String>{};
      return <String, String>{
        for (final e in raw.entries)
          if (e.value is String) '${e.key}': e.value as String,
      };
    } catch (_) {
      return <String, String>{};
    }
  }

  @override
  Future<String> readOperator({required String dog}) async =>
      (await _operators())[dog] ?? '';

  @override
  Future<void> saveOperator(
      {required String dog, required String name}) async {
    final dir = operatorFile.parent;
    if (!await dir.exists()) {
      await dir.create(recursive: true);
    }
    final Map<String, String> all = await _operators();
    all[dog] = name;
    // 先写临时文件再改名，跟名册一个理由：掐在中间要么是旧的整本，要么是
    // 新的整本，不会是半截 JSON。
    final tmp = File('${operatorFile.path}.tmp');
    await tmp.writeAsString(
        '${const JsonEncoder.withIndent('  ').convert(all)}\n',
        flush: true);
    await tmp.rename(operatorFile.path);
  }
}

/// 只在内存里。**测试用，别拿去装到手机上。**
class MemoryRegistryStore implements RegistryStore {
  RobotRegistry _held = RobotRegistry();

  /// SN → 这只狗上一次是谁在开。**按狗一格**，跟盘上那份一个形状。
  final Map<String, String> _operators = <String, String>{};

  @override
  Future<RobotRegistry> load() async => _held;

  @override
  Future<void> save(RobotRegistry registry) async {
    _held = RobotRegistry.fromList(registry.all);
  }

  @override
  Future<String> readOperator({required String dog}) async =>
      _operators[dog] ?? '';

  @override
  Future<void> saveOperator(
      {required String dog, required String name}) async {
    _operators[dog] = name;
  }
}
