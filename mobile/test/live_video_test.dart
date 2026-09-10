/// 视频屏与在线判定。
///
/// **在线判定走 `/api/video/health` 轮询，不看图片加载状态。**
/// 加载状态的回调只在第一帧上响；MJPEG 是一条不结束的响应，第一帧到了之后
/// 它就再也不说话了 —— 画面冻住的时候它跟一切正常长得一模一样，而冻住的
/// 画面正是 §5.9 要防的头号情况。
///
/// **也不用 `Image.network` 拉 MJPEG。** 它读的是**整个**响应体
/// (`painting/_network_image_io.dart` 的 `_loadAsync` 先
/// `consolidateHttpClientResponseBytes(response)` 拿完整字节再解一次，而
/// `foundation/consolidate_response.dart` 里那个 completer 只在 `onDone`
/// 里 complete)。MJPEG 的 `onDone` 永远不来，一帧都解不出来；就算连接被
/// 掐，攒到的也是 multipart 信封而不是裸 JPEG。所以这一屏自己解流。
///
/// **测试起的是真的 `HttpServer`,不是 mock。** 这个任务要证的就是「重连时
/// 真的又发了一次 GET」「token 真的在请求头里」—— 把 HTTP 那头 mock 掉，
/// 恰好把要证的那件事一起 mock 掉了。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:d1max_patrol/net/wire.dart';
import 'package:d1max_patrol/ui/widget/live_video.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/pump.dart';

/// 一张真的 2x2 JPEG。
///
/// **不能拿几个字节冒充。** `Image.memory` 真的会去解码，解不开的时候
/// 解码器抛出来的错会当成测试失败报出来 —— 那种红指向的是这份假数据，
/// 不是被测代码。
final Uint8List _jpeg = base64Decode(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDACgcHiMeGSgjISMtKygwPGRBPDc3PHtYXUlkkYCZlo'
    '+AjIqgtObDoKrarYqMyP/L2u71////m8H////6/+b9//j/2wBDASstLTw1PHZBQXb4pYyl+Pj4'
    '+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj/wAARCAACAA'
    'IDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIE'
    'AwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJi'
    'coKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWW'
    'l5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09f'
    'b3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQA'
    'AQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKj'
    'U2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJma'
    'oqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9'
    'oADAMBAAIRAxEAPwChRRRWhB//2Q==');

/// 假狗发的 boundary。**故意不是狗那头现在用的那个**(`server.py` 的
/// `d1maxframe`)：写死一个常量的实现在这条 boundary 上必须切不出帧来。
const String _boundary = 'zz9-Boundary_x';

/// 把一帧包成一段 multipart，跟 `server.py` 的 `_multipart` 逐字对应。
List<int> _part(String boundary, List<int> jpg) => <int>[
      ...utf8.encode('--$boundary\r\nContent-Type: image/jpeg\r\n'
          'Content-Length: ${jpg.length}\r\n\r\n'),
      ...jpg,
      ...utf8.encode('\r\n'),
    ];

/// 一台只会发 MJPEG 的假狗。
class FakeVideoDog {
  FakeVideoDog({
    this.boundary = _boundary,
    this.frames = 3,
    this.closes = false,
    this.status = 200,
    this.feedForever = false,
    this.silent = false,
  });

  /// 发完那几帧接着一直喂，直到客户端把连接掐了。
  ///
  /// **要的是"掐"这件事被看见。** 不接着喂的话，狗这头根本不知道对面还在
  /// 不在 —— 一条早就没人要的连接跟一条安静的活连接长得一模一样，而狗那头
  /// `MAX_VIEWERS` 的名额是按连接算的。
  final bool feedForever;

  /// 回一个正确的 `content-type`，然后**一个字节都不发**。
  ///
  /// 模拟"连错端口 / 热点上有个登录门户"：头看着像那么回事，画面永远不来。
  final bool silent;

  /// 回这个状态码。503 是狗那头满 6 个观众时的回法
  /// (`video.py` 的 `MAX_VIEWERS` 到 `server.py` 的 503)。
  final int status;

