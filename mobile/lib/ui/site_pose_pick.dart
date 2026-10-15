/// 地图栏上「点一下 → 去这里 / 在这里设位置」（2026-10-14，能力台账行 24、13）。
///
/// 点图选一个位置，拖圆形把手（或按 ±15°）定朝向；「去这里」走已有的 goto 接口（站点拒了就把原因
/// 原样给人看，比如监护模式要手机的租约），「在这里设位置」走 relocalize（先弹确认，写着 x、y、朝向）。
/// 发出去的就是点的那个地图坐标（米）和朝向（弧度），不做别的换算 —— 点下去的位置 Flutter 已经按图的
/// 转角、缩放反着算回图的像素了（跟画区域一个办法，[MapGeo.toMap]）。
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'map_view.dart';
import 'nw/nw_button.dart';
import 'nw/nw_theme.dart';

/// 选出来的位置（地图米、朝向弧度，逆时针）。
class PosePickController extends ChangeNotifier {
  Offset? at;
  double yaw = 0;

  bool get has => at != null;

  void pick(Offset metres) {
    at = Offset(_r2(metres.dx), _r2(metres.dy));
    notifyListeners();
  }

  /// 朝向转到面向地图上的 [metres] 这一点；跟自己重合就不动。
  void faceToward(Offset metres) {
    final p = at;
    if (p == null) return;
    final d = metres - p;
    if (d.distance < 1e-6) return;
    yaw = math.atan2(d.dy, d.dx);
    notifyListeners();
  }

  void turn(double deltaRad) {
    if (at == null) return;
    yaw = math.atan2(math.sin(yaw + deltaRad), math.cos(yaw + deltaRad));
    notifyListeners();
  }

  void clear() {
    at = null;
    notifyListeners();
  }

  static double _r2(double v) => (v * 100).roundToDouble() / 100;
}

String yawDegText(double yaw) => (yaw * 180 / math.pi).toStringAsFixed(0);

/// 铺满图（转过之前的那一层）的点击层 + 选出来的位置的图钉和朝向把手。放在图那个 `Stack` 里，
/// 尺寸 = 图在屏上的大小；[k]：图的一个像素画成多大。
class PoseLayer extends StatelessWidget {
  final PosePickController controller;
  final MapGeo geo;
  final double k;
  const PoseLayer({super.key, required this.controller, required this.geo, required this.k});

  static const Key pinKey = Key('pose-pin');
  static const Key handleKey = Key('pose-handle');
  static const double _arm = 30;

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return ListenableBuilder(
        listenable: controller,
        builder: (context, _) {
          final at = controller.at;
          return Stack(clipBehavior: Clip.none, children: [
            Positioned.fill(
                child: GestureDetector(
                    behavior: HitTestBehavior.translucent,
                    onTapUp: (d) => controller.pick(geo.toMap(d.localPosition / k)))),
            if (at != null) ..._pin(context, c, at),
          ]);
        });
  }

  List<Widget> _pin(BuildContext context, NwColors c, Offset at) {
    final px = geo.toPx(at.dx, at.dy) * k;
    final tip = px + Offset(math.cos(controller.yaw), -math.sin(controller.yaw)) * _arm;
    return [
      Positioned.fill(
          child: IgnorePointer(
              child: CustomPaint(painter: _ArmPainter(px, tip, c.primary)))),
      Positioned(
          left: px.dx - 9,
          top: px.dy - 9,
          child: IgnorePointer(
              child: Container(
                  key: pinKey,
                  width: 18,
                  height: 18,
                  decoration: BoxDecoration(
                      color: c.surface, shape: BoxShape.circle, border: Border.all(color: c.primary, width: 3))))),
      Positioned(
          left: tip.dx - 16,
          top: tip.dy - 16,
          child: GestureDetector(
              key: handleKey,
              behavior: HitTestBehavior.opaque,
              // 手指的位置先倒回图（转角、缩放都算进去）再换成地图米，朝向 = 从图钉看过去的方向
              onPanUpdate: (d) {
                final box = context.findRenderObject() as RenderBox;
                controller.faceToward(geo.toMap(box.globalToLocal(d.globalPosition) / k));
              },
              child: SizedBox(
                  width: 32,
                  height: 32,
                  child: Center(
                      child: Container(
                          width: 14,
                          height: 14,
                          decoration: BoxDecoration(
                              color: c.surface, shape: BoxShape.circle, border: Border.all(color: c.text, width: 2))))))),
    ];
  }
}

