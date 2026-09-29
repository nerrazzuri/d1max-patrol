/// 建图时看得见（W00c6h、W09f）。老服务在线建图、推「正在长的那张图」；W08 决定 4 之后是这几样：
///
/// - **边走边建的预览**（W09f，`SiteMappingTrailPage` 先问它，管理员）：狗上跟在线建图一起跑的预览进程攒一张
///   俯视图，这里每 3 秒问一次（带上次拿到的 `seq`，有新图才传），画上走过的路、狗现在的位置与朝向。断网时
///   狗上照建，这里留着上一张，连上了下一次就是最新的。预览是 MOLA 系的俯视，**坐标跟建好的图不通用**。
/// - **录包轨迹**（W00c6h，同一页；只录包、老狗没有预览时退回它）：录包期间每 2 秒问一次狗走过的点（只要新增
///   的），画出来 —— 哪儿走过了、哪儿还没去。坐标是狗的里程（录包起点为原点），不是地图坐标：新地方还没有图。
///   狗说停了就不再问，点刷新再看一次。
/// - **图的预览**（`SiteMapPreviewPage`，谁都能看）：站点把建好的栅格图渲染成 PNG，这里能缩放，画上这张图上
///   登记的待命点 —— 建出来的图对不对、待命点落在哪。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../net/site_client.dart';

/// 先问预览（还不知道）、在看预览、在看录包轨迹。
enum _Mode { probe, preview, trail }

/// 预览这么久没更新就说一声（狗上 3 秒写一张）。
const int _staleAfterS = 15;

class SiteMappingTrailPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  final Duration period;
  final Duration previewPeriod;
  const SiteMappingTrailPage(
      {super.key,
      required this.api,
      required this.robotId,
      this.period = const Duration(seconds: 2),
      this.previewPeriod = const Duration(seconds: 3)});

  static const Key canvasKey = Key('trail-canvas');
  static const Key refreshKey = Key('trail-refresh');
  static const Key previewKey = Key('mapping-preview');
  static const Key statusKey = Key('mapping-status');

  @override
  State<SiteMappingTrailPage> createState() => _SiteMappingTrailPageState();
}

class _SiteMappingTrailPageState extends State<SiteMappingTrailPage> with WidgetsBindingObserver {
  _Mode _mode = _Mode.probe;

  // ---- 录包轨迹
  final List<Offset> _pts = <Offset>[];
  bool _recording = true;
  bool _starting = false;
  bool _full = false;
  int _jumps = 0;

  /// 狗上的「第几趟」（W00c6h 内审）：变了就是重新开录了，从头取。
  int? _epoch;
  bool _asked = false;

  // ---- 边走边建的预览
  Map<String, dynamic> _pv = const <String, dynamic>{};
  Uint8List? _png;
  int _seq = 0;

  /// 这一趟的号（录包名）：变了就是换了一趟，从头取。
  String? _run;
  bool _pvStarting = false;

  /// 打包完了（狗说不在边走边建了）：留着最后一张，不再问。
  bool _pvDone = false;

  /// 连不上的时候，手上这张图大约是多少秒前的。
  int _staleS = 0;

  bool _busy = false;
  bool _background = false;
  String _err = '';
  Timer? _timer;

  bool get _live {
    if (_background) return false;
    return switch (_mode) {
      _Mode.probe => true,
      _Mode.preview => !_pvDone,
      _Mode.trail => _recording || _starting,
    };
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    unawaited(_poll());
    _arm();
  }

