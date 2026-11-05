/// 站点列表怎么存（W00c4）。存的是**站点地址、证书指纹、账号名**——口令不存（每次登录输），
/// 令牌只在内存里。写法同 `registry_store.dart`：先写临时文件再改名，掐电不会留半截。
library;

import 'dart:convert';
import 'dart:io';

import '../l10n.dart';

/// 一个站点。
class SiteEntry {
  final String name;
  final String url;
  final String fingerprint;
  final String username;

  /// 备用地址（POC 11-05：同一个站点在不同网里地址不同）。登录时 [url] 先试，连不上按顺序试这些。
  final List<String> alts;
  const SiteEntry(
      {required this.name,
      required this.url,
      required this.fingerprint,
      required this.username,
      this.alts = const <String>[]});

  /// 登录时按这个顺序试。
  List<String> get urls => <String>[url, ...alts.where((a) => a != url)];

  /// 把 [u] 挪到最前（登上了的那个下次先试）。
  SiteEntry preferring(String u) {
    final rest = urls.where((x) => x != u).toList();
    return copyWith(url: u, alts: rest);
  }

  SiteEntry copyWith({String? url, String? username, List<String>? alts}) => SiteEntry(
      name: name,
      url: url ?? this.url,
      fingerprint: fingerprint,
      username: username ?? this.username,
      alts: alts ?? this.alts);

  Map<String, dynamic> toJson() => <String, dynamic>{
        'name': name,
        'url': url,
        'fingerprint': fingerprint,
        'username': username,
        if (alts.isNotEmpty) 'alts': alts,
      };

  static SiteEntry fromJson(Map<String, dynamic> d) => SiteEntry(
      name: d['name'] as String? ?? '',
      url: d['url'] as String? ?? '',
      fingerprint: d['fingerprint'] as String? ?? '',
      username: d['username'] as String? ?? '',
      alts: ((d['alts'] as List?) ?? const []).whereType<String>().toList());
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
    if (raw is! List) {
      throw FormatException(tr('The top level of the sites file must be a list', '站点文件格式错误（顶层应为数组）'));
    }
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
