/// 虚拟圆心摇杆。**按下的那一点就是圆心。**
///
/// 不画死一个圈：戴着手套、在阳光下、眼睛盯着狗不盯着屏幕 —— 这三种情况下
/// 人按不准一个画死的圈。§7.7。
///
/// **用 `Listener` 而不是 `GestureDetector`。** 手势识别器要等手指走过
/// `kTouchSlop` 才认这是一次拖动，在那之前 `onPanStart` 不响 —— 于是「按下
/// 去的那一刻输出是零」这件事没人说得出口，而圆心也只能定在**走过 slop 之后**
/// 的那一点上，不是人真正按下的那一点。这一层要的恰恰是原始的按下/移动/抬起。
library;

import 'package:flutter/material.dart';

/// 这根杆管哪个轴。
enum JoystickAxis {
  /// 左杆：平移。**吸附到单轴** —— 真机遥控器推 45° 也只走一个轴。
  ///
  /// 后端（`app/teleop.py` 的 `snap_to_axis`）已经吸了一次。这里再吸不是
  /// 重复：那一次保证**狗**只走一个轴，这一次保证**屏幕上画的那根杆**跟狗
  /// 实际要走的方向一致。不吸的话人斜推着、杆画在斜的位置、狗直着走。
  translate,

  /// 右杆：只转向。推上去要转弯是学不会的。
  turn,
}

/// 一根虚拟圆心摇杆。
///
/// [onChanged] 吐的是一个归一化的向量，**`dx` 向右为正、`dy` 向上为正**。
/// 「向上为正」是这个 widget 对外的约定，机器人那三个轴（fwd/lat/yaw）的换算
/// 归调用方（`ui/teleop_page.dart`）——这一层不认识狗。
class Joystick extends StatefulWidget {
  const Joystick({
    super.key,
    required this.axis,
    required this.onChanged,
    this.enabled = true,
  });

  final JoystickAxis axis;

  /// 值变了就叫一次。**按下（零）和松手（零）也会叫。**
  final ValueChanged<Offset> onChanged;

  /// 假的时候整根杆点不动，而且画成半透明灰。
  ///
  /// §5.9 是无豁免的硬规则：看不见就不许开狗。**不是「点了不算」**——
  /// 见 [knobKey] 那段。
  final bool enabled;

  /// 杆头那个圈的 key。
  ///
  /// **测试要盯的是「屏幕上有没有一根跟着手指走的杆」，不只是回调。**
  /// 只在 `onChanged` 里 return 的实现回调一个都不发，可杆还在跟着手指走 ——
  /// 屏幕上那根杆是现场唯一的反馈，人看着它动就以为狗在走，于是不去看狗、
  /// 也不去修画面，而实际上一拍都没发出去。
  static const Key knobKey = ValueKey<String>('joystick-knob');

  /// 推到底是多少逻辑像素。手指离圆心这么远就是满量程。
  static const double radius = 72.0;

  /// 杆头那个圈的半径。
  static const double knobRadius = 26.0;

  @override
  State<Joystick> createState() => _JoystickState();
}

class _JoystickState extends State<Joystick> {
  /// 按下的那一点。没人按着就是 null —— 也就是屏幕上什么杆都没有。
  Offset? _center;

  /// 现在这根杆指到哪儿（归一化，向上为正）。
  Offset _value = Offset.zero;

  /// 认哪一根手指。
  ///
  /// 两根杆是同时推的（§7.7 允许左前推 + 右转向），第二根手指落在**另一个**
  /// widget 上，但同一个 widget 上也可能落进第二根手指（手滑、误触）——
  /// 不认号的话后落的那根会把圆心挪走，人手里的杆会突然跳一下。
  int? _pointer;

  void _down(PointerDownEvent e) {
    if (_pointer != null) return; // 已经有一根在推了，后来的不管
    _pointer = e.pointer;
    setState(() {
      _center = e.localPosition;
      _value = Offset.zero;
    });
    // 刚按下还没动，输出是零。**这一下必须发**：它是「圆心就在这儿」的
    // 宣告，也让上层知道这根杆开始被人握着了。
    _emit(Offset.zero);
  }

  void _move(PointerMoveEvent e) {
    final Offset? c = _center;
    if (c == null || e.pointer != _pointer) return;
    final Offset v = _valueFor(e.localPosition - c);
    if (v == _value) return;
    setState(() => _value = v);
    _emit(v);
  }