  void _arm() {
    _timer?.cancel();
    _timer = Timer.periodic(_mode == _Mode.trail ? widget.period : widget.previewPeriod, (_) {
      if (_live) unawaited(_poll());
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // 切到后台就不问（W00c6h 内审）；回来马上问一次。拉一下通知栏（inactive）不算。
    if (state == AppLifecycleState.resumed) {
      _background = false;
      if (_live) unawaited(_poll());
    } else if (state != AppLifecycleState.inactive) {
      _background = true;
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _poll() async {
    if (_busy) return;
    _busy = true;
    try {
      if (_mode == _Mode.trail) {
        await _pollTrail();
      } else {
        await _pollPreview();
      }
    } finally {
      _busy = false;
    }
  }

  /// 退回录包轨迹（只录包、老狗）：换成轨迹的节奏，马上问一次。
  Future<void> _toTrail() async {
    if (!mounted) return;
    setState(() => _mode = _Mode.trail);
    _arm();
    await _pollTrail();
  }

  Future<void> _pollPreview() async {
    Map<String, dynamic> d;
    try {
      d = await widget.api.mappingPreview(widget.robotId, since: _seq);
      final run = d['run'];
      if (d['live'] == true && run is String && _run != null && run != _run) {
        // 换了一趟：旧的 seq 对不上新的，从头取（不然新的一趟头几张拿不到图）。
        _seq = 0;
        d = await widget.api.mappingPreview(widget.robotId);
      }
    } on SiteError catch (e) {
      if (e.status == 409 && _mode == _Mode.probe) return _toTrail(); // 老狗：没有预览
      if (mounted) {
        setState(() {
          _staleS += widget.previewPeriod.inSeconds;
          _err = '连不上：$e';
        });
      }
      return;
    }
    if (!mounted) return;
    if (d['live'] != true) {
      if (_mode == _Mode.probe) {
        // 开录还在起（要十几秒）：还不知道是不是边走边建，接着问。
        if (d['starting'] == true) return setState(() => _pvStarting = true);
        return _toTrail(); // 只录包：看轨迹
      }
      return setState(() {
        _pvDone = true;
        _err = '';
      });
    }
    setState(() {
      _mode = _Mode.preview;
      _pv = d;
      if (d['run'] is String) _run = d['run'] as String;
      _seq = (d['seq'] as num?)?.toInt() ?? 0;
      final png = d['png'];
      if (png is String) {
        try {
          _png = base64Decode(png);
        } on FormatException {
          // 坏的就当这次没图，留着上一张
        }
      }
      if (_seq == 0) _png = null;
      _staleS = 0;
      _err = '';
    });
  }

  Future<void> _pollTrail() async {
    try {
      var d = await widget.api.mappingTrail(widget.robotId, since: _pts.length);
      final epoch = d['epoch'];
      if ((epoch is int && _epoch != null && epoch != _epoch) ||
          ((d['total'] as num?) ?? 0) < _pts.length) {
        // 狗上换了一趟（重新开录、清空过）：从头取，不把新的一趟接在旧的后面。
        _pts.clear();
        d = await widget.api.mappingTrail(widget.robotId);
      }
      if (!mounted) return;
      setState(() {
        for (final p in (d['points'] as List? ?? const [])) {
          if (p is List && p.length >= 2 && p[0] is num && p[1] is num) {
            _pts.add(Offset((p[0] as num).toDouble(), (p[1] as num).toDouble()));
          }
        }
        if (d['epoch'] is int) _epoch = d['epoch'] as int;
        _recording = d['recording'] == true;
        _starting = d['starting'] == true;
        _full = d['full'] == true;
        _jumps = (d['jumps'] as num?)?.toInt() ?? 0;
        _asked = true;
        _err = '';
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = '取不到轨迹：$e');
    }
  }

  String _trailStatus() {
    if (!_asked) return _err.isNotEmpty ? _err : '正在问狗……';
    final n = '已记 ${_pts.length} 个点';
    final span = TrailPainter.span(_pts);
    final size = span == null
        ? ''
        : '，走过的范围约 ${span.width.toStringAsFixed(0)} × ${span.height.toStringAsFixed(0)} m';
    final head = _recording
        ? '录包中：$n$size（每 ${widget.period.inSeconds} 秒更新）'
        : _starting
            ? '录包正在起（要十几秒），起来了就开始记点……'
            : '没在录包（下面是最后一次录的轨迹）：$n$size';
    return [
      head,
      if (_full) '点数到上限了，后面的不再记：红点是记下的最后一点，不是狗现在的位置',
      if (_jumps > 0) '里程跳过 $_jumps 次（运控重启、归零），轨迹在跳的地方接上了',
      if (_err.isNotEmpty) _err,
    ].join('\n');
  }

  String _previewStatus() {
    if (_mode == _Mode.probe) {
      if (_err.isNotEmpty) return _err;
      return _pvStarting ? '开录正在起（要十几秒），起来了就有图……' : '正在问狗……';
    }
    final d = _pv;
    final tag = '${d['map_id']}:${d['version']}';
    final rec = d['recording'] == true;
    final res = (d['res'] as num?)?.toDouble() ?? 0;
    final w = ((d['width'] as num?) ?? 0) * res, h = ((d['height'] as num?) ?? 0) * res;
    final age = (d['age_s'] as num?)?.toDouble() ?? 0;
    final pvErr = '${d['preview_error'] ?? ''}';
    final head = _pvDone
        ? '这一趟建完了（建好的图在地图页），下面是最后一张'
        : !rec
            ? '停了，正在打包 $tag（下面是最后一张）'
            : _seq == 0
                ? '边走边建 $tag：在线建图起来了，等第一张预览……'
                : '边走边建 $tag：已累加 ${d['frames']} 帧，范围约 '
                    '${w.toStringAsFixed(0)} × ${h.toStringAsFixed(0)} m（每 ${widget.previewPeriod.inSeconds} 秒更新）';
    return [
      head,
      if (rec && !_pvDone && _seq > 0 && age > _staleAfterS)
        '预览 ${age.toStringAsFixed(0)} 秒没更新（狗上的预览进程可能停了），建图不受影响',
      if (pvErr.isNotEmpty) '预览没起来：$pvErr（建图不受影响，停下后照样出图）',
      if (d['too_big'] == true) '图太大，狗没发（下面是上一张）',
      if (_err.isNotEmpty) _png != null ? '$_err（下面是约 $_staleS 秒前的图）' : _err,
    ].join('\n');
  }

  @override
  Widget build(BuildContext context) {
    final trail = _mode == _Mode.trail;
    return Scaffold(
      appBar: AppBar(title: Text('${widget.robotId} ${trail ? '录包轨迹' : '边走边建'}'), actions: [
        IconButton(
            key: SiteMappingTrailPage.refreshKey,
            tooltip: '刷新',
            icon: const Icon(Icons.refresh),
            onPressed: () {
              // 再看一次；狗说还在录（还在建）就接着问
              setState(() {
                _recording = true;
                _pvDone = false;
              });
              unawaited(_poll());
            }),
      ]),
      body: trail ? _trailBody() : _previewBody(),
    );
  }

  Widget _trailBody() => Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
            child: Text(_trailStatus(), key: SiteMappingTrailPage.statusKey)),
        const Padding(
          padding: EdgeInsets.symmetric(horizontal: 12),
          child: Text('绿点是录包起点，红点是狗现在的位置；坐标是狗自己的里程（新地方还没有图），走远了会有些漂。',
              style: TextStyle(fontSize: 12, color: Colors.black54)),
        ),
        Expanded(
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: DecoratedBox(
              decoration: BoxDecoration(border: Border.all(color: Colors.black12)),
              child: CustomPaint(
                  key: SiteMappingTrailPage.canvasKey,
                  painter: TrailPainter(List<Offset>.of(_pts)),
                  child: const SizedBox.expand()),
            ),
          ),
        ),
      ]);

  /// 横屏：左边是图（能两指缩放），右边一栏是状态。
  Widget _previewBody() {
    final d = _pv;
    final png = _png;
    final w = ((d['width'] as num?) ?? 1).toDouble(), h = ((d['height'] as num?) ?? 1).toDouble();
    final origin = d['origin'];
    final pose = d['pose'];
    final painter = PreviewPainter(
      res: ((d['res'] as num?) ?? 0.1).toDouble(),
      origin: origin is List && origin.length >= 2 && origin[0] is num && origin[1] is num
          ? Offset((origin[0] as num).toDouble(), (origin[1] as num).toDouble())
          : Offset.zero,
      width: w,
      height: h,
      trail: [
        for (final p in (d['trail'] as List? ?? const []))
          if (p is List && p.length >= 2 && p[0] is num && p[1] is num)
            Offset((p[0] as num).toDouble(), (p[1] as num).toDouble()),
      ],
      pose: pose is List && pose.length >= 3 && pose.every((v) => v is num)
          ? [for (final v in pose) (v as num).toDouble()]
          : null,
    );
    return Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Expanded(
        child: Padding(
          padding: const EdgeInsets.all(8),
          child: DecoratedBox(
            decoration: BoxDecoration(border: Border.all(color: Colors.black12)),
            child: png == null
                ? const Center(child: Text('还没有图'))
                : InteractiveViewer(
                    maxScale: 8,
                    child: Center(
                      child: AspectRatio(
                        key: SiteMappingTrailPage.previewKey,
                        aspectRatio: w / h,
                        child: Stack(fit: StackFit.expand, children: [
                          Image.memory(png,
                              fit: BoxFit.fill, filterQuality: FilterQuality.none, gaplessPlayback: true),
                          CustomPaint(painter: painter),
                        ]),
                      ),
                    ),
                  ),
          ),
        ),
      ),
      SizedBox(
        width: 240,
        child: ListView(padding: const EdgeInsets.all(8), children: [
          Text(_previewStatus(), key: SiteMappingTrailPage.statusKey),
          const SizedBox(height: 8),
          const Text('蓝线是走过的路，绿点是起点，红箭头是狗现在的位置和朝向。这是边走边建时的预览：'
              '坐标跟建好的图不通用，建好的图停下后在地图页看。',
              style: TextStyle(fontSize: 12, color: Colors.black54)),
        ]),
      ),
    ]);
  }
}

