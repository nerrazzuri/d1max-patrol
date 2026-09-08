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
    // **假狗自己出的错要看得见。** 不看这一眼的话，假狗里的笔误（回复里塞了
    // 个编码不了的东西之类）长得跟「这条路不回话」一模一样，人会掉头去查
    // 客户端的超时和重试。跟 `teleop_page_test.dart` 的 `unmount` 一个规矩。
    expect(dog.errors, isEmpty, reason: '假狗自己抛了：查的是假狗，不是被测代码');
  });

  test('PIN 从不上网', () async {
    await c.unlock('864209', operator: '张三');
    final exchange = dog.received.firstWhere((r) => r.path == '/api/auth');
    final body = exchange.body as Map<String, dynamic>;
    expect(body.containsKey('pin'), isFalse,
        reason: '明文 PIN 在狗的热点上人人可解；而且那条老路只换得到只读凭证');
    expect(body['proof'], isA<String>());
    expect(body['nonce'], 'a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4');
    // N-5：不只信解析后的 body，原始字节里也不许有 PIN 或 "pin" 这个词
    // （不分大小写）——防的是「解析层把 PIN 过滤掉了，但线路上其实发了」
    // 这种更隐蔽的漏法。
    final rawLower = exchange.raw.toLowerCase();
    expect(rawLower.contains('864209'), isFalse,
        reason: '原始请求体字节里不许出现明文 PIN');
    expect(rawLower.contains('pin'), isFalse,
        reason: '原始请求体字节里不许出现 "pin" 这个词（不分大小写）');
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

  // ---------------------------------------------------------- B-1 复审回合

  test('狗回的质询没有 nonce 时抛 PatrolError，不是裸的类型错误', () async {
    // 连错热点、撞上强制门户时常见的形状：200，但body里什么都没有。
    dog.replies['/api/auth/challenge'] = <String, dynamic>{};
    expect(() => c.unlock('864209', operator: '张三'),
        throwsA(isA<PatrolError>()));
  });

  test('狗回的应答没有 token 时抛 PatrolError，不是裸的类型错误', () async {
    dog.replies['/api/auth'] = <String, dynamic>{
      'operator': '张三',
      'operator_verified': false,
      'readonly': false,
    };
    expect(() => c.unlock('864209', operator: '张三'),
        throwsA(isA<PatrolError>()));
  });

  // ---------------------------------------------------------- S-1 复审回合

  test('质询挂了就抛出去，绝不退回明文 PIN 换 token', () async {
    dog.statusCodes['/api/auth/challenge'] = 500;
    dog.replies['/api/auth/challenge'] = <String, dynamic>{'error': '狗挂了'};
    await expectLater(() => c.unlock('864209', operator: '张三'),
        throwsA(isA<PatrolError>()));
    // 质询这步就失败了，压根不该走到换 token 那一步。
    expect(dog.received.where((r) => r.path == '/api/auth'), isEmpty,
        reason: '质询失败不许退回去用明文 PIN 换只读 token');
    for (final r in dog.received) {
      final body = r.body;
      if (body is Map) {
        expect(body.containsKey('pin'), isFalse,
            reason: '任何一次请求的 body 里都不许出现明文 PIN 字段');
      }
    }
  });

  // ---------------------------------------------------------- B-2 复审回合

  test('body 卡住不来也不会永远转圈——超时罩住整个响应，不只是响应头',
      () async {
    dog.hangPaths.add('/api/auth/challenge');
    final impatient =
        PatrolClient(dog.baseUrl, timeout: const Duration(milliseconds: 200));
    await expectLater(() => impatient.unlock('864209', operator: '张三'),
        throwsA(isA<PatrolError>()));
    impatient.close();
  }, timeout: const Timeout(Duration(seconds: 5)));
}