  /// 发完那几帧就把响应关掉。
  ///
  /// 模拟的是"我们这一头的连接断了，狗那头其实好好的" —— 健康轮询看不见
  /// 这件事，所以只有 widget 自己接得回来。
  final bool closes;

  final String boundary;

  /// 一条连接上发几帧就不发了。**发完不 close** —— MJPEG 是一条永不结束的
  /// 响应，这里一 close 就把被测代码最该扛的那件事测没了。
  final int frames;

  late HttpServer _s;

  /// 收到过几次 GET。「掉线再回来要重新拉流」盯的就是这个数。
  int gets = 0;

  final List<String?> auths = <String?>[];
  final List<String> queries = <String>[];

  /// 每次 GET 的路径。换相机之后拉的是不是另一路，盯的就是这个。
  final List<String> paths = <String>[];

  /// 写不进去了(客户端把连接掐了)的那些路径。**只有 [feedForever] 会填。**
  final List<String> dropped = <String>[];

  String get baseUrl => 'http://127.0.0.1:${_s.port}';

  /// 狗这头此刻还挂着几条连接。**「旧连接收没收干净」盯的就是这个数。**
  int get connections => _s.connectionsInfo().total;

  Future<void> start() async {
    _s = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _s.listen((HttpRequest req) async {
      gets++;
      auths.add(req.headers.value(HttpHeaders.authorizationHeader));
      queries.add(req.uri.query);
      paths.add(req.uri.path);
      final HttpResponse r = req.response;
      r.bufferOutput = false;
      if (status != 200) {
        r.statusCode = status;
        await r.close();
        return;
      }
      r.headers.set(HttpHeaders.contentTypeHeader,
          'multipart/x-mixed-replace; boundary=$boundary');
      try {
        if (silent) {
          await r.flush(); // 只把响应头推出去，正文一个字节都不发
          return;
        }
        for (int i = 0; i < frames; i++) {
          r.add(_part(boundary, _jpeg));
          await r.flush();
        }
        if (closes) {
          await r.close();
        }
        while (feedForever) {
          await Future<void>.delayed(const Duration(milliseconds: 10));
          r.add(_part(boundary, _jpeg));
          await r.flush();
        }
      } catch (_) {
        // 客户端把连接掐了。现场就是这样，不是错。
        dropped.add(req.uri.path);
      }
    });
  }

  Future<void> stop() => _s.close(force: true);
}

/// 只记调用次数的假客户端。
///
/// Dart 每个类都自带一个隐式接口，所以 `implements PatrolClient` 就够了，
/// 不用为这个测试单独抽一个抽象类。
class CountingClient implements PatrolClient {
  int calls = 0;

  /// 真到设成 true 那一刻起，`get` 就抛 —— 用来测「问不到狗」那一支。
  bool broken = false;

  @override
  Future<Map<String, dynamic>> get(String path) async {
    calls++;
    if (broken) {
      throw const PatrolError(0, '连不上狗', '热点掉了');
    }
    return <String, dynamic>{
      'cameras': <String, dynamic>{
        'front': <String, dynamic>{'online': true, 'viewers': 1},
      },
    };
  }

  @override
  String get baseUrl => 'http://x';

  @override
  Duration get timeout => const Duration(seconds: 10);

  @override
  String? get token => null;

  @override
  void close() {}

  @override
  Future<Map<String, dynamic>> post(String path, [Object? body]) async =>
      throw UnimplementedError();

  @override
  Future<Map<String, dynamic>> put(String path, [Object? body]) async =>
      throw UnimplementedError();

  @override
  Future<String> setOperator(String name) async =>
      throw UnimplementedError();

  @override
  Future<Session> unlock(String pin, {required String operator}) async =>
      throw UnimplementedError();

  // 值守那几条（Task 10）。这个假件只数视频那一条 `get`，别的一律不该被碰到
  // —— 碰到了就该当场炸，而不是安静地回一份空的。
  @override
  Future<List<Alert>> alertsOpen() async => throw UnimplementedError();

