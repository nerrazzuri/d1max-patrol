// W42 机器人绑定（手机这头）：读贴纸、WiFi 规矩、添加机器人向导（连热点、发给机器人、核对码、离开热点去批准）、
// 连不上热点教人手动连、换 WiFi（贴纸要是这台的、站点上确认）、工程师用解绑令解绑、列表顶上「等待批准加入」、
// 机器人详情里确认换 WiFi。机器人、热点、相机都是假的。
import 'dart:convert';

import 'package:d1max_patrol/net/robot_bind.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/robot_bind_page.dart';
import 'package:d1max_patrol/ui/site_robot.dart';
import 'package:d1max_patrol/ui/site_robots.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

String sticker({String serial = 'D1M-1', String? addr}) {
  final d = <String, String>{'s': serial, 'c': '7KQ27M9XD4HTPR8W', 'w': 'NW-1', 'p': 'hotspotpw1234567',
    'f': '0123456789abcdef0123456789abcdef', 'a': ?addr};
  return 'D1MAXBOT1.${base64Url.encode(utf8.encode(jsonEncode(d))).replaceAll('=', '')}';
}

const site = SiteEntry(name: '庄园', url: 'https://192.168.1.10:8443', fingerprint: 'ab', username: 'u');

class FakeJoiner implements HotspotJoiner {
  bool auto;
  final List<String> calls = <String>[];
  FakeJoiner({this.auto = true});
  @override
  Future<bool> join(String ssid, String psk) async {
    calls.add('join $ssid $psk');
    return auto;
  }

  @override
  Future<void> leave() async => calls.add('leave');
}

class FakeLink implements RobotBindLink {
  Map<String, dynamic> st = <String, dynamic>{'serial': 'D1M-1', 'state': 'unbound', 'phase': '', 'error': ''};
  final List<String> calls = <String>[];
  RobotBindError? fail;

  @override
  Future<Map<String, dynamic>> status() async {
    calls.add('status');
    return st;
  }

  @override
  Future<Map<String, dynamic>> bind(
      {required String ssid, required String psk, required String siteUrl,
      required String siteFingerprint, required String siteName}) async {
    calls.add('bind $ssid $psk $siteUrl $siteName');
    if (fail != null) throw fail!;
    st = <String, dynamic>{...st, 'state': 'pending', 'phase': 'connecting'};
    return st;
  }

  @override
  Future<Map<String, dynamic>> wifi(String ssid, String psk) async {
    calls.add('wifi $ssid');
    return <String, dynamic>{...st, 'change': <String, dynamic>{'id': '0a1b2c3d4e5f', 'ssid': ssid, 'mode': 'online'}};
  }

  @override
  Future<Map<String, dynamic>> unbind(String token) async {
    calls.add('unbind $token');
    return <String, dynamic>{...st, 'state': 'wiping'};
  }

  @override
  Future<Map<String, dynamic>> cancel() async {
    calls.add('cancel');
    return st;
  }

  @override
  void close() {}
}

Widget host(Widget w) => MaterialApp(home: w);

