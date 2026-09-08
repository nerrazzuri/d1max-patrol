/// widget 测试里「推真时钟」这件事的公共家什。
///
/// **这三个助手是踩坑换来的，所以放在这儿共用，不许各人复制一份。**
/// 复制出去的那份会跟原版漂移：漂移之后它仍然是绿的，只是不再证明任何事。
/// Task 10（`live_video_test.dart`）先踩出来的，Task 11 起共用。
///
/// 三件事，一件都绕不过去：
///
/// 1. **`flutter_test` 的绑定把 `HttpOverrides.global` 换成了一个所有请求都
///    回 400 的假件**（`flutter_test/lib/src/_binding_io.dart` 的
///    `setupHttpOverrides`）。凡是要证「真的发出去一个请求」的文件，都得先
///    把它摘掉、跑完再装回去 —— 见 [useRealHttp]。
/// 2. **widget 测试跑在假时钟上，真的网络 I/O 只有在 `runAsync` 里才推得动，
///    而 `runAsync` 里又不许 pump。** 所以只能一边 `runAsync` 推真时钟、
///    一边 `pump(step)` 推假时钟，交替着来 —— 见 [pumpUntil]。
/// 3. **`testWidgets` 里 `await sub.cancel()` / `await close()` 会永远卡住** ——
///    不报错、不超时，卡到 CI 把它杀掉为止（本机复现过）。收摊只能
///    `unawaited(...)` —— 见 [teardown]。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// 把被测的东西塞进一个最小的 app 壳里。
///
/// `Scaffold` 是必须的：`Text` 之类要有 `Material` 才画得出来，
/// 而没有 `Directionality` 的话连布局都起不来。
Widget wrap(Widget w) => MaterialApp(home: Scaffold(body: w));

/// 在 `main()` 里调一次：让这个文件里的请求真的走网络。
///
/// `flutter_test` 默认把每个请求都掐成 400（理由是「测试不该碰网络」），
/// 而有几个文件要证的**恰恰就是**「请求真的发出去了、长什么样」——
/// 把 HTTP 那头替掉，正好把要证的那件事一起替掉了。
///
/// **`HttpOverrides.global` 只有 setter**，读回来要走 `HttpOverrides.current`。
void useRealHttp() {
  HttpOverrides? saved;
  bool taken = false;
  setUp(() {
    // 只在第一条测试前存一次：后面每条的 tearDown 都把它装回去了，
    // 再存一次存到的是同一个东西，但少一次误存 null 的机会。
    if (!taken) {
      saved = HttpOverrides.current;
      taken = true;
    }
    HttpOverrides.global = null;
  });
  tearDown(() {
    HttpOverrides.global = saved;
  });
}

/// 一边推真时钟一边 pump，直到条件成立。
///
/// widget 测试跑在假时钟上，真的网络 I/O 只有在 `runAsync` 里才推得动；而
/// `runAsync` 里又不许 pump。所以只能这样交替着来。**不是固定 sleep**
/// （§8.5 第 2 条）：条件一成立立刻出去，超时了就报清楚在等什么。
///
/// `step` 是每轮推假时钟多久 —— 挂在假时钟上的定时器（重连退避、发拍周期）
/// 靠它才到得了点，`pump()` 不带时长的话它们永远不响。
Future<void> pumpUntil(WidgetTester t, bool Function() ok, String why,
    {Duration step = const Duration(milliseconds: 200)}) async {
  final DateTime deadline = DateTime.now().add(const Duration(seconds: 10));
  while (!ok()) {
    if (DateTime.now().isAfter(deadline)) {
      fail('等了 10 秒还没等到：$why');
    }
    await t.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 5)));
    await t.pump(step);
  }
}

/// 真时钟上等条件成立。给不带 widget 的那几条用。
Future<void> until(bool Function() ok, String why) async {
  final DateTime deadline = DateTime.now().add(const Duration(seconds: 10));
  while (!ok()) {
    if (DateTime.now().isAfter(deadline)) {
      fail('等了 10 秒还没等到：$why');
    }
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}

/// 收摊：先把 widget 拆了（连接跟着 `dispose` 一起关），再收流、再收假狗。
///
/// **不许 `await` `close()` / `cancel()`。** 在 widget 测试的假时钟上等这两个
/// future，测试体跑完之后整条测试再也回不来 —— 不报错、不超时，卡到 CI 把
/// 它杀掉为止（这台机器上复现过，`testWidgets` 里 `await sub.cancel()` 就够）。
///
/// `stopServer` 那一步要进 `runAsync`：关一个真的 `HttpServer` 是真的 I/O，
/// 假时钟上推不动。
Future<void> teardown<T>(WidgetTester t, StreamController<T>? ctl,
    [Future<void> Function()? stopServer]) async {
  await t.pumpWidget(const SizedBox());
  if (ctl != null) unawaited(ctl.close());
  await t.pump();
  if (stopServer != null) {
    await t.runAsync(stopServer);
  }
}
