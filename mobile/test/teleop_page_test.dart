/// 遥控屏:视频铺满 + 两个虚拟摇杆、持续发拍与松手主动停、视频掉线即变灰。
///
/// **起的是真的 `HttpServer`（`support/fake_dog.dart`），不是 mock。**
/// 这一屏要证的就是「一拍真的发出去了、body 里是什么」—— 把 HTTP 那头
/// mock 掉，恰好把要证的那件事一起 mock 掉了。
///
/// **URL 一律从 `client.baseUrl` 取。** `Robot.host/port` 默认是真狗的
/// `192.168.168.100:8095`：拿 `robot` 拼 URL 的话，测试会去敲真狗地址、
/// 现场会敲错狗。`robot` 在这一屏上只用来显示名字。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'dart:async';

import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:d1max_patrol/net/wire.dart';
import 'package:d1max_patrol/ui/teleop_page.dart';
import 'package:d1max_patrol/ui/widget/joystick.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_dog.dart';
import 'support/pump.dart';

/// 一次挂好的现场:一只假狗、一条健康流、一个解过锁的客户端。
class Rig {
  Rig(this.dog, this.health, this.client);

  final FakeDog dog;
  final StreamController<VideoHealth> health;
  final PatrolClient client;

  /// 发出去的那些拍的 body。
  List<Map<String, dynamic>> get pulses => dog.received
      .where((FakeCall r) => r.path == '/api/teleop')
      .map((FakeCall r) => r.body as Map<String, dynamic>)
      .toList();

  /// 切档发出去的那几条。
  List<Map<String, dynamic>> get modes => dog.received
      .where((FakeCall r) => r.path == '/api/teleop/mode')
      .map((FakeCall r) => r.body as Map<String, dynamic>)
      .toList();

  int get beats =>
      dog.received.where((FakeCall r) => r.path == '/api/teleop/heartbeat').length;
}

/// 起一只假狗、造一条健康流、把遥控屏挂上去。每条测试都从这儿开始。
///
/// **`unlock` 是两步**（`GET /api/auth/challenge` 拿 nonce，再
/// `POST /api/auth` 交证明），两条路径都得先备好回复，少一条就换不到 token。
/// 而且它是真的网络 I/O：假时钟上推不动，得进 `runAsync`。
Future<Rig> mount(WidgetTester t, {bool online = true}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
  dog.replies['/api/auth'] = <String, dynamic>{
    'token': 'tok-abc',
    'operator': '张三',
    'operator_verified': false,
    'readonly': false,
  };
  final PatrolClient c = PatrolClient(dog.baseUrl);
  await t.runAsync(() => c.unlock('864209', operator: '张三'));

  final StreamController<VideoHealth> health =
      StreamController<VideoHealth>.broadcast();
  await t.pumpWidget(MaterialApp(
      home: TeleopPage(
          client: c,
          robot: const Robot(sn: 'C40221', name: '三号'),
          health: health.stream)));
  await pushHealth(t, health, <String, bool>{'front': online});
  return Rig(dog, health, c);
}

/// 收摊。**客户端也要关**：一条没收的 `HttpClient` 会留着自己那个 15 秒
/// 空闲计时器，`flutter_test` 的 `!timersPending` 会当场把测试打红。
Future<void> unmount(WidgetTester t, Rig rig) async {
  await teardown(t, rig.health);
  rig.client.close();
  await t.pump();
  await t.runAsync(rig.dog.stop);
}

/// 把一条健康结论推到屏幕上。
///
/// **两次 pump，不是一次。** 广播流那一下是投递在微任务里的：第一次
/// `pump()` 走进去的时候还没有人 `setState`，它看见"没有排着的帧"就什么也
/// 不做；等这一帧的 await 让微任务跑完，`setState` 才把帧排上。第二次
/// `pump()` 才真的把新的那一帧画出来。只 pump 一次的话，读到的是**上一帧**
/// 的摇杆 —— 那种绿是假的。
Future<void> pushHealth(WidgetTester t, StreamController<VideoHealth> ctl,
    Map<String, bool> cams) async {
  ctl.add(VideoHealth(cams));
  await t.pump();
  await t.pump();
}

/// 等到某一拍在 [key] 上出了个非零，把那一拍取出来。
///
/// **等的是「非零」，断言留给调用方去写。** 直接把号写进 `pumpUntil` 的条件
/// （比如等 `fwd > 0`）的话，号一翻就红成那句通用的「等了 10 秒还没等到」——
/// 跟「压根没发拍」「这根杆没出这个轴」红得一字不差，分不清是哪件事坏了。
/// 先等到非零、再单独断言号，红出来的就是「号反了」这一件事。
Future<Map<String, dynamic>> pulseWith(
    WidgetTester t, Rig rig, String key, String why) async {
  bool hit(Map<String, dynamic> p) => (p[key] as num) != 0;
  await pumpUntil(t, () => rig.pulses.any(hit), why);
  return rig.pulses.firstWhere(hit);
}

