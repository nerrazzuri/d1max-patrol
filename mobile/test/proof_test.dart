/// 质询-应答的跨语言向量。
///
/// **两头各自实现同一个算法、各自绿、合起来对不上**，是这一类代码最典型的
/// 翻车法。它翻车的表现是「手机上输对 PIN 也进不去」，而两边的测试都是绿的。
///
/// 向量由 `tests/app/test_wire_fixtures.py` 从 `auth.proof_for` 生成。
library;

import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('证明跟狗那头算出来的一样', () {
    final fx = jsonDecode(
        File('test/fixtures/proof_vectors.json').readAsStringSync())
        as Map<String, dynamic>;
    expect(fx['alg'], 'HMAC-SHA256');
    for (final v in fx['vectors'] as List<dynamic>) {
      final m = v as Map<String, dynamic>;
      expect(proofFor(m['pin'] as String, m['nonce'] as String), m['proof'],
          reason: 'PIN ${m['pin']} / nonce ${m['nonce']}');
    }
  });
}
