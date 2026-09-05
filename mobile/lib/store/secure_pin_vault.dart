/// 真机上的 PIN 库：落在 Android Keystore 后面。
///
/// `flutter_secure_storage` 在 Android 上用的是 `EncryptedSharedPreferences`，
/// 密钥由 Keystore 管、不出安全硬件。所以：
///
/// * 手机没解锁的时候读不出来；
/// * `adb backup` 和云备份带不走（下面显式关掉了备份）；
/// * root 过的机器另说 —— 那种情况下手机上任何东西都保不住，不是这一层能管的。
///
/// **PIN 只在这里，不在名册文件里。** 名册文件会被导出、会被贴进工单，
/// 钥匙跟进去一次就收不回来。
library;

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'pin_vault.dart';

/// 存在 Keystore 后面的 PIN 库。
class SecurePinVault implements PinVault {
  final FlutterSecureStorage _storage;

  /// **``encryptedSharedPreferences: true`` 是必须显式打开的。**
  /// 这个插件在 Android 上的默认行为是普通 SharedPreferences —— 也就是明文。
  /// 不写这一行，PIN 就是以纯文本躺在 app 私有目录里，这一整个类等于白写。
  SecurePinVault([FlutterSecureStorage? storage])
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  /// 键名。带前缀是为了跟这个库里可能出现的别的东西分开。
  static String keyFor(String sn) => 'pin/${sn.trim()}';

  @override
  Future<String?> read(String sn) => _storage.read(key: keyFor(sn));

  @override
  Future<void> write(String sn, String pin) =>
      _storage.write(key: keyFor(sn), value: pin);

  @override
  Future<void> forget(String sn) => _storage.delete(key: keyFor(sn));
}