/// 在预览图上画走过的路、狗现在的位置与朝向。坐标是预览的平面系（米）：图的左下角是 [origin]、y 朝上，
/// 一格 [res] 米、图 [width] × [height] 格；按画出来的大小缩放。
class PreviewPainter extends CustomPainter {
  PreviewPainter(
      {required this.res,
      required this.origin,
      required this.width,
      required this.height,
      required this.trail,
      required this.pose});
  final double res;
  final Offset origin;
  final double width;
  final double height;
  final List<Offset> trail;

  /// `[x, y, yaw]`；没有是 null。
  final List<double>? pose;

  /// 米 → 画布上的点。
  Offset Function(Offset) layout(Size size) {
    final sx = size.width / (width * res), sy = size.height / (height * res);
    return (p) => Offset((p.dx - origin.dx) * sx, size.height - (p.dy - origin.dy) * sy);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final m = layout(size);
    if (trail.isNotEmpty) {
      final path = Path()..moveTo(m(trail.first).dx, m(trail.first).dy);
      for (final p in trail.skip(1)) {
        final q = m(p);
        path.lineTo(q.dx, q.dy);
      }
      canvas.drawPath(
          path,
          Paint()
            ..color = Colors.blue
            ..style = PaintingStyle.stroke
            ..strokeWidth = 2);
      canvas.drawCircle(m(trail.first), 5, Paint()..color = Colors.green);
    }
    final p = pose;
    if (p != null) {
      final c = m(Offset(p[0], p[1]));
      final d = Offset(math.cos(p[2]), -math.sin(p[2])); // 画布 y 朝下
      final n = Offset(-d.dy, d.dx);
      final arrow = Path()
        ..moveTo(c.dx + d.dx * 12, c.dy + d.dy * 12)
        ..lineTo(c.dx - d.dx * 6 + n.dx * 7, c.dy - d.dy * 6 + n.dy * 7)
        ..lineTo(c.dx - d.dx * 6 - n.dx * 7, c.dy - d.dy * 6 - n.dy * 7)
        ..close();
      canvas.drawPath(arrow, Paint()..color = Colors.red);
    }
  }

