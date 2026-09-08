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
import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/model/robot.dart';
import 'package:d1max_patrol/net/patrol_client.dart';
import 'package:d1max_patrol/net/wire.dart';
import 'package:d1max_patrol/ui/control_panel.dart';
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
///
/// **控制权那份回复也得备好。** Task 12 起，杆亮不亮多了一道闸（`_held`）：
/// 不给 `/api/control` 备回复的话，屏上的面板问到的是一份空的租约，杆从头到
/// 尾是灰的 —— 上面每一条盯发拍的测试都会红在一件跟它无关的事上。
/// [lease] 不给就是「控制权在自己手上，还剩 30 秒」。
Future<Rig> mount(WidgetTester t,
    {bool online = true, Map<String, dynamic>? lease}) async {
  final FakeDog dog = FakeDog();
  await t.runAsync(dog.start);
  dog.replies['/api/auth/challenge'] = <String, dynamic>{'nonce': 'n0nce'};
  dog.replies['/api/auth'] = <String, dynamic>{
    'token': 'tok-abc',
    'operator': '张三',
    'operator_verified': false,
    'readonly': false,
  };
  dog.replies['/api/control'] = lease ?? heldByMe();
  dog.replies['/api/control/heartbeat'] = lease ?? heldByMe();
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
  // **等控制权那一问回来。** 面板问狗是一次真的往返，假时钟上推不动；
  // 不等的话，下面每一条测试都会在「杆还没亮」的那一瞬间跑。
  await pumpUntil(
      t,
      () =>
          find.byKey(ControlPanel.releaseKey).evaluate().isNotEmpty ||
          find.byKey(ControlPanel.acquireKey).evaluate().isNotEmpty ||
          find.byKey(ControlPanel.takeoverKey).evaluate().isNotEmpty,
      '控制权面板拿到第一份租约');
  return Rig(dog, health, c);
}

/// 控制权在自己手上。**照着 `test/fixtures/lease_state.json` 改**，不手搓 JSON。
Map<String, dynamic> heldByMe() => <String, dynamic>{
      ...jsonDecode(File('test/fixtures/lease_state.json').readAsStringSync())
          as Map<String, dynamic>,
      'mine': true,
    };

/// 控制权在别人手上。
Map<String, dynamic> heldByOther() => <String, dynamic>{
      ...heldByMe(),
      'holder': <String, dynamic>{'ref': 'ff00ff00', 'operator': '李四'},
      'mine': false,
    };

