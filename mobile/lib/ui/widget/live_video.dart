/// 一路实时画面，加上「这一刻到底有没有画面」的判定。
///
/// **在线判定走 `/api/video/health` 轮询，不看图片加载状态。**
/// 加载状态的回调只在第一帧上响；MJPEG 是一条不结束的响应，第一帧到了之后
/// 它就再也不说话了 —— 画面冻住的时候它跟一切正常长得一模一样，而冻住的
/// 画面正是 §5.9 要防的头号情况。
///
/// **MJPEG 在 Flutter 里不是白送的，`Image.network` 吃不下它。**
/// `painting/_network_image_io.dart` 的 `_loadAsync` 是 `await request
/// .close()` 之后调 `consolidateHttpClientResponseBytes(response)` 拿**完整
/// 字节**再解一次，而 `foundation/consolidate_response.dart` 里那个 completer
/// **只在 `onDone` 里 complete**。MJPEG 的 `onDone` 永远不来：Future 永不
/// settle，一帧都解不出来；就算连接被掐，攒到的也是 multipart 信封而不是裸
/// JPEG，`instantiateImageCodec` 照样抛。所以这里自己解流、`Image.memory`
/// 逐帧画。
///
/// **不引第三方 MJPEG 包。** 这是要装进客户手机的 app，一个几十行的分帧器
/// 不值得换一个第三方包的供应链和维护面。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:developer' as developer;
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../net/patrol_client.dart';
import '../../net/wire.dart';

/// `--` —— 每个 multipart 边界前面那两个横杠。
const List<int> _dashes = <int>[0x2d, 0x2d];

/// 一段自己的头到净荷之间的那个空行。
const List<int> _blankLine = <int>[13, 10, 13, 10];

/// MJPEG 分帧器：喂字节进去，吐整帧 JPEG 出来。
///
/// **单独一个类，不焊在 widget 里。** 这一层唯一会出问题的地方是分帧，而
/// 分帧的坑(边界被 TCP 切在两个包中间)在 widget 测试里根本喂不进去 ——
/// 焊死之后那条路径就再也没人钉得住了。
class MjpegParser {
  MjpegParser(this.boundary)
      : _mark = Uint8List.fromList(
            <int>[..._dashes, ...utf8.encode(boundary)]);

  /// 从响应头的 `content-type` 里解 `boundary=`。
  ///
  /// **不许写死一个常量。** 狗那头现在用的是 `d1maxframe`
  /// (`app/server.py` 的 `_BOUNDARY`)，但那是它的私事：哪天换了一版，
  /// 写死的实现会一帧都切不出来，而且黑得毫无线索 —— 画面空白、日志干净。
  factory MjpegParser.fromContentType(String? contentType) {
    final String ct = contentType ?? '';
    for (final String piece in ct.split(';')) {
      final String p = piece.trim();
      if (p.length > 9 && p.substring(0, 9).toLowerCase() == 'boundary=') {
        final String b = p.substring(9).trim().replaceAll('"', '');
        if (b.isNotEmpty) return MjpegParser(b);
      }
    }
    throw PatrolError(0, '狗回的画面看不懂',
        'content-type 里没有 boundary：${ct.isEmpty ? '(空)' : ct}');
  }

  /// 攒到这么多字节还切不出一帧就认赔。
  ///
  /// **没有上限的话这里就是一台手机的 OOM。** 段头收到之后如果永远等不到
  /// 下一个边界(狗挂在半句话上、热点上的门户把流截了、对面根本不是
  /// MJPEG)，缓冲会一直涨到进程被系统杀掉 —— 复审的探针实测攒到 24 MiB
  /// 一个字节都没丢。
  ///
  /// 8 MiB 的来历:狗那头出的是 `-q:v 2` 的 MJPEG，1080p 一帧几百 KB，
  /// 2 MB 已经是离谱的一帧。留到 8 MiB 是给"一帧被切成很多包慢慢到"留足
  /// 余量，同时离手机上真出事的量级还差得远。
  static const int maxBuffer = 8 << 20;

  final String boundary;
  final Uint8List _mark;

  /// 还没切完的那点尾巴。
  ///
  /// **这个缓冲就是「跨包」这件事的全部。** 一次 `add` 拿到的可能是半帧，
  /// 也可能是两帧半，边界还可能正好被劈在两次 `add` 中间；按包各切各的
  /// 会漏掉那个边界，往后所有帧全错位。
  final List<int> _buf = <int>[];