  @override
  bool shouldRepaint(PreviewPainter old) => true; // 每次都是新的一份
}

/// 画轨迹：按比例缩进画布（留边）、y 朝上；起点绿、现在红。
class TrailPainter extends CustomPainter {
  TrailPainter(this.points);
  final List<Offset> points;

  static const double _margin = 16;

  /// 走过的范围（米）；没点是 null。
  static Size? span(List<Offset> pts) {
    if (pts.isEmpty) return null;
    var minX = pts.first.dx, maxX = minX, minY = pts.first.dy, maxY = minY;
    for (final p in pts) {
      minX = math.min(minX, p.dx);
      maxX = math.max(maxX, p.dx);
      minY = math.min(minY, p.dy);
      maxY = math.max(maxY, p.dy);
    }
    return Size(maxX - minX, maxY - minY);
  }

  /// 米 → 画布上的点。
  Offset Function(Offset) layout(Size size) {
    if (points.isEmpty) return (p) => size.center(Offset.zero);
    var minX = points.first.dx, maxX = minX, minY = points.first.dy, maxY = minY;
    for (final p in points) {
      minX = math.min(minX, p.dx);
      maxX = math.max(maxX, p.dx);
      minY = math.min(minY, p.dy);
      maxY = math.max(maxY, p.dy);
    }
    final w = math.max(maxX - minX, 1.0), h = math.max(maxY - minY, 1.0); // 至少 1 m 见方
    final scale = math.max(0.0, math.min((size.width - 2 * _margin) / w, (size.height - 2 * _margin) / h));
    final cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    return (p) => Offset(size.width / 2 + (p.dx - cx) * scale, size.height / 2 - (p.dy - cy) * scale);
  }

  @override
  void paint(Canvas canvas, Size size) {
    if (points.isEmpty) return;
    final m = layout(size);
    final path = Path()..moveTo(m(points.first).dx, m(points.first).dy);
    for (final p in points.skip(1)) {
      final q = m(p);
      path.lineTo(q.dx, q.dy);
    }
    canvas.drawPath(
        path,
        Paint()
          ..color = Colors.blue
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2);
    canvas.drawCircle(m(points.first), 6, Paint()..color = Colors.green);
    canvas.drawCircle(m(points.last), 6, Paint()..color = Colors.red);
  }

