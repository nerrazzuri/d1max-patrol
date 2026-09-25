/// 站点列表文件放在手机的哪个目录。
///
/// 用 app 私有的 documents 目录：别的 app 读不到，卸载 app 会跟着删掉。
/// 不用外部存储 —— 那里别的 app 也能改，站点地址或证书指纹被改掉就意味着连错站点。
library;

import 'dart:io';

import 'package:path_provider/path_provider.dart';

import 'site_store.dart';

/// 站点列表文件（W00c4）。
const String sitesFileName = 'sites.json';

Future<SiteStore> openSiteStore() async {
  final dir = await getApplicationDocumentsDirectory();
  return JsonSiteStore(File('${dir.path}/$sitesFileName'));
}
