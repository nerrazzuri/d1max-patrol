/// 站点列表文件放在手机的哪个目录。
///
/// 手机上用 app 私有的 documents 目录：别的 app 读不到，卸载 app 会跟着删掉；桌面版用支持目录（见下）。
/// 不用外部存储 —— 那里别的 app 也能改，站点地址或证书指纹被改掉就意味着连错站点。
library;

import 'dart:io';

import 'package:path_provider/path_provider.dart';

import 'site_store.dart';

/// 站点列表文件（W00c4）。
const String sitesFileName = 'sites.json';

Future<SiteStore> openSiteStore() async {
  // 桌面版（W15）：documents 是用户的「文档」文件夹（~/Documents、我的文档），谁都看得见、改得了；
  // 用 app 自己的支持目录（Linux ~/.local/share/<应用>、Windows %APPDATA%、Mac Application Support）。
  // 这只防误删误改、不摆在用户眼前，不是安全边界：同一个系统用户照样能改（外审备注）。
  final dir = Platform.isLinux || Platform.isWindows || Platform.isMacOS
      ? await getApplicationSupportDirectory()
      : await getApplicationDocumentsDirectory();
  return JsonSiteStore(File('${dir.path}/$sitesFileName'));
}