/// 收摊。**客户端也要关**：一条没收的 `HttpClient` 会留着自己那个 15 秒
/// 空闲计时器，`flutter_test` 的 `!timersPending` 会当场把测试打红。
Future<void> unmount(WidgetTester t, Rig rig) async {
  await teardown(t, rig.health);
  rig.client.close();
  await t.pump();
  await t.runAsync(rig.dog.stop);
  // **假狗自己出的错要看得见。** 它以前整段裹在一个宽 `catch (_)` 里，
  // 于是假狗的笔误长得跟「这条路不回话」一模一样，人会去查客户端的超时。
  expect(rig.dog.errors, isEmpty, reason: '假狗自己抛了：查的是假狗，不是被测代码');
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

/// 发出去的那些全零拍。**停车说了几遍，数的是这个。**
List<Map<String, dynamic>> zeroPulses(Rig rig) => rig.pulses
    .where((Map<String, dynamic> p) =>
        (p['fwd'] as num) == 0 &&
        (p['lat'] as num) == 0 &&
        (p['yaw'] as num) == 0)
    .toList();

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

  testWidgets('松手之后连着发几拍全零,不止一拍', (WidgetTester t) async {
    /// **一拍丢了就等于没停。** 松手那一拍是立刻发的，可热点抖一下它就没
    /// 了，而两个杆都在零位的时候周期发拍是不发的 —— 于是停车退回狗那头的
    /// 兜底（一拍 0.4 秒走完 + 守死人 0.6 秒）。兜底是兜底，不是停车方式。
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    await pumpUntil(t, () => rig.pulses.isNotEmpty, '推着的那一拍');
    await g.up();
    await t.pump();
    await pumpUntil(t, () => zeroPulses(rig).length >= 3,
        '松手之后补上的那几拍全零（合计 3 拍），它们要绕过「零位不发」那条抑制');
    await unmount(t, rig);
  });

  testWidgets('松手那一拍发失败,后面几拍照发', (WidgetTester t) async {
    /// 补发要在**第一拍就失败**的时候还管用 —— 那正是它存在的理由。
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    await pumpUntil(t, () => rig.pulses.isNotEmpty, '推着的那一拍');
    rig.dog.statusCodes['/api/teleop'] = 500; // 从松手那一拍起，全拒
    await g.up();
    await t.pump();
    await pumpUntil(t, () => zeroPulses(rig).length >= 3,
        '松手那一拍被拒之后补上的那两拍（失败不该让补发停下来）');
    await unmount(t, rig);
  });

  testWidgets('报文一律不带 seconds', (WidgetTester t) async {
    /// 派工令第八条：**一拍多长是狗的手感参数**（`profile` 那一档算出来的）。
    /// 客户端传死一个 `seconds`，等于把狗的标定钉在这个 app 的某个版本上 ——
    /// 标定改了，现场得挨个升级手机才跟得上。
    final Rig rig = await mount(t);
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    await pumpUntil(t, () => rig.pulses.isNotEmpty, '推着的那一拍');
    await g.up();
    await t.pump();
    await pumpUntil(t, () => zeroPulses(rig).isNotEmpty, '松手那一拍');
    for (final Map<String, dynamic> p in rig.pulses) {
      expect(p.containsKey('seconds'), isFalse,
          reason: '不传 seconds，让狗按 profile 取默认');
      expect(p, containsPair('profile', 'roam'));
    }
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
    ///
    /// **这条只管「合到一拍」这一件事，号的对错不归它管**（归上面那三条）。
    /// 所以断的是「两个轴都非零」而不是「fwd > 0」：写成后者的话，`fwd` 的
    /// 号一翻，这条也跟着红，而它红出来的话人会去查发拍的合并逻辑 —— 查的
    /// 是完全另一件事。复审在这儿真踩过：斜推一下（生产代码一个字不动）
    /// 就能让它红成跟「fwd 号翻了」一字不差的那句话。
    final Rig rig = await mount(t);
    final TestGesture l = await t.startGesture(leftStick(t), pointer: 11);
    await l.moveBy(const Offset(0, -50)); // 纯向上，别让 45 度吸附吃掉 fwd
    final TestGesture r = await t.startGesture(rightStick(t), pointer: 12);
    await r.moveBy(const Offset(50, 0));
    await t.pump();
    await pumpUntil(
        t,
        () => rig.pulses.any((Map<String, dynamic> p) =>
            (p['fwd'] as num) != 0 && (p['yaw'] as num) != 0),
        '一拍里同时带着非零的 fwd 和非零的 yaw（两根杆没合到一拍，'
            '或者其中一根压根没出这个轴）');
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

  testWidgets('切档没切成就把屏幕退回上一档', (WidgetTester t) async {
    /// **档位决定的是速度上限。** 屏幕上写着「远程」而狗那头还按「现场」
    /// 记账，人就会照远程那一档的手感推杆，狗却收着走（反过来更糟）。
    /// 一句红字纠正不了一整屏的视觉 —— 档得退回去。
    final Rig rig = await mount(t);
    rig.dog.statusCodes['/api/teleop/mode'] = 500;
    await t.tap(find.byKey(TeleopPage.remoteKey));
    await t.pump();
    await t.pump(const Duration(milliseconds: 400));
    await t.tap(find.byKey(TeleopPage.confirmRemoteKey));
    await t.pump();
    await pumpUntil(t, () => rig.modes.isNotEmpty, '那条切档请求');
    await pumpUntil(t, () => find.textContaining('档没切成').evaluate().isNotEmpty,
        '切档失败那句话');
    expect(find.text(onsiteHint), findsOneWidget,
        reason: '狗那头还在现场档，屏幕上就不许写着远程');
    await unmount(t, rig);
  });

  testWidgets('心跳连着断了一秒就说一句话,但不把杆变灰', (WidgetTester t) async {
    /// 顾虑 3 的定夺：**不把心跳失败并进那三次。** 它 5 Hz 地跑，拿它去关
    /// 控制通路是过敏 —— 整网断掉的时候 `VideoHealthPoller` 走 `_degrade()`
    /// 发全 false，≤2 秒经视频那道闸就变灰了，不依赖这条路。
    /// 这里要证的是另一件事：这条路断了得**说出来**，不是默默地断。
    final Rig rig = await mount(t);
    rig.dog.statusCodes['/api/teleop/heartbeat'] = 500;
    await pumpUntil(t, () => find.text(beatTroubleHint).evaluate().isNotEmpty,
        '心跳连着失败之后状态带上那句话');
    expect(enabledLeft(t), isTrue,
        reason: '心跳抖一下就关掉控制通路是过敏：变灰归视频那道闸和发拍那三次管');
    await unmount(t, rig);
  });

  testWidgets('心跳一直不通的时候那句话不许闪', (WidgetTester t) async {
    /// R-1：**每条路只清自己写的那一句。** 发拍成功那一下要是顺手把心跳那句
    /// 也抹了，在「心跳端点挂了、发拍还通」这个场景里就成了：发拍每 300 毫秒
    /// 抹一次、心跳每 200 毫秒挂回去 —— 人正推着杆，那行字在屏上闪。
    /// **闪着的字比没有字更糟**：人当它是花屏，于是真断了也不当回事。
    ///
    /// **这条测试非推着杆不可。** 手停在零位就不发拍，也就没人来抹 ——
    /// 上面那条心跳测试正是这么漏掉的。
    final Rig rig = await mount(t);
    rig.dog.statusCodes['/api/teleop/heartbeat'] = 500;
    final TestGesture g = await t.startGesture(leftStick(t));
    await g.moveBy(const Offset(0, -50));
    await t.pump();
    await pumpUntil(t, () => find.text(beatTroubleHint).evaluate().isNotEmpty,
        '心跳连着失败之后状态带上那句话（推着杆）');

    // 采样：连着好几拍地看，那句话一次都不许不见。
    final int pulsesAtStart = rig.pulses.length;
    int missing = 0;
    for (int i = 0; i < 16; i++) {
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 5)));
      await t.pump(const Duration(milliseconds: 100));
      if (find.text(beatTroubleHint).evaluate().isEmpty) missing++;
    }
    // **先证这一段里发拍真的在跑**，否则「没闪」是空绿的。
    expect(rig.pulses.length - pulsesAtStart, greaterThanOrEqualTo(3),
        reason: '这一段里几乎没发出拍：那这条测试根本没碰到「发拍会不会抹掉心跳那句」');
    expect(missing, 0,
        reason: '心跳还断着，那句话却在 16 次采样里没了 $missing 次：'
            '发拍成功那一下把别人写的话也清了');

    await g.up();
    await unmount(t, rig);
  });

  testWidgets('心跳抖一两下不上屏,连着五次才说话', (WidgetTester t) async {
    /// R-2：Ruling 78 有两半，这条钉的是**「一次抖动不上屏」**那半。
    /// 5 Hz 的心跳掉一两拍是热点常态，每掉一拍就往状态带上写一句，那条带子
    /// 三天就没人看了 —— 真断的时候那句话也就不起作用了。
    ///
    /// 数的是**狗收到了几次**：客户端那头的 `_beatFails` 只会比它少（回复还
    /// 在路上），所以「狗才收到 4 次」时失败次数必然 < 5，这时候上屏就是错的。
    final Rig rig = await mount(t);
    rig.dog.statusCodes['/api/teleop/heartbeat'] = 500;
    final DateTime deadline = DateTime.now().add(const Duration(seconds: 10));
    while (rig.beats < 4) {
      if (DateTime.now().isAfter(deadline)) {
        fail('等了 10 秒还没等到：心跳发够 4 次');
      }
      await t.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 5)));
      await t.pump(const Duration(milliseconds: 50));
      expect(find.text(beatTroubleHint), findsNothing,
          reason: '狗才收到 ${rig.beats} 次心跳就上屏了：抖一下就喊，'
              '喊多了这条状态带就没人看，真断的时候也白喊');
    }
    await pumpUntil(t, () => find.text(beatTroubleHint).evaluate().isNotEmpty,
        '连着五次之后那句话', step: const Duration(milliseconds: 50));
    expect(rig.beats, greaterThanOrEqualTo(5),
        reason: '不到五次就说话了：门槛没在起作用');
    await unmount(t, rig);
  });

  testWidgets('窄屏上两根杆不许叠在一起', (WidgetTester t) async {
    /// 盒子写死 200 的话，<400dp 宽的竖屏上两根杆是叠着的，而**重叠那一块
    /// 归 `Stack` 里靠后的右杆** —— 人按左下角想往前走，狗原地转弯。
    /// 320dp 是常见的小屏竖屏宽度。
    ///
    /// （横屏锁定不在这一轮做，那一条记在真机清单上。）
    t.view.physicalSize = const Size(320, 640);
    t.view.devicePixelRatio = 1.0;
    addTearDown(t.view.resetPhysicalSize);
    addTearDown(t.view.resetDevicePixelRatio);
    final Rig rig = await mount(t);
    final Rect l = t.getRect(find.byType(Joystick).at(0));
    final Rect r = t.getRect(find.byType(Joystick).at(1));
    expect(l.width, greaterThan(0), reason: '收窄不等于收没了');
    expect(l.right, lessThanOrEqualTo(r.left),
        reason: '两根杆叠上了：重叠那一块归靠后的右杆，'
            '人按左下角想往前走，狗原地转弯');
    await unmount(t, rig);
  });

  testWidgets('没有控制权就把杆变灰', (WidgetTester t) async {
    /// 狗那头**会动腿的接口全都要控制权**（`app/control.py` 的 `CONTROLLED`）。
    /// 没有这道闸，人推杆得到的是一串 409 —— 而杆还亮着、还跟着手指走，
    /// 人以为狗在走。
    final Rig rig = await mount(t, lease: heldByOther());
    expect(enabledLeft(t), isFalse, reason: '控制权在李四手上，这台手机推不动');
    await unmount(t, rig);
  });

  testWidgets('两道闸的提示语分开:没有控制权和没有画面不是一句话', (WidgetTester t) async {
    /// 合成一句「不能操作」的话，人会朝错误的方向去排查：没有控制权要去
    /// 面板上要一份（或者请对方交还），没有画面要去看相机和网 —— 两件事的
    /// 处置一点都不像。
    final Rig rig = await mount(t, lease: heldByOther());
    // 有画面、没控制权：只该说控制权那一句。
    expect(find.text(noControlHint), findsOneWidget);
    expect(find.text(noVideoHint), findsNothing,
        reason: '画面好好的，还说「没有画面」的话，人会去查相机和网 —— 查的是另一件事');
    expect(find.textContaining('不能操作'), findsNothing,
        reason: '合成一句的话，人会朝错误的方向去排查');
    await unmount(t, rig);
  });

  testWidgets('两道闸的提示语分开:有控制权而没画面时只说画面', (WidgetTester t) async {
    final Rig rig = await mount(t);
    await pushHealth(
        t, rig.health, <String, bool>{'front': false, 'back': false});
    expect(find.text(noVideoHint), findsOneWidget);
    expect(find.text(noControlHint), findsNothing,
        reason: '控制权在自己手上，还说「没有控制权」的话人会去抢一份已经拿着的租约');
    expect(find.textContaining('不能操作'), findsNothing);
    await unmount(t, rig);
  });

  // ------------------------------------------------- 顶上那条带子吃不吃手势
  //
  // 复审问的是「上面那个 `SingleChildScrollView` 会不会把纵向拖拽抢走」。
  // 这两条把它量出来：**第一条量「抢的是哪一块」，第二条量「那一块够不够
  // 得着摇杆」。** 只量第二条的话，明天带子多两行、或者屏变矮，它就悄悄地
  // 从「够不着」滑到「够得着」而没人知道。

  testWidgets('带子的壳子会吃掉自己那块矩形里的纵向拖拽', (WidgetTester t) async {
    /// `Scrollable` 建的 `RawGestureDetector` 带的是 `HitTestBehavior.opaque`，
    /// 而 `RenderStack` 的命中测试**停在第一个命中的孩子**上 —— 于是落在带子
    /// 那块矩形里的指针，压根到不了 `Stack` 里排在它下面的东西。
    /// **内容有没有溢出、滚不滚得动，都一样吃。**
    ///
    /// 量的是 `teleop_page.dart` 导出的那个 `bandShell` 本身，不是另搭一个长
    /// 得像的壳子：另搭的那个明天就跟真的那个分家了，分家那天两边都是绿的。
    final List<String> under = <String>[];
    await t.pumpWidget(wrap(Stack(
      children: <Widget>[
        Positioned.fill(
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onVerticalDragUpdate: (DragUpdateDetails d) => under.add('drag'),
          ),
        ),
        Positioned(
          top: 0,
          left: 0,
          right: 0,
          child: bandShell(
              maxHeight: 240,
              child: const SizedBox(height: 60, child: Text('带子'))),
        ),
      ],
    )));
    final Rect band = t.getRect(find.byType(SingleChildScrollView));
    expect(band.height, 60,
        reason: '内容没溢出的时候壳子按内容高（60），不是按限高（240）铺开：'
            '按限高铺的话它吃掉的那块比看得见的带子大得多');

    final TestGesture inside = await t.startGesture(band.center);
    await inside.moveBy(const Offset(0, 40));
    await t.pump();
    await inside.moveBy(const Offset(0, 40));
    await t.pump();
    await inside.up();
    expect(under, isEmpty,
        reason: '带子那块矩形里的纵向拖拽漏到了底下：'
            '那说明这个壳子不是「吃自己那块」，下面这条的结论也就靠不住了');

    final TestGesture outside =
        await t.startGesture(Offset(band.center.dx, band.bottom + 80));
    await outside.moveBy(const Offset(0, 40));
    await t.pump();
    await outside.moveBy(const Offset(0, 40));
    await t.pump();
    await outside.up();
    expect(under, isNotEmpty,
        reason: '壳子外面的拖拽也接不到的话，上面那句 isEmpty 是空绿的 —— '
            '可能这个探针从头到尾就没有一次拖拽送到过');
  });

  testWidgets('带子那块矩形够不着两根摇杆', (WidgetTester t) async {
    /// 上一条证了「壳子吃自己那块矩形」，这一条量那块矩形到底在哪儿：
    /// **它的下沿在两根杆的上沿之上，两块不相交。** 所以带子吃手势这件事
    /// 影响不到摇杆 —— 复审担心的那件事在当前布局下不成立。
    ///
    /// 另外两层保险，都不是这条在管的：杆在 `Stack` 里排在带子**后面**
    /// （真叠上了也是杆压着带子），而且杆是贴着屏幕底部的。这条只管几何。
    final Rig rig = await mount(t);
    final Rect band = t.getRect(find.byType(SingleChildScrollView));
    final Rect l = t.getRect(find.byType(Joystick).at(0));
    final Rect r = t.getRect(find.byType(Joystick).at(1));
    expect(band.height, greaterThan(0),
        reason: '带子量出来是空的话，下面那两句是空绿的');
    expect(band.bottom, lessThanOrEqualTo(l.top),
        reason: '带子的下沿压到左杆上了：那一块的纵向拖拽会被带子吃掉，'
            '人推杆想让狗往前走，狗一动不动');
    expect(band.bottom, lessThanOrEqualTo(r.top),
        reason: '带子的下沿压到右杆上了：同上');
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
