/// 建图时看得见（W00c6h）。老服务在线建图、推「正在长的那张图」；W08 决定 4 不再在线跑 2D 建图，改成两样：
///
/// - **录包轨迹**（`SiteMappingTrailPage`，管理员）：录包期间每 2 秒问一次狗走过的点（只要新增的），画出来 ——
///   哪儿走过了、哪儿还没去。坐标是狗的里程（录包起点为原点），不是地图坐标：新地方还没有图。狗说停了就不再问，
///   点刷新再看一次。
/// - **图的预览**（`SiteMapPreviewPage`，谁都能看）：站点把建好的栅格图渲染成 PNG，这里能缩放，画上这张图上
///   登记的待命点 —— 建出来的图对不对、待命点落在哪。
library;

import 'dart:async';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../net/site_client.dart';

class SiteMappingTrailPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  final Duration period;
  const SiteMappingTrailPage(
      {super.key, required this.api, required this.robotId, this.period = const Duration(seconds: 2)});

  static const Key canvasKey = Key('trail-canvas');
  static const Key refreshKey = Key('trail-refresh');

  @override
  State<SiteMappingTrailPage> createState() => _SiteMappingTrailPageState();
}

class _SiteMappingTrailPageState extends State<SiteMappingTrailPage> {
  final List<Offset> _pts = <Offset>[];
  bool _recording = true;
  bool _full = false;
  bool _asked = false;
  bool _busy = false;
  String _err = '';
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    unawaited(_poll());
    _timer = Timer.periodic(widget.period, (_) {
      if (_recording) unawaited(_poll());
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _poll() async {
    if (_busy) return;
    _busy = true;
    try {
      var d = await widget.api.mappingTrail(widget.robotId, since: _pts.length);
      if (((d['total'] as num?) ?? 0) < _pts.length) {
        // 狗上重新开录了（清空过）：从头取。
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
        _recording = d['recording'] == true;
        _full = d['full'] == true;
        _asked = true;
        _err = '';
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = '取不到轨迹：$e');
    } finally {
      _busy = false;
    }
  }

  String _status() {
    if (!_asked) return _err.isNotEmpty ? _err : '正在问狗……';
    final n = '已记 ${_pts.length} 个点';
    final span = TrailPainter.span(_pts);
    final size = span == null
        ? ''
        : '，走过的范围约 ${span.width.toStringAsFixed(0)} × ${span.height.toStringAsFixed(0)} m';
    final head = _recording ? '录包中：$n$size（每 ${widget.period.inSeconds} 秒更新）' : '没在录包（下面是最后一次录的轨迹）：$n$size';
    return [head, if (_full) '点数到上限了，后面的不再记', if (_err.isNotEmpty) _err].join('\n');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('${widget.robotId} 录包轨迹'), actions: [
        IconButton(
            key: SiteMappingTrailPage.refreshKey,
            tooltip: '刷新',
            icon: const Icon(Icons.refresh),
            onPressed: () {
              setState(() => _recording = true); // 再看一次；狗说还在录就接着每 2 秒问
              unawaited(_poll());
            }),
      ]),
      body: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Padding(padding: const EdgeInsets.fromLTRB(12, 8, 12, 4), child: Text(_status())),
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
      ]),
    );
  }
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
  bool shouldRepaint(TrailPainter old) => old.points.length != points.length;
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
