/// 站点列表怎么存（W00c4）。存的是**站点地址、证书指纹、账号名**——口令不存（每次登录输），
/// 令牌只在内存里。写法同 `registry_store.dart`：先写临时文件再改名，掐电不会留半截。
library;

import 'dart:convert';
import 'dart:io';

/// 一个站点。
class SiteEntry {
  final String name;
  final String url;
  final String fingerprint;
  final String username;
  const SiteEntry(
      {required this.name,
      required this.url,
      required this.fingerprint,
      required this.username});

  Map<String, dynamic> toJson() => <String, dynamic>{
        'name': name,
        'url': url,
        'fingerprint': fingerprint,
        'username': username,
      };

  static SiteEntry fromJson(Map<String, dynamic> d) => SiteEntry(
      name: d['name'] as String? ?? '',
      url: d['url'] as String? ?? '',
      fingerprint: d['fingerprint'] as String? ?? '',
      username: d['username'] as String? ?? '');
}

abstract class SiteStore {
  Future<List<SiteEntry>> load();
  Future<void> save(List<SiteEntry> sites);
}

class JsonSiteStore implements SiteStore {
  final File file;
  const JsonSiteStore(this.file);

  @override
  Future<List<SiteEntry>> load() async {
    if (!await file.exists()) return <SiteEntry>[];
    final text = await file.readAsString();
    if (text.trim().isEmpty) return <SiteEntry>[];
    final raw = jsonDecode(text);
    if (raw is! List) throw const FormatException('站点文件的顶层应该是个数组');
    return raw.whereType<Map<String, dynamic>>().map(SiteEntry.fromJson).toList();
  }

  @override
  Future<void> save(List<SiteEntry> sites) async {
    final tmp = File('${file.path}.tmp');
    await tmp.writeAsString(jsonEncode(sites.map((s) => s.toJson()).toList()),
        flush: true);
    await tmp.rename(file.path);
  }
}

/// 测试用：存在内存里。
class MemorySiteStore implements SiteStore {
  List<SiteEntry> sites;
  MemorySiteStore([List<SiteEntry>? initial]) : sites = initial ?? <SiteEntry>[];

  @override
  Future<List<SiteEntry>> load() async => List<SiteEntry>.of(sites);

  @override
  Future<void> save(List<SiteEntry> s) async => sites = List<SiteEntry>.of(s);
}
