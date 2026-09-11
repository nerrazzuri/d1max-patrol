/// 一台假狗，给 `patrol_client_test.dart` 和 Task 11、12、13 的测试共用。
///
/// **起的是真的 `HttpServer`，不是 mock。** 这个任务要证的就是「请求真的
/// 长什么样」—— PIN 在不在请求体里、token 在不在请求头里 —— 把 `HttpClient`
/// mock 掉，恰好把要证的那件事一起 mock 掉了。
///
/// **标识符是 ASCII 的**：这台机器上的 Dart SDK（3.13.2）不接受非 ASCII
/// 字符出现在标识符里（`class 假狗 { ... }` 这类写法编译不过，
/// `dart analyze`/`dart run` 都报 `illegal_character`），中文只留在注释、
/// docstring 和字符串字面量里，跟仓库里其它 Dart 文件的写法一致。
library;

import 'dart:convert';
import 'dart:io';

/// 狗收到的一个请求。
///
/// **`raw` 留着原始字节**：要证「PIN 不在请求体里」只能在没解析过的那份上
/// 证 —— 解析过的 `body` 是一个 Map，`{'pin': ...}` 有没有出现在别的字段名
/// 底下、有没有被塞进某个字符串里，它都答不上来。
typedef FakeCall = ({
  String method,
  String path,
  String? auth,
  dynamic body,
  String raw,
});

/// 一台假狗。记下每个请求，好让测试事后翻。
class FakeDog {
  late HttpServer _s;
  final List<FakeCall> received = <FakeCall>[];
  final Map<String, dynamic> replies = <String, dynamic>{};

  /// 按路径分的状态码，默认 200。
  ///
  /// **不能是全局一个 int。** `unlock` 第一步是
  /// `GET /api/auth/challenge`，第二步才是 `POST /api/auth`——如果整台狗
  /// 只有一个状态，测「`/api/auth` 回 401」时会连带把 `/api/auth/challenge`
  /// 也打成 401，断言就对不上真正要测的那一步。
  final Map<String, int> statusCodes = <String, int>{};

  /// 这些路径只发响应头就不再往下走——不写完 body、不 close。
  ///
  /// 用来在测试里逼真地复现「狗发完头就沉默」这种网络故障（B-2）：
  /// `HttpClientRequest.close()` 在响应头一到就完成，真正会挂住的是读
  /// body 那一步——光靠状态码/回复内容模拟不出这个场景，得有一条真的卡在
  /// 半路的连接。
  final Set<String> hangPaths = <String>{};

  /// 这台狗查不查 `Authorization` 头。**默认不查** —— 绝大多数测试不关心
  /// token 死没死，多一道闸只会让它们莫名其妙地红。
  ///
  /// 打开之后这台狗的行为跟真狗对齐（`app/auth.py` 的 `Guard.gate`）：
  /// `/api/auth*` 那几条不拦（真狗的 `OPEN_PATHS`），别的 `/api/` 路由一律
  /// 要一张在 [liveTokens] 里的票，否则 **401「登录过期了，重新输一次 PIN」**。
  ///
  /// **401 发在路由匹配之前**，跟真狗 `server.handle()` 的顺序一模一样 ——
  /// 也就是说被 401 掉的那条请求什么都没做过。手机那头敢在 401 之后原样
  /// 重发，靠的就是这个顺序。
  bool checkTokens = false;

  /// 还活着的 token。清空它 = 「闲置 30 分钟，这张票作废了」
  /// （`auth.AP_TOKEN_IDLE_S`）。只在 [checkTokens] 打开时有意义。
  final Set<String> liveTokens = <String>{};

  /// 这台狗发出去过的票，按先后顺序。
  ///
  /// [checkTokens] 打开时，每成功换一次 token 就发一张**新的**，上一张当场
  /// 作废 —— 真狗那头同名换座也是这样（`TokenStore.revoke_ref`）。手机那侧
  /// 的单飞（single-flight）要的正是「两次重新解锁拿到的票不一样」才证得出来。
  final List<String> issued = <String>[];

  /// **假狗自己出的错，记在这儿让测试看得见。**
  ///
  /// 以前整个处理器裹在一个宽 `catch (_)` 里：`replies` 里填了个
  /// `jsonEncode` 编不动的东西、或者这段代码自己写错了，都会被吞成
  /// 「这条路不回话」—— 而不回话在测试里长得跟 [hangPaths] 一模一样，
  /// 调试的人会去查客户端的超时，查的却是假狗的笔误。
  ///
  /// 收摊那一下的 `HttpException`/`SocketException` 不记（见 [start]）：
  /// 那不是错，那就是收摊。
  final List<String> errors = <String>[];

