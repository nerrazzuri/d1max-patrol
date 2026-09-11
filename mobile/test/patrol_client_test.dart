/// 狗的 HTTP 客户端。
///
/// **测试起的是真的 `HttpServer`，不是 mock。** 这个任务要证的就是「请求真的
/// 长什么样」—— PIN 在不在请求体里、token 在不在请求头里 —— 把 HttpClient
/// mock 掉，恰好把要证的那件事一起 mock 掉了。
library;

import 'dart:async';
import 'dart:io';

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

  test('狗跟着应答发的那句 notice 要留下来', () async {
    /// §6.3：**这句话跟着每一个带 operator 的响应发出去**
    /// （`server.py:1158` 的 `OPERATOR_NOTICE`）。丢掉它，界面上就只剩
    /// `operator_verified: false` 这一位 —— 那一位说得出「没核」，说不出
    /// 「为什么不核、要可信的身份该怎么办」，而措辞的真理源在狗那头：
    /// 手机硬编一句的话，狗改了措辞手机不跟。
    const String notice = '操作人姓名是本机记账用的,狗不核实它。要可信的人身份,得接服务器。';
    dog.replies['/api/auth'] = <String, dynamic>{
      ...dog.replies['/api/auth'] as Map<String, dynamic>,
      'notice': notice,
    };
    final s = await c.unlock('864209', operator: '张三');
    expect(s.notice, notice);
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

  // ---------------------------------------------------------- 必修 4

  test('周期路径可以自带一个更短的期限,不必等满那 10 秒', () async {
    /// 10 秒那个默认值是给 `unlock`、盘况这类**一次性**请求的。摇杆一拍、
    /// 一次心跳、一次校准的答案在一秒之后就没有意义了 —— 等满 10 秒的结果
    /// 是这条路上的闸一直关着，后面十几拍一拍也发不出去。
    dog.hangPaths.add('/api/state');
    final Stopwatch sw = Stopwatch()..start();
    await expectLater(() => c.get('/api/state', const Duration(milliseconds: 150)),
        throwsA(isA<PatrolError>()));
    sw.stop();
    expect(sw.elapsed, lessThan(const Duration(seconds: 3)),
        reason: '走的是这一条请求自带的期限,不是客户端那个 10 秒的默认值');
  }, timeout: const Timeout(Duration(seconds: 15)));

  test('超时那一下真的把 socket 掐掉,不是只让 Future 完成', () async {
    /// **必修 4 的另一半。** `Future.timeout` 只管「我不等了」：它一个字节
    /// 也不往 socket 上写，那条 TCP 连接照样连着。一分钟几十拍地超时下去，
    /// 连接会一条条堆起来，把狗热点那点上行挤死 —— 而屏上报的是「看不见就
    /// 不许开狗」，人会掉头去修相机。
    ///
    /// **光靠 `HttpClientRequest.abort()` 不够。** `http_impl.dart` 里那一句
    /// `if (!_responseCompleter.isCompleted)` 写得很清楚：响应头**已经到了**
    /// 的时候 `abort()` 是个空操作。而「头到了、body 卡住」恰恰是狗那头
    /// HTTP 线程忙着扫盘写归档时最常落到的那一步。那一半只有把响应体那条
    /// 订阅取消掉才掐得断。本机实测过这四种收尾方式，只有取消订阅那一种
    /// 真的把 socket 关上了。
    ///
    /// **观测点在 socket 自己身上。** 从狗那头数连接数是数不出来的：
    /// 服务器那个 `HttpConnection` 对象在它下一次往这条连接上写之前不会察觉
    /// 对面已经走了，`connectionsInfo()` 照样把它算进去 —— 两种局面在那儿
    /// 长得一模一样。
    dog.hangPaths.add('/api/state');
    final List<Socket> sockets = <Socket>[];
    final HttpClient io = HttpClient();
    io.connectionFactory = (Uri uri, String? proxyHost, int? proxyPort) async {
      final ConnectionTask<Socket> task =
          await Socket.startConnect(uri.host, uri.port);
      unawaited(task.socket.then(sockets.add));
      return task;
    };
    // **注入进来的 `HttpClient` 不归 `PatrolClient` 所有**（N-1/N-2），
    // 所以这条测试自己负责关它。
    final PatrolClient impatient = PatrolClient(dog.baseUrl, io: io);
    await expectLater(
        () => impatient.get('/api/state', const Duration(milliseconds: 200)),
        throwsA(isA<PatrolError>()));
    expect(sockets, hasLength(1), reason: '这一条请求只该开出一条连接来');
    // 掐是异步的（取消一条订阅要走几个 microtask/事件循环）。**等的是条件
    // 成立，不是固定 sleep**：掐掉了立刻走，没掐掉才等满这几秒再红。
    bool dead() {
      try {
        sockets.first.remotePort;
        return false;
      } on SocketException catch (_) {
        return true;
      }
    }

    final DateTime deadline = DateTime.now().add(const Duration(seconds: 5));
    while (!dead() && DateTime.now().isBefore(deadline)) {
      await Future<void>.delayed(const Duration(milliseconds: 20));
    }
    expect(dead(), isTrue,
        reason: 'socket 还活着 —— 超时只是不再等，连接还占着狗那头的槽位');
    io.close(force: true);
  }, timeout: const Timeout(Duration(seconds: 15)));
}