Offset leftStick(WidgetTester t) => t.getCenter(find.byType(Joystick).at(0));
Offset rightStick(WidgetTester t) => t.getCenter(find.byType(Joystick).at(1));

bool enabledLeft(WidgetTester t) =>
    t.widget<Joystick>(find.byType(Joystick).at(0)).enabled;

void main() {
  // 摘掉 `flutter_test` 那个「所有请求都回 400」的 HttpOverrides:这个文件
  // 要证的正是「一拍真的发出去了」。
  useRealHttp();

  // ------------------------------------------------------- 三个轴的号
  //
  // **这三条是从摇杆手势一路量到 `POST /api/teleop` 报文字段的。**
  // 狗那头的口径在 `app/static/index.html`（「左移」是 `lat=1`、「右移」是
  // `lat=-1`、「左转」是 `yaw=1`、「右转」是 `yaw=-1`，就是 ROS 那套右手系）。
  // 屏幕上向右是 `dx` 为正，所以从杆到报文这一路上 `lat`/`yaw` 必须翻一次号 ——
  // 翻漏了或者翻两次，真机上的表现是「人推右、狗往左」，在现场就是撞人。
  //
  // **每条都是「纯」推一个轴**：斜推会被左杆的 45° 吸附（有意的，跟真狗自带
  // 遥控器一致）吃掉一个轴，量到的就不是以为的那个东西。

  testWidgets('左杆纯向上推:报文里 fwd 为正', (WidgetTester t) async {
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50)); // 纯向上，一点横的都不带
    await t.pump();
    final Map<String, dynamic> p = await pulseWith(t, rig, 'fwd', '一拍带着非零的 fwd');
    expect(p['fwd'] as num, greaterThan(0),
        reason: '推上去就是前进，前进是正的；这一处号反了真机上是「推上去往后退」');
    expect(p['lat'], 0.0, reason: '纯向上推不该带出横向的量');
    await g.up();
    await unmount(t, rig);
  });

  testWidgets('左杆纯向右推:报文里 lat 为负', (WidgetTester t) async {
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(50, 0)); // 纯向右
    await t.pump();
    final Map<String, dynamic> p = await pulseWith(t, rig, 'lat', '一拍带着非零的 lat');
    expect(p['lat'] as num, lessThan(0),
        reason: '狗那头 lat=1 是左移，所以右移必须是负的；'
            '号反了真机上是「人推右、狗左移」');
    expect(p['fwd'], 0.0, reason: '纯向右推不该带出前后的量');
    await g.up();
    await unmount(t, rig);
  });

  testWidgets('右杆纯向右推:报文里 yaw 为负', (WidgetTester t) async {
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(rightStick(t));
    await g.moveBy(const Offset(50, 0)); // 纯向右
    await t.pump();
    final Map<String, dynamic> p = await pulseWith(t, rig, 'yaw', '一拍带着非零的 yaw');
    expect(p['yaw'] as num, lessThan(0),
        reason: '狗那头 yaw=1 是左转，所以右转必须是负的；'
            '号反了真机上是「人推右、狗左转」');
    expect(p['fwd'], 0.0, reason: '右杆只转向，不该出前后的量');
    await g.up();
    await unmount(t, rig);
  });

  testWidgets('松手主动发一次全零停', (WidgetTester t) async {
    /// **守死人（0.6 秒）是兜底，不是停车方式。**
    /// 靠它停意味着松手之后狗还会往前走大半秒 —— 那半秒足够撞上东西。
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    await g.up();
    await t.pump();

    await pumpUntil(t, () => rig.pulses.isNotEmpty, '松手那一拍');
    final Map<String, dynamic> last = rig.pulses.last;
    expect(last, containsPair('fwd', 0.0));
    expect(last, containsPair('lat', 0.0));
    expect(last, containsPair('yaw', 0.0));
    await unmount(t, rig);
  });

  testWidgets('视频掉线摇杆变灰', (WidgetTester t) async {
    final Rig rig = await mount(t);
    expect(enabledLeft(t), isTrue, reason: '有画面的时候杆是亮的');
    await pushHealth(
        t, rig.health, <String, bool>{'front': false, 'back': false});
    expect(enabledLeft(t), isFalse);
    await unmount(t, rig);
  });

  testWidgets('视频掉线不弹可关掉的框', (WidgetTester t) async {
    /// §5.9 明写**不设「确认后继续」的绕过口子**。弹框能被点掉，灰掉的杆
    /// 点不动。这一条盯的是「屏幕上没有一个能让人继续开狗的按钮」。
    final Rig rig = await mount(t);
    await pushHealth(
        t, rig.health, <String, bool>{'front': false, 'back': false});
    await t.pump(const Duration(milliseconds: 400));
    expect(find.byType(AlertDialog), findsNothing);
    expect(find.textContaining('继续'), findsNothing);
    await unmount(t, rig);
  });

  testWidgets('推着的时候在持续发拍', (WidgetTester t) async {
    /// 一拍最长 2 秒（狗那头 `MAX_PULSE_S`），所以推着不放必须续。
    /// 不续的话狗走完一拍就停，人感觉像卡顿，会本能地把杆推得更狠。
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    await pumpUntil(t, () => rig.pulses.length > 1, '推着不放的第二拍');
    expect(rig.pulses.length, greaterThan(1));
    await g.up();
    await unmount(t, rig);
  });

  testWidgets('两个杆同时推,合到一拍里发出去', (WidgetTester t) async {
    /// §7.7 明写这是允许的：左杆前推 + 右杆右推 = 向右画弧。
    /// **两个杆各发各的拍是错的** —— 后发的那一拍会把先发的那一拍覆盖掉，
    /// 于是人推着两个杆，狗只执行其中一个，而且是哪个手先动就听哪个。
    final Rig rig = await mount(t);
    final TestGesture l = await t.startGesture(leftStick(t), pointer: 11);
    await l.moveBy(const Offset(0, -50));
    final TestGesture r = await t.startGesture(rightStick(t), pointer: 12);
    await r.moveBy(const Offset(50, 0));
    await t.pump();
    await pumpUntil(
        t,
        () => rig.pulses.any((Map<String, dynamic> p) =>
            (p['fwd'] as num) > 0 && (p['yaw'] as num) != 0),
        '一拍里同时带着 fwd 和 yaw');
    await l.up();
    await r.up();
    await unmount(t, rig);
  });

  testWidgets('心跳一直在发', (WidgetTester t) async {
    /// **跟发拍分开。** 手停在零位的时候心跳不能停 —— 停了狗那头会判掉线，
    /// 一恢复推杆就要重新起一拍，中间那一下人感觉是卡了。
    final Rig rig = await mount(t);
    await pumpUntil(t, () => rig.beats > 0, '心跳');
    expect(rig.pulses, isEmpty, reason: '杆在零位，心跳照发但一拍都不该发');
    await unmount(t, rig);
  });

  testWidgets('发拍连续三次失败就把杆变灰', (WidgetTester t) async {
    /// **狗不回话跟看不见画面是一回事。** 屏幕上那根杆还亮着、还跟着手指
    /// 走，人以为狗在走 —— 而每一拍都被拒了。一两次是热点抖，连着三次就
    /// 不是抖。
    final Rig rig = await mount(t);
    rig.dog.statusCodes['/api/teleop'] = 500;
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    // **等的是「杆变灰」这件事本身，不是「狗收到了三条」。**
    // 狗收到那一刻手机还没看见回复 —— 拿收到的条数当信号，断言会在计数还
    // 停在 2 的时候就跑，红得跟真出错一模一样。
    await pumpUntil(t, () => !enabledLeft(t), '连着三拍被拒之后杆变灰');
    expect(rig.pulses.length, greaterThanOrEqualTo(3),
        reason: '不到三次就变灰是过敏了：一两次是热点抖');
    await g.up();
    await t.pump();
    await unmount(t, rig);
  });

  testWidgets('切远程要先确认，而且确认过一次就不再问', (WidgetTester t) async {
    /// §7.8：**每次都弹同一个框，第三次就被条件反射点掉了。**
    /// 所以确认只问一次；真正兜底的是狗那头那条留痕（Task 7）。
    final Rig rig = await mount(t);
    await t.tap(find.byKey(TeleopPage.remoteKey));
    await t.pump();
    await t.pump(const Duration(milliseconds: 400));
    expect(find.byType(AlertDialog), findsOneWidget, reason: '第一次切远程要问一句');
    expect(rig.modes, isEmpty, reason: '还没确认就先发出去，等于没问');

    await t.tap(find.byKey(TeleopPage.confirmRemoteKey));
    await t.pump();
    await t.pump(const Duration(milliseconds: 400));
    await pumpUntil(t, () => rig.modes.isNotEmpty, '确认之后那次切档');
    expect(rig.modes.first, containsPair('mode', 'remote'));
    expect(rig.modes.first, containsPair('confirmed', true));

    // 切回现场，再切一次远程 —— 这一次不许再问。
    await t.tap(find.byKey(TeleopPage.onsiteKey));
    await t.pump();
    await pumpUntil(t, () => rig.modes.length >= 2, '切回现场那条');
    await t.tap(find.byKey(TeleopPage.remoteKey));
    await t.pump();
    await t.pump(const Duration(milliseconds: 400));
    expect(find.byType(AlertDialog), findsNothing,
        reason: '§7.8：每次都弹同一个框，第三次就被条件反射点掉了');
    await pumpUntil(t, () => rig.modes.length >= 3, '第二次切远程那条');
    expect(rig.modes.last, containsPair('mode', 'remote'));
    await unmount(t, rig);
  });

  testWidgets('现场档只有一行轻提示', (WidgetTester t) async {
    /// §7.8：现场模式是**轻提示** —— 「请与机器狗保持在同一视线范围内」。
    /// 不是弹框：现场档是默认档，每次进来都弹框的话，它就是那个第三次被
    /// 条件反射点掉的框。
    final Rig rig = await mount(t);
    expect(find.textContaining('视线'), findsOneWidget);
    expect(find.byType(AlertDialog), findsNothing);
    await unmount(t, rig);
  });
}