  @override
  Future<List<Alert>> alertsAll() async => throw UnimplementedError();

  @override
  Future<Alert> ackAlert(String key, {required String who}) async =>
      throw UnimplementedError();

  @override
  Future<Alert> resolveAlert(String key) async => throw UnimplementedError();

  @override
  Future<WatchSummary> watchSummary() async => throw UnimplementedError();
}

void main() {
  // 摘掉 `flutter_test` 那个「所有请求都回 400」的 HttpOverrides:这个文件
  // 要证的正是「真的发了一次 GET」。助手在 `support/pump.dart`。
  useRealHttp();

  group('分帧器', () {
    test('boundary 从响应头里解出来，手工拼的流切得对', () {
      // 喂的 boundary 跟狗那头现在用的那个不一样 —— 写死常量的实现在这里
      // 一帧都切不出来。
      final MjpegParser p = MjpegParser.fromContentType(
          'multipart/x-mixed-replace; boundary=$_boundary');
      final List<int> wire = <int>[
        ..._part(_boundary, <int>[1, 2, 3]),
        ..._part(_boundary, <int>[4, 5]),
        ..._part(_boundary, <int>[6, 7, 8, 9]),
        ...utf8.encode('--$_boundary'), // 下一段的头还没到
      ];
      final List<Uint8List> got = p.add(wire);
      expect(got.length, 3);
      expect(got[0], <int>[1, 2, 3]);
      expect(got[1], <int>[4, 5]);
      expect(got[2], <int>[6, 7, 8, 9]);
    });

    test('边界被切在两个块中间也切得对', () {
      // **TCP 不保证一帧一个包。** 这是这类分帧器最常见的 bug：按块各切各的
      // 就会把跨块的那个边界漏掉，而现场那条热点上跨块是常态。
      final MjpegParser p = MjpegParser.fromContentType(
          'multipart/x-mixed-replace;boundary="$_boundary"');
      final List<int> wire = <int>[
        ..._part(_boundary, <int>[10, 11, 12, 13, 14]),
        ..._part(_boundary, <int>[20, 21]),
        ..._part(_boundary, <int>[30]),
        ...utf8.encode('--$_boundary'),
      ];
      final List<Uint8List> got = <Uint8List>[];
      // 3 个字节一块，块边界跟帧边界完全错开。
      for (int i = 0; i < wire.length; i += 3) {
        got.addAll(p.add(wire.sublist(
            i, i + 3 > wire.length ? wire.length : i + 3)));
      }
      expect(got.length, 3);
      expect(got[0], <int>[10, 11, 12, 13, 14]);
      expect(got[1], <int>[20, 21]);
      expect(got[2], <int>[30]);
    });

    test('段头之后一直等不到边界就认赔，不是把手机内存吃光', () {
      // **这是手机上真会出人命的那一条。** 段头收到之后如果永远等不到下一
      // 个边界(狗挂在半句话上、热点上的门户把流截了、对面根本不是 MJPEG)，
      // 没有上限的缓冲会一直涨到进程被系统杀掉 —— 复审的探针实测攒到
      // 24 MiB 一个字节都没丢。认赔之后上层会收连接、按退避重开。
      final MjpegParser p = MjpegParser.fromContentType(
          'multipart/x-mixed-replace;boundary=$_boundary');
      p.add(<int>[
        ...utf8.encode('--$_boundary'), 13, 10,
        ...utf8.encode('Content-Type: image/jpeg'), 13, 10, 13, 10,
      ]);
      final Uint8List junk = Uint8List(1 << 20); // 1 MiB 一块，里面没有边界
      Object? blew;
      int fed = 0;
      while (blew == null && fed < 64) {
        fed++;
        try {
          p.add(junk);
        } catch (e) {
          blew = e;
        }
      }
      expect(blew, isA<PatrolError>(),
          reason: '喂了 $fed MiB 都不叫停的话，这台手机就是在等 OOM');
      expect(fed * (1 << 20),
          lessThanOrEqualTo(MjpegParser.maxBuffer + (1 << 20)));
    });

    test('响应头里没有 boundary 就明说，不是默默给张黑图', () {
      expect(() => MjpegParser.fromContentType('image/jpeg'),
          throwsA(isA<PatrolError>()));
    });
  });

  test('拉流：token 走请求头，不进查询串', () async {
    // token 进了 URL 就会进日志、进代理记录。请求是我们自己发的，设得了头。
    final FakeVideoDog dog = FakeVideoDog();
    await dog.start();
    final HttpClient io = HttpClient();
    final List<Uint8List> frames = <Uint8List>[];
    Object? blew;
    final StreamSubscription<Uint8List> sub = mjpegFrames(
            Uri.parse('${dog.baseUrl}/api/video/front'),
            io: io,
            token: 'T0KEN')
        .listen(frames.add, onError: (Object e) => blew = e);

    await until(() => frames.isNotEmpty || blew != null, '第一帧');
    expect(blew, isNull);
    expect(frames.first, _jpeg, reason: '解出来的得是裸 JPEG，不是 multipart 信封');
    expect(dog.auths.single, 'Bearer T0KEN');
    expect(dog.queries.single, isEmpty);

    // **先掐连接、再取消订阅。** 反过来 `cancel()` 会挂住 —— 它要等这条
    // 响应收完，而 MJPEG 永远收不完。
    io.close(force: true);
    await sub.cancel();
    await dog.stop();
  });

  testWidgets('在线时画出画面', (WidgetTester t) async {
    final FakeVideoDog dog = FakeVideoDog();
    await t.runAsync(dog.start);
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: ctl.stream)));

    ctl.add(const VideoHealth(<String, bool>{'front': true}));
    await pumpUntil(
        t, () => find.byType(Image).evaluate().isNotEmpty, '画面出来');

    expect(find.byType(Image), findsOneWidget);
    // 画的是自己解出来的那一帧，不是 `Image.network` 去拉的。
    expect(t.widget<Image>(find.byType(Image)).image, isA<MemoryImage>());

    await teardown(t, ctl, dog.stop);
  });

  testWidgets('不在线时说人话，不是转圈', (WidgetTester t) async {
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: 'http://127.0.0.1:9', camera: 'front', health: ctl.stream)));

    ctl.add(const VideoHealth(<String, bool>{'front': false}));
    await t.pump();

    expect(find.textContaining('没有画面'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing,
        reason: '一直转圈的圈跟「连不上」长得一样，人分不出该等还是该走过去看');

    await teardown(t, ctl);
  });

  testWidgets('掉线再回来要重新拉流', (WidgetTester t) async {
    /// **这一条是这个 widget 唯一的技术难点。**
    ///
    /// MJPEG 那条连接断了之后不会自己回来。连接在我们自己手里，所以恢复
    /// 就是**关掉旧的、再发一次 GET** —— 盯的是假狗那头收到的 GET 次数，
    /// 不是界面上换没换一个 key。
    final FakeVideoDog dog = FakeVideoDog();
    await t.runAsync(dog.start);
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: ctl.stream)));

    ctl.add(const VideoHealth(<String, bool>{'front': true}));
    await pumpUntil(
        t, () => find.byType(Image).evaluate().isNotEmpty, '第一条流的画面');
    expect(dog.gets, 1);

    ctl.add(const VideoHealth(<String, bool>{'front': false}));
    // **两次 pump。** 第一次把流上那条事件派下去(`setState` 到此为止只是把
    // 这一帧标脏)，重建要等下一帧才落地。只 pump 一次的话断言看到的还是
    // 上一帧的画面 —— 这台机器上实测过。
    await t.pump();
    await t.pump();
    expect(find.byType(Image), findsNothing,
        reason: '掉线了还挂着最后一帧，人会以为看到的是此刻');

    ctl.add(const VideoHealth(<String, bool>{'front': true}));
    await pumpUntil(t, () => dog.gets >= 2, '第二次 GET');
    expect(dog.gets, 2);
    await pumpUntil(
        t, () => find.byType(Image).evaluate().isNotEmpty, '重连后的画面');

    await teardown(t, ctl, dog.stop);
  });

  testWidgets('流自己断了、健康还说在线，也要自己把画面接回来', (WidgetTester t) async {
    // **断的是我们这一头。** 狗还在线，健康轮询问不出毛病，`_onHealth` 也
    // 就不会翻面 —— 没有人会来重开这条流。不自己接的话，屏幕停在最后那一
    // 帧上不动，人以为看到的是此刻(§5.9 的头号情况)。
    final FakeVideoDog dog = FakeVideoDog(closes: true);
    await t.runAsync(dog.start);
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: ctl.stream)));

    // 只喂这一次「在线」。后面那次 GET 不是谁通知出来的。
    ctl.add(const VideoHealth(<String, bool>{'front': true}));
    await pumpUntil(t, () => dog.gets >= 2, '流断掉之后自己发的第二次 GET');

    expect(dog.gets, greaterThanOrEqualTo(2));
    await teardown(t, ctl, dog.stop);
  });

  testWidgets('一直被拒就越等越久，而且屏幕上说得出「人太多了」', (WidgetTester t) async {
    // **满员(503)不能变成 0.5 Hz 的无限猛敲。** 狗那头一路最多 6 个观众，
    // 第 7 个人的手机要是每 2 秒敲一次、敲到天荒地老，现场那条本来就不宽的
    // 热点还要被它占着。屏幕也得说人话:满员该做的事(等一下、让人退出来)
    // 跟「狗没起来」(走过去看)完全相反。
    final FakeVideoDog dog = FakeVideoDog(status: 503);
    await t.runAsync(dog.start);
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: ctl.stream)));

    ctl.add(const VideoHealth(<String, bool>{'front': true}));
    // 自己攒假时钟:要断言的是「第三次 GET 之前过了多久」，不是「发生了没有」。
    Duration fake = Duration.zero;
    const Duration step = Duration(milliseconds: 100);
    while (dog.gets < 3 && fake < const Duration(seconds: 40)) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 3)));
      await t.pump(step);
      fake += step;
    }

    expect(dog.gets, 3, reason: '等了 40 秒还没敲够三次，那是压根没在重连');
    expect(fake, greaterThanOrEqualTo(const Duration(seconds: 6)),
        reason: '2 秒起、每次翻倍的话第三次 GET 最早也在 2+4=6 秒；'
            '每次都只等 2 秒的话 4 秒出头就到了');
    expect(find.textContaining('最多 6 个人看'), findsOneWidget,
        reason: '满员跟「狗没起来」要做的事完全相反，不能共用一句话');
    expect(find.textContaining('HttpException'), findsNothing,
        reason: '现场屏幕上要的是「该做什么」，不是 Dart 的异常文本');

    await teardown(t, ctl, dog.stop);
  });

  testWidgets('父层换了相机，画面要跟着换，不能停在上一路上', (WidgetTester t) async {
    // **这个项目从第一天就是多机的**，机群页上点另一台狗走的就是这条路:
    // State 被复用，`initState` 不会再跑一次。不接这一下的话画面稳稳地停在
    // 上一路上，而屏幕上什么异常也没有 —— 人以为看的是刚点的那一台。
    final FakeVideoDog dog = FakeVideoDog();
    await t.runAsync(dog.start);
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: ctl.stream)));

    ctl.add(const VideoHealth(<String, bool>{'front': true, 'back': true}));
    await pumpUntil(
        t, () => find.byType(Image).evaluate().isNotEmpty, '第一路的画面');
    expect(dog.paths.single, '/api/video/front');

    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'back', health: ctl.stream)));
    await pumpUntil(t, () => dog.paths.length >= 2, '换了相机之后的那次 GET');

    expect(dog.paths.last, '/api/video/back');
    await teardown(t, ctl, dog.stop);
  });

  testWidgets('接上了就把退避清回 2 秒，别拿着 16 秒去等一条好狗',
      (WidgetTester t) async {
    // **退避不清零的坏法是"恢复之后还在按 16 秒等"。** 一条时好时坏的热点
    // 上，每断一次都把等待翻一倍，而中间明明接上过、画面也出来过 —— 人看到
    // 的是"越用越卡，最后干脆再也不回来"。
    //
    // **量假时钟只能用累加变量，不能用 `DateTime.now()`。**
    // `testWidgets` 跑在 FakeAsync 上:`pump(step)` 推的是假时钟，而
    // `DateTime.now()` 读的是真钟 —— 拿真钟去量这里的间隔，量出来的是几十
    // 毫秒的真实耗时，跟被测的那个 2/4/8 秒毫无关系，断言会永远绿。
    // `frames: 2`:分帧器要看见**下一个边界**才敢吐上一帧,发一帧就关的话
    // 一帧都解不出来 —— 那样测的就不是"接上过"，是"一次都没接上"。
    final FakeVideoDog dog = FakeVideoDog(frames: 2, closes: true);

    // **「真的解出过帧」是这条测试的前提，得自己钉住。**
    // 下面量的是"接上过之后退避有没有清回 2 秒"，而"接上过"这个前提整个系在
    // `frames: 2` 上:改成 1 的话一帧都解不出来(分帧器要看见**下一个**边界才
    // 敢吐上一帧)，于是测的变成了"一次都没接上"—— 可那时候两条断言的红跟
    // "退避没清零"的红长得一模一样，分不出是哪一种坏。
    // 这里把假狗要写的字节原样再喂一遍分帧器:切不出帧就在这儿红，红得指着
    // 前提本身。
    // (不能改去盯屏幕上的 `Image`:帧到达和 `onDone` 落在同一个事件轮次里，
    // 中间没有 pump，画面被清掉之后那一帧在树上从来没出现过。)
    final MjpegParser premise = MjpegParser(dog.boundary);
    int decodable = 0;
    for (int i = 0; i < dog.frames; i++) {
      decodable += premise.add(_part(dog.boundary, _jpeg)).length;
    }
    expect(decodable, greaterThan(0),
        reason: '假狗这一条连接上一帧都切不出来的话，下面量的就不是'
            '"接上过之后的退避"，而是"一次都没接上"');

    await t.runAsync(dog.start);
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: ctl.stream)));

    ctl.add(const VideoHealth(<String, bool>{'front': true}));
    Duration fake = Duration.zero;
    const Duration step = Duration(milliseconds: 100);
    while (dog.gets < 3 && fake < const Duration(seconds: 40)) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 3)));
      await t.pump(step);
      fake += step;
    }

    expect(dog.gets, 3, reason: '每次都拿到过一帧，还敲不够三次就是压根没重连');
    expect(fake, lessThan(const Duration(seconds: 5, milliseconds: 500)),
        reason: '每次都拿到过画面，退避就该清回 2 秒:第三次 GET 在 2+2≈4 秒。'
            '不清零的话是 2+4≈6 秒起步');

    await teardown(t, ctl, dog.stop);
  });

  testWidgets('换了健康流之后，旧那条连接要真的被掐掉', (WidgetTester t) async {
    // **这一条盯的是 `didUpdateWidget` 里那一句 `_shut()`。**
    // 换相机那条路上 `_open()` 自己会先收一次，所以漏收也看不出来;
    // 换健康流这条路不会重开(新那条流还没说过话，`_live` 打回 false)，
    // 漏收的话那条连接就**永远**留在狗那头占着 `MAX_VIEWERS`(6)里一个名额,
    // 现场表现是"切来切去几次之后视频再也打不开"。
    //
    // 框架那条 `!timersPending` 不变式抓不到它:旧连接正在活跃收流，不是
    // 空闲连接，没有那个 15 秒空闲计时器。所以这里直接盯**狗那头看见连接
    // 断了**。
    final FakeVideoDog dog = FakeVideoDog(feedForever: true);
    await t.runAsync(dog.start);
    final StreamController<VideoHealth> first = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: first.stream)));

    first.add(const VideoHealth(<String, bool>{'front': true}));
    await pumpUntil(
        t, () => find.byType(Image).evaluate().isNotEmpty, '第一路的画面');
    expect(dog.connections, 1, reason: '这会儿连接还该好好地连着');

    final StreamController<VideoHealth> second =
        StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: second.stream)));
    await pumpUntil(t, () => dog.connections == 0 || dog.dropped.isNotEmpty,
        '狗那头看见旧连接断掉');

    expect(dog.connections, 0, reason: '旧连接还挂着就是在占 6 个观看名额之一');
    expect(dog.gets, 1, reason: '换的只是健康流，新那条还没说话，不该急着开新连接');

    unawaited(first.close());
    await teardown(t, second, dog.stop);
  });

  testWidgets('一个字节都不来的时候三秒就断开重来，不是先烧 8 MiB 流量',
      (WidgetTester t) async {
    // **连错端口、或者热点上有个登录门户**的时候，对面会给一个像模像样的
    // 头然后什么也不发(或者一直发不是 MJPEG 的字节)。光靠 8 MiB 那个上限
    // 的话，是攒满 8 MiB 才抛一次、退避重开、再攒 8 MiB —— 封顶 16 秒就是
    // 每 16 秒白烧 8 MiB 手机流量，而屏幕上还在说"过几秒自己再试"。
    final FakeVideoDog dog = FakeVideoDog(silent: true);
    await t.runAsync(dog.start);
    final StreamController<VideoHealth> ctl = StreamController<VideoHealth>();
    await t.pumpWidget(wrap(LiveVideo(
        baseUrl: dog.baseUrl, camera: 'front', health: ctl.stream)));

    ctl.add(const VideoHealth(<String, bool>{'front': true}));
    Duration fake = Duration.zero;
    const Duration step = Duration(milliseconds: 100);
    while (dog.gets < 2 && fake < const Duration(seconds: 40)) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 3)));
      await t.pump(step);
      fake += step;
    }

    expect(dog.gets, 2, reason: '一个字节都不来的时候得自己认赔重来');
    expect(fake, lessThan(const Duration(seconds: 7)),
        reason: '3 秒宽限 + 2 秒退避 ≈ 5 秒就该敲第二次;'
            '等 8 MiB 攒满的话这里根本走不到');
    expect(find.textContaining('地址和端口'), findsOneWidget,
        reason: '"地址不对"跟"狗没起来""人太多了"要做的事完全不同');

    await teardown(t, ctl, dog.stop);
  });

  test('轮询器按周期问，停了就不再问', () async {
    final CountingClient c = CountingClient();
    final VideoHealthPoller p = VideoHealthPoller(
        client: c, period: const Duration(milliseconds: 10));
    await p.stream.first;
    p.stop();
    final int atStop = c.calls;
    await Future<void>.delayed(const Duration(milliseconds: 60));
    expect(c.calls, atStop,
        reason: '人切走了还在问，就是在白耗电和白占热点带宽');
  });

  test('轮询问不到狗时发全 false 的那一份，不许把流关掉', () async {
    // 问不到狗等于看不见，看不见就该变灰。**把流关掉的话摇杆会停在最后一个
    // 状态上，而那个状态很可能是「亮着」。**
    final CountingClient c = CountingClient()..broken = true;
    final VideoHealthPoller p = VideoHealthPoller(
        client: c, period: const Duration(milliseconds: 10));

    expect((await p.stream.first).anyLive, isFalse);

    final Future<VideoHealth> next = p.stream.first;
    c.broken = false;
    expect((await next).anyLive, isTrue,
        reason: '出一次错就把流关掉的话，狗回来了也没人知道');

    p.stop();
  });
}
