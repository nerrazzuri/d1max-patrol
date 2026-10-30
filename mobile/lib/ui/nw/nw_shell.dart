/// 登录之后的外壳：底部五栏 —— 值守、机器人、地图、记录，第 5 栏按角色（W53，用户 2026-10-30）：
/// 管理员、工程师是「管理」，经理是「日程」，保安和只看的是「我的」。
///
/// **站点级的事还在狗列表（[SiteRobotsPage]）身上**：事件流、响铃、后台值守、登录过期退回站点列表。
/// 所以它从进站点起就一直活着（`IndexedStack` 里不显示也在），切到哪一栏都照样响铃。别的几栏只在显示的
/// 时候才建、切走就拆：值守屏自己的轮询和事件流不在背后空跑，切回来看到的是现读的。
///
/// 值守栏是 W53 的 [SiteDutyPage]（跟着一台机器人走）；它建好以后外壳查这个账号看没看过上手导览。
/// 记录栏三段：巡逻 / 事件 / 报警。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../../l10n.dart';
import '../../net/background_watch.dart';
import '../../net/push.dart';
import '../../net/site_client.dart';
import '../../store/site_store.dart';
import '../alarm_log.dart';
import '../guide.dart';
import '../site_duty_page.dart';
import '../site_incidents.dart';
import '../site_maps.dart';
import '../site_robots.dart';
import '../site_runs.dart';
import '../site_tabs.dart';
import 'nw_p4_admin.dart' show nwSideInset;
import 'nw_theme.dart';
import 'nw_widgets.dart';

/// 底部导航的一栏。
class NwNavItem {
  final Key key;
  final IconData icon;
  final String label;
  const NwNavItem({required this.key, required this.icon, required this.label});
}

/// 底部导航：一排等宽的栏，图标在上、字在下；选中的是主色加粗，别的是次要色。上面一条线，没有阴影。
/// 每一栏的 Key（[NwNavItem.key]）安在能点的那一层上。
class NwBottomNav extends StatelessWidget {
  final List<NwNavItem> items;
  final int index;
  final ValueChanged<int> onTap;
  const NwBottomNav({super.key, required this.items, required this.index, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Material(
        color: c.surface,
        shape: Border(top: BorderSide(color: c.outline)),
        child: SafeArea(
            top: false,
            child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: Nw.s8),
                child: Row(children: [
                  for (var i = 0; i < items.length; i++)
                    Expanded(
                        child: Semantics(
                            button: true,
                            selected: i == index,
                            child: InkWell(
                                key: items[i].key,
                                onTap: () => onTap(i),
                                child: ConstrainedBox(
                                    constraints: const BoxConstraints(minHeight: 56),
                                    child: Column(
                                        mainAxisAlignment: MainAxisAlignment.center,
                                        mainAxisSize: MainAxisSize.min,
                                        children: [
                                          Icon(items[i].icon,
                                              size: 24, color: i == index ? c.primary : c.textSecondary),
                                          const SizedBox(height: 2),
                                          Text(items[i].label,
                                              maxLines: 1,
                                              overflow: TextOverflow.ellipsis,
                                              style: NwText.bodySmall.copyWith(
                                                  color: i == index ? c.primary : c.textSecondary,
                                                  fontWeight: i == index ? FontWeight.w600 : FontWeight.w400)),
                                        ]))))),
                ]))));
  }
}

class NwShell extends StatefulWidget {
  final SiteApi api;

  /// 以下几项原样交给狗列表（[SiteRobotsPage]）：站点名、响铃、后台值守、站点条目、推送登记、多久再响一次。
  final String? title;
  final void Function() ring;
  final BackgroundWatch watch;
  final SiteEntry? entry;
  final PushSession? push;
  final Duration ringEvery;

  const NwShell(
      {super.key,
      required this.api,
      this.title,
      this.ring = defaultRing,
      this.watch = const ChannelBackgroundWatch(),
      this.entry,
      this.push,
      this.ringEvery = const Duration(seconds: 4)});

  static const Key dutyKey = Key('open-watch');
  static const Key dogsKey = Key('open-dogs');
  static const Key mapKey = Key('open-maps');
  static const Key historyKey = Key('open-runs');

  /// 管理栏（只有管理员有）；别的角色这一栏是 [moreKey]。
  static const Key adminKey = Key('open-health');
  static const Key moreKey = Key('open-more');

  /// 经理的第 5 栏：日程（W53）。
  static const Key planKey = Key('open-plan');

  /// 记录栏的三个分段。
  static const Key runsKey = Key('history-runs');
  static const Key incidentsKey = Key('open-incidents');
  static const Key scheduleKey = Key('open-schedule');

  /// 记录栏的「报警」分段（W53，原来的「排程」分段挪到了日程页）。
  static const Key alarmsKey = Key('history-alarms');

  /// 管理 / 更多栏里的入口。
  static const Key releasesKey = Key('open-releases');
  static const Key interceptsKey = Key('open-intercepts');
  static const Key camerasKey = Key('open-cameras');
  static const Key routesKey = Key('open-routes');
  static const Key weatherKey = Key('open-weather');

  @override
  State<NwShell> createState() => _NwShellState();
}

class _NwShellState extends State<NwShell> {
  /// 狗列表是第几栏（它一直活着，别的栏现建现拆）。
  static const int _dogs = 1;

  /// 进来先看值守。
  int _tab = 0;

  /// 上手导览（W53 §8.2）：这一次进站点查过没有；「使用教程」要再走一遍。
  bool _tourChecked = false;
  bool _replay = false;

