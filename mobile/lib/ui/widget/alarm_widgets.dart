/// 报警的几样共用小件（App V2，视觉规范 2.1 / 2.5）：优先级三重编码（颜色 + 形状 + 文字）、全部急停。
library;

import 'package:flutter/material.dart';

import '../../design/tokens.g.dart';
import '../../l10n.dart';
import '../../net/site_client.dart';
import '../theme.dart';

/// 优先级的形状：P1 实心菱形、P2 实心三角、别的空心圆。颜色由调用方给（实底上是白的）。
class PriorityShape extends StatelessWidget {
  final String level;
  final Color color;
  final double size;
  const PriorityShape({super.key, required this.level, required this.color, this.size = 14});

  @override
  Widget build(BuildContext context) =>
      CustomPaint(size: Size.square(size), painter: _ShapePainter(level, color));
}

class _ShapePainter extends CustomPainter {
  final String level;
  final Color color;
  const _ShapePainter(this.level, this.color);

  @override
  void paint(Canvas canvas, Size s) {
    final w = s.width, h = s.height;
    final fill = Paint()..color = color;
    if (level == 'P1') {
      canvas.drawPath(
          Path()
            ..moveTo(w / 2, 0)
            ..lineTo(w, h / 2)
            ..lineTo(w / 2, h)
            ..lineTo(0, h / 2)
            ..close(),
          fill);
    } else if (level == 'P2') {
      canvas.drawPath(
          Path()
            ..moveTo(w / 2, h * 0.08)
            ..lineTo(w, h * 0.92)
            ..lineTo(0, h * 0.92)
            ..close(),
          fill);
    } else {
      canvas.drawCircle(
          Offset(w / 2, h / 2),
          w / 2 - 1,
          Paint()
            ..color = color
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.5);
    }
  }

  @override
  bool shouldRepaint(_ShapePainter old) => old.level != level || old.color != color;
}

/// 优先级的字色：P1 红、P2 琥珀、别的中性。
Color priorityColor(String level) => switch (level) {
      'P1' => D1Color.p1Text,
      'P2' => D1Color.p2,
      _ => D1Color.textMuted,
    };

/// 「全部急停」：整个界面唯一的红色按钮（规范 2.1），红底 + 2px 报警框。按了先问一遍，再对每台在线的狗
/// 发急停；哪台没停住说出来（叫人去按它身上的急停）。
class StopAllButton extends StatelessWidget {
  final SiteApi api;

  /// 结果说给谁（页面自己摆，或者弹个提示）。
  final void Function(String message) onResult;
  const StopAllButton({super.key, required this.api, required this.onResult});

  static const Key buttonKey = Key('stop-all');
  static const Key confirmKey = Key('stop-all-go');

  Future<void> _press(BuildContext context) async {
    final List<Map<String, dynamic>> robots;
    try {
      // 在线 = 狗报在线、并且站点最近收到过它的消息（跟狗列表、网页值班台同一个判据）
      robots = (await api.robots())
          .where((r) => r['fresh'] == true && r['status'] is Map && (r['status'] as Map)['online'] == true)
          .toList();
    } on SiteError catch (e) {
      onResult(tr("Can't reach the site: $e. Use the E-stop on each robot.", '连不上站点：$e。去按每台狗身上的急停。'));
      return;
    }
    if (!context.mounted) return;
    if (robots.isEmpty) {
      onResult(tr('No robots are online.', '没有在线的狗。'));
      return;
    }
    final n = robots.length;
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              content: Text(tr(
                  'Stop all $n robots now? Each one halts where it is until you resume it.',
                  '现在让 $n 台狗全部急停？每台就地停住，直到你恢复它。')),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
                FilledButton(
                    key: confirmKey,
                    style: _stopStyle,
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('Stop all', '全部急停'))),
              ],
            ));
    if (ok != true) return;
    final bad = <String>[];
    await Future.wait(robots.map((r) async {
      final id = '${r['robot_id']}';
      try {
        await api.halt(id);
      } on SiteError catch (e) {
        bad.add(tr("$id didn't stop: $e. Use the E-stop on the robot.", '$id 没停住：$e。去按它身上的急停。'));
      }
    }));
    onResult(bad.isEmpty ? tr('Stopped $n robots.', '$n 台狗都停了。') : bad.join(' '));
  }

  static final ButtonStyle _stopStyle = FilledButton.styleFrom(
      backgroundColor: D1Color.p1,
      foregroundColor: D1Color.onSolid,
      side: const BorderSide(color: D1Color.p1Frame, width: D1Size.stopAllRing),
      textStyle: D1Text.label);

  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.symmetric(horizontal: 8),
      child: FilledButton.icon(
          key: buttonKey,
          style: _stopStyle,
          onPressed: () => _press(context),
          icon: const Icon(Icons.report, size: 18),
          label: Text(tr('Stop all', '全部急停'))));
}