class _ArmPainter extends CustomPainter {
  final Offset from;
  final Offset to;
  final Color color;
  const _ArmPainter(this.from, this.to, this.color);

  @override
  void paint(Canvas canvas, Size size) => canvas.drawLine(from, to, Paint()
    ..color = color
    ..strokeWidth = 3
    ..strokeCap = StrokeCap.round);

  @override
  bool shouldRepaint(_ArmPainter old) => old.from != from || old.to != to || old.color != color;
}

/// 图下面那一条：选出来的位置、哪只狗、去这里 / 设位置。[robots]：这张图上的狗（站点的狗视图）。
/// 没选位置时只有一句提示。
class PoseBar extends StatefulWidget {
  final SiteApi api;
  final PosePickController controller;
  final String mapId;
  final String version;
  final List<Map<String, dynamic>> robots;
  const PoseBar(
      {super.key,
      required this.api,
      required this.controller,
      required this.mapId,
      required this.version,
      required this.robots});

  static const Key barKey = Key('pose-bar');
  static const Key goKey = Key('pose-go');
  static const Key setKey = Key('pose-set');
  static const Key setOkKey = Key('pose-set-ok');
  static const Key clearKey = Key('pose-clear');
  static const Key turnLeftKey = Key('pose-turn-left');
  static const Key turnRightKey = Key('pose-turn-right');
  static const Key robotKey = Key('pose-robot');
  static const Key textKey = Key('pose-text');
  static const Key messageKey = Key('pose-message');

  @override
  State<PoseBar> createState() => _PoseBarState();
}

class _PoseBarState extends State<PoseBar> {
  String? _robot;
  String? _msg;
  bool _busy = false;
  /// 上下文（图、选的狗）每变一次加一：变之前发出去的请求，回来时不说成现在这只狗、这张图的（外审 W39-I2）。
  int _gen = 0;

  @override
  void didUpdateWidget(PoseBar old) {
    super.didUpdateWidget(old);
    if (old.mapId != widget.mapId || old.version != widget.version) {
      _gen++;
      _msg = null;
    }
  }

  void _pickRobot(String? v) => setState(() {
        if (v != _robot) {
          _gen++;
          _msg = null;
        }
        _robot = v;
      });

  List<String> get _ids => [for (final r in widget.robots) if (robotCan(r, 'goto')) '${r['robot_id']}'];

  String? get _selected {
    final ids = _ids;
    if (_robot != null && ids.contains(_robot)) return _robot;
    return ids.isEmpty ? null : ids.first;
  }

  Future<void> _run(Future<Map<String, dynamic>> Function() call, String ok) async {
    final mine = _gen;
    setState(() {
      _busy = true;
      _msg = null;
    });
    String msg;
    try {
      final r = await call();
      final ack = r['ack'];
      final result = ack is Map ? ack['result'] : null;
      // 只有狗收下了才算（外审 W39-I3：没回执、被拒、过期、重复都不算）
      msg = result == 'accepted'
          ? ok
          : tr('The robot did not accept: $result ${ack is Map ? (ack['reason'] ?? '') : ''}',
              '狗没接：$result ${ack is Map ? (ack['reason'] ?? '') : ''}');
    } on SiteError catch (e) {
      // 站点拒了：原因原样给人看（监护模式要租约、没就绪、点不可达……）
      msg = tr('Refused: ${e.message}', '被拒：${e.message}');
    }
    if (mounted) {
      setState(() {
        _busy = false;
        if (mine == _gen) _msg = msg;           // 上下文变了：不把结果说成现在这只狗、这张图的
      });
    }
  }

  Future<void> _go() async {
    final id = _selected, at = widget.controller.at;
    if (id == null || at == null) return;
    await _run(
        () => widget.api.goto(id,
            mapId: widget.mapId, version: widget.version, x: at.dx, y: at.dy, yaw: widget.controller.yaw),
        tr('Go-to sent to $id', '已让 $id 去那里'));
  }

