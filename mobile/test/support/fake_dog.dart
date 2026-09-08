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
        req.response.statusCode = statusCodes[req.uri.path] ?? 200;
        req.response.headers.contentType = ContentType.json;
        if (hangPaths.contains(req.uri.path)) {
          // 逼头真的发出去（写一个字节触发 flush），然后就不管了：不写完
          // body、不 close，模拟「头到了，body 永远不来」。
          req.response.write(' ');
          await req.response.flush();
          return;
        }
        req.response
            .write(jsonEncode(replies[req.uri.path] ?? <String, dynamic>{}));
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
}