  @override
  bool shouldRepaint(TrailPainter old) => true; // 每次都是新的一份点（重取之后点数可能碰巧一样）
}

class SiteMapPreviewPage extends StatefulWidget {
  final SiteApi api;
  final String mapId;
  final String version;
  const SiteMapPreviewPage(
      {super.key, required this.api, required this.mapId, required this.version});

  static const Key overlayKey = Key('preview-overlay');

  @override
  State<SiteMapPreviewPage> createState() => _SiteMapPreviewPageState();
}

class _SiteMapPreviewPageState extends State<SiteMapPreviewPage> {
  late final Future<(Map<String, dynamic>, Uint8List)> _data = _load();

  Future<(Map<String, dynamic>, Uint8List)> _load() async => (
        await widget.api.mapPreview(widget.mapId, widget.version),
        await widget.api.mapPreviewPng(widget.mapId, widget.version),
      );

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('${widget.mapId}:${widget.version}')),
      body: FutureBuilder<(Map<String, dynamic>, Uint8List)>(
        future: _data,
        builder: (context, snap) {
          if (snap.hasError) {
            final e = snap.error;
            return Padding(
                padding: const EdgeInsets.all(16),
                child: Text('看不了：${e is SiteError ? e.message : e}'));
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final (meta, png) = snap.data!;
          final w = (meta['width'] as num? ?? 1).toDouble();
          final h = (meta['height'] as num? ?? 1).toDouble();
          final mpp = (meta['m_per_px'] as num? ?? 1).toDouble();
          final left = (meta['left_x'] as num? ?? 0).toDouble();
          final top = (meta['top_y'] as num? ?? 0).toDouble();
          final pts = (meta['standby'] as List? ?? const []).cast<Map<String, dynamic>>();
          final pixels = [
            for (final p in pts)
              Offset(((p['x'] as num).toDouble() - left) / mpp,
                  (top - (p['y'] as num).toDouble()) / mpp),
          ];
          // 横屏：左边是图（能缩放），右边一栏是待命点和比例。
          return Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Expanded(
              child: InteractiveViewer(
                maxScale: 8,
                child: FittedBox(
                  child: SizedBox(
                    width: w,
                    height: h,
                    child: Stack(children: [
                      Image.memory(png,
                          width: w, height: h, filterQuality: FilterQuality.none, gaplessPlayback: true),
                      CustomPaint(
                          key: SiteMapPreviewPage.overlayKey,
                          size: Size(w, h),
                          painter: StandbyPainter(pixels, radius: math.max(2.0, math.max(w, h) / 120))),
                    ]),
                  ),
                ),
              ),
            ),
            SizedBox(
              width: 220,
              child: ListView(padding: const EdgeInsets.all(8), children: [
                Text('一像素 ${mpp.toStringAsFixed(2)} m · 图 ${w.toInt()} × ${h.toInt()} 像素 · '
                    '约 ${(w * mpp).toStringAsFixed(0)} × ${(h * mpp).toStringAsFixed(0)} m'),
                if (meta['source'] != null)
                  Text('用的是 ${meta['source']}', style: const TextStyle(fontSize: 12)),
                if (meta['warning'] != null)
                  Text('${meta['warning']}', style: const TextStyle(color: Colors.deepOrange)),
                const Divider(),
                if (pts.isEmpty) const Text('这张图上还没有登记待命点'),
                for (final p in pts)
                  Text('${p['robot_id']} · ${p['name']}${p['default'] == true ? '（默认）' : ''}  '
                      '(${(p['x'] as num).toStringAsFixed(1)}, ${(p['y'] as num).toStringAsFixed(1)})'),
              ]),
            ),
          ]);
        },
      ),
    );
  }
}

/// 在图上画待命点（图的像素坐标）。
class StandbyPainter extends CustomPainter {
  StandbyPainter(this.pixels, {this.radius = 3});
  final List<Offset> pixels;
  final double radius;

  @override
  void paint(Canvas canvas, Size size) {
    for (final p in pixels) {
      canvas.drawCircle(p, radius, Paint()..color = Colors.orange);
      canvas.drawCircle(
          p,
          radius,
          Paint()
            ..color = Colors.black
            ..style = PaintingStyle.stroke
            ..strokeWidth = radius / 3);
    }
  }

  @override
  bool shouldRepaint(StandbyPainter old) => old.pixels != pixels;
}
