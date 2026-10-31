// W55 设置页（用户 2026-10-31）：设置类的集中在一页 —— 语言、后台值守、P1 推送、App 版本、检查更新、开源许可；
// 站点列表进来的多一组工程师工具、没有值守和检查更新；站点里换语言先问（会回到站点列表）。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/net/app_update.dart';
import 'package:d1max_patrol/net/background_watch.dart';
import 'package:d1max_patrol/net/site_client.dart';
import 'package:d1max_patrol/ui/app_settings.dart';
import 'package:d1max_patrol/ui/nw/nw_shell.dart';
import 'package:d1max_patrol/ui/site_tabs.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'site_page_test.dart' show FakeApi;

Future<void> _show(WidgetTester t, Widget w) async {
  appLang.value = AppLang.zh;
  await t.binding.setSurfaceSize(const Size(500, 1200));
  addTearDown(() => t.binding.setSurfaceSize(null));
  await t.pumpWidget(MaterialApp(home: w));
  await t.pumpAndSettle();
}

void main() {
  tearDown(() => appLang.value = AppLang.en);

  testWidgets('站点列表进来：语言、版本、许可、工程师工具；没有值守、检查更新', (t) async {
    await _show(t, const AppSettingsPage());
    expect(find.byKey(AppSettingsPage.langKey), findsOneWidget);
    expect(find.textContaining('$appVersion（构建 $appBuild）'), findsOneWidget);
    expect(find.byKey(AppSettingsPage.licensesKey), findsOneWidget);
    expect(find.byKey(AppSettingsPage.unbindKey), findsOneWidget);
    expect(find.byKey(AppSettingsPage.watchKey), findsNothing);
    expect(find.byKey(AppSettingsPage.checkUpdateKey), findsNothing);
  });

  testWidgets('站点里：后台值守开关点的是机器人栏那个开关；没有工程师工具', (t) async {
    final ctl = WatchControl();
    var toggled = 0;
    ctl.bind(() async {
      toggled++;
      ctl.report(on: !ctl.on, busy: false);
    });
    ctl.report(on: true, busy: false);
    await _show(t, AppSettingsPage(api: FakeApi('guard'), watch: ctl));
    expect(t.widget<SwitchListTile>(find.byKey(AppSettingsPage.watchKey)).value, isTrue);
    await t.tap(find.byKey(AppSettingsPage.watchKey));
    await t.pumpAndSettle();
    expect(toggled, 1);
    expect(t.widget<SwitchListTile>(find.byKey(AppSettingsPage.watchKey)).value, isFalse);
    expect(find.byKey(AppSettingsPage.unbindKey), findsNothing);
  });

  testWidgets('检查更新：没有新的说已是最新；站点没开分发说清楚', (t) async {
    final api = FakeApi('guard');
    await _show(t, AppSettingsPage(api: api, updater: (a) => AppUpdater(api: a, platform: 'android')));
    await t.tap(find.byKey(AppSettingsPage.checkUpdateKey));
    await t.pumpAndSettle();
    expect(find.text('已是最新版本'), findsOneWidget);
    expect(api.appUpdateAsks.single, ('android', appBuild));
  });

  testWidgets('检查更新：有新的先问，点「稍后」不下载', (t) async {
    final api = FakeApi('guard')
      ..appUpdateBody = <String, dynamic>{'available': true, 'version': '1.2.0+9', 'build': 9, 'size': 1,
        'name': 'nw.apk', 'sha256': 'c' * 64};
    var installs = 0;
    await _show(t, AppSettingsPage(api: api, updater: (a) => AppUpdater(api: a, platform: 'android',
        install: (_) async {
      installs++;
      return '';
    })));
    await t.tap(find.byKey(AppSettingsPage.checkUpdateKey));
    await t.pumpAndSettle();
    expect(find.text('有新版本 1.2.0+9，是否立即下载安装？'), findsOneWidget);
    await t.tap(find.text('稍后'));
    await t.pumpAndSettle();
    expect(installs, 0);
  });

  testWidgets('站点里换语言先问；取消就不换', (t) async {
    await _show(t, AppSettingsPage(api: FakeApi('guard')));
    await t.tap(find.byKey(AppSettingsPage.langKey));
    await t.pumpAndSettle();
    await t.tap(find.text('English').last);
    await t.pumpAndSettle();
    expect(find.text('切换语言后将回到站点列表，是否继续？'), findsOneWidget);
    await t.tap(find.text('取消'));
    await t.pumpAndSettle();
    expect(appLang.value, AppLang.zh);
  });

  testWidgets('入口：保安在「我的」、管理员在「管理」、经理在「日程」右上角', (t) async {
    for (final (role, open) in <(String, List<Key>)>[
      ('guard', <Key>[NwShell.moreKey, MeTab.settingsKey]),
      ('admin', <Key>[NwShell.adminKey, AdminHomeTab.entryKey('settings')]),
      ('owner', <Key>[NwShell.planKey, SchedulePage.settingsKey]),
    ]) {
      await _show(t, NwShell(api: FakeApi(role), updater: null));
      for (final k in open) {
        await t.ensureVisible(find.byKey(k));
        await t.tap(find.byKey(k));
        await t.pumpAndSettle();
      }
      expect(find.byType(AppSettingsPage), findsOneWidget, reason: role);
      await t.pumpWidget(const SizedBox());
    }
  });
}
