/// 名册文件放在手机的哪个目录。
///
/// 用 app 私有的 documents 目录：别的 app 读不到，卸载 app 会跟着删掉。
/// 不用外部存储 —— 那里别的 app 也能改，名册被改掉就意味着按错狗。
library;

import 'dart:io';

import 'package:path_provider/path_provider.dart';

import 'registry_store.dart';

/// 名册文件叫什么。
const String rosterFileName = 'robots.json';

/// 真机上的名册存放位置。
Future<RegistryStore> openRegistryStore() async {
  final dir = await getApplicationDocumentsDirectory();
  return JsonFileStore(File('${dir.path}/$rosterFileName'));
}
