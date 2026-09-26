// 站点模式的几屏（W00c4）：按角色显示按钮、列表与单狗页用真站点的夹具、首屏二选一、站点列表存取。
import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:d1max_patrol/main.dart';
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_video.dart';
import 'package:d1max_patrol/ui/widget/live_video.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/fake_site.dart';

/// 1×1 的 PNG：照片解得出来（测试里的图片解码认真的）。
final Uint8List onePixelPng = Uint8List.fromList(<int>[
  0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
  0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
  0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
  0x00, 0x03, 0x01, 0x01, 0x00, 0xC9, 0xFE, 0x92, 0xEF, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E,
  0x44, 0xAE, 0x42, 0x60, 0x82,
]);

class FakeTeleopLink implements TeleopLink {
  final StreamController<Map<String, dynamic>> ctl =
      StreamController<Map<String, dynamic>>.broadcast();
  final List<List<double>> sent = <List<double>>[];
  bool released = false;
  bool isClosed = false;
  @override
  Stream<Map<String, dynamic>> get messages => ctl.stream;
  @override
  bool get closed => isClosed;
  @override
  void send(double vx, double wz) => sent.add(<double>[vx, wz]);
  @override
  void release() => released = true;
  @override
  Future<void> close() async => isClosed = true;
}

class FakeApi implements SiteApi {
  FakeApi(String role, {Map<String, dynamic>? robotView})
      : session = SiteSession('tok', 'u', role),
        view = robotView ?? siteFixture('site_robot');
  @override
  SiteSession? session;
  Map<String, dynamic> view;

  /// 给了就从第二次起换成它（模拟页面上的旧状态、站点上的新任务）。
  Map<String, dynamic>? laterView;
  int robotCalls = 0;
  int robotsCalls = 0;
  SiteError? patrolError;
  SiteError? robotsError;
  final List<String> calls = <String>[];
  StreamController<Map<String, dynamic>> sse = StreamController.broadcast();
  int eventsOpened = 0;

  @override
  Future<SiteSession> login(String n, String p) async => session!;
  @override
  Future<void> logout() async {}
  @override
  Future<List<Map<String, dynamic>>> robots() async {
    robotsCalls++;
    if (robotsError != null) {
      if (robotsError!.status == 401) session = null;
      throw robotsError!;
    }
    return robotsOverride ??
        (siteFixture('site_robots')['robots'] as List).cast<Map<String, dynamic>>();
  }