  @override
  void initState() {
    super.initState();
    replayTour = () async {
      _replay = true;
      if (_tab == 0) {
        // 已经在值守页：值守页不会再建一次，直接在它上面走
        final t = _targets, ctx = _dutyContext;
        _replay = false;
        if (t != null && ctx != null && ctx.mounted) await runTour(ctx, widget.api, t);
      } else {
        setState(() => _tab = 0);
      }
    };
  }

  @override
  void dispose() {
    replayTour = null;
    super.dispose();
  }

  DutyTargets? _targets;
  BuildContext? _dutyContext;

  void _onDutyReady(BuildContext ctx, DutyTargets t) {
    _targets = t;
    _dutyContext = ctx;
    if (_replay) {
      _replay = false;
      unawaited(runTour(ctx, widget.api, t));
      return;
    }
    if (_tourChecked) return;
    _tourChecked = true;
    unawaited(maybeStartTour(ctx, widget.api, t));
  }

  /// 第 5 栏是什么：管理员、工程师「管理」；经理「日程」；保安、只看「我的」。
  String get _fifth {
    final s = widget.api.session;
    if (s?.canSeeHealth ?? false) return 'admin';
    if (s?.role == 'owner') return 'plan';
    return 'me';
  }

  Widget _page(int tab) => switch (tab) {
        0 => SiteDutyPage(api: widget.api, title: widget.title, onReady: _onDutyReady),
        2 => SiteMapTab(api: widget.api),
        3 => _HistoryTab(api: widget.api),
        _ => switch (_fifth) {
            'admin' => AdminHomeTab(
                api: widget.api, entry: widget.entry, switchTab: (i) => setState(() => _tab = i)),
            'plan' => SchedulePage(api: widget.api, embedded: true),
            _ => MeTab(api: widget.api, entry: widget.entry),
          },
      };

  @override
  Widget build(BuildContext context) {
    final fifth = _fifth;
    final items = <NwNavItem>[
      NwNavItem(key: NwShell.dutyKey, icon: Icons.shield_rounded, label: tr('Duty', '值守')),
      NwNavItem(key: NwShell.dogsKey, icon: Icons.smart_toy_rounded, label: tr('Robots', '机器人')),
      NwNavItem(key: NwShell.mapKey, icon: Icons.map_rounded, label: tr('Map', '地图')),
      NwNavItem(key: NwShell.historyKey, icon: Icons.history_rounded, label: tr('History', '记录')),
      switch (fifth) {
        'admin' => NwNavItem(
            key: NwShell.adminKey, icon: Icons.admin_panel_settings_rounded, label: tr('Admin', '管理')),
        'plan' => NwNavItem(key: NwShell.planKey, icon: Icons.event_rounded, label: tr('Schedule', '日程')),
        _ => NwNavItem(key: NwShell.moreKey, icon: Icons.person_rounded, label: tr('Me', '我的')),
      },
    ];
    return Scaffold(
      body: IndexedStack(index: _tab == _dogs ? 0 : 1, sizing: StackFit.expand, children: [
        SiteRobotsPage(
            api: widget.api,
            title: widget.title,
            ring: widget.ring,
            watch: widget.watch,
            entry: widget.entry,
            push: widget.push,
            ringEvery: widget.ringEvery),
        if (_tab == _dogs)
          const SizedBox.shrink()
        else
          KeyedSubtree(key: ValueKey<int>(_tab), child: _page(_tab)),
      ]),
      bottomNavigationBar:
          NwBottomNav(items: items, index: _tab, onTap: (i) => setState(() => _tab = i)),
    );
  }
}

// ------------------------------------------------------------ 记录栏

enum _History { runs, incidents, alarms }

/// 记录栏：巡检记录 / 事件 / 排程三个分段当页头，下面是各自那一页的内容（嵌着，不带自己的顶栏）。
class _HistoryTab extends StatefulWidget {
  final SiteApi api;
  const _HistoryTab({required this.api});

  @override
  State<_HistoryTab> createState() => _HistoryTabState();
}

class _HistoryTabState extends State<_HistoryTab> {
  _History _seg = _History.runs;

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Column(children: [
      Material(
          color: c.surface,
          shape: Border(bottom: BorderSide(color: c.outline)),
          child: SafeArea(
              bottom: false,
              child: LayoutBuilder(
                  builder: (context, box) => Padding(
                      // 宽屏：分段跟下面的内容一样宽、一样居中
                      padding: EdgeInsets.symmetric(horizontal: nwSideInset(box.maxWidth), vertical: Nw.s8),
                      child: NwSegment<_History>(
                          values: _History.values,
                          selected: _seg,
                          onChanged: (v) => setState(() => _seg = v),
                          labelOf: (v) => switch (v) {
                                _History.runs => tr('Patrols', '巡检'),
                                _History.incidents => tr('Incidents', '事件'),
                                _History.alarms => tr('Alarms', '报警'),
                              },
                          keyOf: (v) => switch (v) {
                                _History.runs => NwShell.runsKey,
                                _History.incidents => NwShell.incidentsKey,
                                _History.alarms => NwShell.alarmsKey,
                              }))))),
      Expanded(
          // 刘海已经让上面那一条躲开了
          child: MediaQuery.removePadding(
              context: context,
              removeTop: true,
              child: KeyedSubtree(
                  key: ValueKey<_History>(_seg),
                  child: switch (_seg) {
                    _History.runs => SiteRunsPage(api: widget.api, embedded: true),
                    _History.incidents => SiteIncidentsPage(api: widget.api, embedded: true),
                    _History.alarms => AlarmLogPage(api: widget.api),
                  }))),
    ]);
  }
}
