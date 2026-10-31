/// App 从站点更新（W55）：自助客户没有应用商店渠道，新版 App 由管理员 / 工程师传到站点（公司签名，站点验签）；
/// App 登录后问站点有没有比自己新的（比构建号），有就提示，点了下载、核对大小和指纹，再交给系统安装：
/// - 安卓：系统的安装确认（`d1max/install` 通道，原生 `PackageInstaller`；第一次要人允许「安装未知应用」）。
/// - 桌面：存到「下载」文件夹并打开那个文件夹，由人关掉 App 后运行安装包。
library;

import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';

import '../l10n.dart';
import 'site_client.dart';

/// 这台设备在站点上按哪个平台取安装包；不认的（iOS、网页）回 null：不问。
String? updatePlatform() {
  if (kIsWeb) return null;
  if (Platform.isAndroid) return 'android';
  if (Platform.isLinux) return 'linux';
  if (Platform.isWindows) return 'windows';
  if (Platform.isMacOS) return 'macos';
  return null;
}

/// 安装包名只许站点那边认的样子（防路径穿越：名字要拿来拼本地路径）。
final RegExp _safeName = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$');

/// 下载、安装。测试换掉 [install]。
class AppUpdater {
  final SiteApi api;
  final String platform;

  /// 下载到哪个目录（默认：安卓用缓存目录，桌面用「下载」文件夹）。
  final Future<Directory> Function()? dir;

  /// 交给系统安装；回给人看的一句话（成功也回：桌面要告诉人文件在哪）。
  final Future<String> Function(File file)? install;

  AppUpdater({required this.api, required this.platform, this.dir, this.install});

  /// 有更新回站点给的信息，没有回 null。
  Future<Map<String, dynamic>?> check() async {
    final d = await api.appUpdate(platform, appBuild);
    return d['available'] == true ? d : null;
  }

  Future<String> downloadAndInstall(Map<String, dynamic> info, {void Function(double)? progress}) async {
    final name = '${info['name'] ?? ''}';
    final size = (info['size'] as num?)?.toInt() ?? 0;
    final sha = '${info['sha256'] ?? ''}';
    if (!_safeName.hasMatch(name) || size <= 0 || sha.length != 64) {
      throw SiteError(0, tr('The site sent incomplete installer details', '站点返回的安装包信息不完整'));
    }
    final d = await (dir ?? _defaultDir)();
    await d.create(recursive: true);
    final f = File('${d.path}${Platform.pathSeparator}$name');
    await api.downloadAppUpdate(platform, f,
        size: size, expectSha256: sha, progress: (got) => progress?.call(got / size));
    return (install ?? _installDefault)(f);
  }

  Future<Directory> _defaultDir() async {
    if (platform == 'android') {
      return Directory('${(await getTemporaryDirectory()).path}${Platform.pathSeparator}updates');
    }
    return await getDownloadsDirectory() ?? await getApplicationDocumentsDirectory();
  }

  static const MethodChannel _ch = MethodChannel('d1max/install');

  Future<String> _installDefault(File f) async {
    if (platform == 'android') {
      final r = await _ch.invokeMethod<String>('apk', <String, dynamic>{'path': f.path});
      if (r == 'need_permission') {
        return tr('Allow "Install unknown apps" for this app in the settings page that just opened, then tap Update again.',
            '请在刚打开的设置页中允许本应用「安装未知应用」，然后再点一次「更新」。');
      }
      return tr('Confirm the installation in the system dialog.', '请在系统弹窗中确认安装。');
    }
    final folder = f.parent.path;
    try {
      if (Platform.isLinux) {
        await Process.run('xdg-open', <String>[folder]);
      } else if (Platform.isWindows) {
        await Process.run('explorer', <String>[folder]);
      } else if (Platform.isMacOS) {
        await Process.run('open', <String>[folder]);
      }
    } on Object {
      // 打不开文件夹也没关系：路径写在提示里
    }
    return tr('Saved to ${f.path}. Close the app, then run the installer.',
        '已保存到 ${f.path}。请关闭 App 后运行安装包。');
  }
}
