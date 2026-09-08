/// 虚拟圆心摇杆。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'package:d1max_patrol/ui/widget/joystick.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/pump.dart';

void main() {
  testWidgets('按下的那一点就是圆心', (WidgetTester t) async {
    /// **不是屏幕上画死的一个圈。** 戴着手套、在阳光下、眼睛盯着狗不盯着
    /// 屏幕 —— 这三种情况下人按不准一个画死的圈。
    Offset? out;
    await t.pumpWidget(wrap(Joystick(
        axis: JoystickAxis.translate, onChanged: (Offset v) => out = v)));
    final Offset corner =
        t.getTopLeft(find.byType(Joystick)) + const Offset(20, 20);
    final TestGesture g = await t.startGesture(corner);
    await t.pump();
    expect(out, Offset.zero, reason: '刚按下还没动，输出必须是零');
    await g.moveBy(const Offset(0, -40));
    await t.pump();
    // **推上去 = fwd 为正。** 屏幕往上是 `dy` 变小，而「前进」是正的 ——
    // 这一处的号翻错了，真机上的表现是「推上去往后退」，也就是撞人。
    expect(out!.dy, greaterThan(0),
        reason: '推上去就是前进，前进是正的');
    await g.up();
  });

  testWidgets('平移杆斜推只出一个轴', (WidgetTester t) async {
    Offset? out;
    await t.pumpWidget(wrap(Joystick(
        axis: JoystickAxis.translate, onChanged: (Offset v) => out = v)));
    final TestGesture g =
        await t.startGesture(t.getCenter(find.byType(Joystick)));
    await g.moveBy(const Offset(30, -50)); // 斜的，前多一点
    await t.pump();
    expect(out!.dx, 0.0, reason: '§7.7：真机推 45° 也只走一个轴');
    expect(out!.dy, isNot(0.0));
    await g.up();
  });

  testWidgets('转向杆只出横轴', (WidgetTester t) async {
    Offset? out;
    await t.pumpWidget(wrap(Joystick(
        axis: JoystickAxis.turn, onChanged: (Offset v) => out = v)));
    final TestGesture g =
        await t.startGesture(t.getCenter(find.byType(Joystick)));
    await g.moveBy(const Offset(40, -40));
    await t.pump();
    expect(out!.dy, 0.0, reason: '右杆只转向。推上去要转弯是学不会的');
    await g.up();
  });

  testWidgets('松手回零', (WidgetTester t) async {
    final List<Offset> seen = <Offset>[];
    await t.pumpWidget(
        wrap(Joystick(axis: JoystickAxis.translate, onChanged: seen.add)));
    final TestGesture g =
        await t.startGesture(t.getCenter(find.byType(Joystick)));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    await g.up();
    await t.pump();
    expect(seen.last, Offset.zero);
  });

  testWidgets('变灰之后推不动', (WidgetTester t) async {
    final List<Offset> seen = <Offset>[];
    await t.pumpWidget(wrap(Joystick(
        axis: JoystickAxis.translate, enabled: false, onChanged: seen.add)));
    final TestGesture g =
        await t.startGesture(t.getCenter(find.byType(Joystick)));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    expect(seen, isEmpty, reason: '§5.9 是无豁免的硬规则，不是一个能点掉的框');
    await g.up();
  });

  testWidgets('变灰之后杆的视觉位置也不许跟着手指走', (WidgetTester t) async {
    /// **「没发出回调」不等于「推不动」。**
    ///
    /// 只在 `onChanged` 里 return 的实现照样过得了上面那条:回调确实一个
    /// 都没发，可**杆还在跟着手指走**。屏幕上那根杆是现场唯一的反馈 ——
    /// 人看着它在动，就以为狗在走，于是不去看狗、也不去修画面，而实际上
    /// 一拍都没发出去。§5.9 要的是「点不动」，不是「点了不算」。
    ///
    /// 两半都要:先证亮着的时候杆**确实**跟着手指走 —— 不然下面那半条可以
    /// 靠「压根不画杆」白白通过。
    await t.pumpWidget(wrap(
        Joystick(axis: JoystickAxis.translate, onChanged: (Offset _) {})));
    final Offset c = t.getCenter(find.byType(Joystick));
    final TestGesture live = await t.startGesture(c);
    await live.moveBy(const Offset(0, -50));
    await t.pump();
    expect(find.byKey(Joystick.knobKey), findsOneWidget,
        reason: '亮着的时候按下去就该有一根杆');
    expect(t.getCenter(find.byKey(Joystick.knobKey)).dy, lessThan(c.dy - 20),
        reason: '亮着的时候杆要跟着手指往上走');
    await live.up();
    await t.pump();

    // 灰的那一半:手指照样推，屏幕上不许有任何跟着手指走的杆。
    await t.pumpWidget(wrap(Joystick(
        axis: JoystickAxis.translate,
        enabled: false,
        onChanged: (Offset _) {})));
    final TestGesture dead = await t.startGesture(c);
    await dead.moveBy(const Offset(0, -50));
    await t.pump();
    expect(find.byKey(Joystick.knobKey), findsNothing,
        reason: '灰的杆连圆心都不该有 —— 杆跟着手指动，'
            '屏幕上看着像在开，其实一拍都没发出去');
    await dead.up();
  });

  testWidgets('推着的时候变灰,杆当场就没了', (WidgetTester t) async {
    /// **画面是在人推着的时候掉的** —— 那正是 §5.9 要拦的那一刻。
    /// 上面那条「变灰之后推不动」进不到这里：它是先松手、再变灰、再重新按，
    /// 而按在灰杆上根本不会有圆心。这一条走的是另一条路 ——
    /// `didUpdateWidget` 里那次清场。清不掉的话杆会**僵在最后一个位置**，
    /// 屏幕上看着还握在人手里，而一拍都发不出去了。
    await t.pumpWidget(wrap(
        Joystick(axis: JoystickAxis.translate, onChanged: (Offset _) {})));
    final TestGesture g =
        await t.startGesture(t.getCenter(find.byType(Joystick)));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    expect(find.byKey(Joystick.knobKey), findsOneWidget,
        reason: '亮着、推着的时候本来就该有一根杆');
    // 画面掉了：同一根杆，`enabled` 翻成假 —— 手指还没抬。
    await t.pumpWidget(wrap(Joystick(
        axis: JoystickAxis.translate,
        enabled: false,
        onChanged: (Offset _) {})));
    await t.pump();
    expect(find.byKey(Joystick.knobKey), findsNothing,
        reason: '杆僵在最后一个位置上，看着像还握在人手里');
    await g.up();
  });

  testWidgets('推到底也不超过 1', (WidgetTester t) async {
    Offset? out;
    await t.pumpWidget(wrap(Joystick(
        axis: JoystickAxis.translate, onChanged: (Offset v) => out = v)));
    final TestGesture g =
        await t.startGesture(t.getCenter(find.byType(Joystick)));
    await g.moveBy(const Offset(0, -5000));
    await t.pump();
    expect(out!.dy.abs(), lessThanOrEqualTo(1.0),
        reason: '狗那头 [-1,1] 之外直接 400，越界的一拍等于白发');
    await g.up();
  });
}