  /// 找"这一帧后面那个边界"时从哪儿接着扫。
  ///
  /// **不记这个数就是 O(n²)。** 一帧被切成 N 包到达时，每来一包都从缓冲
  /// 头上重扫一遍;10 fps 一直开着的屏，这点开销全落在客户手机的电池上。
  ///
  /// 记住的位置要**往回退 `_mark.length - 1` 个字节**:边界完全可能正好
  /// 骑在上一次扫描的终点上，不退回去就会漏掉它，往后所有帧全错位。
  int _resume = 0;

  /// 喂一段字节，拿回这一段里切得出来的所有整帧。切不满一帧就返回空表。
  ///
  /// 攒过 [maxBuffer] 还切不出一帧就抛 `PatrolError`:那不是"还没收全"，
  /// 是这条流坏了。抛出去让上层收连接、重开 —— **不许静默清掉缓冲接着等**，
  /// 那会变成"永远不出画面，而且没有人知道为什么"。
  List<Uint8List> add(List<int> chunk) {
    _buf.addAll(chunk);
    final List<Uint8List> out = <Uint8List>[];
    while (true) {
      final int start = _find(_buf, _mark, 0);
      if (start < 0) {
        // 一个边界都没有。**不能整段丢掉**：边界可能正好被切在这一包的
        // 末尾，留住最后 mark.length-1 个字节去接下一包。
        _keepTail(_mark.length - 1);
        _resume = 0;
        break;
      }
      final int head = _find(_buf, _blankLine, start + _mark.length);
      if (head < 0) break; // 这一段自己的头还没收全
      final int from = head + _blankLine.length;
      final int next = _find(_buf, _mark, from < _resume ? _resume : from);
      if (next < 0) {
        // 这一帧还没收全，等下一包。下次从"这次扫到的地方往回退一个边界
        // 长度"接着扫，别再从头来一遍。
        final int back = _buf.length - (_mark.length - 1);
        final int mark = back < from ? from : back;
        _resume = mark;
        break;
      }
      int end = next;
      if (end - from >= 2 && _buf[end - 2] == 13 && _buf[end - 1] == 10) {
        end -= 2; // 净荷和下一个边界之间那个 CRLF 不算画面
      }
      out.add(Uint8List.fromList(_buf.sublist(from, end)));
      _buf.removeRange(0, next); // 留着下一个边界，接着切
      _resume = 0; // 缓冲挪过位了，记着的那个位置作废
    }
    if (_buf.length > maxBuffer) {
      final int n = _buf.length;
      _buf.clear();
      _resume = 0;
      throw PatrolError(0, '这一路画面接不下去',
          '攒了 $n 字节还没切出一帧，这条流不是 MJPEG 或者已经断在半句话上');
    }
    return out;
  }

  void _keepTail(int n) {
    if (_buf.length > n) _buf.removeRange(0, _buf.length - n);
  }
}

