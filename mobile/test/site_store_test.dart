// 站点列表落盘（W00c4）：往返、坏文件报错不吞、口令不在文件里。
import 'dart:io';

import 'package:d1max_patrol/store/site_store.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('写了读回来一样；文件里没有口令', () async {
    final dir = await Directory.systemTemp.createTemp('sites');
    final f = File('${dir.path}/sites.json');
    final store = JsonSiteStore(f);
    expect(await store.load(), isEmpty);
    final s = [
      const SiteEntry(name: '庄园', url: 'https://h:8443', fingerprint: 'ab', username: 'gina')
    ];
    await store.save(s);
    final back = await store.load();
    expect(back.single.toJson(), s.single.toJson());
    expect(await f.readAsString(), isNot(contains('password')));
    await dir.delete(recursive: true);
  });

  test('坏文件：报错，不当成空列表', () async {
    final dir = await Directory.systemTemp.createTemp('sites');
    final f = File('${dir.path}/sites.json')..writeAsStringSync('{"not": "a list"}');
    await expectLater(JsonSiteStore(f).load(), throwsA(isA<FormatException>()));
    await dir.delete(recursive: true);
  });
}
