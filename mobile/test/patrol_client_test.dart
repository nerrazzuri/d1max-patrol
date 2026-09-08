/// 狗的 HTTP 客户端。
///
/// **测试起的是真的 `HttpServer`，不是 mock。** 这个任务要证的就是「请求真的
/// 长什么样」—— PIN 在不在请求体里、token 在不在请求头里 —— 把 HttpClient
/// mock 掉，恰好把要证的那件事一起 mock 掉了。
library;

import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_dog.dart';

void main() {
  late FakeDog dog;
  late PatrolClient c;

  setUp(() async {
    dog = FakeDog();
    await dog.start();
    dog.replies['/api/auth/challenge'] = <String, dynamic>{
      'nonce': 'a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4',
      'ttl_s': 60,
      'alg': 'HMAC-SHA256',
    };
    dog.replies['/api/auth'] = <String, dynamic>{
      'token': 'tok-123',
      'operator': '张三',
      'operator_verified': false,
      'readonly': false,
    };
    c = PatrolClient(dog.baseUrl);
  });

  tearDown(() async {
    c.close();
    await dog.stop();
  });

  test('PIN 从不上网', () async {
    await c.unlock('864209', operator: '张三');
    final exchange = dog.received.firstWhere((r) => r.path == '/api/auth');
    final body = exchange.body as Map<String, dynamic>;
    expect(body.containsKey('pin'), isFalse,
        reason: '明文 PIN 在狗的热点上人人可解；而且那条老路只换得到只读凭证');
    expect(body['proof'], isA<String>());
    expect(body['nonce'], 'a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4');
  });

  test('拿到 token 之后每个请求都带着它', () async {
    await c.unlock('864209', operator: '张三');
    await c.get('/api/state');
    final last = dog.received.last;
    expect(last.auth, 'Bearer tok-123');
  });

  test('换 token 之前不带请求头', () async {
    await c.unlock('864209', operator: '张三');
    final challenge = dog.received.first;
    expect(challenge.path, '/api/auth/challenge');
    expect(challenge.auth, isNull);
  });

  test('operator_verified 是 false 要能读出来', () async {
    final s = await c.unlock('864209', operator: '张三');
    expect(s.operatorVerified, isFalse,
        reason: '§6.3：狗记下名字但不核实。界面上不许说成「已登录：张三」');
  });

  test('狗回错就抛出说得清楚的异常', () async {
    dog.statusCodes['/api/auth'] = 401;
    dog.replies['/api/auth'] = <String, dynamic>{
      'error': 'PIN 不对',
      'detail': '还可以试 3 次',
    };
    expect(() => c.unlock('000000', operator: ''),
        throwsA(isA<PatrolError>()
            .having((e) => e.status, 'status', 401)
            .having((e) => e.detail, 'detail', contains('3 次'))));
  });

  test('连不上也抛 PatrolError，不是裸的 SocketException', () async {
    final deadClient = PatrolClient('http://127.0.0.1:1');
    expect(
        () => deadClient.get('/api/state'), throwsA(isA<PatrolError>()));
    deadClient.close();
  });
}