  /// 给了就用它当狗列表（比如一台没报能力的狗）。
  List<Map<String, dynamic>>? robotsOverride;
  @override
  Future<Map<String, dynamic>> robot(String id) async {
    robotCalls++;
    return robotCalls > 1 && laterView != null ? laterView! : view;
  }
  @override
  Future<Map<String, dynamic>> patrol(String id, String m) async {
    calls.add('patrol $id $m');
    if (patrolError != null) throw patrolError!;
    return siteFixture('site_dispatch');
  }
  @override
  Future<Map<String, dynamic>> abort(String id, String t) async {
    calls.add('abort $id $t');
    return {'ack': {'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> returnToStandby(String id) async {
    calls.add('standby $id');
    return {'ack': {'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> schedule() async => siteFixture('site_schedule');
  @override
  Future<List<Map<String, dynamic>>> incidents() async =>
      (siteFixture('site_incidents')['incidents'] as List).cast<Map<String, dynamic>>();
  /// 录包轨迹（W00c6h）：一次回一条；[trailSince] 记下每次的 since。回完了就说「没在录」。
  List<Map<String, dynamic>> trailReplies = <Map<String, dynamic>>[];
  final List<int> trailSince = <int>[];
  @override
  Future<Map<String, dynamic>> mappingTrail(String robotId, {int since = 0}) async {
    trailSince.add(since);
    if (trailReplies.isEmpty) {
      return <String, dynamic>{'points': <dynamic>[], 'since': since, 'total': since,
        'full': false, 'recording': false};
    }
    return trailReplies.removeAt(0);
  }

  /// 地图预览（W00c6h）。[previewMissing]：这张图没有栅格（站点回 404）。
  bool previewMissing = false;
  @override
  Future<Map<String, dynamic>> mapPreview(String mapId, String version) async {
    calls.add('mapPreview $mapId:$version');
    if (previewMissing) {
      throw SiteError(404, '$mapId:$version 没有栅格(.yaml + .pgm),没法预览');
    }
    return <String, dynamic>{'width': 200, 'height': 100, 'm_per_px': 0.1, 'left_x': -10.0,
      'top_y': 5.0, 'standby': <dynamic>[
        {'robot_id': 'A', 'name': 'dock', 'x': 1.5, 'y': 2.0, 'yaw': 0.5, 'default': true},
      ]};
  }

  @override
  Future<Uint8List> mapPreviewPng(String mapId, String version) async {
    calls.add('mapPreviewPng $mapId:$version');
    return fakePreviewPng;
  }

  /// 建图进程日志（W00c6g）。给了列表就用它；给了错误就抛。
  List<Map<String, dynamic>>? procLogRows;
  SiteError? procLogError;
  @override
  Future<Map<String, dynamic>> procLogs(String robotId) async {
    calls.add('procLogs $robotId');
    return <String, dynamic>{
      'logs': procLogRows ??
          <Map<String, dynamic>>[
            {'name': 'slam', 'size': 5 * 1024 * 1024, 'mtime_ms': 1790400000000},
            {'name': 'bagrecord', 'size': 900, 'mtime_ms': 1790300000000},
          ],
    };
  }

  /// 给了就用它当尾巴；[procLogGate] 给了就等它完成再回（模拟慢网）。
  String? procLogText;
  Completer<void>? procLogGate;
  @override
  Future<Map<String, dynamic>> procLog(String robotId, String name, {int bytes = 0}) async {
    calls.add('procLog $robotId $name');
    if (procLogGate != null) await procLogGate!.future;
    if (procLogError != null) throw procLogError!;
    return <String, dynamic>{'name': name, 'size': 5 * 1024 * 1024, 'bytes': 65000,
      'truncated': true, 'text': procLogText ?? '[slam_toolbox] 回环 12 次\n[slam_toolbox] 存图\n'};
  }

  /// 告警（W00c5a）。给了就用它，不给用真站点的夹具。
  List<Map<String, dynamic>>? alertRows;

  /// 给了就原样交给 `alertsFromWire`（模拟站点回了一份读不懂的东西）。
  Map<String, dynamic>? alertsBody;
  SiteError? alertsError;
  SiteError? summaryError;
  int alertsCalls = 0;
  /// 给了就等它完成再回（模拟还没读回来）。
  Completer<void>? alertsGate;
  @override
  Future<List<Alert>> alerts({bool all = false}) async {
    alertsCalls++;
    if (alertsGate != null) await alertsGate!.future;
    if (alertsError != null) throw alertsError!;
    return alertsFromWire(alertsBody ??
        (alertRows != null ? <String, dynamic>{'alerts': alertRows} : siteFixture('site_alerts')));
  }
  @override
  Future<Map<String, dynamic>> ackAlert(String key) async {
    calls.add('ack $key');
    return siteFixture('site_alert_ack');
  }
  @override
  Future<Map<String, dynamic>> resolveAlert(String key) async {
    calls.add('resolve $key');
    return siteFixture('site_alert_ack');
  }
  /// 给了就用它，不给用真站点的夹具。
  Map<String, dynamic>? summaryBody;
  @override
  Future<Map<String, dynamic>> watchSummary() async {
    if (summaryError != null) throw summaryError!;
    return summaryBody ?? siteFixture('site_watch_summary');
  }
  /// 遥控（W00c5c）。
  FakeTeleopLink? link;
  SiteError? teleopError;
  String? lastTakeover;
  /// 给了就等它完成再回连接（模拟连接、授权还在路上）。
  Completer<void>? teleopGate;
  @override
  Future<TeleopLink> teleop(String robotId, {String takeoverReason = ''}) async {
    lastTakeover = takeoverReason;
    if (teleopGate != null) await teleopGate!.future;
    if (teleopError != null) throw teleopError!;
    return link = FakeTeleopLink();
  }
  /// 运行记录（W00c5d）：真站点的夹具。
  SiteError? runsError;
  @override
  Future<List<Map<String, dynamic>>> runs({String? robotId}) async {
    calls.add('runs ${robotId ?? '*'}');
    if (runsError != null) throw runsError!;
    return (siteFixture('site_runs')['runs'] as List).cast<Map<String, dynamic>>();
  }
  @override
  Future<Map<String, dynamic>> run(int id) async {
    calls.add('run $id');
    return siteFixture('site_run');
  }
  /// 前几次拿照片失败（模拟 4G 抖）。
  int photoErrors = 0;
  @override
  Future<Uint8List> runPhoto(int id, String name) async {
    calls.add('photo $id $name');
    if (photoErrors > 0) {
      photoErrors--;
      throw const SiteError(0, '连不上站点');
    }
    return onePixelPng;
  }
  @override
  Future<Map<String, dynamic>> maps() async => siteFixture('site_maps');
  @override
  Future<Map<String, dynamic>> releases() async => siteFixture('site_releases');
  @override
  Future<Map<String, dynamic>> releaseAction(String robotId, String action,
      {String name = ''}) async {
    calls.add('release $robotId $action $name');
    return <String, dynamic>{'ack': <String, dynamic>{'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> activateMap(String robotId, String mapId, String version) async {
    calls.add('map $robotId $mapId:$version');
    return <String, dynamic>{'ack': <String, dynamic>{'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> mapping(String robotId, String action, {String name = ''}) async {
    calls.add('mapping $robotId $action $name');
    return <String, dynamic>{'ack': <String, dynamic>{'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> buildMap(
      String robotId, String bag, String mapId, String version) async {
    calls.add('build $robotId $bag $mapId:$version');
    return <String, dynamic>{'ack': <String, dynamic>{'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> judgeRun(int id) async {
    calls.add('judge $id');
    return <String, dynamic>{'findings': <dynamic>[]};
  }
  @override
  Future<Map<String, dynamic>> reviewPhoto(int id, String photo, String verdict, String note) async {
    calls.add('review $id $photo $verdict $note');
    return <String, dynamic>{'photo': photo, 'verdict': verdict, 'reviewed': 1};
  }
  @override
  Future<Map<String, dynamic>> halt(String robotId) async {
    calls.add('halt $robotId');
    return <String, dynamic>{'ack': <String, dynamic>{'result': 'accepted'}};
  }
  @override
  Future<Map<String, dynamic>> resume(String robotId) async {
    calls.add('resume $robotId');
    return <String, dynamic>{'robot_id': robotId, 'was_held': true};
  }
  /// 监护心跳（W00c6i）。给了就按它回（模拟狗不接），不给回收下；[superviseGate] 给了就等它
  /// 完成再回（模拟慢网）；[superviseSeqs] 记下每次的（动作、会话、序号）。
  Map<String, dynamic>? superviseAck;
  SiteError? superviseError;
  Completer<void>? superviseGate;
  final List<(String, String, int)> superviseSeqs = <(String, String, int)>[];
  @override
  Future<Map<String, dynamic>> supervise(String robotId, String action,
      {required String session, required int seq}) async {
    calls.add('supervise $robotId $action');
    superviseSeqs.add((action, session, seq));
    if (superviseGate != null && action == 'renew') await superviseGate!.future;
    if (superviseError != null && action == 'renew') throw superviseError!;
    return <String, dynamic>{'robot_id': robotId,
      'ack': superviseAck ?? <String, dynamic>{'result': 'accepted'}};
  }

  @override
  Future<Map<String, dynamic>> markHome(String robotId,
      {String name = '', bool replace = false}) async {
    calls.add('markHome $robotId${name.isEmpty ? '' : ' $name'}${replace ? ' replace' : ''}');
    return <String, dynamic>{
      'ack': <String, dynamic>{
        'result': 'accepted',
        'data': <String, dynamic>{'map_id': 'm', 'map_version': '1', 'x': 1.5, 'y': -2.0,
          'yaw': 0.0, 'sigma_m': 0.2},
      },
      'standby': <String, dynamic>{'name': 'home', 'default': true},
    };
  }

  @override
  Future<Map<String, dynamic>> relocalize(String robotId,
      {bool atHome = false, double x = 0, double y = 0, double yaw = 0}) async {
    calls.add(atHome ? 'relocalize $robotId home' : 'relocalize $robotId $x $y $yaw');
    return <String, dynamic>{'ack': <String, dynamic>{'result': 'accepted'}};
  }

  /// 视频（W00c5b）。
  int videoHealthCalls = 0;
  @override
  Future<Map<String, dynamic>> videoHealth(String robotId) async {
    videoHealthCalls++;
    return siteFixture('site_video_health');
  }
  @override
  String get baseUrl => 'https://127.0.0.1:1';
  @override
  HttpClient pinnedClient() => HttpClient();

  @override
  Stream<Map<String, dynamic>> events() {
    eventsOpened++;
    return sse.stream;
  }
  @override
  void close() {}
}

/// 狗正在跑任务时的视图：真站点取的（`site_robot_busy.json`）。
Map<String, dynamic> busyView() => siteFixture('site_robot_busy');

String busyTaskId() =>
    ((busyView()['status'] as Map)['task'] as Map)['task_id'] as String;

Future<void> openRobot(WidgetTester t, FakeApi api) async {
  await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
  await t.pumpAndSettle();
  await t.tap(find.byKey(const Key('robot-A')));
  await t.pumpAndSettle();
}

void main() {
  test('画面开流看的是狗在不在线（新鲜），不是画面健康 —— 不然两头互相等，一次都起不来', () async {
    final api = FakeApi('owner'); // 画面健康夹具里两路都是 offline，狗的视图是新鲜的
    final p = SiteVideoHealthPoller(
        api: api, robotId: 'A', period: const Duration(milliseconds: 50));
    final h = await p.stream.first;
    p.close();
    expect(h.online, <String, bool>{'front': true, 'back': true});
    expect(api.videoHealthCalls, 0, reason: '开流的判据不许是画面健康');
    final stale = FakeApi('owner', robotView: <String, dynamic>{...siteFixture('site_robot'), 'fresh': false});
    final p2 = SiteVideoHealthPoller(
        api: stale, robotId: 'A', period: const Duration(milliseconds: 50));
    final h2 = await p2.stream.first;
    p2.close();
    expect(h2.anyLive, isFalse, reason: '狗不新鲜就不去拉');
  });

  testWidgets('单狗页有画面：前后切换、问站点的画面健康、取流走站点路径与钉证书的客户端', (t) async {
    final api = FakeApi('owner');
    await t.pumpWidget(MaterialApp(home: SiteRobotPage(api: api, robotId: 'A')));
    await t.pump();
    await t.pump(const Duration(milliseconds: 100));
    expect(find.byKey(SiteVideo.cameraKey('front')), findsOneWidget);
    expect(api.robotCalls, greaterThan(1), reason: '开流看狗新不新鲜');
    var lv = t.widget<LiveVideo>(find.byType(LiveVideo));
    expect(lv.streamPath, '/api/robots/A/video/front');
    expect(lv.openClient, isNotNull);
    await t.tap(find.byKey(SiteVideo.cameraKey('back')));
    await t.pump();
    lv = t.widget<LiveVideo>(find.byType(LiveVideo));
    expect(lv.streamPath, '/api/robots/A/video/back');
    await t.pumpWidget(Container());
  });

  testWidgets('狗的列表：来自真站点的夹具，显示在线与任务', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.text('A'), findsOneWidget);
    expect(find.textContaining('在线'), findsOneWidget);
  });

  testWidgets('保安：能派巡检、回待命点、叫停', (t) async {
    final api = FakeApi('guard', robotView: busyView());
    await openRobot(t, api);
    expect(find.byKey(const Key('btn-patrol')), findsOneWidget);
    expect(find.byKey(const Key('btn-standby')), findsOneWidget);
    await t.tap(find.byKey(const Key('btn-abort')));
    await t.pumpAndSettle();
    expect(api.calls, ['abort A ${busyTaskId()}']);
    await t.tap(find.byKey(const Key('btn-patrol')));
    await t.pumpAndSettle();
    await t.tap(find.text('loop'));
    await t.pumpAndSettle();
    expect(api.calls.last, 'patrol A loop');
  });

  testWidgets('业主：只有叫停', (t) async {
    final api = FakeApi('owner', robotView: busyView());
    await openRobot(t, api);
    expect(find.byKey(const Key('btn-patrol')), findsNothing);
    expect(find.byKey(const Key('btn-standby')), findsNothing);
    expect(find.byKey(const Key('btn-abort')), findsOneWidget);
  });

  testWidgets('被叫停了：说清楚，保安有「恢复」，业主没有', (t) async {
    final held = Map<String, dynamic>.of(siteFixture('site_robot'))
      ..['held'] = <String, dynamic>{'by': 'olga', 'at_ms': 1, 'reason': 'operator'};
    final guard = FakeApi('guard', robotView: held);
    await openRobot(t, guard);
    expect(find.byKey(const Key('robot-held')), findsOneWidget);
    expect(find.textContaining('被 olga 叫停了'), findsOneWidget);
    expect(find.textContaining('已叫停'), findsOneWidget);
    await t.tap(find.byKey(const Key('btn-resume')));
    await t.pumpAndSettle();
    expect(guard.calls, contains('resume A'));
    await t.pumpWidget(const SizedBox()); // 把上面那一页连同它的导航栈整个卸掉
    await t.pumpWidget(MaterialApp(
        home: SiteRobotPage(key: UniqueKey(), api: FakeApi('owner', robotView: held), robotId: 'A')));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('robot-held')), findsOneWidget);
    expect(find.byKey(const Key('btn-resume')), findsNothing);
  });

  testWidgets('没在跑任务就没有叫停按钮', (t) async {
    final idle = Map<String, dynamic>.of(siteFixture('site_robot'));
    final st = Map<String, dynamic>.of(idle['status'] as Map<String, dynamic>)..['task'] = null;
    idle['status'] = st;
    await openRobot(t, FakeApi('guard', robotView: idle));
    expect(find.byKey(const Key('btn-abort')), findsNothing);
  });

  testWidgets('打开就是站点列表：没有直连入口（W00c5e）', (t) async {
    await t.pumpWidget(PatrolApp(siteStore: MemorySiteStore()));
    await t.pumpAndSettle();
    expect(find.text('还没有站点。右下角添加。'), findsOneWidget);
    expect(find.byKey(const Key('mode-direct')), findsNothing);
    expect(find.textContaining('直连'), findsNothing);
  });

  testWidgets('添加站点存进去：指纹规范化，口令不存', (t) async {
    final store = MemorySiteStore();
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-add')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-name')), '庄园');
    await t.enterText(find.byKey(const Key('site-url')), 'https://site.local:8443');
    await t.enterText(find.byKey(const Key('site-fp')), 'AB:' * 31 + 'AB');
    await t.enterText(find.byKey(const Key('site-user')), 'gina');
    await t.tap(find.text('保存'));
    await t.pumpAndSettle();
    expect(store.sites.single.fingerprint, 'ab' * 32);
    expect(store.sites.single.toJson().keys, isNot(contains('password')));
  });

  testWidgets('叫停用的是现取的任务号，不是页面上几秒前的', (t) async {
    final later = Map<String, dynamic>.of(busyView());
    final st = Map<String, dynamic>.of(later['status'] as Map<String, dynamic>);
    st['task'] = Map<String, dynamic>.of(st['task'] as Map<String, dynamic>)
      ..['task_id'] = 'task-new';
    later['status'] = st;
    final api = FakeApi('guard', robotView: busyView())..laterView = later;
    await openRobot(t, api);
    await t.tap(find.byKey(const Key('btn-abort')));
    await t.pumpAndSettle();
    expect(api.calls, ['abort A task-new']);
  });

  testWidgets('站点回 403：错误原样显示，不假装成功', (t) async {
    final api = FakeApi('guard')..patrolError = const SiteError(403, 'guard 没有 dispatch 权限');
    await openRobot(t, api);
    await t.tap(find.byKey(const Key('btn-patrol')));
    await t.pumpAndSettle();
    await t.tap(find.text('loop'));
    await t.pumpAndSettle();
    expect(find.textContaining('没成'), findsOneWidget);
    expect(find.textContaining('403'), findsOneWidget);
  });

  testWidgets('事件来了就刷新；事件流断了显示提示并重连', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteRobotsPage(api: api)));
    await t.pumpAndSettle();
    final before = api.robotsCalls;
    api.sse.add(siteFixture('site_event_frame'));
    await t.pump(const Duration(milliseconds: 600));
    expect(api.robotsCalls, greaterThan(before));
    expect(find.byKey(const Key('live-lost')), findsNothing);
    final old = api.sse;
    api.sse = StreamController.broadcast();
    await old.close();
    await t.pump();
    expect(find.byKey(const Key('live-lost')), findsOneWidget);
    await t.pump(const Duration(seconds: 6));
    expect(api.eventsOpened, 2, reason: '5 秒后自己重连');
    await t.pumpWidget(const SizedBox());
  });

  testWidgets('令牌过期（401）退回站点列表并说明', (t) async {
    final store = MemorySiteStore([
      SiteEntry(name: '庄园', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'gina')
    ]);
    final api = FakeApi('guard')..robotsError = const SiteError(401, '没登录或登录已过期');
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store, apiFactory: (_) => api)));
    await t.pumpAndSettle();
    await t.tap(find.text('庄园'));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.tap(find.text('登录'));
    await t.pumpAndSettle();
    expect(find.text('还没有站点。右下角添加。'), findsNothing);
    expect(find.textContaining('重新登录'), findsOneWidget);
  });

