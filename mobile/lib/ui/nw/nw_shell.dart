/// 登录之后的外壳（2026-10-14 重做）：底部五栏 —— 值守、狗、地图、记录、管理（只有管理员有；别的
/// 角色这一栏是「更多」）。原来狗列表顶栏上那一排去别的页面的图标挪到了这里，Key 跟着走
/// （`open-watch`、`open-maps`、`open-runs`、`open-health`；`open-incidents`、`open-schedule` 是记录栏
/// 的分段，`open-releases` 在管理 / 更多栏里）。
///
/// **站点级的事还在狗列表（[SiteRobotsPage]）身上，一个字没搬**：事件流、响铃、后台值守、登录过期
/// 退回站点列表。所以它从进站点起就一直活着（`IndexedStack` 里不显示也在），切到哪一栏都照样响铃。
/// 别的几栏只在显示的时候才建、切走就拆 —— 跟原来「推进去、退出来」一样：值守屏自己的轮询和事件流
/// 不在背后空跑，切回来看到的是现读的。
///
/// 地图、记录、管理三栏是第四批（P4）换的：地图栏是当前这张图（[SiteMapTab]，地图列表从它右上角进）；
/// 记录栏是三个分段当页头、下面嵌各自那一页；管理栏是站点体检，最下面接那几个入口。
library;

import 'package:flutter/material.dart';

import '../../l10n.dart';
import '../../net/background_watch.dart';
import '../../net/push.dart';
import '../../net/site_client.dart';
import '../../store/site_store.dart';
import '../site_cameras.dart';
import '../site_health.dart';
import '../site_incidents.dart';
import '../site_intercepts.dart';
import '../site_maps.dart';
import '../site_releases.dart';
import '../site_robots.dart';
import '../site_runs.dart';
import '../site_schedule.dart';
import '../site_watch_page.dart';
import '../site_routes.dart';
import '../site_weather.dart';
import 'nw_button.dart';
import 'nw_p4_admin.dart';
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

  /// 记录栏的三个分段。
  static const Key runsKey = Key('history-runs');
  static const Key incidentsKey = Key('open-incidents');
  static const Key scheduleKey = Key('open-schedule');

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

  Widget _page(int tab, bool admin) => switch (tab) {
        0 => SiteWatchPage(api: widget.api, title: widget.title),
        2 => SiteMapTab(api: widget.api),
        3 => _HistoryTab(api: widget.api),
        _ => admin ? _AdminTab(api: widget.api) : _MoreTab(api: widget.api),
      };

  @override
  Widget build(BuildContext context) {
    final admin = widget.api.session?.canSeeHealth ?? false;
    final items = <NwNavItem>[
      NwNavItem(key: NwShell.dutyKey, icon: Icons.shield_rounded, label: tr('Duty', '值守')),
      NwNavItem(key: NwShell.dogsKey, icon: Icons.smart_toy_rounded, label: tr('Robots', '机器人')),
      NwNavItem(key: NwShell.mapKey, icon: Icons.map_rounded, label: tr('Map', '地图')),
      NwNavItem(key: NwShell.historyKey, icon: Icons.history_rounded, label: tr('History', '记录')),
      if (admin)
        NwNavItem(
            key: NwShell.adminKey, icon: Icons.admin_panel_settings_rounded, label: tr('Admin', '管理'))
      else
        NwNavItem(key: NwShell.moreKey, icon: Icons.more_horiz_rounded, label: tr('More', '更多')),
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
          KeyedSubtree(key: ValueKey<int>(_tab), child: _page(_tab, admin)),
      ]),
      bottomNavigationBar:
          NwBottomNav(items: items, index: _tab, onTap: (i) => setState(() => _tab = i)),
    );
  }
}

// ------------------------------------------------------------ 记录栏

enum _History { runs, incidents, schedule }

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
                                _History.schedule => tr('Schedule', '排程'),
                              },
                          keyOf: (v) => switch (v) {
                                _History.runs => NwShell.runsKey,
                                _History.incidents => NwShell.incidentsKey,
                                _History.schedule => NwShell.scheduleKey,
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
                    _History.schedule => SiteSchedulePage(api: widget.api, embedded: true),
                  }))),
    ]);
  }
}

// ------------------------------------------------------------ 管理栏、更多栏

/// 管理 / 更多栏里去别的页面的入口：版本、拦截点、摄像头、天气（这几页谁都能看，能改的只有管理员，
/// 各页自己管）。
List<(Key, IconData, String, Widget Function())> _entries(SiteApi api) => [
      (NwShell.releasesKey, Icons.system_update_rounded, tr('Releases', '版本'), () => SiteReleasesPage(api: api)),
      (NwShell.interceptsKey, Icons.fence_rounded, tr('Intercept points', '拦截点'), () => SiteInterceptsPage(api: api)),
      (NwShell.camerasKey, Icons.videocam_rounded, tr('Cameras', '摄像头'), () => SiteCamerasPage(api: api)),
      (NwShell.routesKey, Icons.route_rounded, tr('Patrol routes', '巡检路线'), () => SiteRoutesPage(api: api)),
      (NwShell.weatherKey, Icons.cloud_rounded, tr('Weather', '天气'), () => SiteWeatherPage(api: api)),
    ];

void _go(BuildContext context, Widget Function() page) =>
    Navigator.push(context, MaterialPageRoute<void>(builder: (_) => page()));

/// 管理栏（管理员）：站点体检（站点状态、版本、诊断包），最下面是去版本、拦截点、摄像头、天气的入口 ——
/// 跟体检在同一页里一起滚。
class _AdminTab extends StatelessWidget {
  final SiteApi api;
  const _AdminTab({required this.api});

  @override
  Widget build(BuildContext context) {
    return SiteHealthPage(
        api: api,
        embedded: true,
        footer: Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
          NwSectionLabel(tr('Site settings', '站点设置')),
          nwGap8,
          // 竖屏的手机一行一个（两个并排「Intercept points」摆不下）；宽了并排
          NwGrid(minTile: 220, children: [
            for (final e in _entries(api))
              NwButton(
                  buttonKey: e.$1,
                  icon: e.$2,
                  label: e.$3,
                  variant: NwButtonVariant.outline,
                  size: NwButtonSize.large,
                  fullWidth: true,
                  onPressed: () => _go(context, e.$4)),
          ]),
        ]));
  }
}

/// 更多栏（不是管理员）：那几个入口，没有站点体检。
class _MoreTab extends StatelessWidget {
  final SiteApi api;
  const _MoreTab({required this.api});

  @override
  Widget build(BuildContext context) {
    final entries = _entries(api);
    return Scaffold(
        appBar: AppBar(title: Text(tr('More', '更多'))),
        body: ListView(padding: const EdgeInsets.all(Nw.s16), children: [
          for (var i = 0; i < entries.length; i += 2)
            Padding(
                padding: const EdgeInsets.only(bottom: Nw.s8),
                child: Row(children: [
                  for (var j = i; j < i + 2; j++) ...[
                    if (j > i) const SizedBox(width: Nw.s8),
                    Expanded(
                        child: j < entries.length
                            ? NwActionTile(
                                tileKey: entries[j].$1,
                                icon: entries[j].$2,
                                label: entries[j].$3,
                                onTap: () => _go(context, entries[j].$4))
                            : const SizedBox.shrink()),
                  ],
                ])),
        ]));
  }
}
