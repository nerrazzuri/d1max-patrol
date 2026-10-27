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
/// - **禁行区、限速区**（W10，同一页；管理员画、删、确认）：点图加顶点画多边形，存的时候带看到的修订号（别人
///   先改了站点回 409）；**发布前人工确认**水体、落差、陡坡、花坛都画了，没确认的图下发不了。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'map_view.dart';
import 'nw/nw_map_raster.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

/// 先问预览（还不知道）、在看预览、在看录包轨迹。
enum _Mode { probe, preview, trail }

/// 预览这么久没更新就说一声（狗上 3 秒写一张）。
const int _staleAfterS = 15;

/// 区域的名字在英文界面上怎么显示。名字是存在站点上的数据：画的时候 app 给的缺省名是中文的
/// 「禁行区」「限速区」，英文界面把这两个翻过来；人自己起的名字原样显示。
String zoneLabelText(Object? label) {
  final l = '${label ?? ''}';
  if (appLang.value == AppLang.zh) return l;
  return switch (l) { '禁行区' => 'No-go zone', '限速区' => 'Speed-limit zone', '抹掉区' => 'Erase zone', '虚拟墙' => 'Virtual wall', _ => l };
}

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
    var newRun = false;
    try {
      d = await widget.api.mappingPreview(widget.robotId, since: _seq);
      final run = d['run'];
      if (d['live'] == true && run is String && _run != null && run != _run) {
        // 换了一趟：旧的 seq 对不上新的，从头取（不然新的一趟头几张拿不到图）。
        _seq = 0;
        newRun = true;
        d = await widget.api.mappingPreview(widget.robotId);
      }
    } on SiteError catch (e) {
      // 老狗（站点说不支持）、老站点（没这个接口，404）：没有预览，看录包轨迹。别的 409（狗暂时不在线、
      // 代理在重启）不算：接着问（W09f 内审应修 1）。
      if (_mode == _Mode.probe && (e.status == 404 || e.body['unsupported'] == true)) {
        return _toTrail();
      }
      if (mounted) {
        setState(() {
          _staleS += widget.previewPeriod.inSeconds;
          _err = tr("Can't reach the site: $e", '连不上：$e');
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
          _setPng(base64Decode(png));
        } on FormatException {
          // 坏的就当这次没图，留着上一张
        }
      } else if (newRun && _seq == 0) {
        // 新的一趟还没第一张：不留上一趟的图。同一趟里回到 0（打包收了中间目录）留着最后一张（内审小 1）
        _setPng(null);
      }
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
      if (mounted) setState(() => _err = tr('Trail unavailable: $e', '取不到轨迹：$e'));
    }
  }

  /// 换图：旧的那张从图片缓存里清掉（每 3 秒一张新的，不清会一直攒到缓存上限；内审小 2）。
  void _setPng(Uint8List? png) {
    final old = _png;
    if (old != null && !identical(old, png)) unawaited(MemoryImage(old).evict());
    _png = png;
  }

  String _trailStatus() {
    if (!_asked) return _err.isNotEmpty ? _err : tr('Asking the robot…', '正在问机器人……');
    final n = tr('${_pts.length} points logged', '已记 ${_pts.length} 个点');
    final span = TrailPainter.span(_pts);
    final size = span == null
        ? ''
        : tr(', area covered about ${span.width.toStringAsFixed(0)} × ${span.height.toStringAsFixed(0)} m',
            '，走过的范围约 ${span.width.toStringAsFixed(0)} × ${span.height.toStringAsFixed(0)} m');
    final head = _recording
        ? tr('Recording: $n$size (updates every ${widget.period.inSeconds} s)',
            '录包中：$n$size（每 ${widget.period.inSeconds} 秒更新）')
        : _starting
            ? tr('Recording is starting (takes 10–20 s). Points are logged once it is up…',
                '录包正在起（要十几秒），起来了就开始记点……')
            : tr('Not recording (trail of the last recording below): $n$size',
                '没在录包（下面是最后一次录的轨迹）：$n$size');
    return [
      head,
      if (_full)
        tr('Point limit reached, no more points are logged: the red dot is the last logged point, '
            'not where the robot is now',
            '点数到上限了，后面的不再记：红点是记下的最后一点，不是机器人现在的位置'),
      if (_jumps > 0)
        tr('Odometry jumped $_jumps time(s) (motion controller restart or reset); '
            'the trail was joined at the jumps',
            '里程跳过 $_jumps 次（运控重启、归零），轨迹在跳的地方接上了'),
      if (_err.isNotEmpty) _err,
    ].join('\n');
  }

  String _previewStatus() {
    if (_mode == _Mode.probe) {
      if (_err.isNotEmpty) return _err;
      return _pvStarting
          ? tr('Recording is starting (takes 10–20 s). The image appears once it is up…',
              '开录正在起（要十几秒），起来了就有图……')
          : tr('Asking the robot…', '正在问机器人……');
    }
    final d = _pv;
    final tag = '${d['map_id']}:${d['version']}';
    final rec = d['recording'] == true;
    final res = (d['res'] as num?)?.toDouble() ?? 0;
    final w = ((d['width'] as num?) ?? 0) * res, h = ((d['height'] as num?) ?? 0) * res;
    final age = (d['age_s'] as num?)?.toDouble() ?? 0;
    final pvErr = '${d['preview_error'] ?? ''}';
    final head = _pvDone
        ? tr('This run is finished (the built map is on the Maps page). Last preview below',
            '这一趟建完了（建好的图在地图页），下面是最后一张')
        : !rec
            ? tr('Stopped, packaging $tag (last preview below)', '停了，正在打包 $tag（下面是最后一张）')
            : _seq == 0
                ? tr('Live mapping $tag: mapping is up, waiting for the first preview…',
                    '边走边建 $tag：在线建图起来了，等第一张预览……')
                : tr(
                    'Live mapping $tag: ${d['frames']} frames merged, area about '
                        '${w.toStringAsFixed(0)} × ${h.toStringAsFixed(0)} m '
                        '(updates every ${widget.previewPeriod.inSeconds} s)',
                    '边走边建 $tag：已累加 ${d['frames']} 帧，范围约 '
                        '${w.toStringAsFixed(0)} × ${h.toStringAsFixed(0)} m（每 ${widget.previewPeriod.inSeconds} 秒更新）');
    return [
      head,
      if (rec && !_pvDone && d['preview_running'] == false)
        tr('Preview process stopped (log: mapview.log). Mapping is not affected; '
            'the map is still built after you stop',
            '预览进程停了（日志 mapview.log），建图不受影响，停下后照样出图')
      else if (rec && !_pvDone && _seq > 0 && age > _staleAfterS)
        tr('Preview not updated for ${age.toStringAsFixed(0)} s (the preview process on the robot '
            'may have stopped). Mapping is not affected',
            '预览 ${age.toStringAsFixed(0)} 秒没更新（机器人上的预览进程可能停了），建图不受影响'),
      if (pvErr.isNotEmpty)
        tr('Preview did not start: $pvErr (mapping is not affected; the map is still built after you stop)',
            '预览没起来：$pvErr（建图不受影响，停下后照样出图）'),
      if (d['too_big'] == true)
        tr('Image too large, the robot did not send it (previous one below)', '图太大，机器人没发（下面是上一张）'),
      if (_err.isNotEmpty)
        _png != null
            ? tr('$_err (the image below is about $_staleS s old)', '$_err（下面是约 $_staleS 秒前的图）')
            : _err,
    ].join('\n');
  }

  @override
  Widget build(BuildContext context) {
    final trail = _mode == _Mode.trail;
    return Scaffold(
      appBar: AppBar(title: Text('${widget.robotId} ${trail ? tr('recording trail', '录包轨迹') : tr('live mapping', '边走边建')}'), actions: [
        IconButton(
            key: SiteMappingTrailPage.refreshKey,
            tooltip: tr('Refresh', '刷新'),
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
            child: Text(_trailStatus(),
                key: SiteMappingTrailPage.statusKey, style: NwText.bodyMedium.copyWith(color: Nw.of(context).text))),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Text(
              tr('The green dot is where recording started, the red dot is where the robot is now. '
                  'Coordinates are the robot\'s own odometry (a new place has no map yet) and drift over distance.',
                  '绿点是录包起点，红点是机器人现在的位置；坐标是机器人自己的里程（新地方还没有图），走远了会有些漂。'),
              style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
        ),
        Expanded(
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: DecoratedBox(
              decoration: BoxDecoration(border: Border.all(color: Nw.of(context).outline)),
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
            decoration: BoxDecoration(border: Border.all(color: Nw.of(context).outline)),
            child: png == null
                ? Center(child: Text(tr('No image yet', '还没有图')))
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
      _sidePanel(context, [
        Text(_previewStatus(), key: SiteMappingTrailPage.statusKey),
        const SizedBox(height: 8),
        Text(
            tr('The blue line is the path covered, the green dot is the start, the red arrow is the '
                'robot\'s position and heading. This is a preview during live mapping: its coordinates '
                'do not match the built map. View the built map on the Maps page after you stop.',
                '蓝线是走过的路，绿点是起点，红箭头是机器人现在的位置和朝向。这是边走边建时的预览：'
                '坐标跟建好的图不通用，建好的图停下后在地图页看。'),
            style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
      ]),
    ]);
  }
}

/// 图右边那一栏（状态、图上登记的东西、区域）：卡片的底、左边一条线，里面能滚。不按需建：区域、带过来的
/// 建议、确认按钮一项不少都在（R2 之后这一栏长了，按需建的列表里滚到下面之前按钮还不存在）。
Widget _sidePanel(BuildContext context, List<Widget> children) {
  final c = Nw.of(context);
  return SizedBox(
      width: 240,
      child: Material(
          color: c.surface,
          shape: Border(left: BorderSide(color: c.outline)),
          child: DefaultTextStyle.merge(
              style: NwText.bodyMedium.copyWith(color: c.text),
              child: SingleChildScrollView(
                  padding: const EdgeInsets.all(8),
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch, children: children)))));
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

/// 图上额外标的一个点（W17：告警现场 —— 狗在哪、拦截点在哪）。
class MapMark {
  final String label;
  final double x;
  final double y;
  final Color color;
  const MapMark(this.label, this.x, this.y, this.color);
}

class SiteMapPreviewPage extends StatefulWidget {
  final SiteApi api;
  final String mapId;
  final String version;
  /// 告警现场（W17）：大红圈标出来、右栏最上面列出来。
  final List<MapMark> marks;
  const SiteMapPreviewPage(
      {super.key,
      required this.api,
      required this.mapId,
      required this.version,
      this.marks = const []});

  static const Key marksKey = Key('preview-marks');

  static const Key overlayKey = Key('preview-overlay');
  static const Key zonesKey = Key('preview-zones');
  static const Key canvasKey = Key('preview-canvas');

  /// 片区淡淡的底和边（`CustomPaint`）；片区的名字（`Text`）。
  static const Key areasKey = Key('preview-areas');
  static Key areaLabelKey(String name) => Key('preview-area-$name');
  static const Key drawNogoKey = Key('zone-draw-nogo');
  static const Key drawSlowKey = Key('zone-draw-slow');

  /// 台账 44：抹掉一块（区域种类 `erase`：狗永远不去，站点画图时抹成没建图）。
  static const Key drawEraseKey = Key('zone-draw-erase');
  static const Key drawWallKey = Key('zone-draw-wall');
  static const Key finishKey = Key('zone-finish');
  static const Key undoKey = Key('zone-undo');
  static const Key cancelKey = Key('zone-cancel');
  static const Key confirmKey = Key('zone-confirm');
  static const Key confirmOkKey = Key('zone-confirm-ok');
  static Key deleteKey(String id) => Key('zone-del-$id');
  /// 决策 65：画禁行区时标「硬禁区」（水体、落差：遥控也开不进去）。
  static const Key hardKey = Key('zone-hard');
  /// 决策 66：临时区多久到期（下拉框；里面每一项按秒，null = 一直有效）。
  static const Key untilKey = Key('zone-until');
  static Key forKey(int? seconds) => Key('zone-for-${seconds ?? 0}');
  /// R2：新版本从上一版带过来、等人核的那一块；每一项的勾；不要了。
  static const Key proposalKey = Key('zone-proposal');
  static Key proposedKey(String kind, String id) => Key('zone-prop-$kind-$id');
  static const Key discardProposalKey = Key('zone-proposal-discard');
  /// R2：确认一个区域都没有的那一版，多弹的那道提示上的「真的没有」。
  static const Key confirmEmptyKey = Key('zone-confirm-empty');

  @override
  State<SiteMapPreviewPage> createState() => _SiteMapPreviewPageState();
}

class _SiteMapPreviewPageState extends State<SiteMapPreviewPage> {
  /// （预览 JSON，底图，底图是不是平面图）。抹掉区改了重新取（站点把底图那一块抹了，平面图地址也变了）。
  late Future<(Map<String, dynamic>, Uint8List, bool)> _data = _load();

  /// 图的缩放（片区的名字跟着它决定放不放得下）。
  final TransformationController _zoom = TransformationController();

  /// 区域（W10）：`GET …/zones` 那一份；取不到就只看图。
  Map<String, dynamic>? _zones;
  String? _zonesError;

  /// 正在画的：种类（`nogo` / `slow`）与已经点下的顶点（地图坐标，米）。
  String? _drawing;
  final List<Offset> _draft = <Offset>[];
  double _speed = 0.3;
  bool _busy = false;
  String? _msg;

  /// 正在画的禁行区是不是硬禁区（决策 65）、临时区多久到期（决策 66，秒；null = 一直有效）。
  bool _hard = false;
  int? _forS;

  /// 带过来的建议里勾上了哪几个（R2）：`zone:id`、`standby:狗/名字`、`intercept:名字`。
  final Set<String> _adopt = <String>{};

  /// 预览 JSON，再取底图：站点给了平面图先取平面图，取不到退回栅格预览图（[loadMapBase]）。
  Future<(Map<String, dynamic>, Uint8List, bool)> _load() async {
    final meta = await widget.api.mapPreview(widget.mapId, widget.version);
    final (png, plan) = await loadMapBase(widget.api, widget.mapId, widget.version, MapGeo.fromPreview(meta));
    return (meta, png, plan);
  }

  @override
  void initState() {
    super.initState();
    _loadZones();
  }

  @override
  void dispose() {
    _zoom.dispose();
    super.dispose();
  }

  Future<void> _loadZones() async {
    try {
      final z = await widget.api.mapZones(widget.mapId, widget.version);
      if (mounted) {
        setState(() {
          _zones = z;
          _zonesError = null;
        });
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _zonesError = e.message);
    }
  }

  List<Map<String, dynamic>> get _zoneList =>
      (_zones?['zones'] as List? ?? const []).cast<Map<String, dynamic>>();
  int get _rev => (_zones?['revision'] as num? ?? 0).toInt();

  /// 新版本从上一版带过来、等人核的（R2）；没有是 null。
  Map<String, dynamic>? get _proposal => _zones?['proposal'] as Map<String, dynamic>?;
  List<Map<String, dynamic>> _propList(String k) =>
      (_proposal?[k] as List? ?? const []).cast<Map<String, dynamic>>();

  /// 整份存（新加、删一个都走这里）；站点回什么就说什么（409 = 别人先改了）。[done] 拿站点说的
  /// 「只收紧、当场算确认了」（决策 66）给出那句话。
  Future<void> _save(List<Map<String, dynamic>> zones, String Function(bool auto) done) async {
    setState(() {
      _busy = true;
      _msg = null;
    });
    String erased(List<Map<String, dynamic>> zs) =>
        [for (final z in zs) if (z['kind'] == 'erase') '${z['id']}:${z['polygon']}'].join('|');
    final before = erased(_zoneList);
    var saved = false;
    try {
      final r = await widget.api.saveZones(widget.mapId, widget.version, zones, _rev);
      _msg = done(r['auto_confirmed'] == true);
      saved = true;
    } on SiteError catch (e) {
      _msg = tr('Not saved: ${e.message}', '没存上：${e.message}');
    }
    await _loadZones();
    if (!mounted) return;
    setState(() {
      _busy = false;
      // 抹掉区变了：底图重新取（站点已经按新的抹掉区画好了）
      if (saved && erased(zones) != before) _data = _load();
    });
  }

  void _startDrawing(String kind) => setState(() {
        _drawing = kind;
        _hard = false;
        _forS = null;
      });

  Future<void> _finish() async {
    final kind = _drawing!;
    var id = 'z${_rev + 1}';
    while (_zoneList.any((z) => z['id'] == id)) {
      id = '${id}x';
    }
    final pts = [
      for (final p in _draft) [double.parse(p.dx.toStringAsFixed(2)), double.parse(p.dy.toStringAsFixed(2))]
    ];
    // 虚拟墙（2026-10-15）：只给画的线，多边形（约 0.3 m 宽的禁行带）站点算
    final zone = <String, dynamic>{
      'id': id,
      'kind': kind == 'wall' ? 'nogo' : kind,
      'label': switch (kind) { 'nogo' => '禁行区', 'erase' => '抹掉区', 'wall' => '虚拟墙', _ => '限速区' },
      if (kind == 'wall') 'wall': pts else 'polygon': pts,
      if (kind == 'slow') 'max_speed_mps': double.parse(_speed.toStringAsFixed(1)),
      if ((kind == 'nogo' || kind == 'erase' || kind == 'wall') && _hard) 'hard': true,
      // 临时区：给时长，站点按它自己的钟换成到期时刻（手机的钟可能不准）
      if (_forS != null) 'for_s': _forS,
    };
    setState(() {
      _drawing = null;
      _draft.clear();
    });
    final next = _rev + 1;
    await _save(
        [..._zoneList, zone],
        (auto) => auto
            ? tr('Saved: revision $next (only tightens: stays confirmed, robots can still be sent out)',
                '存好了：第 $next 版（只收紧：不用重新确认，照常派机器人）')
            : tr('Saved: revision $next (changes must be confirmed again)',
                '存好了：第 $next 版（改过要重新确认）'));
  }

  Future<void> _confirm() async {
    final prop = _proposal;
    final zoneIds = [
      for (final z in _propList('zones'))
        if (_adopt.contains('zone:${z['id']}')) '${z['id']}'
    ];
    final standby = [
      for (final p in _propList('standby'))
        if (_adopt.contains('standby:${p['robot_id']}/${p['name']}')) ['${p['robot_id']}', '${p['name']}']
    ];
    final intercepts = [
      for (final p in _propList('intercepts'))
        if (_adopt.contains('intercept:${p['name']}')) '${p['name']}'
    ];
    final picked = zoneIds.length + standby.length + intercepts.length;
    final offered =
        _propList('zones').length + _propList('standby').length + _propList('intercepts').length;
    // 二次确认（W10 内审小 15）：把确认的那句话念一遍，别跟画区域的按钮误触
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              scrollable: true,
              title: Text(tr('Confirm zones revision $_rev', '确认第 $_rev 版区域')),
              content: Text(tr(
                  '${_zones?['confirm_text'] ?? ''}.\nThe map can be sent to robots only after you confirm.'
                      '${prop == null ? '' : '\nCopied from version ${prop['from_version']}: adopting the $picked ticked item(s) of $offered; the rest are dropped.'}',
                  '${_zones?['confirm_text'] ?? ''}。\n确认之后这张图才能下发给机器人。'
                      '${prop == null ? '' : '\n从第 ${prop['from_version']} 版带过来的 $offered 个里，采纳勾上的 $picked 个，没勾的不要了。'}')),
              actions: [
                TextButton(
                    onPressed: () => Navigator.pop(c, false),
                    child: Text(tr('Review again', '再看看'))),
                FilledButton(
                    key: SiteMapPreviewPage.confirmOkKey,
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('Confirm', '确认'))),
              ],
            ));
    if (ok != true || !mounted) return;
    var emptyOk = false;
    if (_zoneList.isEmpty && zoneIds.isEmpty) {
      // R2：一个区域都没有的那一版再问一道 —— 重建图之后，上一版画的泳池可能悄悄没了
      final prev = _zones?['previous'] as Map<String, dynamic>?;
      final had = (prev?['zones'] as num? ?? 0).toInt();
      final ok2 = await showDialog<bool>(
          context: context,
          builder: (c) => AlertDialog(
                scrollable: true,
                title: Text(tr('No zones at all', '一个区域都没有')),
                content: Text(tr(
                    'This version has no no-go or speed-limit zone at all'
                        '${had > 0 ? ' (version ${prev?['version']} had $had)' : ''}. Confirm only if there '
                        'really is no water, drop, steep slope or flower bed anywhere on this map.',
                    '这一版一个禁行区、限速区都没有${had > 0 ? '（上一版第 ${prev?['version']} 版画了 $had 个）' : ''}。'
                        '确认之前再看一眼：水体、落差、陡坡、花坛真的一处都没有吗？')),
                actions: [
                  TextButton(
                      onPressed: () => Navigator.pop(c, false),
                      child: Text(tr('Review again', '再看看'))),
                  FilledButton(
                      key: SiteMapPreviewPage.confirmEmptyKey,
                      onPressed: () => Navigator.pop(c, true),
                      child: Text(tr('Yes, there are none', '真的没有'))),
                ],
              ));
      if (ok2 != true || !mounted) return;
      emptyOk = true;
    }
    setState(() {
      _busy = true;
      _msg = null;
    });
    try {
      final r = await widget.api.confirmZones(widget.mapId, widget.version, _rev,
          emptyOk: emptyOk,
          adopt: prop == null
              ? null
              : <String, dynamic>{'zones': zoneIds, 'standby': standby, 'intercepts': intercepts});
      // 待命点、拦截点没搬过去的（上一版的改过、拦截点走不到……）说原因
      final adopted = r['adopted'] as Map<String, dynamic>?;
      final failed = [
        for (final k in const ['standby', 'intercepts'])
          for (final e in ((adopted?[k] as Map?) ?? const {}).entries)
            if ('${e.value}'.isNotEmpty) '${e.key}: ${e.value}'
      ];
      _adopt.clear();
      _msg = tr('Revision ${r['revision'] ?? _rev} confirmed', '确认了第 ${r['revision'] ?? _rev} 版') +
          (failed.isEmpty ? '' : '\n${failed.join('\n')}');
    } on SiteError catch (e) {
      _msg = tr('Not confirmed: ${e.message}', '没确认上：${e.message}');
    }
    await _loadZones();
    if (mounted) setState(() => _busy = false);
  }

  Future<void> _discardProposal() async {
    setState(() {
      _busy = true;
      _msg = null;
    });
    try {
      await widget.api.discardZoneProposal(widget.mapId, widget.version);
      _adopt.clear();
      _msg = tr('Copied items dropped', '带过来的不要了');
    } on SiteError catch (e) {
      _msg = tr('Not dropped: ${e.message}', '没丢掉：${e.message}');
    }
    await _loadZones();
    if (mounted) setState(() => _busy = false);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('${widget.mapId}:${widget.version}')),
      body: FutureBuilder<(Map<String, dynamic>, Uint8List, bool)>(
        future: _data,
        builder: (context, snap) {
          if (snap.hasError) {
            final e = snap.error;
            return SingleChildScrollView(
                padding: const EdgeInsets.all(16),
                child: NwNotice(
                    tone: NwTone.warning,
                    text: tr('Preview unavailable: ${e is SiteError ? e.message : e}',
                        '看不了：${e is SiteError ? e.message : e}')));
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final (meta, png, plan) = snap.data!;
          final geo = MapGeo.fromPreview(meta);
          final w = geo.width, h = geo.height, mpp = geo.mPerPx, rad = geo.radians;
          Offset px(double x, double y) => geo.toPx(x, y);
          final pts = (meta['standby'] as List? ?? const []).cast<Map<String, dynamic>>();
          final pixels = [
            for (final p in pts) px((p['x'] as num).toDouble(), (p['y'] as num).toDouble()),
          ];
          // 原点（W13a）：单独画（蓝方块），跟待命点（橙圆点）分开。
          final homes = (meta['homes'] as List? ?? const []).cast<Map<String, dynamic>>();
          final homePixels = [
            for (final p in homes) px((p['x'] as num).toDouble(), (p['y'] as num).toDouble()),
          ];
          // 区域：GET …/zones 那一份最新；取不到退回预览里带的。
          final zones = _zones != null
              ? _zoneList
              : (meta['zones'] as List? ?? const []).cast<Map<String, dynamic>>();
          List<Offset> poly(Map<String, dynamic> z) => [
                for (final v in (z['polygon'] as List? ?? const []))
                  px(((v as List)[0] as num).toDouble(), (v[1] as num).toDouble())
              ];
          final shapes = [
            for (final z in zones) ZoneShape.of(z, poly(z), wall: zoneWallPixels(z, px)),
            // R2：从上一版带过来、还没人核的：只画轮廓，跟生效的分开
            for (final z in _propList('zones')) ZoneShape.of(z, poly(z), proposed: true, wall: zoneWallPixels(z, px)),
          ];
          final draft = [for (final p in _draft) px(p.dx, p.dy)];
          final r = math.max(2.0, math.max(w, h) / 120);
          final areas = [
            for (final a in geo.areas) (a.name, [for (final p in a.points) px(p.dx, p.dy)]),
          ];
          final c = Nw.of(context);
          // 横屏：左边是图（能缩放），右边一栏是待命点、区域和比例。图按站点给的转角摆正：点图加顶点时
          // 点下去的位置 Flutter 按同一个变换反算回图的像素（GestureDetector 在转过的那一层里头）。
          // 打开时按有内容的范围放（台账 M7，[MapGeo.contentFit]：图按 k 缩放、往反方向挪，范围在正中；
          // 站点没给范围就是整张图放进来，跟原来的 FittedBox 一个样）。
          return Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Expanded(
              child: LayoutBuilder(builder: (context, box) {
                final (k, shift) = geo.contentFit(box.biggest);
                return InteractiveViewer(
                  transformationController: _zoom,
                  maxScale: 8,
                  child: OverflowBox(
                    maxWidth: double.infinity,
                    maxHeight: double.infinity,
                    child: Transform.translate(
                    offset: -shift,
                    child: Transform.scale(
                    scale: k,
                    child: NwMapRotation(
                      rad: rad,
                      size: Size(w, h),
                      child: GestureDetector(
                        key: SiteMapPreviewPage.canvasKey,
                        // 画区域：点一下加一个顶点（图的像素 → 地图米）
                        onTapUp: _drawing == null
                            ? null
                            : (d) => setState(() => _draft.add(geo.toMap(d.localPosition))),
                        child: SizedBox(
                          width: w,
                          height: h,
                          child: Stack(children: [
                            SizedBox(width: w, height: h, child: mapBaseImage(context, png, plan: plan)),
                            if (areas.isNotEmpty)
                              CustomPaint(
                                  key: SiteMapPreviewPage.areasKey,
                                  size: Size(w, h),
                                  painter: AreasPainter([for (final a in areas) a.$2],
                                      fill: mapAreaFill(c), stroke: mapAreaStroke(c), strokeWidth: r / 2)),
                            CustomPaint(
                                key: SiteMapPreviewPage.zonesKey,
                                size: Size(w, h),
                                painter: ZonesPainter(shapes, draft, radius: r, draftOpen: _drawing == 'wall')),
                            CustomPaint(
                                key: SiteMapPreviewPage.overlayKey,
                                size: Size(w, h),
                                painter: StandbyPainter(pixels, radius: r, homes: homePixels)),
                            if (widget.marks.isNotEmpty)
                              CustomPaint(
                                  key: SiteMapPreviewPage.marksKey,
                                  size: Size(w, h),
                                  painter: MarksPainter([
                                    for (final m in widget.marks) (px(m.x, m.y), m.color),
                                  ], radius: r * 2.5)),
                            if (areas.isNotEmpty)
                              Positioned.fill(
                                  child: MapAreaLabels(
                                      areas: areas,
                                      rad: rad,
                                      // 图被缩放了这么多（[MapGeo.contentFit]）
                                      unit: k,
                                      zoom: _zoom,
                                      labelKey: SiteMapPreviewPage.areaLabelKey)),
                          ]),
                        ),
                      ),
                    ),
                    ),
                    ),
                  ),
                );
              }),
            ),
            _sidePanel(context, [
                for (final m in widget.marks)
                  Text('${m.label}  (${m.x.toStringAsFixed(1)}, ${m.y.toStringAsFixed(1)})',
                      style: TextStyle(color: m.color, fontWeight: FontWeight.bold)),
                if (widget.marks.isNotEmpty) const Divider(),
                Text(tr(
                    '${mpp.toStringAsFixed(2)} m per pixel · image ${w.toInt()} × ${h.toInt()} px · '
                        'about ${(w * mpp).toStringAsFixed(0)} × ${(h * mpp).toStringAsFixed(0)} m',
                    '一像素 ${mpp.toStringAsFixed(2)} m · 图 ${w.toInt()} × ${h.toInt()} 像素 · '
                        '约 ${(w * mpp).toStringAsFixed(0)} × ${(h * mpp).toStringAsFixed(0)} m')),
                if (meta['source'] != null)
                  Text(tr('Source: ${meta['source']}', '用的是 ${meta['source']}'),
                      style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
                if (meta['warning'] != null)
                  Text('${meta['warning']}', style: TextStyle(color: Nw.of(context).warning)),
                // 台账 M9：淡淡的地面是站点画进底图的，这里说一句是什么
                if (geo.seen)
                  Text(
                      tr('Faint floor: seen through doors or glass, not mapped for the robot (it never goes there)',
                          '淡淡的地面：从门口、玻璃看到过，机器人没走过（机器人不去）'),
                      style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
                const Divider(),
                for (final p in homes)
                  Text(
                      tr(
                          'Origin · ${p['robot_id']} · ${p['name']}  '
                              '(${(p['x'] as num).toStringAsFixed(1)}, ${(p['y'] as num).toStringAsFixed(1)})',
                          '原点 · ${p['robot_id']} · ${p['name']}  '
                              '(${(p['x'] as num).toStringAsFixed(1)}, ${(p['y'] as num).toStringAsFixed(1)})'),
                      style: TextStyle(color: Nw.of(context).tertiary)),
                if (pts.isEmpty) Text(tr('No standby points on this map yet', '这张图上还没有登记待命点')),
                for (final p in pts)
                  Text('${p['robot_id']} · ${p['name']}${p['default'] == true ? tr(' (default)', '（默认）') : ''}  '
                      '(${(p['x'] as num).toStringAsFixed(1)}, ${(p['y'] as num).toStringAsFixed(1)})'),
                const Divider(),
                ..._zonePanel(zones),
            ]),
          ]);
        },
      ),
    );
  }

  /// 临时区到期的时刻（决策 66），按手机的钟显示：` · 到 10-14 18:30`。
  static String _untilText(Object? untilMs) {
    if (untilMs is! num) return '';
    final t = DateTime.fromMillisecondsSinceEpoch(untilMs.toInt());
    String two(int v) => v.toString().padLeft(2, '0');
    final s = '${two(t.month)}-${two(t.day)} ${two(t.hour)}:${two(t.minute)}';
    return tr(' · until $s', ' · 到 $s');
  }

  /// R2：新版本从上一版带过来、等人核的。横幅说清楚坐标可能不一样（坐标系参数不一样就明说）；每一项
  /// 一个勾（管理员才勾得动），勾上的在确认时才采纳；管理员也能整个不要。
  List<Widget> _proposalPanel(Map<String, dynamic> prop, bool admin) {
    final c = Nw.of(context);
    final from = '${prop['from_version']}';
    final same = (prop['frame'] as Map<String, dynamic>?)?['same'];
    Widget tick(String key, String kind, String id, String title) => CheckboxListTile(
        key: SiteMapPreviewPage.proposedKey(kind, id),
        dense: true,
        contentPadding: EdgeInsets.zero,
        value: _adopt.contains(key),
        onChanged: admin && !_busy
            ? (v) => setState(() => v == true ? _adopt.add(key) : _adopt.remove(key))
            : null,
        title: Text(title));
    return [
      NwNotice(
          key: SiteMapPreviewPage.proposalKey,
          tone: same == false ? NwTone.error : NwTone.warning,
          text: tr(
              'Copied from version $from: check each one against the new map; coordinates may differ. '
                  'Nothing here is in force until you tick it and confirm.',
              '从第 $from 版带过来的：每一个都要对着新图核，坐标可能不一样。勾上、确认之后才生效。')),
      if (same == false)
        Text(
            tr('The coordinate frame changed (frames.json differs): copied positions are probably wrong. '
                'Redraw instead.', '坐标系变了（frames.json 不一样）：带过来的位置很可能是错的，重画。'),
            style: NwText.bodyMedium.copyWith(color: c.error, fontWeight: FontWeight.bold)),
      if (same == null)
        Text(tr('Cannot tell whether the coordinate frame is the same.', '看不出坐标系是不是一样。'),
            style: NwText.bodySmall.copyWith(color: c.warning)),
      for (final z in _propList('zones')) tick('zone:${z['id']}', 'zone', '${z['id']}', zoneTitle(z, withId: false)),
      for (final p in _propList('standby'))
        tick('standby:${p['robot_id']}/${p['name']}', 'standby', '${p['robot_id']}/${p['name']}',
            tr('Standby point · ${p['robot_id']} · ${p['name']}', '待命点 · ${p['robot_id']} · ${p['name']}')),
      for (final p in _propList('intercepts'))
        tick('intercept:${p['name']}', 'intercept', '${p['name']}',
            tr('Intercept point · ${p['name']}', '拦截点 · ${p['name']}')),
      if (admin)
        Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
                key: SiteMapPreviewPage.discardProposalKey,
                onPressed: _busy ? null : _discardProposal,
                child: Text(tr('Drop all copied items', '带过来的都不要')))),
      const Divider(),
    ];
  }

  List<Widget> _zonePanel(List<Map<String, dynamic>> zones) {
    final admin = widget.api.session?.canManageMaps ?? false;
    if (_zonesError != null) {
      return [Text(tr('Zones unavailable: $_zonesError', '区域取不到：$_zonesError'))];
    }
    if (_zones == null) return [Text(tr('Loading zones…', '区域取的中…'))];
    final conf = _zones!['confirmed'] as Map<String, dynamic>?;
    final confirmed = conf != null && conf['current'] == true;
    final prop = _proposal;
    return [
      Text(tr('No-go / speed-limit zones · revision $_rev', '禁行区 / 限速区 · 第 $_rev 版'),
          style: const TextStyle(fontWeight: FontWeight.bold)),
      Text(
          confirmed
              ? switch (conf['how']) {
                  // 决策 66：只收紧的改动当场算确认了；临时区到期删掉也算（到期是确认时就定好的）
                  'tighten' => tr('Confirmed (${conf['by']}, only tightened)',
                      '已确认（${conf['by']}，只收紧）'),
                  'expiry' => tr('Confirmed (a temporary zone expired)', '已确认（临时区到期删了）'),
                  _ => tr('Confirmed (${conf['by']})', '已确认（${conf['by']}）'),
                }
              : conf == null
                  ? tr('Not confirmed: this map cannot be sent to robots', '还没确认：这张图下发不了')
                  : tr(
                      'Changed since it was confirmed (revision ${conf['revision']} was confirmed): '
                          'cannot be sent to robots',
                      '改过之后没重新确认（确认的是第 ${conf['revision']} 版）：下发不了'),
          style: TextStyle(color: confirmed ? Nw.of(context).text : Nw.of(context).warning)),
      if (prop != null) ..._proposalPanel(prop, admin),
      if (zones.isEmpty) Text(tr('No zones drawn', '没画区域')),
      for (final z in zones)
        Row(children: [
          Expanded(child: Text(zoneTitle(z) + _untilText(z['until_ms']))),
          if (admin && _drawing == null)
            IconButton(
                key: SiteMapPreviewPage.deleteKey('${z['id']}'),
                icon: const Icon(Icons.delete_outline),
                tooltip: tr('Delete (relaxes limits: applied only when the robot is idle)',
                    '删掉（放宽：机器人空闲时才换上）'),
                onPressed: _busy
                    ? null
                    : () {
                        final next = _rev + 1;
                        _save(
                            [for (final o in zones) if (o['id'] != z['id']) o],
                            (auto) => auto
                                ? tr('Deleted: revision $next (nothing was relaxed: stays confirmed)',
                                    '删了：第 $next 版（没放宽：不用重新确认）')
                                : tr('Deleted: revision $next (changes must be confirmed again)',
                                    '删了：第 $next 版（改过要重新确认）'));
                      }),
        ]),
      if (_msg != null) Text(_msg!),
      if (admin && _drawing == null) ...[
        Wrap(spacing: 4, children: [
          OutlinedButton(
              key: SiteMapPreviewPage.drawNogoKey,
              onPressed: _busy ? null : () => _startDrawing('nogo'),
              child: Text(tr('Draw no-go zone', '画禁行区'))),
          OutlinedButton(
              key: SiteMapPreviewPage.drawSlowKey,
              onPressed: _busy ? null : () => _startDrawing('slow'),
              child: Text(tr('Draw speed-limit zone', '画限速区'))),
          OutlinedButton(
              key: SiteMapPreviewPage.drawWallKey,
              onPressed: _busy ? null : () => _startDrawing('wall'),
              child: Text(tr('Draw virtual wall', '画虚拟墙'))),
          OutlinedButton(
              key: SiteMapPreviewPage.drawEraseKey,
              onPressed: _busy ? null : () => _startDrawing('erase'),
              child: Text(tr('Erase a region', '抹掉一块'))),
        ]),
        Text(
            tr('Adding or enlarging a zone, a lower limit or the hard mark takes effect at once. Deleting, '
                'shrinking or raising a limit must be confirmed again before robots are sent out.',
                '加禁行区、放大、调低限速、标硬禁区当场生效；删、缩、调高限速要重新确认才派机器人。'),
            style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
        Text(
            tr('Virtual wall: tap along the line (2 points or more). Robots do not cross it; it is stored as a '
                '0.3 m wide no-go strip and follows the same rules as a no-go zone.',
                '虚拟墙：沿着墙点（两个点以上）。机器人不会穿过；存成 0.3 米宽的禁行带，规则跟禁行区一样。'),
            style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
        Text(
            tr('Erase a region: robots never go there (like a no-go zone) and every map shows it as removed '
                '(junk, glass reflections, places robots must not go).',
                '抹掉一块：机器人永远不去（跟禁行区一样），所有地图上都显示成抹掉了（建图时的杂物、玻璃反射、机器人不该去的地方）。'),
            style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
        if (!confirmed || prop != null) ...[
          Text('${_zones!['confirm_text'] ?? ''}',
              style: NwText.bodySmall.copyWith(color: Nw.of(context).textSecondary)),
          FilledButton(
              key: SiteMapPreviewPage.confirmKey,
              onPressed: _busy ? null : _confirm,
              child: Text(prop == null
                  ? tr('Confirm revision $_rev', '确认第 $_rev 版')
                  : tr('Confirm revision $_rev with the ticked items', '确认第 $_rev 版（带勾上的）'))),
        ],
      ],
      if (_drawing != null) ...[
        Text(tr(
            _drawing == 'wall'
                ? 'Virtual wall: tap along the line, ${_draft.length} points so far'
                : '${switch (_drawing) { 'nogo' => 'No-go zone', 'erase' => 'Erase', _ => 'Speed-limit zone' }}: '
                    'tap the map to add corners, ${_draft.length} so far',
            _drawing == 'wall'
                ? '虚拟墙：沿着墙点，已 ${_draft.length} 个'
                : '${switch (_drawing) { 'nogo' => '禁行区', 'erase' => '抹掉', _ => '限速区' }}：点图加顶点，已 ${_draft.length} 个')),
        if (_drawing == 'nogo' || _drawing == 'erase' || _drawing == 'wall')
          CheckboxListTile(
              key: SiteMapPreviewPage.hardKey,
              dense: true,
              contentPadding: EdgeInsets.zero,
              value: _hard,
              onChanged: (v) => setState(() => _hard = v ?? false),
              title: Text(tr('Hard no-go (water, drops): remote control also keeps out',
                  '硬禁区（水体、落差）：遥控也开不进去'))),
        Row(children: [
          Text(tr('Expires: ', '到期：'), style: NwText.bodySmall),
          Expanded(
            child: DropdownButton<int?>(
                key: SiteMapPreviewPage.untilKey,
                isExpanded: true,
                value: _forS,
                onChanged: (s) => setState(() => _forS = s),
                items: [
                  for (final (s, label) in [
                    (null, tr('Never', '一直有效')),
                    (3600, tr('In 1 h', '1 小时后')),
                    (8 * 3600, tr('In 8 h', '8 小时后')),
                    (86400, tr('In 1 day', '1 天后')),
                    (7 * 86400, tr('In 7 days', '7 天后')),
                  ])
                    DropdownMenuItem<int?>(
                        key: SiteMapPreviewPage.forKey(s), value: s, child: Text(label)),
                ]),
          ),
        ]),
        if (_drawing == 'slow')
          Row(children: [
            Text(tr('Limit ${_speed.toStringAsFixed(1)} m/s', '限速 ${_speed.toStringAsFixed(1)} m/s')),
            Expanded(
                child: Slider(
                    value: _speed,
                    min: 0.1,
                    max: 1.0,
                    divisions: 9,
                    onChanged: (v) => setState(() => _speed = v))),
          ]),
        Wrap(spacing: 4, children: [
          FilledButton(
              key: SiteMapPreviewPage.finishKey,
              onPressed: _draft.length >= (_drawing == 'wall' ? 2 : 3) && !_busy ? _finish : null,
              child: Text(tr('Finish zone', '完成'))),
          OutlinedButton(
              key: SiteMapPreviewPage.undoKey,
              onPressed: _draft.isEmpty ? null : () => setState(_draft.removeLast),
              child: Text(tr('Undo point', '撤销一点'))),
          TextButton(
              key: SiteMapPreviewPage.cancelKey,
              onPressed: () => setState(() {
                    _drawing = null;
                    _draft.clear();
                  }),
              child: Text(tr('Cancel', '不画了'))),
        ]),
      ],
    ];
  }
}

/// 一个区域在图上的样子：禁行（红）还是限速（黄），顶点（图的像素坐标）；硬禁区（决策 65）更深、
/// 边更粗；从上一版带过来、等人核的（R2）只画轮廓；抹掉区（台账 44，[erase]）只描一道细边 —— 底图那一块
/// 站点已经抹了。
class ZoneShape {
  const ZoneShape(this.nogo, this.pixels,
      {this.hard = false, this.proposed = false, this.erase = false, this.ignore = false, this.wall});

  /// 按站点给的区域（`kind`、`hard`）；[wall]：虚拟墙画的那条线（图的像素，[zoneWallPixels]）。
  factory ZoneShape.of(Map<String, dynamic> z, List<Offset> pixels, {bool proposed = false, List<Offset>? wall}) =>
      ZoneShape(z['kind'] == 'nogo', pixels,
          hard: z['hard'] == true,
          proposed: proposed,
          erase: z['kind'] == 'erase',
          ignore: z['kind'] == 'ignore',
          wall: wall);
  final bool nogo;
  final List<Offset> pixels;
  final bool hard;
  final bool proposed;
  final bool erase;
  /// 人员检测忽略区（W41）：不管狗怎么走，画成靛蓝色的框。
  final bool ignore;
  /// 虚拟墙（2026-10-15）：画的那条线；画成粗线，不画站点算的那条窄带子。
  final List<Offset>? wall;
}

/// 虚拟墙的线（地图米 → 图的像素）；不是虚拟墙、线不成形是 null。
List<Offset>? zoneWallPixels(Map<String, dynamic> z, Offset Function(double, double) px) {
  final raw = z['wall'];
  if (raw is! List || raw.length < 2) return null;
  final out = <Offset>[];
  for (final p in raw) {
    if (p is! List || p.length < 2 || p[0] is! num || p[1] is! num) return null;
    out.add(px((p[0] as num).toDouble(), (p[1] as num).toDouble()));
  }
  return out;
}

/// 区域在列表、勾选里怎么写（种类 + 名字 + id）：禁行、硬禁区、抹掉（台账 44）、限速。
String zoneTitle(Map<String, dynamic> z, {bool withId = true}) {
  final idEn = withId ? ' (${z['id']})' : '', idZh = withId ? '\uFF08${z['id']}\uFF09' : '';
  final label = z['label'] ?? '';
  if (z['kind'] == 'nogo' && z['wall'] is List) {
    return z['hard'] == true
        ? tr('Hard virtual wall · ${zoneLabelText(label)}$idEn', '硬虚拟墙 · $label$idZh')
        : tr('Virtual wall · ${zoneLabelText(label)}$idEn', '虚拟墙 · $label$idZh');
  }
  return switch (z['kind']) {
    'nogo' => z['hard'] == true
        ? tr('Hard no-go · ${zoneLabelText(label)}$idEn', '硬禁区 · $label$idZh')
        : tr('No-go · ${zoneLabelText(label)}$idEn', '禁行 · $label$idZh'),
    'erase' => z['hard'] == true
        ? tr('Erased, hard · ${zoneLabelText(label)}$idEn', '抹掉（硬） · $label$idZh')
        : tr('Erased · ${zoneLabelText(label)}$idEn', '抹掉 · $label$idZh'),
    'ignore' => tr('Person-detection ignored · ${zoneLabelText(label)}$idEn', '人员忽略区 · $label$idZh'),
    _ => tr('Limit ${z['max_speed_mps']} m/s · ${zoneLabelText(label)}$idEn',
        '限速 ${z['max_speed_mps']} m/s · $label$idZh'),
  };
}

/// 画区域（W10）：禁行红、限速黄，半透明；硬禁区深红、粗边；带过来的只有灰蓝色的轮廓；正在画的那一个
/// 用虚一点的蓝线连着顶点。
class ZonesPainter extends CustomPainter {
  ZonesPainter(this.shapes, this.draft, {this.radius = 3, this.draftOpen = false});
  final List<ZoneShape> shapes;
  final List<Offset> draft;
  final double radius;
  /// 正在画的是虚拟墙：连成线、不闭合。
  final bool draftOpen;

  @override
  void paint(Canvas canvas, Size size) {
    for (final z in shapes) {
      final w = z.wall;
      if (w != null && w.length >= 2) {
        // 虚拟墙：粗线（硬的更深更粗）；带过来、等人核的灰蓝
        final line = Path()..addPolygon(w, false);
        canvas.drawPath(
            line,
            Paint()
              ..color = z.proposed ? Colors.blueGrey : (z.hard ? Colors.red.shade900 : Colors.red)
              ..style = PaintingStyle.stroke
              ..strokeCap = StrokeCap.round
              ..strokeJoin = StrokeJoin.round
              ..strokeWidth = z.hard ? radius * 1.4 : radius);
        continue;
      }
      if (z.pixels.length < 3) continue;
      final path = Path()..addPolygon(z.pixels, true);
      if (z.erase && !z.proposed) {
        canvas.drawPath(
            path,
            Paint()
              ..color = Colors.blueGrey.withValues(alpha: 0.8)
              ..style = PaintingStyle.stroke
              ..strokeWidth = z.hard ? radius / 2 : radius / 4);
        continue;
      }
      if (z.proposed) {
        canvas.drawPath(
            path,
            Paint()
              ..color = Colors.blueGrey
              ..style = PaintingStyle.stroke
              ..strokeWidth = radius / 2);
        continue;
      }
      final c = z.ignore ? Colors.indigo : z.nogo ? (z.hard ? Colors.red.shade900 : Colors.red) : Colors.amber;
      canvas.drawPath(path, Paint()..color = c.withValues(alpha: z.hard ? 0.55 : 0.35));
      canvas.drawPath(
          path,
          Paint()
            ..color = c
            ..style = PaintingStyle.stroke
            ..strokeWidth = z.hard ? radius : radius / 2);
    }
    if (draft.isNotEmpty) {
      final line = Paint()
        ..color = Colors.blue
        ..style = PaintingStyle.stroke
        ..strokeWidth = radius / 2;
      canvas.drawPath(Path()..addPolygon(draft, !draftOpen && draft.length >= 3), line);
      for (final p in draft) {
        canvas.drawCircle(p, radius / 1.5, Paint()..color = Colors.blue);
      }
    }
  }

  @override
  bool shouldRepaint(ZonesPainter old) => true;
}

/// 在图上画待命点（图的像素坐标）。
/// 告警现场的点（W17）：粗圈，圈心一个实点，远看也找得到。
class MarksPainter extends CustomPainter {
  MarksPainter(this.marks, {this.radius = 8});
  final List<(Offset, Color)> marks;
  final double radius;

  @override
  void paint(Canvas canvas, Size size) {
    for (final (p, c) in marks) {
      canvas.drawCircle(
          p,
          radius,
          Paint()
            ..color = c
            ..style = PaintingStyle.stroke
            ..strokeWidth = radius / 3);
      canvas.drawCircle(p, radius / 3, Paint()..color = c);
    }
  }

  @override
  bool shouldRepaint(MarksPainter old) => old.marks != marks;
}

class StandbyPainter extends CustomPainter {
  StandbyPainter(this.pixels, {this.radius = 3, this.homes = const []});
  final List<Offset> pixels;
  final double radius;
  /// 原点（W13a）：蓝方块。
  final List<Offset> homes;

  @override
  void paint(Canvas canvas, Size size) {
    for (final p in homes) {
      final r = Rect.fromCenter(center: p, width: radius * 2.4, height: radius * 2.4);
      canvas.drawRect(r, Paint()..color = Colors.blue);
      canvas.drawRect(
          r,
          Paint()
            ..color = Colors.black
            ..style = PaintingStyle.stroke
            ..strokeWidth = radius / 3);
    }
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
  bool shouldRepaint(StandbyPainter old) => old.pixels != pixels || old.homes != homes;
}
