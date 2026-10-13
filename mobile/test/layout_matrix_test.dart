// App V2 的版面：每一页在**竖屏的手机（390×844）和横屏的手机（800×360）**上、**英文和中文**两种文字
// 下都不溢出（RenderFlex overflowed 在测试里是报错）。英文比中文长，竖屏比横屏窄：
// 四种组合都画一遍。测试里的字体每个字都是方块（比真字体宽），过了这里真机上只会更松。
//
// 主题三档（2026-10-14 重做）：新主题的**浅色**、**深色**（App 现在用的，跟随系统），和**不带主题**
// （大多数测试是光秃秃的 MaterialApp，页面在那儿也得画得出来、不抛）。老的专业版主题那一档跟着
// 主题本身一起删了。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/nw/nw_theme.dart';
import 'package:d1max_patrol/ui/site_health.dart';
import 'package:d1max_patrol/ui/site_add.dart';
import 'package:d1max_patrol/ui/site_alert_scene.dart';
import 'package:d1max_patrol/ui/site_cameras.dart';
import 'package:d1max_patrol/ui/site_deter.dart';
import 'package:d1max_patrol/ui/site_deterrence.dart';
import 'package:d1max_patrol/ui/site_intercepts.dart';
import 'package:d1max_patrol/ui/site_logs.dart';
import 'package:d1max_patrol/ui/site_maps.dart';
import 'package:d1max_patrol/ui/site_mode.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:d1max_patrol/ui/site_recordings.dart';
import 'package:d1max_patrol/ui/site_releases.dart';
import 'package:d1max_patrol/ui/site_runs.dart';
import 'package:d1max_patrol/ui/site_standby.dart';
import 'package:d1max_patrol/ui/site_watch_page.dart';
import 'package:d1max_patrol/ui/site_weather.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi, busyView;

const _portrait = Size(390, 844);
const _landscape = Size(800, 360);

Alert _p1() => Alert.fromWire(<String, dynamic>{
      'key': 'A/dog_sees_person#1', 'level': 'P1', 'kind': 'dog_sees_person', 'robot': 'A',
      'title': '狗看见人', 'detail': '前方 6.2 m', 'first_ms': 1800000000000, 'last_ms': 1800000000000,
      'context': <String, dynamic>{'zone': 'East wall'}});

/// 一页一行：名字 + 怎么造。每个组合现造一份（页面有状态，不能复用）。
final _pages = <String, Widget Function()>{
  'site list (empty)': () => SiteListPage(store: MemorySiteStore()),
  'site list': () => SiteListPage(store: MemorySiteStore([
        SiteEntry(name: 'East Gate Estate', url: 'https://192.168.100.200:8443',
            fingerprint: 'ab' * 32, username: 'night.guard')
      ])),
  'add site': () => SiteAddPage(scanner: (_) async => null),
  'login': () => SiteLoginPage(
      site: SiteEntry(name: 'East Gate Estate', url: 'https://192.168.100.200:8443',
          fingerprint: 'ab' * 32, username: 'night.guard'),
      signIn: (_) async => FakeApi('guard')),
  'shell (admin)': () => NwShell(api: FakeApi('admin'), title: 'East Gate Estate'),
  'shell (owner)': () => NwShell(api: FakeApi('owner'), title: 'East Gate Estate'),
  'robots': () => SiteRobotsPage(api: FakeApi('admin'), title: 'East Gate Estate'),
  'robot (admin, busy)': () => SiteRobotPage(api: FakeApi('admin', robotView: busyView()), robotId: 'A'),
  'robot (guard)': () => SiteRobotPage(api: FakeApi('guard'), robotId: 'A'),
  'robot (owner)': () => SiteRobotPage(api: FakeApi('owner'), robotId: 'A'),
  'watch': () => SiteWatchPage(api: FakeApi('guard')),
  'alarm scene': () => SiteAlertScenePage(api: FakeApi('guard'), alert: _p1()),
  'map tab': () => Scaffold(body: SiteMapTab(api: FakeApi('admin'))),
  'maps': () => SiteMapsPage(api: FakeApi('admin')),
  'releases': () => SiteReleasesPage(api: FakeApi('admin')),
  'runs': () => SiteRunsPage(api: FakeApi('admin')),
  'run': () => SiteRunPage(api: FakeApi('admin'), runId: 1),
  'incidents': () => SiteIncidentsPage(api: FakeApi('admin')),
  'schedule': () => SiteSchedulePage(api: FakeApi('admin')),
  'process logs': () => SiteProcLogsPage(api: FakeApi('admin'), robotId: 'A'),
  'standby': () => SiteStandbyPage(api: FakeApi('admin'), robotId: 'A', canMarkHere: true),
  'intercepts': () => SiteInterceptsPage(api: FakeApi('admin')),
  'mode': () => SiteModePage(api: FakeApi('admin')),
  'health': () => SiteHealthPage(api: FakeApi('admin'), canShare: true, save: (n, b) async => '/x/\$n'),
  'weather': () => SiteWeatherPage(api: FakeApi('admin')),
  'deterrence settings': () => SiteDeterrencePage(api: FakeApi('admin'), robotId: 'A'),
  'deter': () => SiteDeterPage(api: FakeApi('guard'), robotId: 'A',
      caps: const <String, dynamic>{'light': true, 'siren': true, 'tts': true}),
  'cameras': () => SiteCamerasPage(api: FakeApi('admin')),
  'recordings': () => SiteRecordingsPage(api: FakeApi('guard'), robotId: 'A'),
};

/// 主题三档：名字 + 主题（null = 不带，MaterialApp 自己的默认主题）。
final _themes = <String, ThemeData? Function()>{
  'light': () => nwTheme(Brightness.light),
  'dark': () => nwTheme(Brightness.dark),
  'no theme': () => null,
};

void main() {
  for (final th in _themes.entries) {
    for (final lang in AppLang.values) {
      for (final size in const [_portrait, _landscape]) {
        final where = '${th.key} ${lang.name} ${size.width.toInt()}×${size.height.toInt()}';
        for (final e in _pages.entries) {
          testWidgets('不溢出 [$where] ${e.key}', (t) async {
            appLang.value = lang;
            addTearDown(() => appLang.value = AppLang.zh);
            t.view.physicalSize = size * 2;
            t.view.devicePixelRatio = 2.0;
            addTearDown(t.view.reset);
            await t.pumpWidget(MaterialApp(theme: th.value(), home: e.value()));
            await t.pumpAndSettle();
            expect(t.takeException(), isNull);
            await t.pumpWidget(Container());
          });
        }
      }
    }
  }
}