void main() {
  group('读贴纸、WiFi 规矩', () {
    test('贴纸：来回一致；前缀不对、指纹形状不对、地址乱写都拒', () {
      final s = parseSticker(sticker());
      expect((s.serial, s.ssid, s.addr), ('D1M-1', 'NW-1', defaultBindAddr));
      expect(parseSticker(sticker(addr: '127.0.0.2:18450')).addr, '127.0.0.2:18450');
      expect(() => parseSticker('D1MAXSITE1.abc'), throwsFormatException);
      expect(() => parseSticker(sticker(addr: 'x y')), throwsFormatException);
      expect(() => parseSticker(sticker(serial: '有空格 1')), throwsFormatException);
    });

    test('手输：序列号 + 绑定码，热点名算出来、密码统一、没有指纹', () {
      final s = manualSticker('D1M-000123', '7kq2-m9xd-4htp-r8wo');
      expect((s.ssid, s.psk, s.fingerprint, s.code), ('NW-000123', hotspotPsk, '', '7KQ2M9XD4HTPR8W0'));
      expect(hotspotSsid('AB'), 'NW-AB');
      expect(() => manualSticker('有空格 1', '7KQ27M9XD4HTPR8W'), throwsFormatException);
      expect(() => manualSticker('..', '7KQ27M9XD4HTPR8W'), throwsFormatException);
      expect(() => manualSticker('D1M-1', '123'), throwsFormatException);
      expect(() => manualSticker('D1M-1', 'UUUUUUUUUUUUUUUU'), throwsFormatException);
    });

    test('WiFi：名字 1–32 字节；密码空、8–63 个可见字符、64 位十六进制', () {
      expect(checkWifi('Estate', ''), isNull);
      expect(checkWifi('庄园', '12345678'), isNull);
      expect(checkWifi('x', 'A1' * 32), isNull);
      expect(checkWifi('', '12345678'), isNotNull);
      expect(checkWifi('x' * 33, ''), isNotNull);
      expect(checkWifi('x', '1234567'), isNotNull);
      expect(checkWifi('x', '密码密码密码密码'), isNotNull);
    });
  });

  group('添加机器人', () {
    testWidgets('扫贴纸、填 WiFi、连热点发给机器人、显示核对码、离开热点去批准', (t) async {
      final api = FakeApi('admin');
      final link = FakeLink();
      final joiner = FakeJoiner();
      await t.pumpWidget(host(RobotAddPage(api: api, site: site, scanner: (_) async => sticker(),
          joiner: joiner, link: (_) => link, pollEvery: const Duration(milliseconds: 100))));
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      expect(find.textContaining('D1M-1'), findsOneWidget);
      await t.enterText(find.byKey(const Key('bind-ssid')), 'Estate');
      await t.enterText(find.byKey(const Key('bind-psk')), '123');
      await t.tap(find.byKey(const Key('bind-send')));
      await t.pump();
      expect(link.calls, isEmpty, reason: 'WiFi 密码不对，不发');
      await t.enterText(find.byKey(const Key('bind-psk')), '12345678');
      await t.tap(find.byKey(const Key('bind-send')));
      await t.pumpAndSettle();
      expect(joiner.calls, ['join NW-1 hotspotpw1234567']);
      expect(link.calls.take(2), ['status', 'bind Estate 12345678 https://192.168.1.10:8443 庄园']);
      expect(link.calls.skip(2).toSet(), {'status'}, reason: '之后一直问状态');
      expect(find.byKey(const Key('bind-phase')), findsOneWidget);
      link.st = <String, dynamic>{...link.st, 'phase': 'waiting', 'check': '042917'};
      await t.pump(const Duration(milliseconds: 150));
      await t.pump();
      expect(find.text('042917'), findsOneWidget);
      api.binds = <Map<String, dynamic>>[
        <String, dynamic>{'id': 'ab12', 'serial': 'D1M-1', 'check': '042917', 'source': '192.168.1.20'}
      ];
      await t.tap(find.byKey(const Key('bind-leave')));
      await t.pumpAndSettle();
      expect(joiner.calls.last, 'leave');
      expect(find.byKey(const Key('approve-check')), findsOneWidget);
      await t.enterText(find.byKey(const Key('approve-pw')), 'pw');
      await t.tap(find.byKey(const Key('approve-ok')));
      await t.pumpAndSettle();
      expect(api.calls, contains('approveBind ab12 pw '));
      expect(find.byKey(const Key('bind-done')), findsOneWidget);
    });

    testWidgets('已经绑在别的站点：不发', (t) async {
      final link = FakeLink()..st = <String, dynamic>{'state': 'bound'};
      await t.pumpWidget(host(RobotAddPage(api: FakeApi('guard'), site: site, scanner: (_) async => sticker(),
          joiner: FakeJoiner(), link: (_) => link)));
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('bind-ssid')), 'Estate');
      await t.tap(find.byKey(const Key('bind-send')));
      await t.pumpAndSettle();
      expect(link.calls, ['status']);
      expect(find.textContaining('先在原来的站点上解绑'), findsOneWidget);
    });

    testWidgets('自动连不上热点：摆出热点名和密码，人手动连好了接着发', (t) async {
      final link = FakeLink();
      await t.pumpWidget(host(RobotAddPage(api: FakeApi('guard'), site: site, scanner: (_) async => sticker(),
          joiner: FakeJoiner(auto: false), link: (_) => link)));
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('bind-ssid')), 'Estate');
      await t.tap(find.byKey(const Key('bind-send')));
      await t.pumpAndSettle();
      expect(find.textContaining('NW-1'), findsWidgets);
      expect(find.textContaining('hotspotpw1234567'), findsOneWidget);
      await t.tap(find.byKey(const Key('hotspot-joined')));
      await t.pumpAndSettle();
      expect(link.calls.last, startsWith('bind Estate'));
    });

    testWidgets('外审 I12：手动连的热点，离开时教人切回去；站点连不上给「去批准」再试', (t) async {
      final api = FakeApi('admin');
      final link = FakeLink();
      await t.pumpWidget(host(RobotAddPage(api: api, site: site, scanner: (_) async => sticker(),
          joiner: FakeJoiner(auto: false), link: (_) => link, pollEvery: const Duration(milliseconds: 100))));
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('bind-ssid')), 'Estate');
      await t.tap(find.byKey(const Key('bind-send')));
      await t.pumpAndSettle();
      await t.tap(find.byKey(const Key('hotspot-joined')));
      await t.pumpAndSettle();
      link.st = <String, dynamic>{...link.st, 'phase': 'waiting', 'check': '042917'};
      await t.pump(const Duration(milliseconds: 150));
      await t.pump();
      await t.tap(find.byKey(const Key('bind-leave')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('hotspot-left')), findsOneWidget, reason: '手动连的：要人自己切回去');
      await t.tap(find.byKey(const Key('hotspot-left')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('bind-approve-error')), findsOneWidget, reason: '站点上还没看到申请');
      api.binds = <Map<String, dynamic>>[
        <String, dynamic>{'id': 'ab12', 'serial': 'D1M-1', 'check': '042917', 'source': 'x'}
      ];
      await t.ensureVisible(find.byKey(const Key('bind-approve-retry')));
      await t.tap(find.byKey(const Key('bind-approve-retry')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('approve-check')), findsOneWidget);
    });

    testWidgets('桌面版没相机：粘贴贴纸码', (t) async {
      await t.pumpWidget(host(RobotAddPage(api: FakeApi('guard'), site: site,
          joiner: FakeJoiner(), link: (_) => FakeLink())));
      await t.tap(find.byKey(const Key('bind-paste')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('sticker-code')), sticker());
      await t.tap(find.byKey(const Key('sticker-code-ok')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('bind-serial')), findsOneWidget);
    });

    testWidgets('手输序列号和绑定码', (t) async {
      final link = FakeLink();
      RobotSticker? got;
      await t.pumpWidget(host(RobotAddPage(api: FakeApi('guard'), site: site,
          joiner: FakeJoiner(), link: (s) { got = s; return link; })));
      await t.tap(find.byKey(const Key('bind-manual')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('manual-serial')), 'D1M-000123');
      await t.enterText(find.byKey(const Key('manual-code')), '7KQ2-M9XD-4HTP-R8WC');
      await t.tap(find.byKey(const Key('manual-ok')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('bind-serial')), findsOneWidget);
      await t.enterText(find.byKey(const Key('bind-ssid')), 'Estate');
      await t.tap(find.byKey(const Key('bind-send')));
      await t.pumpAndSettle();
      expect(got?.ssid, 'NW-000123');
      expect(link.calls.take(2).last, startsWith('bind Estate'));
    });

    testWidgets('不是机器人贴纸：说清楚', (t) async {
      await t.pumpWidget(host(RobotAddPage(api: FakeApi('guard'), site: site,
          scanner: (_) async => 'D1MAXSITE1.xx', joiner: FakeJoiner(), link: (_) => FakeLink())));
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      expect(find.textContaining('不是机器人身上的贴纸码'), findsOneWidget);
    });
  });

  group('换 WiFi、工程师解绑', () {
    testWidgets('贴纸要是这台的；发给机器人，在线时站点上确认；经站点看到最后怎样了', (t) async {
      final api = FakeApi('admin');
      final link = FakeLink();
      var serial = 'D1M-2';
      await t.pumpWidget(host(RobotWifiPage(api: api, robotId: 'D1M-1', scanner: (_) async => sticker(serial: serial),
          joiner: FakeJoiner(), link: (_) => link, watchEvery: const Duration(milliseconds: 100))));
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      expect(find.textContaining('不是 D1M-1'), findsOneWidget);
      serial = 'D1M-1';
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('wifi-ssid')), 'New');
      await t.enterText(find.byKey(const Key('wifi-psk')), 'abcdefgh');
      await t.tap(find.byKey(const Key('wifi-send')));
      await t.pumpAndSettle();
      expect(link.calls, ['wifi New']);
      await t.tap(find.byKey(const Key('wifi-confirm')));
      await t.pumpAndSettle();
      expect(api.calls, contains('confirmRobotWifi D1M-1 0a1b2c3d4e5f true'));
      api.view = <String, dynamic>{...api.view,
        'wifi': <String, dynamic>{'ssid': 'Estate', 'link': 'wifi', 'change': null,
          'result': <String, dynamic>{'id': '0a1b2c3d4e5f', 'result': 'unreachable'}}};
      await t.pump(const Duration(milliseconds: 150));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('wifi-result')), findsOneWidget);
      expect(find.textContaining('退回原来的了'), findsOneWidget);
      await t.tap(find.byKey(const Key('wifi-again')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('wifi-ssid')), findsOneWidget, reason: '没换成：回到表单再改');
    });

    testWidgets('工程师：解绑令 + 贴纸 → 机器人清机', (t) async {
      final link = FakeLink();
      await t.pumpWidget(host(EngineerUnbindPage(scanner: (_) async => sticker(), joiner: FakeJoiner(),
          link: (_) => link)));
      await t.enterText(find.byKey(const Key('unbind-token')), 'D1MAXUNBIND1.a.b');
      await t.tap(find.byKey(const Key('bind-scan')));
      await t.pumpAndSettle();
      await t.ensureVisible(find.byKey(const Key('unbind-send')));
      await t.tap(find.byKey(const Key('unbind-send')));
      await t.pumpAndSettle();
      expect(link.calls, ['unbind D1MAXUNBIND1.a.b']);
      expect(find.byKey(const Key('unbind-done')), findsOneWidget);
    });
  });

  group('站点上', () {
    testWidgets('列表顶上「等待批准加入」：管理员看得到、批得了；保安看不到', (t) async {
      final api = FakeApi('admin')
        ..binds = <Map<String, dynamic>>[
          <String, dynamic>{'id': 'ab12', 'serial': 'D1M-7', 'check': '111222', 'source': 'x'}
        ];
      await t.pumpWidget(host(SiteRobotsPage(api: api, title: 'S')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('bind-req-D1M-7')), findsOneWidget);
      await t.tap(find.byKey(const Key('bind-reject-D1M-7')));
      await t.pumpAndSettle();
      expect(api.calls, contains('rejectBind ab12'));
      expect(find.byKey(const Key('bind-req-D1M-7')), findsNothing);

      final guard = FakeApi('guard')..binds = api.binds;
      await t.pumpWidget(host(SiteRobotsPage(api: guard, title: 'S')));
      await t.pumpAndSettle();
      expect(guard.calls.where((c) => c == 'bindRequests'), isEmpty);
    });

    testWidgets('批准密码不对：对话框不关、说密码不对', (t) async {
      final api = FakeApi('admin')..approveError = const SiteError(403, '密码不对');
      await t.pumpWidget(host(Builder(builder: (c) => TextButton(
          onPressed: () => approveBindDialog(c, api, <String, dynamic>{'id': 'ab12', 'serial': 'D1M-7', 'check': '1'}),
          child: const Text('go')))));
      await t.tap(find.text('go'));
      await t.pumpAndSettle();
      await t.enterText(find.byKey(const Key('approve-pw')), 'x');
      await t.tap(find.byKey(const Key('approve-ok')));
      await t.pumpAndSettle();
      expect(find.byKey(const Key('approve-error')), findsOneWidget);
    });

    testWidgets('机器人详情：显示 WiFi、走哪条线；有改动等确认，管理员确认', (t) async {
      final api = FakeApi('admin');
      api.view = <String, dynamic>{
        ...api.view,
        'wifi': <String, dynamic>{'ssid': 'Estate', 'link': '4g',
          'change': <String, dynamic>{'id': '0a1b2c3d4e5f', 'ssid': 'New', 'mode': 'trial', 'expires_ms': 1}},
      };
      await t.pumpWidget(host(SiteRobotPage(api: api, robotId: 'A')));
      await t.pumpAndSettle();
      expect(find.textContaining('正在试 WiFi「New」'), findsOneWidget);
      await t.tap(find.byKey(const Key('btn-wifi-approve')));
      await t.pumpAndSettle();
      expect(api.calls, contains('confirmRobotWifi A 0a1b2c3d4e5f true'));
      expect(robotWifiLine(<String, dynamic>{'ssid': 'Estate', 'link': '4g'}), 'Estate · 走 4G');
    });
  });
}