  void _release(PointerEvent e) {
    if (e.pointer != _pointer) return;
    _pointer = null;
    setState(() {
      _center = null;
      _value = Offset.zero;
    });
    // **松手要主动吐一个零。** 守死人（0.6 秒）是兜底，不是停车方式：
    // 靠它停意味着松手之后狗还会往前走大半秒 —— 那半秒足够撞上东西。
    _emit(Offset.zero);
  }

  void _emit(Offset v) => widget.onChanged(v);

  /// 手指相对圆心的位移 -> 归一化的控制量。
  Offset _valueFor(Offset delta) {
    final double x = (delta.dx / Joystick.radius).clamp(-1.0, 1.0);
    // **y 轴在这儿翻号，就这一处。**
    // 屏幕往上是 `dy` 变小，而「前进」是正的。这一行的号错了，真机上的表现
    // 是「推上去往后退」—— 那不是手感问题，那是撞人。**推上去 = 正**。
    final double y = (-delta.dy / Joystick.radius).clamp(-1.0, 1.0);
    if (widget.axis == JoystickAxis.turn) {
      // 右杆只转向：竖轴直接丢掉，不是吸附。推上去要转弯是学不会的。
      return Offset(x, 0.0);
    }
    // 左杆吸附到单轴：留大的那一轴，另一轴清零（§7.7）。
    return x.abs() >= y.abs() ? Offset(x, 0.0) : Offset(0.0, y);
  }

  /// 推着的时候画面掉了、或者狗连着不回话 —— 杆在这一帧变灰。
  ///
  /// **把圆心和杆头一起清掉**，不然那根杆会僵在最后一个位置上，看着像还
  /// 握在人手里。这里**不发**回调：一个在 build 里回调上层的 widget 会把
  /// 上层的 `setState` 挤进构建期。停车那一下归上层自己发 ——
  /// `teleop_page.dart` 的 `_onHealth` 在把杆变灰的同一刻就发了全零。
  @override
  void didUpdateWidget(Joystick old) {
    super.didUpdateWidget(old);
    if (!widget.enabled && _center != null) {
      _pointer = null;
      // **走 `setState`,哪怕这一刻它是多余的。** 这里后面必然跟着一次重建
      // （新 widget 正在装上去），不经 `setState` 直接改也画得对 —— 可这是
      // 一个会被下一个人照抄的写法，抄到别处就是「改了状态屏幕不跟着变」。
      setState(() {
        _center = null;
        _value = Offset.zero;
      });
    }
  }

  /// 灰的时候整根杆的不透明度。
  ///
  /// **调颜色的 alpha，不套 `Opacity`。** `Opacity` 每帧要 `saveLayer` 一次
  /// （离屏一层），而这两根杆正压在一路铺满全屏的视频上 —— 那一层是整屏
  /// 大小的。灰不灰只是几块纯色的事，直接把 alpha 乘进颜色里就够了。
  double get _dim => widget.enabled ? 1.0 : 0.35;

  @override
  Widget build(BuildContext context) {
    final Offset? c = _center;
    final double dim = _dim;
    final Widget face = Stack(
      fit: StackFit.expand,
      children: <Widget>[
        // 底：一块几乎透明的区域，把这一角标出来又不挡画面。
        // **不画死一个圈** —— 画了人就会去按它，而按不准正是要避开的事。
        DecoratedBox(
          decoration: BoxDecoration(
            color: Colors.black.withValues(alpha: 0.18 * dim),
            borderRadius: const BorderRadius.all(Radius.circular(12)),
          ),
        ),
        if (c != null) ...<Widget>[
          _circle(c, Joystick.radius, 0.24, dim, null),
          _circle(
              c +
                  Offset(_value.dx * Joystick.radius,
                      -_value.dy * Joystick.radius),
              Joystick.knobRadius,
              0.70,
              dim,
              Joystick.knobKey),
        ],
      ],
    );

    // **灰的时候整根杆不接触摸**，不是「接了不算」：接了不算的那种实现，
    // 杆照样跟着手指走，屏幕上看着像在开，其实一拍都没发出去。
    return IgnorePointer(
      ignoring: !widget.enabled,
      child: Listener(
        behavior: HitTestBehavior.opaque,
        onPointerDown: _down,
        onPointerMove: _move,
        onPointerUp: _release,
        onPointerCancel: _release,
        child: face,
      ),
    );
  }

  Widget _circle(Offset at, double r, double alpha, double dim, Key? key) =>
      Positioned(
        left: at.dx - r,
        top: at.dy - r,
        width: r * 2,
        height: r * 2,
        child: DecoratedBox(
          key: key,
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: alpha * dim),
            shape: BoxShape.circle,
            border: Border.all(
                color: Colors.white.withValues(alpha: 0.54 * dim), width: 1.5),
          ),
        ),
      );
}