/// `needle` 在 `hay` 里从 `from` 起第一次出现的位置，没有就是 -1。
int _find(List<int> hay, List<int> needle, int from) {
  final int last = hay.length - needle.length;
  outer:
  for (int i = from < 0 ? 0 : from; i <= last; i++) {
    for (int j = 0; j < needle.length; j++) {
      if (hay[i + j] != needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

/// 从 `/api/video/<camera>` 拉一条 MJPEG，逐帧吐出来。
///
/// **不 await 整个响应体**，把 `response` 当 `Stream<List<int>>` 增量读 ——
/// 理由见文件头那段 `Image.network` 为什么不行。
///
/// **token 先走请求头** `Authorization: Bearer <token>`：请求是我们自己发的，
/// 设得了头，token 就不必进 URL、不进日志、不进代理记录。狗那头
/// (`app/auth.py` 的 `QUERY_TOKEN_PATHS`)确实也认 `?token=`，但那条是留给
/// `<img src=>` 那种设不了头的地方的**降级路径** —— 头这条路真机上不通再
/// 退过去，别反过来把这里"修"成查询串。
///
/// `io` 是**调用方的**：要停这条流就取消订阅、再 `close(force: true)` 掉它。
/// 这里不替调用方关，跟 `PatrolClient` 那头一个规矩。
///
/// [abandoned] 是调用方说"这条流我已经不要了"的那个问句。**掐连接自己会
/// 掀起一个错**(`Connection closed before full header was received` 之类)，
/// 而那时候订阅已经取消了 —— 再抛出去就是一个没人接的 zone 错误，在测试里
/// 直接是框架级的红，在真机上会打进 Flutter 的全局错误通道。是自己掐的就
/// 咽下去，不是自己掐的照抛不误。
Stream<Uint8List> mjpegFrames(Uri url,
    {required HttpClient io,
    String token = '',
    bool Function()? abandoned}) async* {
  try {
    // 这里是 `await for` 不是 `yield*`:`yield*` 把内层的错**直接转交**给外层
    // 那条流，绕过下面这个 catch;`await for` 才会把它抛进函数体里。
    await for (final Uint8List f in _mjpegFrames(url, io: io, token: token)) {
      yield f;
    }
  } catch (e) {
    if (abandoned?.call() ?? false) return;
    rethrow;
  }
}

Stream<Uint8List> _mjpegFrames(Uri url,
    {required HttpClient io, String token = ''}) async* {
  final HttpClientRequest req = await io.openUrl('GET', url);
  // **不跟重定向。** 跟的话 302 会把下面这个 `Authorization: Bearer` 原样
  // 带到重定向目标去 —— 热点上狗是唯一的主机，今天打不着，但那是"今天的
  // 拓扑",不是这行代码的保证。狗从不发 3xx，跟不跟都不影响正常路径。
  req.followRedirects = false;
  if (token.isNotEmpty) {
    req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $token');
  }
  final HttpClientResponse resp = await req.close();
  if (resp.statusCode != 200) {
    await resp.drain<void>();
    throw PatrolError(resp.statusCode, '这一路画面拉不下来',
        '狗回了 ${resp.statusCode}');
  }
  final MjpegParser parser = MjpegParser.fromContentType(
      resp.headers.value(HttpHeaders.contentTypeHeader));
  await for (final List<int> chunk in resp) {
    for (final Uint8List frame in parser.add(chunk)) {
      yield frame;
    }
  }
}

/// 一路画面。
///
/// **收 `baseUrl` + `token`，不收 `PatrolClient`。** 它要的是一条 URL，
/// 不是一个能 POST 的客户端；把整个能改狗的客户端递进一个只画画面的
/// widget，等于白送权限。`token` 默认空串是为了让 widget 测试不必造一个
/// 会话。
class LiveVideo extends StatefulWidget {
  const LiveVideo({
    super.key,
    required this.baseUrl,
    required this.camera,
    required this.health,
    this.token = '',
  });

  final String baseUrl;
  final String camera;

  /// 「这一刻有没有画面」从这条流上来 —— 通常是 [VideoHealthPoller] 的。
  final Stream<VideoHealth> health;

  final String token;

  /// 真在出画面的那一格。
  ///
  /// **没有画面的时候这一格压根不在**（那时候画的是 [_LiveVideoState._panel]
  /// 那块板子）。所以「画面回来了没有」是一句 `findsOneWidget`，不是去比对
  /// 板子上那句中文 —— 裁决 124 的同一条规矩。
  static const Key frameKey = ValueKey<String>('live-frame');

  @override
  State<LiveVideo> createState() => _LiveVideoState();
}

class _LiveVideoState extends State<LiveVideo> {
  StreamSubscription<VideoHealth>? _health;
  StreamSubscription<Uint8List>? _frames;
  HttpClient? _io;
  Uint8List? _frame;
  bool _live = false;
  String _trouble = '';

  /// 第几条流。
  ///
  /// 掐掉的那条连接**过一会儿还会报一次错**(见 [_shut])，那个错不属于
  /// 现在这条流；不认这个号的话，它会把刚接上的新画面又清成空的。
  int _gen = 0;

  /// 等着重开一条流的那个定时器。见 [_reopenSoon]。
  Timer? _retry;

  /// 重连的起步间隔。
  ///
  /// 跟健康轮询同一个 2 秒:再快也没有新信息(狗那头 `STALE_S` 就是 2 秒)，
  /// 再慢就会在"画面没了"和"画面回来"之间多留一段黑屏。
  static const Duration _backoffMin = Duration(seconds: 2);

  /// 退避的封顶。
  ///
  /// **这个上限是给"满员"那种情况留的。** 狗那头一路最多 6 个观众
  /// (`video.py` 的 `MAX_VIEWERS`)，第 7 个人拿到的是 503;固定 2 秒的话
  /// 那台手机会以 0.5 Hz 无限期地猛敲狗，而现场那条热点本来就不宽。
  /// 2→4→8→16 之后停在 16 秒:人退出来之后最多再等这么久画面就回来了。
  static const Duration _backoffMax = Duration(seconds: 16);

  /// 下一次重连等多久。拿到一帧就清回 [_backoffMin]。
  Duration _backoff = _backoffMin;

  /// 开了流之后多久还没切出**第一帧**就认赔。
  ///
  /// **这个短超时是给"地址根本不对"留的。** 连错端口、或者热点上有个登录
  /// 门户把流截了的时候，对面会一直发字节但永远没有边界:光靠 8 MiB 那个
  /// 上限的话，是攒满 8 MiB 才抛一次、退避重开、再攒 8 MiB —— 封顶 16 秒
  /// 就是每 16 秒白烧 8 MiB 手机流量，而屏幕上还在说"过几秒自己再试"。
  ///
  /// **只管第一帧。** 出过一帧就说明这条流是真的 MJPEG、地址也对，后面
  /// 卡多久都归 8 MiB 上限和退避管，不给正常的流再加一道超时。
  static const Duration _firstFrameGrace = Duration(seconds: 3);

  /// 等第一帧的那个闹钟。第一帧到了就取消。
  Timer? _firstFrame;

  /// 这一格此刻还在不在台上。**被一个不透明的全屏页盖住时是 false。**
  ///
  /// 挂账 64：推一个全屏页上去，底下这个 widget **不 dispose** —— `Navigator`
  /// 默认 `maintainState: true`，State 原样留着，那条 MJPEG 连接也就原样连着。
  /// 而狗那头 `MAX_VIEWERS` 的名额是**按连接**算的（`video.py`），不是按
  /// 「谁真的在看」算的。现场的样子就是：连开两个页面，第二个人被告知
  /// 「看的人满了」，而占着位子的那一路谁也没在看。
  ///
  /// **守的必须是狗那头的连接数，不是 widget 树里有没有这个对象** ——
  /// 挂账 64 的病恰恰就是「对象在、位子占着」，守对象等于没守。
  bool _onstage = true;

  @override
  void initState() {
    super.initState();
    _health = widget.health.listen(_onHealth);
  }

  /// 台上台下翻面的那一下。
  ///
  /// **靠 `TickerMode`，不靠 `RouteAware`。** `Overlay` 给「排在第一个不透明
  /// entry 之下」的每一层挂的就是 `TickerMode(enabled: false)`
  /// （`overlay.dart` 的 `_Theatre`），而 `TickerMode.valuesOf` 走的是
  /// `dependOnInheritedWidgetOfExactType` —— 翻面这一下一定会把这里叫醒。
  /// `RouteAware` 那条路要求 app 顶上挂一个 `RouteObserver`，而那是**这个
  /// widget 管不着的地方**：谁哪天新起一个 `MaterialApp` 忘了挂，这一格就
  /// 无声地退回今天的坏法 —— 没有任何测试会红。
  ///
  /// **不必 `setState`。** 依赖变了这一下 element 已经被标脏，这个回调跑完
  /// 紧接着就是一次 build；在这儿再 `setState` 只是多标一次。
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final bool on = TickerMode.valuesOf(context).enabled;
    if (on == _onstage) return;
    _onstage = on;
    if (on) {
      // **回来要重新发一次 GET。** 跟掉线再回来是同一件事：那条连接已经
      // 被我们自己掐了，它不会自己回来。
      //
      // 只做「盖住就断」的话，这一屏会变成「断了就再也不回来」—— 人从全屏
      // 页退回来，看到的是一块「正在接画面」的板子，一直到下一次健康翻面
      // 为止（而健康那头压根不会翻：狗一直在线）。
      if (_live) _open();
    } else {
      _shut();
      // 位子让出来了，最后那一帧也不留：留着的话人退回来的那一瞬间看见的是
      // 一张旧图，而那正是 §5.9 要防的头号情况。
      _frame = null;
    }
  }

  void _onHealth(VideoHealth h) {
    final bool on = h.online[widget.camera] ?? false;
    if (on == _live) return; // 只在翻面的那一下动连接
    _live = on;
    if (on) {
      _open();
    } else {
      _shut();
      _frame = null; // 掉线了还挂着最后一帧，人会以为看到的是此刻
    }
    // 掐旧连接自己报出来的那个错不算故障，摆在这儿一起抹掉。
    _trouble = '';
    if (mounted) setState(() {});
  }

  /// 开一条新的流。
  ///
  /// **掉线再回来必须重新发一次 GET。** MJPEG 那条连接断了之后不会自己
  /// 回来，而它断掉的样子跟"还连着、只是没新帧"在这一头长得一模一样。
  void _open() {
    _shut();
    // **台下不许开流。** 这一句挡的不是 [didChangeDependencies]（那儿本来就
    // 只在回台上时才开），挡的是**盖住期间健康那条流翻的面**：狗掉线又回来，
    // `_onHealth` 会照常走到这儿 —— 于是一个谁也看不见的页面，又把位子占回去了。
    // 健康那头照常记着 [_live]，回到台上那一下 [didChangeDependencies] 再开。
    if (!_onstage) return;
    final int gen = _gen;
    final HttpClient io = HttpClient()
      ..connectionTimeout = const Duration(seconds: 5);
    _io = io;
    _frames = mjpegFrames(
      Uri.parse('${widget.baseUrl}/api/video/${widget.camera}'),
      io: io,
      token: widget.token,
      // 这一茬被换掉之后，那条连接掀起来的错就是我们自己掐出来的。
      abandoned: () => gen != _gen,
    ).listen(
      (Uint8List f) {
        if (!mounted || gen != _gen) return;
        _firstFrame?.cancel(); // 画面来了，那个闹钟的活儿干完了
        _firstFrame = null;
        _backoff = _backoffMin; // 真拿到画面了，退避从头算
        setState(() {
          _frame = f;
          _trouble = '';
        });
      },
      onError: (Object e) {
        if (!mounted || gen != _gen) return;
        _fail(e);
      },
      // **断在半路也要自己接回来。** 这条流结束(或者出错)的时候，狗那头
      // 完全可能还是在线的 —— 断的是我们这一头的连接。健康轮询看不见这
      // 件事(它问的是狗，狗没毛病)，所以 `_onHealth` 不会翻面，也就不会
      // 重开。不自己接的话，屏幕会一直停在最后那一帧或者一句"正在接画面"，
      // 而那正是 §5.9 要防的:人看着一张不动的图，以为那是此刻。
      onDone: () {
        if (!mounted || gen != _gen) return;
        _shut();
        setState(() => _frame = null);
        _reopenSoon();
      },
    );
    _firstFrame = Timer(_firstFrameGrace, () {
      if (!mounted || gen != _gen) return;
      _fail(
          const PatrolError(0, '一直没收到画面数据',
              '开了流之后 3 秒一帧都没切出来'),
          say: '一直没收到画面数据。确认一下地址和端口对不对 —— '
              '也可能是热点上有个登录页把这条流截下来了');
    });
  }

  /// 一条流走不下去了:收连接、把话说给人听、按退避排下一次。
  ///
  /// **所有失败都走这一条路。** 分开写就会有一条忘了收连接，而那条连接
  /// 会一直占着狗那头 `MAX_VIEWERS` 里的一个名额。
  void _fail(Object raw, {String? say}) {
    // 原文只进日志。屏幕上要的是"该做什么"，不是 Dart 的异常文本。
    developer.log('$raw', name: 'live_video/${widget.camera}');
    _shut();
    setState(() {
      _frame = null;
      _trouble = say ?? _human(raw);
    });
    _reopenSoon();
  }

  /// 过一会儿再开一条，**每失败一次就等得更久**。
  ///
  /// 不立刻重开:热点断了的时候立刻重开会变成一个满速的重连风暴，把本来就
  /// 不宽的那条上行占死。不翻倍的话，满员被拒(503)会变成 0.5 Hz 的无限期
  /// 猛敲 —— 那台手机永远等不到，狗那头却一直在被敲。
  ///
  /// 记的号是**排队这一刻的** [_gen]:排完之后要是有人掐了流(掉线、换狗、
  /// dispose)，那一茬就作废，定时器醒来什么也不做。
  void _reopenSoon() {
    final int gen = _gen;
    final Duration wait = _backoff;
    final Duration doubled = wait * 2;
    _backoff = doubled > _backoffMax ? _backoffMax : doubled;
    _retry?.cancel();
    _retry = Timer(wait, () {
      if (!mounted || gen != _gen || !_live) return;
      _open();
    });
  }

  /// 把异常翻成现场看得懂的一句话。
  ///
  /// **原样的异常文本不上屏。** 值守的人要的是"该做什么"，
  /// `HttpException: Connection closed while receiving data` 回答不了这个;
  /// 而 503 和"狗没起来"要做的事完全相反 —— 一个是等人退出来，一个是走
  /// 过去看那台狗。原文由 [_fail] 记进 `dart:developer` 的日志，屏幕上不留。
  String _human(Object e) {
    if (e is PatrolError && e.status == HttpStatus.serviceUnavailable) {
      return '这一路同时最多 6 个人看，现在满了。'
          '等一会儿，或者让先看的人退出来';
    }
    if (e is PatrolError && e.status == HttpStatus.unauthorized) {
      return '狗不认这个身份了。退出去重新输 PIN';
    }
    return '这一路的画面断了，过几秒会自己再试一次。'
        '一直是这句就走过去看看那台狗';
  }

  /// 掐掉当前这条流。
  ///
  /// **顺序是先掐连接、再取消订阅，不能反。** 掐了之后那条流会以一个
  /// `HttpException` 结束，那个错**必须还有人接**，所以取消排在掐的后面;
  /// [_gen] 负责让它别再改到已经换了一茬的状态上。
  ///
  /// **这件事有测试盯着，红从两个地方来**(复审亲手切过三个变体，都是
  /// 1 秒内退出码 1，不是挂住)：
  ///
  /// - 顺序对调 → `在线时画出画面` / `掉线再回来要重新拉流` / `流自己断了…`
  ///   三条红成框架级 `HttpException: Connection closed while receiving
  ///   data`(掐起来的那个错没人接，逸出去了)。
  /// - 连接漏了没收 → `流自己断了…` 红在 `flutter_test` 的 `!timersPending`
  ///   上:一条没收的 `HttpClient` 一定留着自己那个 15 秒空闲计时器
  ///   (`_HttpClientConnection.startTimer`)，跑不掉。
  ///
  /// 也就是说"来回切几屏就把狗那头 6 个观看名额占满"(`video.py` 的
  /// `MAX_VIEWERS`)这个现场坏法，盯它的是框架不变式而不是某一条断言 ——
  /// 重构这里的人会立刻看到红，不必自己再造一条测试。
  void _shut() {
    _gen++;
    _retry?.cancel();
    _retry = null;
    _firstFrame?.cancel();
    _firstFrame = null;
    final StreamSubscription<Uint8List>? sub = _frames;
    final HttpClient? io = _io;
    _frames = null;
    _io = null;
    io?.close(force: true);
    sub?.cancel();
  }

  /// 父层换了狗、换了相机、换了会话或者换了健康流。
  ///
  /// **这个项目从第一天就是多机的**,机群页上点另一台狗走的就是这条路:
  /// State 被复用，`initState` 不会再跑一次。不接这一下的话，画面会稳稳地
  /// 停在**上一台狗**的那一路上 —— 而屏幕上什么异常都没有，人以为看的是
  /// 刚点的这台。
  @override
  void didUpdateWidget(LiveVideo old) {
    super.didUpdateWidget(old);
    final bool healthSwapped = old.health != widget.health;
    final bool targetMoved = old.baseUrl != widget.baseUrl ||
        old.camera != widget.camera ||
        old.token != widget.token;
    if (!healthSwapped && !targetMoved) return;
    if (healthSwapped) {
      _health?.cancel();
      _health = widget.health.listen(_onHealth);
      // 新的这条还没说过话。拿旧狗的结论顶着就会去开一条新狗上的流，
      // 而那台狗在不在线这一头根本还不知道。
      _live = false;
    }
    _shut();
    _frame = null;
    _trouble = '';
    _backoff = _backoffMin;
    if (_live) _open();
    setState(() {});
  }

  @override
  void dispose() {
    _health?.cancel();
    _shut();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final Uint8List? f = _frame;
    if (_live && f != null) {
      // `gaplessPlayback`：不加的话每来一帧都会先白一下再画。
      return Image.memory(
        f,
        key: LiveVideo.frameKey,
        gaplessPlayback: true,
        fit: BoxFit.cover,
        // 一帧坏 JPEG 不该走 Flutter 的全局错误通道 —— 那条路上屏幕什么
        // 也不说，人只看到画面卡住。
        errorBuilder: (_, _, _) => _panel(
            '${widget.camera} 这一帧解不开', '下一帧一般就好了。一直这样就是这一路出了问题'),
      );
    }
    if (_live) {
      return _panel(
          '${widget.camera} 正在接画面',
          _trouble.isEmpty
              ? '第一帧还没到。要是一直停在这句，这一路就是真的没起来'
              : _trouble);
    }
    return _panel('${widget.camera} 没有画面', '开不了狗。看看还连着热点吗，或者走过去挪');
  }

  /// 没画面时那块板子。
  ///
  /// **不许画转圈。** 一直转圈的圈跟「连不上」长得一样，人分不出该等还是
  /// 该走过去看 —— 而这两件事在现场差着十分钟。
  Widget _panel(String title, String hint) => Container(
        color: const Color(0xFF202124),
        alignment: Alignment.center,
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Text(title,
                style: const TextStyle(color: Colors.white, fontSize: 18)),
            const SizedBox(height: 8),
            Text(hint,
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.white70, fontSize: 13)),
          ],
        ),
      );
}

