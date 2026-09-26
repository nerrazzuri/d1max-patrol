// 「我在现场监护」（W00c6i）：只对要人监护的狗、只给能派单的人；开着每秒续、关掉放、离开页面放、
// 切后台放；续不上、狗不接就说清楚、自己关掉；派单被 unsupervised 拒时说人话。
import 'package:d1max_patrol/net/site_client.dart' show SiteError;
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_supervise.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;
import 'support/fake_site.dart' show siteFixture;

Map<String, dynamic> _view(String autonomy) {
  final v = Map<String, dynamic>.of(siteFixture('site_robot'));
  final caps = Map<String, dynamic>.of(v['capabilities'] as Map<String, dynamic>);
  final tasks = <String, dynamic>{};
  (caps['tasks'] as Map).forEach((k, t) {
    tasks['$k'] = t is Map && (k == 'goto' || k == 'patrol')
        ? <String, dynamic>{...t.cast<String, dynamic>(), 'autonomy': autonomy}
        : t;
  });
  caps['tasks'] = tasks;
  v['capabilities'] = caps;
  return v;
}

Future<void> _open(WidgetTester t, FakeApi api) async {
  // 每次一个新 key:同一个位置上的页面会复用旧的 State。
  await t.pumpWidget(MaterialApp(home: SiteRobotPage(key: UniqueKey(), api: api, robotId: 'A')));
  await t.pump();
  await t.pump();
}

int _count(FakeApi api, String action) => api.calls.where((c) => c == 'supervise A $action').length;

void main() {
  testWidgets('要人监护的狗、保安才有开关；可自主的狗、业主没有', (t) async {
    await _open(t, FakeApi('guard', robotView: _view('supervised')));
    expect(find.byKey(SupervisionSwitch.switchKey), findsOneWidget);
    await _open(t, FakeApi('guard', robotView: _view('autonomous')));
    expect(find.byKey(SupervisionSwitch.switchKey), findsNothing);
    await _open(t, FakeApi('owner', robotView: _view('supervised')));
    expect(find.byKey(SupervisionSwitch.switchKey), findsNothing);
    await t.pumpWidget(Container());
  });

  testWidgets('打开立刻续一次、之后每秒续；关掉放掉、不再续', (t) async {
    final api = FakeApi('guard', robotView: _view('supervised'));
    await _open(t, api);
    await t.tap(find.byKey(SupervisionSwitch.switchKey));
    await t.pump();
    expect(_count(api, 'renew'), 1);
    await t.pump(const Duration(milliseconds: 3100));
    expect(_count(api, 'renew'), 4);
    expect(find.text('监护中'), findsOneWidget);
    await t.tap(find.byKey(SupervisionSwitch.switchKey));
    await t.pump();
    expect(_count(api, 'release'), 1);
    await t.pump(const Duration(seconds: 3));
    expect(_count(api, 'renew'), 4, reason: '关掉之后不再续');
    await t.pumpWidget(Container());
  });

  testWidgets('开着离开这一页：放掉', (t) async {
    final api = FakeApi('guard', robotView: _view('supervised'));
    await _open(t, api);
    await t.tap(find.byKey(SupervisionSwitch.switchKey));
    await t.pump();
    await t.pumpWidget(Container());
    await t.pump();
    expect(_count(api, 'release'), 1);
    final n = _count(api, 'renew');
    await t.pump(const Duration(seconds: 3));
    expect(_count(api, 'renew'), n);
  });

  testWidgets('开着切到后台：放掉、开关关上、说清楚', (t) async {
    final api = FakeApi('guard', robotView: _view('supervised'));
    await _open(t, api);
    await t.tap(find.byKey(SupervisionSwitch.switchKey));
    await t.pump();
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await t.pump();
    expect(_count(api, 'release'), 1);
    final n = _count(api, 'renew');
    await t.pump(const Duration(seconds: 3));
    expect(_count(api, 'renew'), n, reason: '后台里不续');
    t.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await t.pump();
    expect(find.textContaining('切到后台了，监护已放'), findsOneWidget);
    expect(find.text('我在现场监护'), findsOneWidget, reason: '回来是关着的，要人重新打开');
    await t.pumpWidget(Container());
  });

  testWidgets('续不上、狗不接：自己关掉、说清楚', (t) async {
    final api = FakeApi('guard', robotView: _view('supervised'))
      ..superviseError = SiteError(504, '等狗的回执超时');
    await _open(t, api);
    await t.tap(find.byKey(SupervisionSwitch.switchKey));
    await t.pump();
    await t.pump();
    expect(find.textContaining('监护续不上'), findsOneWidget);
    expect(find.text('我在现场监护'), findsOneWidget);
    final api2 = FakeApi('guard', robotView: _view('supervised'))
      ..superviseAck = <String, dynamic>{'result': 'rejected', 'reason': 'unsupported'};
    await _open(t, api2);
    await t.tap(find.byKey(SupervisionSwitch.switchKey));
    await t.pump();
    await t.pump();
    expect(find.textContaining('狗没接监护'), findsOneWidget);
    await t.pumpWidget(Container());
  });

  testWidgets('派单被狗以 unsupervised 拒：说人话', (t) async {
    final api = _RejectingApi('guard', robotView: _view('supervised'));
    await _open(t, api);
    await t.tap(find.byKey(const Key('btn-standby')));
    await t.pump();
    await t.pump();
    expect(find.textContaining('它要人现场监护'), findsOneWidget);
    await t.pumpWidget(Container());
  });
}

class _RejectingApi extends FakeApi {
  _RejectingApi(super.role, {super.robotView});
  @override
  Future<Map<String, dynamic>> returnToStandby(String id) async => <String, dynamic>{
        'ack': <String, dynamic>{'result': 'rejected', 'reason': 'unsupervised'}
      };
}
