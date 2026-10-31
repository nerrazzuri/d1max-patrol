// 上装（W21）：单狗页的入口按角色和狗的能力出；只列接上的几路；开带时长、关随时；喇叭放话术、现打一段（狗配了 TTS 才有）；
// 狗拒了说人话。横屏 800×360。
import 'package:d1max_patrol/ui/site_deter.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart';

const Map<String, dynamic> _caps = <String, dynamic>{
  'outputs': <String>['strobe', 'siren', 'speaker'],
  'clips': <String>['warn-zh', 'warn-en'],
  'tts': true,
  'max_s': 600.0,
};

Future<void> _open(WidgetTester t, FakeApi api, {Map<String, dynamic> caps = _caps}) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(home: SiteDeterPage(api: api, robotId: 'A', caps: caps)));
  await t.pumpAndSettle();
}

Map<String, dynamic> _view({bool deter = true}) {
  final v = Map<String, dynamic>.from(siteFixture('site_robot'));
  final caps = Map<String, dynamic>.from(v['capabilities'] as Map);
  final tasks = Map<String, dynamic>.from(caps['tasks'] as Map);
  if (deter) tasks['deter'] = _caps;
  caps['tasks'] = tasks;
  v['capabilities'] = caps;
  return v;
}

Future<void> _robotPage(WidgetTester t, FakeApi api) async {
  await t.binding.setSurfaceSize(const Size(800, 360));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(const SizedBox()); // 同一个测试里开几次：不复用上一次的页面状态
  await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
  await t.pumpAndSettle();
}

