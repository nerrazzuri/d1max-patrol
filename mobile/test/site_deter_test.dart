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
  testWidgets('单狗页：保安、狗报了上装才有入口；业主没有；狗没装没有', (t) async {
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
    expect(find.byKey(const Key('deter-spotlight')), findsNothing, reason: '狗没接聚光灯');
    await t.tap(find.byKey(const Key('deter-on-siren')));
    await t.pumpAndSettle();
    expect(api.calls.last, 'deter A siren true 10.0');
    expect(find.text('开警笛 10 秒：好了'), findsOneWidget);
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

  testWidgets('狗没配 TTS：没有现打那一行', (t) async {
    await _open(t, FakeApi('guard'), caps: <String, dynamic>{..._caps, 'tts': false});
    expect(find.byKey(const Key('deter-tts')), findsNothing);
    expect(find.byKey(const Key('deter-play')), findsOneWidget);
  });

  testWidgets('狗拒了说人话：喇叭忙、设备没回', (t) async {
    final api = FakeApi('guard')..deterAck = <String, dynamic>{'result': 'rejected', 'reason': 'busy'};
    await _open(t, api);
    await t.tap(find.byKey(const Key('deter-play')));
    await t.pumpAndSettle();
    expect(find.textContaining('更要紧'), findsOneWidget);
    api.deterAck = <String, dynamic>{'result': 'rejected', 'reason': 'device: 继电器板(站号 1):没回'};
    await t.tap(find.byKey(const Key('deter-on-strobe')));
    await t.pumpAndSettle();
    expect(find.textContaining('设备没做成：继电器板'), findsOneWidget);
  });
}