  Future<void> _set() async {
    final id = _selected, at = widget.controller.at;
    if (id == null || at == null) return;
    final yaw = widget.controller.yaw;
    final mapId = widget.mapId, version = widget.version, mine = _gen;
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              title: Text(tr('Set the position of $id here?', '把 $id 的位置设在这里？')),
              content: Text(tr(
                  'Tell $id it is standing at x ${at.dx} m, y ${at.dy} m, heading ${yawDegText(yaw)}° on this map. '
                      'If that is wrong the robot will navigate from the wrong place.',
                  '告诉 $id：它现在站在这张图上 x ${at.dx} 米、y ${at.dy} 米、朝向 ${yawDegText(yaw)}°。'
                      '设错了狗会按错的位置走。')),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
                FilledButton(
                    key: PoseBar.setOkKey,
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('Set position', '设位置'))),
              ],
            ));
    if (ok != true || !mounted) return;
    // 确认框开着的时候图、狗、点换了：不发（外审 W39-I2）。换了图的狗由站点按带上的图号拒
    if (mine != _gen || _selected != id || widget.controller.at != at || widget.controller.yaw != yaw) {
      setState(() => _msg = tr('The map, robot or spot changed — choose again.', '图、狗或位置变了：请重新选。'));
      return;
    }
    await _run(() => widget.api.relocalize(id, x: at.dx, y: at.dy, yaw: yaw, mapId: mapId, version: version),
        tr('Position set for $id', '已设 $id 的位置'));
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return ListenableBuilder(
        listenable: widget.controller,
        builder: (context, _) {
          final at = widget.controller.at;
          final ids = _ids;
          final sel = _selected;
          return Material(
              key: PoseBar.barKey,
              color: c.surface,
              child: Container(
                  decoration: BoxDecoration(border: Border(top: BorderSide(color: c.outline))),
                  padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                    if (at == null)
                      Text(tr('Tap the map to choose a spot; drag the round handle to set the heading.',
                          '点地图选一个位置；拖圆形把手定朝向。'),
                          style: NwText.bodySmall.copyWith(color: c.textSecondary))
                    else ...[
                      Text(
                          key: PoseBar.textKey,
                          tr('x ${at.dx} m, y ${at.dy} m, heading ${yawDegText(widget.controller.yaw)}°',
                              'x ${at.dx} 米，y ${at.dy} 米，朝向 ${yawDegText(widget.controller.yaw)}°'),
                          style: NwText.labelMedium.copyWith(color: c.text)),
                      const SizedBox(height: Nw.s4),
                      Wrap(spacing: Nw.s8, runSpacing: Nw.s4, crossAxisAlignment: WrapCrossAlignment.center, children: [
                        if (ids.length > 1)
                          DropdownButton<String>(
                              key: PoseBar.robotKey,
                              value: sel,
                              items: [for (final id in ids) DropdownMenuItem(value: id, child: Text(id))],
                              onChanged: _pickRobot)
                        else if (sel != null)
                          Text(sel, style: NwText.labelMedium.copyWith(color: c.text)),
                        IconButton(
                            key: PoseBar.turnLeftKey,
                            tooltip: tr('Turn 15° left', '左转 15°'),
                            icon: const Icon(Icons.rotate_left_rounded),
                            onPressed: () => widget.controller.turn(math.pi / 12)),
                        IconButton(
                            key: PoseBar.turnRightKey,
                            tooltip: tr('Turn 15° right', '右转 15°'),
                            icon: const Icon(Icons.rotate_right_rounded),
                            onPressed: () => widget.controller.turn(-math.pi / 12)),
                        NwButton(
                            buttonKey: PoseBar.goKey,
                            label: tr('Go here', '去这里'),
                            icon: Icons.near_me_rounded,
                            size: NwButtonSize.small,
                            onPressed: sel == null || _busy ? null : _go),
                        NwButton(
                            buttonKey: PoseBar.setKey,
                            label: tr('Set position here', '在这里设位置'),
                            variant: NwButtonVariant.outline,
                            size: NwButtonSize.small,
                            onPressed: sel == null || _busy ? null : _set),
                        NwButton(
                            buttonKey: PoseBar.clearKey,
                            label: tr('Clear', '清除'),
                            variant: NwButtonVariant.ghost,
                            size: NwButtonSize.small,
                            onPressed: () {
                              widget.controller.clear();
                              setState(() => _msg = null);
                            }),
                      ]),
                      if (sel == null)
                        Text(tr('No robot on this map can be sent.', '这张图上没有能派的狗。'),
                            style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                    ],
                    if (_msg != null)
                      Padding(
                          padding: const EdgeInsets.only(top: Nw.s4),
                          child: Text(_msg!, key: PoseBar.messageKey, style: NwText.bodySmall.copyWith(color: c.text))),
                  ])));
        });
  }
}