/// 定期问「现在有画面吗」。
///
/// **走 `/api/video/health` 轮询，不看图片加载状态**(理由见文件头)。
///
/// 2 秒一次：狗那头 `STALE_S` 也是 2 秒，问得比它还密没有更多信息，问得比
/// 它稀就会在「画面已经冻了」和「摇杆变灰」之间留一段空窗。
class VideoHealthPoller {
  // `prefer_initializing_formals` 在这里给的建议(`required this._client`)是
  // 写不出来的：Dart 不许命名参数以下划线开头(`private_optional_parameter`)。
  // 要么把这个字段公开出去 —— 那等于把一个能改狗的客户端挂在轮询器的外面 ——
  // 要么在这儿压掉这一条。选后者。
  VideoHealthPoller({required PatrolClient client, this.period = defaultPeriod})
      // ignore: prefer_initializing_formals
      : _client = client {
    _tick(); // 先问一次，不然头一个 2 秒里界面上什么都说不出来
    _timer = Timer.periodic(period, (_) => _tick());
  }

  static const Duration defaultPeriod = Duration(seconds: 2);

  final PatrolClient _client;
  final Duration period;

  /// 广播：前后两路画面加上摇杆，都要听同一份判定。
  final StreamController<VideoHealth> _ctl =
      StreamController<VideoHealth>.broadcast();

