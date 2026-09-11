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

  // ------------------------------------------------- 赛道 4c：401 自动重解锁

  /// 「一只狗一条连接」之后，`PatrolClient` 是按 SN 缓存住的，进屏不再
  /// `unlock`。而热点通道上的 token 闲置 30 分钟就作废
  /// （`app/auth.py` 的 `AP_TOKEN_IDLE_S`）—— 人退回名册把手机一揣，半小时
  /// 后再点进任何一屏，那条缓存连接上的票已经死了。没有这一组行为的话，
  /// 这台手机对这只狗就彻底废掉，唯一的出路是杀掉 app 重开。

  /// 数这台狗被打了几次「换 token」。
  int unlocksOn(FakeDog d) =>
      d.received.where((r) => r.path == '/api/auth').length;

  /// 让狗开始查票，并且换一张新的给手机。
  Future<void> armTokens(FakeDog d, PatrolClient client) async {
    d.checkTokens = true;
    await client.unlock('864209', operator: '张三');
  }

  test('token 闲置死了：401 之后自己重新解锁，调用方拿到的是成功的结果', () async {
    dog.replies['/api/state'] = <String, dynamic>{'mode': 'idle'};
    await armTokens(dog, c);
    expect(unlocksOn(dog), 1);
    // 半小时过去了。狗那头把这张票当成不存在。
    dog.liveTokens.clear();

    final Map<String, dynamic> got = await c.get('/api/state');

    expect(got['mode'], 'idle',
        reason: '调用方该拿到成功的结果，而不是一个 401 —— 否则这只狗对这台手机就废了');
    expect(unlocksOn(dog), 2, reason: '重新解锁只许发生一次');
    // 原请求确实重发了一次，而且第二次带的是新票。
    final List<FakeCall> states =
        dog.received.where((r) => r.path == '/api/state').toList();
    expect(states, hasLength(2), reason: '一次撞 401、一次重发，就这两次');
    expect(states.last.auth, 'Bearer ${dog.issued.last}');
  });

  test('三条请求同时撞 401：重新解锁仍然只发生一次（单飞）', () async {
    /// 遥控屏上同时挂着发拍、心跳、校准三条周期路径。三条各自去 `unlock`
    /// 一次 = 一口气烧掉狗那头三个会话名额（`auth.MAX_SESSIONS` 一共 3 个），
    /// 等于把「一只狗一条连接」当场废掉。
    dog.replies['/api/teleop'] = <String, dynamic>{'ok': true};
    dog.replies['/api/teleop/heartbeat'] = <String, dynamic>{'ok': true};
    dog.replies['/api/control'] = <String, dynamic>{'ok': true};
    await armTokens(dog, c);
    dog.liveTokens.clear();

    final List<Map<String, dynamic>> all = await Future.wait(
        <Future<Map<String, dynamic>>>[
          c.post('/api/teleop', const <String, dynamic>{}),
          c.post('/api/teleop/heartbeat'),
          c.get('/api/control'),
        ]);

    for (final Map<String, dynamic> m in all) {
      expect(m['ok'], isTrue, reason: '三条都该在重新解锁之后成功');
    }
    expect(unlocksOn(dog), 2,
        reason: '三条撞 401，重新解锁只许有一次在路上 —— 否则一次烧掉三个名额');
    expect(dog.issued, hasLength(2), reason: '狗只该多发出一张票');
  });

  test('重新解锁也回 401（PIN 被改了）：不死循环，抛出去的是原来那个 401', () async {
    dog.replies['/api/state'] = <String, dynamic>{'mode': 'idle'};
    await armTokens(dog, c);
    dog.liveTokens.clear();
    // 狗那头 PIN 换了：拿旧 PIN 去换票，换不到。
    dog.statusCodes['/api/auth'] = 401;
    dog.replies['/api/auth'] = <String, dynamic>{
      'error': 'PIN 不对',
      'detail': '还可以试 3 次',
    };

    await expectLater(
        () => c.get('/api/state'),
        throwsA(isA<PatrolError>()
            .having((e) => e.status, 'status', 401)
            .having((e) => e.error, 'error', '登录过期了,重新输一次 PIN')));

    expect(unlocksOn(dog), 2,
        reason: '一次是最开始那次，一次是失败的重新解锁 —— 再多就是在死循环');
    expect(dog.received.where((r) => r.path == '/api/state'), hasLength(1),
        reason: '重新解锁没成，原请求不许重发');
  });

  test('身上没有记着的凭证时撞 401：行为跟以前一模一样，不重试', () async {
    // 这个客户端从头到尾没 `unlock` 过。
    dog.checkTokens = true;

    await expectLater(() => c.get('/api/state'),
        throwsA(isA<PatrolError>().having((e) => e.status, 'status', 401)));

    expect(unlocksOn(dog), 0, reason: '没有凭证可用，不许凭空去换票');
    expect(dog.received.where((r) => r.path == '/api/state'), hasLength(1),
        reason: '不重试');
  });

  test('重新解锁走的还是质询-应答，PIN 不进任何请求体', () async {
    /// S-3：`_send` 最后那句 `throw PatrolError(0, '连不上狗', '$e')` 原样带
    /// `$e` 的前提，就是「请求体里从来没有长期凭证」。自动重新解锁**不许**
    /// 破坏这个前提。
    dog.replies['/api/state'] = <String, dynamic>{'mode': 'idle'};
    await armTokens(dog, c);
    dog.liveTokens.clear();
    await c.get('/api/state');

    expect(dog.received.where((r) => r.path == '/api/auth/challenge'),
        hasLength(2),
        reason: '重新解锁也得先取 nonce —— 没有质询就说明走了明文 PIN 那条老路');
    for (final FakeCall r in dog.received) {
      final String raw = r.raw.toLowerCase();
      expect(raw.contains('864209'), isFalse,
          reason: '${r.path} 的原始请求体里出现了明文 PIN');
      expect(raw.contains('pin'), isFalse,
          reason: '${r.path} 的原始请求体里出现了 "pin" 这个词');
    }
  });

  test('重新解锁报的是狗回来的那个规范名，不是调用方递进来的原样字符串', () async {
    /// 名册页落盘留的就是 `session.operator`（`5266a22`）。重新解锁如果报回
    /// 原样字符串，狗那头的审计环上前后两段就挂在两个名字下面，而「甲忘了切、
    /// 乙的操作签在甲名下」事后能翻的就是这条记录。
    dog.replies['/api/auth'] = <String, dynamic>{
      ...dog.replies['/api/auth'] as Map<String, dynamic>,
      'operator': '张三-狗规范过的',
    };
    dog.replies['/api/state'] = <String, dynamic>{'mode': 'idle'};
    dog.checkTokens = true;
    await c.unlock('864209', operator: '  张三  ');
    dog.liveTokens.clear();
    await c.get('/api/state');

    final FakeCall relock =
        dog.received.where((r) => r.path == '/api/auth').last;
    expect((relock.body as Map<String, dynamic>)['operator'], '张三-狗规范过的');
  });
}