  String get baseUrl => 'http://127.0.0.1:${_s.port}';

  /// 收到过的每一次记名确认的请求体（`POST /api/alerts/<key>/ack`）。
  ///
  /// **是从 [received] 里挑出来的，不是另存一份状态。** 另存一份的话，这台
  /// 假狗身上就有了两个关于「收到过什么」的说法，而对不上的那天两边都不报错。
  ///
  /// **解的是 `raw`**，跟 [FakeCall] 那条 docstring 一个理由：要证「名字真的
  /// 带上去了」得在没解析过的那份上证。
  List<Map<String, dynamic>> get acks => <Map<String, dynamic>>[
        for (final FakeCall r in received)
          if (r.method == 'POST' && r.path.endsWith('/ack'))
            (jsonDecode(r.raw.isEmpty ? '{}' : r.raw) as Map<String, dynamic>),
      ];

  Future<void> start() async {
    _s = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _s.listen((HttpRequest req) async {
      // **整段包起来。** 这个回调是 `async` 的，它返回的 future 没有人接 ——
      // 里面抛出来的任何东西都是一个逸出去的 zone 错误，在 `flutter_test`
      // 里就是一片框架级的红，而且红在跟被测代码毫无关系的地方。
      //
      // 真会抛的是收摊那一下：测试结束时 `stop()` 会 `force: true` 地关掉
      // 整台服务器，而那一刻很可能还有一条请求在半路上（遥控屏 300 毫秒
      // 一拍、200 毫秒一次心跳，几乎总有一条在飞）。读 body 那一步于是抛
      // `HttpException: Connection closed while receiving data`。
      // 那不是错，那就是收摊。
      //
      // **但只有这两种才吞得无声无息。** 别的都进 [errors]：假狗自己写错了
      // 被吞成「不回话」的话，测试只会在别处超时，人会往完全错的方向找。
      try {
        final raw = await utf8.decoder.bind(req).join();
        received.add((
          method: req.method,
          path: req.uri.path,
          auth: req.headers.value('authorization'),
          body: raw.isEmpty ? null : jsonDecode(raw),
          raw: raw,
        ));
        final bool open = req.uri.path.startsWith('/api/auth');
        if (checkTokens && !open && !liveTokens.contains(_bearer(req))) {
          // 真狗那句原话（`auth.py` 的 `Denied(401, ...)`）。手机那头认的是
          // 状态码，但屏上照抄的是这句，所以这儿也照抄。
          req.response.statusCode = 401;
          req.response.headers.contentType = ContentType.json;
          req.response.write(jsonEncode(<String, dynamic>{
            'error': '登录过期了,重新输一次 PIN',
            'detail': 'token 无效或太久没用。',
          }));
          await req.response.close();
          return;
        }
        final int code = statusCodes[req.uri.path] ?? 200;
        Object? payload = replies[req.uri.path] ?? <String, dynamic>{};
        if (checkTokens && req.uri.path == '/api/auth' && code == 200) {
          final String fresh = 'tok-${issued.length + 1}';
          issued.add(fresh);
          // 上一张当场作废：同一只手机换了座，旧票就不该还开得动狗。
          liveTokens
            ..clear()
            ..add(fresh);
          payload = <String, dynamic>{
            ...?(payload as Map<String, dynamic>?),
            'token': fresh,
          };
        }
        req.response.statusCode = code;
        req.response.headers.contentType = ContentType.json;
        if (hangPaths.contains(req.uri.path)) {
          // 逼头真的发出去（写一个字节触发 flush），然后就不管了：不写完
          // body、不 close，模拟「头到了，body 永远不来」。
          req.response.write(' ');
          await req.response.flush();
          return;
        }
        req.response.write(jsonEncode(payload));
        await req.response.close();
      } on HttpException catch (_) {
        // 见上。对面走了就是走了。
      } on SocketException catch (_) {
        // 同上：收摊那一下连接被硬关。
      } catch (e, s) {
        // 假狗自己的毛病。**记下来，别让它长得像「这条路不回话」。**
        errors.add('${req.method} ${req.uri.path}: $e\n$s');
      }
    });
  }

  Future<void> stop() => _s.close(force: true);

  /// `Authorization: Bearer <token>` 里的那个 token。没有头、或者不是
  /// `Bearer` 开头的，返回空串 —— 空串永远不在 [liveTokens] 里。
  static String _bearer(HttpRequest req) {
    final String v = req.headers.value('authorization') ?? '';
    const String prefix = 'Bearer ';
    return v.startsWith(prefix) ? v.substring(prefix.length) : '';
  }
}