  testWidgets('登录失败（比如证书指纹对不上）：提示原因，不进去', (t) async {
    final store = MemorySiteStore([
      SiteEntry(name: '庄园', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'gina')
    ]);
    await t.pumpWidget(MaterialApp(
        home: SiteListPage(store: store, apiFactory: (_) => _FailingApi())));
    await t.pumpAndSettle();
    await t.tap(find.text('庄园'));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-password')), 'pw');
    await t.tap(find.text('登录'));
    await t.pumpAndSettle();
    expect(find.textContaining('指纹对不上'), findsOneWidget);
    expect(find.byType(SiteRobotsPage), findsNothing);
  });

  testWidgets('添加站点：坏指纹不保存', (t) async {
    final store = MemorySiteStore();
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: store)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-add')));
    await t.pumpAndSettle();
    await t.enterText(find.byKey(const Key('site-name')), '庄园');
    await t.enterText(find.byKey(const Key('site-url')), 'https://site.local:8443');
    await t.enterText(find.byKey(const Key('site-fp')), 'abc');
    await t.enterText(find.byKey(const Key('site-user')), 'gina');
    await t.tap(find.text('保存'));
    await t.pumpAndSettle();
    expect(store.sites, isEmpty);
    expect(find.textContaining('没保存'), findsOneWidget);
  });

  testWidgets('站点文件坏了：说出来，不给添加（免得覆盖掉其余站点）；能删站点', (t) async {
    await t.pumpWidget(MaterialApp(home: SiteListPage(store: _BrokenStore())));
    await t.pumpAndSettle();
    expect(find.byKey(const Key('site-load-error')), findsOneWidget);
    expect(find.byKey(const Key('site-add')), findsNothing);
    final store = MemorySiteStore([
      SiteEntry(name: 'a', url: 'https://h:1', fingerprint: 'ab' * 32, username: 'u')
    ]);
    await t.pumpWidget(MaterialApp(home: SiteListPage(key: const Key('fresh'), store: store)));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('site-remove-a')));
    await t.pumpAndSettle();
    expect(store.sites, isEmpty);
  });

  testWidgets('事件页与排程页读的是真站点的形状', (t) async {
    final api = FakeApi('guard');
    await t.pumpWidget(MaterialApp(home: SiteIncidentsPage(api: api)));
    await t.pumpAndSettle();
    expect(find.textContaining('yard'), findsWidgets);
    await t.pumpWidget(MaterialApp(home: SiteSchedulePage(api: api)));
    await t.pumpAndSettle();
    expect(find.textContaining('nightly'), findsOneWidget);
    expect(find.textContaining('estate-kl'), findsOneWidget);
  });
}

class _FailingApi extends FakeApi {
  _FailingApi() : super('guard');
  @override
  Future<SiteSession> login(String n, String p) async =>
      throw const SiteError(0, '站点证书指纹对不上：实际是 cd…');
}

class _BrokenStore implements SiteStore {
  @override
  Future<List<SiteEntry>> load() async => throw const FormatException('站点文件的顶层应该是个数组');
  @override
  Future<void> save(List<SiteEntry> s) async => throw StateError('不许写');

}
