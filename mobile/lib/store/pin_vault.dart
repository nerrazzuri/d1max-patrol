/// PIN 放哪。
///
/// PIN 是控制一台机器的钥匙：拿到它就能遥控、能急停、能让狗自己跑起来。
/// 所以它跟名册分开存，而且**不进任何会被导出、备份、贴进工单的文件**。
///
/// 真机上的实现要落在 Android Keystore 后面（`flutter_secure_storage` 之类）。
/// 那一步要引插件、要动 Gradle，等操作界面那条岔路定了再一起做 —— 这里先把
/// 口子留出来，让上面的逻辑现在就能写、能测。
library;

/// 按 SN 存取 PIN。
abstract class PinVault {
  /// 取这只狗的 PIN。没存过返回 null。
  Future<String?> read(String sn);

  /// 存这只狗的 PIN。已经有就覆盖。
  Future<void> write(String sn, String pin);

  /// 忘掉这只狗的 PIN。
  ///
  /// 从名册里删一只狗的时候必须连着调这个 —— 不然条目没了钥匙还留着，
  /// 是一份谁也管不到的残留。
  Future<void> forget(String sn);
}

/// 只在内存里。
///
/// **测试用，别拿去装到手机上。** 进程一退就没了，而且从来没加密过。
class MemoryPinVault implements PinVault {
  final Map<String, String> _held = <String, String>{};

  @override
  Future<String?> read(String sn) async => _held[sn.trim()];

  @override
  Future<void> write(String sn, String pin) async {
    _held[sn.trim()] = pin;
  }

  @override
  Future<void> forget(String sn) async {
    _held.remove(sn.trim());
  }
}