void main() {
  testWidgets('单机器人页：保安、机器人报了上装才有入口；管理员没有；机器人没装没有', (t) async {
    await _robotPage(t, FakeApi('guard', robotView: _view()));
    expect(find.byKey(const Key('btn-deter')), findsOneWidget);
    await _robotPage(t, FakeApi('owner', robotView: _view()));
    expect(find.byKey(const Key('btn-deter')), findsNothing);
    await _robotPage(t, FakeApi('guard', robotView: _view(deter: false)));
    expect(find.byKey(const Key('btn-deter')), findsNothing);
  });

  testWidgets('只列接上的几路；开带默认时长，关随时', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    expect(find.byKey(const Key('deter-strobe')), findsOneWidget);
    expect(find.byKey(const Key('deter-siren')), findsOneWidget);
    expect(find.byKey(const Key('deter-spotlight')), findsNothing, reason: '机器人没接聚光灯');
    await t.tap(find.byKey(const Key('deter-on-siren')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A siren true 10.0');
    expect(find.text('开启警笛 10 秒：完成'), findsOneWidget);
    await t.tap(find.byKey(const Key('deter-off-strobe')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A strobe false');
  });

  testWidgets('喇叭：挑话术放、现打一段、停', (t) async {
    final api = FakeApi('guard');
    await _open(t, api);
    await t.tap(find.byKey(const Key('deter-clip')));
    await t.pumpAndSettle();
    await t.tap(find.text('warn-en').last);
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('deter-play')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A speaker true 30.0 warn-en');
    await t.enterText(find.byKey(const Key('deter-tts')), '请马上离开');
    await t.tap(find.byKey(const Key('deter-say')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A speaker true 30.0 tts:zh:请马上离开');
    await t.tap(find.byKey(const Key('deter-off-speaker')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A speaker false');
  });

  testWidgets('机器人没配 TTS：没有现打那一行', (t) async {
    await _open(t, FakeApi('guard'), caps: <String, dynamic>{..._caps, 'tts': false});
    expect(find.byKey(const Key('deter-tts')), findsNothing);
    expect(find.byKey(const Key('deter-play')), findsOneWidget);
  });

  testWidgets('机器人拒了说人话：喇叭忙、设备没回', (t) async {
    final api = FakeApi('guard')..deterAck = <String, dynamic>{'result': 'rejected', 'reason': 'busy'};
    await _open(t, api);
    await t.tap(find.byKey(const Key('deter-play')));
    await t.pumpAndSettle();
    expect(find.textContaining('更紧急'), findsOneWidget);
    api.deterAck = <String, dynamic>{'result': 'rejected', 'reason': 'device: 继电器板(站号 1):没回'};
    await t.tap(find.byKey(const Key('deter-on-strobe')));
    await t.pumpAndSettle();
    expect(find.textContaining('设备执行失败：继电器板'), findsOneWidget);
  });

  testWidgets('W40：机器人报了左右灯、警灯颜色：多两行、颜色下拉框，开的时候带上', (t) async {
    final api = FakeApi('guard');
    await _open(t, api, caps: <String, dynamic>{
      ..._caps,
      'outputs': <String>['strobe', 'siren', 'spotlight', 'speaker'],
      'spotlight_sides': <String>['left', 'right'],
      'strobe_colors': <String>['red', 'yellow', 'green'],
      'strobe_default': 'yellow',
    });
    expect(find.byKey(const Key('deter-spotlight')), findsOneWidget);
    expect(find.byKey(const Key('deter-spotlight-left')), findsOneWidget);
    expect(find.byKey(const Key('deter-spotlight-right')), findsOneWidget);
    await t.tap(find.byKey(const Key('deter-on-spotlight-left')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A spotlight true 120.0 side=left');
    expect(find.text('开启左聚光灯 120 秒：完成'), findsOneWidget);
    await t.tap(find.byKey(const Key('deter-off-spotlight-right')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A spotlight false side=right');
    await t.tap(find.byKey(const Key('deter-on-strobe')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A strobe true 60.0 color=yellow', reason: '缺省颜色按机器人报的');
    await t.tap(find.byKey(const Key('deter-color')));
    await t.pumpAndSettle();
    await t.tap(find.text('绿').last);
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('deter-on-strobe')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A strobe true 60.0 color=green');
    await t.tap(find.byKey(const Key('deter-off-strobe')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A strobe false', reason: '关不带颜色');
  });

  testWidgets('W40：老机器人没报左右、颜色：跟原来一样', (t) async {
    final api = FakeApi('guard');
    await _open(t, api, caps: <String, dynamic>{..._caps, 'outputs': <String>['strobe', 'spotlight']});
    expect(find.byKey(const Key('deter-spotlight-left')), findsNothing);
    expect(find.byKey(const Key('deter-color')), findsNothing);
    await t.tap(find.byKey(const Key('deter-on-strobe')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A strobe true 60.0');
  });

  testWidgets('W40：单机器人页列设备健康，坏的排前面，过时了标出来', (t) async {
    final v = _view()
      ..['devices'] = <String, dynamic>{
        'items': <Map<String, dynamic>>[
          {'id': 'relay', 'state': 'online', 'detail': '6 个线圈都确认过'},
          {'id': 'rtk', 'state': 'degraded', 'detail': '浮点解'},
          {'id': 'link:router', 'state': 'offline', 'detail': '连不上 192.168.10.1:80'},
          {'id': 'camera:thermal', 'state': 'error', 'detail': '录像断了'},
        ],
        'at_ms': 1,
        'stale': false,
      };
    await _robotPage(t, FakeApi('guard', robotView: v));
    await t.scrollUntilVisible(find.byKey(const Key('robot-devices')), 200);
    expect(find.text('路由器：离线 · 连不上 192.168.10.1:80'), findsOneWidget);
    expect(find.text('相机 热像：故障 · 录像断了'), findsOneWidget);
    expect(find.text('RTK：降级 · 浮点解'), findsOneWidget);
    final ys = [for (final k in ['camera:thermal', 'link:router', 'rtk', 'relay'])
      t.getTopLeft(find.byKey(Key('device-$k'))).dy];
    expect(ys, [...ys]..sort(), reason: '故障、离线、降级、在线');
    (v['devices'] as Map)['stale'] = true;
    await _robotPage(t, FakeApi('guard', robotView: v));
    await t.scrollUntilVisible(find.byKey(const Key('robot-devices')), 200);
    expect(find.text('设备（长时间未更新）'), findsOneWidget);
  });
}
