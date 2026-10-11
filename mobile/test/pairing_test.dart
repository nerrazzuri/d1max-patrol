// 配对码（App V2）：手机读的跟站点出的是同一个形状；坏码、别的码、形状不对的地址都不收。
import 'dart:convert';

import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/pairing.dart';
import 'package:flutter_test/flutter_test.dart';

// 这两条是站点的 d1max_site.pairing.encode 真出的（中文站点名、IPv6 地址各一条）。
const _fromSite = 'D1MAXSITE1.eyJmIjoiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYiIsIm4iOiLluoTlm63kuIDlj7ciLCJ1IjoiaHR0cHM6Ly8xOTIuMTY4LjEuNTo4NDQzIn0';
const _fromSiteV6 = 'D1MAXSITE1.eyJmIjoiY2RjZGNkY2RjZGNkY2RjZGNkY2RjZGNkY2RjZGNkY2RjZGNkY2RjZGNkY2RjZGNkY2RjZGNkY2RjZGNkY2RjZCIsIm4iOiJFYXN0IEdhdGUiLCJ1IjoiaHR0cHM6Ly9bZmQwMDo6MV06ODQ0MyJ9';

String _code(Object? payload) =>
    pairingPrefix + base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');

final _fp = 'ab' * 32;

void main() {
  test('站点出的码读得出来：中文站点名、IPv6 地址；前后的空白不算', () {
    final a = decodePairing('  $_fromSite\n');
    expect(a.name, '庄园一号');
    expect(a.url, 'https://192.168.1.5:8443');
    expect(a.fingerprint, _fp);
    final b = decodePairing(_fromSiteV6);
    expect(b.name, 'East Gate');
    expect(b.url, 'https://[fd00::1]:8443');
    expect(b.fingerprint, 'cd' * 32);
  });

  test('狗的开通码、别的二维码：说清楚不是站点的码', () {
    expect(() => decodePairing('D1MAX1.abcdef'),
        throwsA(isA<PairingException>().having((e) => e.message, 'message', contains('狗的开通码'))));
    expect(() => decodePairing('https://example.com/'),
        throwsA(isA<PairingException>().having((e) => e.message, 'message', contains('不是 D1 Max 站点'))));
    expect(() => decodePairing(''), throwsA(isA<PairingException>()));
  });

  test('坏码一律不收', () {
    final bad = <String>[
      pairingPrefix,
      '$pairingPrefix!!!!',
      '$pairingPrefix${base64Url.encode(<int>[0xff, 0xfe])}',
      _code(<int>[1]),
      _code(<String, Object?>{'n': 'a', 'u': 'https://s:8443'}),
      _code(<String, Object?>{'n': 1, 'u': 'https://s:8443', 'f': _fp}),
      _code(<String, Object?>{'n': ' ', 'u': 'https://s:8443', 'f': _fp}),
      _code(<String, Object?>{'n': 'x' * 65, 'u': 'https://s:8443', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://s:8443', 'f': 'AB' * 32}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://s:8443', 'f': 'ab' * 31}),
      // 地址只收 https://主机[:端口]
      _code(<String, Object?>{'n': 'a', 'u': 'http://s:8443', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://u:p@s:8443', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://s:8443/api', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://s:8443?x=1', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://s:8443#f', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://${'a' * 200}', 'f': _fp}),
      _code(<String, Object?>{'n': 'a', 'u': 'https://[::1', 'f': _fp}),
    ];
    for (final c in bad) {
      expect(() => decodePairing(c),
          throwsA(isA<PairingException>().having((e) => e.message, 'message', contains('坏了'))),
          reason: c);
    }
  });

  test('末尾的斜杠去掉；英文界面的报错是英文', () {
    expect(decodePairing(_code(<String, Object?>{'n': 'a', 'u': 'https://s:8443/', 'f': _fp})).url,
        'https://s:8443');
    appLang.value = AppLang.en;
    addTearDown(() => appLang.value = AppLang.zh);
    expect(() => decodePairing('nope'),
        throwsA(isA<PairingException>().having((e) => e.message, 'message', "That's not a D1 Max site code.")));
  });
}
