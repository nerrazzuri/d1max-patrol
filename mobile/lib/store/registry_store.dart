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
}

/// 只在内存里。**测试用，别拿去装到手机上。**
class MemoryRegistryStore implements RegistryStore {
  RobotRegistry _held = RobotRegistry();

  @override
  Future<RobotRegistry> load() async => _held;

  @override
  Future<void> save(RobotRegistry registry) async {
    _held = RobotRegistry.fromList(registry.all);
  }
}