  Timer? _timer;

  /// 上一次问到过哪几路相机。出错时拿它拼一份全 false 的 —— 保住相机名，
  /// 界面上那两块板子不会因为一次问不到就凭空少一块。
  Set<String> _seen = <String>{};

  Stream<VideoHealth> get stream => _ctl.stream;

  Future<void> _tick() async {
    try {
      final VideoHealth h =
          VideoHealth.fromJson(await _client.get('/api/video/health'));
      _seen = h.online.keys.toSet();
      _emit(h);
    } on PatrolError {
      _degrade();
    } catch (_) {
      // 客户端契约上只抛 `PatrolError`；真漏出别的来，也不能让轮询这条线
      // 悄悄断掉 —— 那正是「摇杆停在亮着」的那种坏法。
      _degrade();
    }
  }

  /// 问不到狗时**发一个全 false 的，不是把流关掉**。
  ///
  /// 问不到狗等于看不见，看不见就该变灰。关流的话摇杆会停在最后一个状态
  /// 上，而那个状态很可能是「亮着」。
  void _degrade() =>
      _emit(VideoHealth(<String, bool>{for (final String k in _seen) k: false}));

  void _emit(VideoHealth h) {
    if (!_ctl.isClosed) _ctl.add(h);
  }

  /// 取消 Timer 并 close 掉 controller。人切走了还在问，就是在白耗电和白占
  /// 热点带宽。
  void stop() {
    _timer?.cancel();
    _timer = null;
    _ctl.close();
  }
}
