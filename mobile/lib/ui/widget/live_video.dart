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

  final String boundary;
  final Uint8List _mark;

  /// 还没切完的那点尾巴。
  ///
  /// **这个缓冲就是「跨包」这件事的全部。** 一次 `add` 拿到的可能是半帧，
  /// 也可能是两帧半，边界还可能正好被劈在两次 `add` 中间；按包各切各的
  /// 会漏掉那个边界，往后所有帧全错位。
  final List<int> _buf = <int>[];

  /// 喂一段字节，拿回这一段里切得出来的所有整帧。切不满一帧就返回空表。
  List<Uint8List> add(List<int> chunk) {
    _buf.addAll(chunk);
    final List<Uint8List> out = <Uint8List>[];
    while (true) {
      final int start = _find(_buf, _mark, 0);
      if (start < 0) {
        // 一个边界都没有。**不能整段丢掉**：边界可能正好被切在这一包的
        // 末尾，留住最后 mark.length-1 个字节去接下一包。
        _keepTail(_mark.length - 1);
        break;
      }
      final int head = _find(_buf, _blankLine, start + _mark.length);
      if (head < 0) break; // 这一段自己的头还没收全
      final int from = head + _blankLine.length;
      final int next = _find(_buf, _mark, from);
      if (next < 0) break; // 这一帧还没收全，等下一包
      int end = next;
      if (end - from >= 2 && _buf[end - 2] == 13 && _buf[end - 1] == 10) {
        end -= 2; // 净荷和下一个边界之间那个 CRLF 不算画面
      }
      out.add(Uint8List.fromList(_buf.sublist(from, end)));
      _buf.removeRange(0, next); // 留着下一个边界，接着切
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
Stream<Uint8List> mjpegFrames(Uri url,
    {required HttpClient io, String token = ''}) async* {
  final HttpClientRequest req = await io.openUrl('GET', url);
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

  /// 流自己断了之后隔多久再拉一次。
  ///
  /// 跟健康轮询同一个 2 秒:再快也没有新信息(狗那头 `STALE_S` 就是 2 秒)，
  /// 再慢就会在"画面没了"和"画面回来"之间多留一段黑屏。
  static const Duration _retryAfter = Duration(seconds: 2);

  @override
  void initState() {
    super.initState();
    _health = widget.health.listen(_onHealth);
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
    final int gen = _gen;
    final HttpClient io = HttpClient()
      ..connectionTimeout = const Duration(seconds: 5);
    _io = io;
    _frames = mjpegFrames(
      Uri.parse('${widget.baseUrl}/api/video/${widget.camera}'),
      io: io,
      token: widget.token,
    ).listen(
      (Uint8List f) {
        if (!mounted || gen != _gen) return;
        setState(() {
          _frame = f;
          _trouble = '';
        });
      },
      onError: (Object e) {
        if (!mounted || gen != _gen) return;
        setState(() {
          _frame = null;
          _trouble = '$e';
        });
        _reopenSoon(gen);
      },
      // **断在半路也要自己接回来。** 这条流结束(或者出错)的时候，狗那头
      // 完全可能还是在线的 —— 断的是我们这一头的连接。健康轮询看不见这
      // 件事(它问的是狗，狗没毛病)，所以 `_onHealth` 不会翻面，也就不会
      // 重开。不自己接的话，屏幕会一直停在最后那一帧或者一句"正在接画面"，
      // 而那正是 §5.9 要防的:人看着一张不动的图，以为那是此刻。
      onDone: () {
        if (!mounted || gen != _gen) return;
        setState(() => _frame = null);
        _reopenSoon(gen);
      },
    );
  }

  /// 隔 [_retryAfter] 再开一条。
  ///
  /// 不立刻重开:热点断了的时候立刻重开会变成一个满速的重连风暴，把本来就
  /// 不宽的那条上行占死。[gen] 是排掉已经换过茬的那条流的迟到回调。
  void _reopenSoon(int gen) {
    _retry?.cancel();
    _retry = Timer(_retryAfter, () {
      if (!mounted || gen != _gen || !_live) return;
      _open();
    });
  }

  /// 掐掉当前这条流。
  ///
  /// **顺序是先掐连接、再取消订阅，不能反。** 反过来 `cancel()` 会挂住：
  /// 它要等这条响应收完，而 MJPEG 永远收不完 —— 那条连接就会一直挂在狗那头
  /// 占着一个观众名额(`video.py` 的 `MAX_VIEWERS` 是 6，重连几次就满了)。
  ///
  /// 掐了之后那条流会以一个 `HttpException` 结束。那个错**必须还有人接**，
  /// 所以取消排在掐的后面；[_gen] 负责让它别再改到已经换了一茬的状态上。
  void _shut() {
    _gen++;
    _retry?.cancel();
    _retry = null;
    final StreamSubscription<Uint8List>? sub = _frames;
    final HttpClient? io = _io;
    _frames = null;
    _io = null;
    io?.close(force: true);
    sub?.cancel();
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
      return Image.memory(f, gaplessPlayback: true, fit: BoxFit.cover);
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
