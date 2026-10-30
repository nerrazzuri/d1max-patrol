/// 底部第 5 栏（W53，按角色）：管理员、工程师是「管理」，经理是「日程」，保安和只看的是「我的」。
///
/// - **管理**：最上面「要你处理」（挡着机器人干活、挡着别人用的事，一行一个、带按钮直接去）；开站清单；
///   下面三组（巡逻 / 报警联动 / 人和系统），每项一句话说清楚是干嘛的。
/// - **日程**：今天（跟值守页「今天的巡逻」同一份）、每周（每条日程哪几天几点、哪条路线、哪台；能加、改、删）。
/// - **我的**：账号、角色、使用教程、常见问题、退出站点。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import '../store/site_store.dart';
import 'guide.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p4_admin.dart' show nwGap8, NwListView;
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'patrol_dispatch.dart';
import 'site_cameras.dart';
import 'site_health.dart';
import 'site_intercepts.dart';
import 'site_mapping.dart';
import 'site_releases.dart';
import 'site_routes.dart';
import 'site_weather.dart';

void _push(BuildContext context, Widget page) =>
    Navigator.push(context, MaterialPageRoute<void>(builder: (_) => page));

/// 退出站点（回站点列表；外壳那一页退掉时站点列表注销会话、停推送）。每个角色的第 5 栏都有。
Future<void> leaveSite(BuildContext context) => Navigator.of(context, rootNavigator: true).maybePop();

const Key leaveSiteKey = Key('leave-site');

/// 一个入口：图标、名字、一句说明。
class _Entry extends StatelessWidget {
  final Key entryKey;
  final IconData icon;
  final String title;
  final String hint;
  final VoidCallback onTap;
  const _Entry({required this.entryKey, required this.icon, required this.title, required this.hint, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: Nw.s8),
      child: NwCard(
        key: entryKey,
        onTap: onTap,
        child: Row(children: [
          Icon(icon, color: c.textSecondary),
          const SizedBox(width: Nw.s16),
          Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text(hint, style: NwText.bodySmall.copyWith(color: c.textSecondary)),
          ])),
          Icon(Icons.chevron_right_rounded, color: c.textSecondary),
        ]),
      ),
    );
  }
}

// ------------------------------------------------------------ 开站清单

/// 开站清单每一项的名字、说明（站点只给 id）。
(String, String) setupItemText(String id) => switch (id) {
      'robots' => (tr('Add robots', '加机器人'), tr('Robots → Add, scan the sticker on the robot.', '机器人 → 添加，扫机器人上的贴纸。')),
      'map' => (
          tr('Map', '地图'),
          tr('Mapping needs the engineer tools. Ask your delivery engineer, or follow the mapping manual.',
              '建图要工程师工具：请交付工程师做，或者照《建图手册》做。')
        ),
      'zones' => (tr('Confirm no-go zones', '确认禁行区'),
          tr("Robots don't go out on their own until the zones on this map are confirmed.", '这张图的禁行区确认之前，机器人不自己出去。')),
      'standby' => (tr('Standby points', '待命点'),
          tr('Where each robot waits and returns to after a patrol.', '每台机器人平时在哪等、巡逻完回哪。')),
      'routes' => (tr('Patrol routes', '巡逻路线'), tr('Draw routes on the map.', '在地图上画路线。')),
      'schedule' => (tr('Patrol schedule', '巡逻日程'), tr('When each route runs, and on which robot.', '几点走哪条路线、哪台机器人。')),
      'arming' => (tr('Arming times', '布防时间'),
          tr('When the site arms and disarms by itself (set on the web console). Skip it if you always switch by hand.',
              '几点自动布防、撤防（在网页值班台设）。一直手动切就点「不用了」。')),
      'accounts' => (tr('Accounts for your team', '给保安、经理开账号'),
          tr('Approve applications or add accounts.', '批准申请，或者直接加账号。')),
      'push' => (tr('Phone alerts', '手机收推送'), tr('At least one phone signed in and receiving P1 alerts.', '至少一部手机登录着、收得到 P1 推送。')),
      _ => (id, ''),
    };

class SetupChecklist extends StatefulWidget {
  final SiteApi api;
  /// 「去做」按到哪儿（外壳给：切到哪一栏或推哪一页）。
  final void Function(BuildContext context, String id) go;
  const SetupChecklist({super.key, required this.api, required this.go});

  static const Key cardKey = Key('setup-card');
  static Key itemKey(String id) => Key('setup-$id');
  static Key goKey(String id) => Key('setup-go-$id');
  static Key skipKey(String id) => Key('setup-skip-$id');
  static const Key hideKey = Key('setup-hide');
  static const Key showKey = Key('setup-show');

  @override
  State<SetupChecklist> createState() => _SetupChecklistState();
}

class _SetupChecklistState extends State<SetupChecklist> {
  Map<String, dynamic>? _setup;
  Map<String, dynamic>? _guide;
  bool _open = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final got = await Future.wait([widget.api.setup(), widget.api.me()]);
      if (!mounted) return;
      setState(() {
        _setup = got[0];
        final g = got[1]['guide'];
        _guide = g is Map ? g.cast<String, dynamic>() : <String, dynamic>{};
        _open = _guide!['setup_hidden'] != true;
      });
    } on SiteError {
      // 老站点没有清单：不摆
    } on FormatException {
      // 同上
    }
  }

  Future<void> _patch(Map<String, dynamic> p) async {
    try {
      final g = await widget.api.setGuide(p);
      if (mounted) setState(() => _guide = g);
    } on SiteError catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(refusalText(e))));
    }
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = _setup;
    if (s == null) return const SizedBox.shrink();
    final items = [for (final x in (s['items'] as List? ?? const [])) if (x is Map) x.cast<String, dynamic>()];
    final done = s['done'], total = s['total'];
    final all = done == total;
    final skip = [for (final x in (_guide?['skip'] as List? ?? const [])) '$x'];
    return Padding(
      padding: const EdgeInsets.only(bottom: Nw.s16),
      child: NwCard(
        key: SetupChecklist.cardKey,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(children: [
            Expanded(
                child: Text(
                    all ? tr('Site setup done ($done/$total)', '开站清单已完成（$done/$total）') : tr('Site setup $done/$total', '开站清单 $done/$total'),
                    style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700))),
            if (_open)
              TextButton(
                  key: SetupChecklist.hideKey,
                  onPressed: () {
                    setState(() => _open = false);
                    unawaited(_patch(<String, dynamic>{'setup_hidden': true}));
                  },
                  child: Text(all ? tr('Hide', '收起') : tr('Not now', '先不管了')))
            else
              TextButton(
                  key: SetupChecklist.showKey,
                  onPressed: () => setState(() => _open = true),
                  child: Text(tr('Show', '展开'))),
          ]),
          if (_open)
            for (final it in items)
              Padding(
                key: SetupChecklist.itemKey('${it['id']}'),
                padding: const EdgeInsets.only(top: Nw.s8),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Icon(
                      it['done'] == true
                          ? Icons.check_circle_rounded
                          : (it['skipped'] == true ? Icons.remove_circle_outline_rounded : Icons.radio_button_unchecked_rounded),
                      size: 20,
                      color: it['done'] == true ? c.success : c.textSecondary),
                  const SizedBox(width: Nw.s8),
                  Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(setupItemText('${it['id']}').$1, style: NwText.bodyMedium.copyWith(color: c.text)),
                    if (it['done'] != true)
                      Text(setupItemText('${it['id']}').$2, style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                  ])),
                  if (it['done'] != true && it['skipped'] != true) ...[
                    TextButton(
                        key: SetupChecklist.skipKey('${it['id']}'),
                        onPressed: () => _patch(<String, dynamic>{'skip': <String>[...skip, '${it['id']}']}),
                        child: Text(tr('Skip', '不用了'))),
                    NwButton(
                        buttonKey: SetupChecklist.goKey('${it['id']}'),
                        size: NwButtonSize.small,
                        label: tr('Do it', '去做'),
                        onPressed: () async {
                          widget.go(context, '${it['id']}');
                        }),
                  ],
                ]),
              ),
        ]),
      ),
    );
  }
}

// ------------------------------------------------------------ 管理栏

class AdminHomeTab extends StatefulWidget {
  final SiteApi api;
  final SiteEntry? entry;
  /// 切到底部的某一栏（「去做」用：机器人栏、地图栏）。
  final void Function(int tab) switchTab;
  const AdminHomeTab({super.key, required this.api, required this.switchTab, this.entry});

  static const Key todoKey = Key('admin-todo');
  static Key entryKey(String id) => Key('admin-entry-$id');

  @override
  State<AdminHomeTab> createState() => _AdminHomeTabState();
}

class _AdminHomeTabState extends State<AdminHomeTab> {
  List<(String, String, VoidCallback)> _todo = const [];

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    final s = widget.api.session;
    final todo = <(String, String, VoidCallback)>[];
    try {
      final setup = await widget.api.setup();
      for (final x in (setup['items'] as List? ?? const [])) {
        if (x is Map && x['id'] == 'zones' && x['done'] != true) {
          for (final m in (x['maps'] as List? ?? const [])) {
            final parts = '$m'.split(':');
            if (parts.length != 2) continue;
            todo.add(('zones-$m', tr('No-go zones on $m are not confirmed: robots won\'t go out on their own',
                    '$m 的禁行区还没确认：机器人不自己出去'), () {
              _push(context, SiteMapPreviewPage(api: widget.api, mapId: parts[0], version: parts[1]));
            }));
          }
        }
      }
    } on SiteError {
      // 老站点：没有清单
    } on FormatException {
      // 同上
    }
    if (s?.canManageRobots ?? false) {
      try {
        final n = (await widget.api.bindRequests()).length;
        if (n > 0) {
          todo.add(('bind', tr('$n robot(s) waiting to join', '$n 台机器人等批准加入'), () => widget.switchTab(1)));
        }
      } on SiteError {
        // 站点没开绑定
      }
    }
    if (s?.canManageAccounts ?? false) {
      try {
        final n = (await widget.api.accountRequests()).length;
        if (n > 0) {
          todo.add(('accounts', tr('$n account application(s) waiting', '$n 个账号等批准'),
              () => _push(context, AccountsPage(api: widget.api))));
        }
      } on SiteError {
        // 老站点
      }
    }
    try {
      final d = await widget.api.intercepts();
      final used = {for (final z in (d['zones'] as List? ?? const [])) if (z is Map) '${z['intercept']}'};
      for (final i in (d['intercepts'] as List? ?? const [])) {
        if (i is Map && !used.contains('${i['name']}')) {
          todo.add(('intercept-${i['name']}', tr('Intercept point "${i['name']}" isn\'t linked to any zone',
                  '拦截点「${i['name']}」没绑防区'), () => _push(context, SiteInterceptsPage(api: widget.api))));
        }
      }
    } on SiteError {
      // 站点没开
    }
    if (mounted) setState(() => _todo = todo);
  }

  void _go(BuildContext context, String id) {
    switch (id) {
      case 'robots' || 'standby':
        widget.switchTab(1);
      case 'map' || 'zones':
        widget.switchTab(2);
      case 'routes':
        _push(context, SiteRoutesPage(api: widget.api));
      case 'schedule':
        _push(context, SchedulePage(api: widget.api));
      case 'accounts':
        _push(context, AccountsPage(api: widget.api));
      case 'push' || 'arming':
        _push(context, HelpPage(role: widget.api.session?.role ?? ''));
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = widget.api.session;
    final api = widget.api;
    Widget group(String label, List<Widget> entries) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [NwSectionLabel(label), nwGap8, ...entries, const SizedBox(height: Nw.s8)]);
    return Scaffold(
      body: Column(children: [
        NwHeader(overline: 'Nexus Warden', title: tr('Admin', '管理')),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _load,
            child: NwListView(children: [
              if (_todo.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(bottom: Nw.s16),
                  child: NwCard(
                    key: AdminHomeTab.todoKey,
                    tone: NwTone.warning,
                    child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                      Text(tr('Needs you', '要你处理'),
                          style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                      for (final t in _todo)
                        Padding(
                          padding: const EdgeInsets.only(top: Nw.s8),
                          child: Row(children: [
                            Expanded(child: Text(t.$2, style: NwText.bodyMedium.copyWith(color: c.text))),
                            NwButton(
                                buttonKey: Key('todo-${t.$1}'),
                                size: NwButtonSize.small,
                                label: tr('Go', '去处理'),
                                onPressed: t.$3),
                          ]),
                        ),
                    ]),
                  ),
                ),
              SetupChecklist(api: api, go: _go),
              group(tr('Patrols', '巡逻'), [
                _Entry(
                    entryKey: AdminHomeTab.entryKey('routes'),
                    icon: Icons.route_rounded,
                    title: tr('Patrol routes', '巡逻路线'),
                    hint: tr('Draw routes on the map: which points, how long to stop, what to photograph.',
                        '在地图上画路线：走哪些点、每点停多久、拍什么。'),
                    onTap: () => _push(context, SiteRoutesPage(api: api))),
                _Entry(
                    entryKey: AdminHomeTab.entryKey('schedule'),
                    icon: Icons.event_rounded,
                    title: tr('Schedule', '日程'),
                    hint: tr('What time, which route, which robot.', '几点、哪条路线、哪台机器人。'),
                    onTap: () => _push(context, SchedulePage(api: api))),
              ]),
              group(tr('Alarm response', '报警联动'), [
                _Entry(
                    entryKey: AdminHomeTab.entryKey('cameras'),
                    icon: Icons.videocam_rounded,
                    title: tr('Cameras', '摄像头'),
                    hint: tr('Fixed cameras: watch them, and send a robot when they report an intruder.',
                        '登记固定摄像头：看画面、它报入侵时派机器人。'),
                    onTap: () => _push(context, SiteCamerasPage(api: api))),
                _Entry(
                    entryKey: AdminHomeTab.entryKey('intercepts'),
                    icon: Icons.fence_rounded,
                    title: tr('Intercept points', '拦截点'),
                    hint: tr('Where a robot goes when a zone reports an intrusion: mark the point, then link zones.',
                        '防区报入侵时机器人去哪：标点，再绑防区。'),
                    onTap: () => _push(context, SiteInterceptsPage(api: api))),
                _Entry(
                    entryKey: AdminHomeTab.entryKey('weather'),
                    icon: Icons.cloud_rounded,
                    title: tr('Weather', '天气'),
                    hint: tr('Slow down in rain, pause patrols in thunderstorms; set the site location.',
                        '下雨限速、雷暴暂停巡逻；设站点坐标。'),
                    onTap: () => _push(context, SiteWeatherPage(api: api))),
              ]),
              group(tr('People and system', '人和系统'), [
                if (s?.canManageAccounts ?? false)
                  _Entry(
                      entryKey: AdminHomeTab.entryKey('accounts'),
                      icon: Icons.people_alt_rounded,
                      title: tr('Accounts', '账号'),
                      hint: tr('Approve applications, change roles, disable accounts.', '批准申请、改权限、停用。'),
                      onTap: () => _push(context, AccountsPage(api: api))),
                _Entry(
                    entryKey: AdminHomeTab.entryKey('health'),
                    icon: Icons.monitor_heart_rounded,
                    title: tr('Health check', '体检'),
                    hint: tr('Robots online, disk, backups, certificates; versions and clock offset of each robot.',
                        '机器人在线、硬盘、备份、证书；每台的版本和钟差。'),
                    onTap: () => _push(context, SiteHealthPage(api: api))),
                _Entry(
                    entryKey: AdminHomeTab.entryKey('releases'),
                    icon: Icons.system_update_rounded,
                    title: tr('Software updates', '软件升级'),
                    hint: tr('Install new versions on the robots, roll back if something goes wrong.',
                        '给机器人装新版本，出问题退回。'),
                    onTap: () => _push(context, SiteReleasesPage(api: api))),
                _Entry(
                    entryKey: AdminHomeTab.entryKey('tour'),
                    icon: Icons.school_rounded,
                    title: tr('How to use', '使用教程'),
                    hint: tr('Walk through the duty page again.', '再走一遍值守页的导览。'),
                    onTap: () => restartTour(context, api)),
                _Entry(
                    entryKey: AdminHomeTab.entryKey('help'),
                    icon: Icons.help_outline_rounded,
                    title: tr('Common questions', '常见问题'),
                    hint: tr('What to do when an alarm comes in, when a robot won\'t go, and more.',
                        '报警来了怎么办、派不出去怎么办……'),
                    onTap: () => _push(context, HelpPage(role: s?.role ?? ''))),
                _Entry(
                    entryKey: leaveSiteKey,
                    icon: Icons.logout_rounded,
                    title: tr('Leave this site', '退出站点'),
                    hint: tr('Signed in as ${s?.name ?? ''} (${roleName(s?.role ?? '')}).',
                        '现在登录的是 ${s?.name ?? ''}（${roleName(s?.role ?? '')}）。'),
                    onTap: () => leaveSite(context)),
              ]),
            ]),
          ),
        ),
      ]),
    );
  }
}

// ------------------------------------------------------------ 账号审批（W52）

class AccountsPage extends StatefulWidget {
  final SiteApi api;
  const AccountsPage({super.key, required this.api});

  static Key approveKey(String id) => Key('acct-approve-$id');
  static Key rejectKey(String id) => Key('acct-reject-$id');
  static Key roleKey(String id) => Key('acct-role-$id');

  @override
  State<AccountsPage> createState() => _AccountsPageState();
}

class _AccountsPageState extends State<AccountsPage> {
  List<Map<String, dynamic>>? _reqs;
  final Map<String, String> _roles = <String, String>{};
  String? _msg;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final r = await widget.api.accountRequests();
      if (mounted) setState(() => _reqs = r);
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = refusalText(e));
    }
  }

  Future<void> _approve(Map<String, dynamic> r) async {
    final id = '${r['id']}';
    final role = _roles[id] ?? 'viewer';
    final pw = TextEditingController();
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              title: Text(tr('Approve ${r['display']}', '批准 ${r['display']}')),
              content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(tr('Account "${r['name']}" as ${roleName(role)}. Enter your password to confirm.',
                    '账号「${r['name']}」，权限：${roleName(role)}。输你的密码确认。')),
                TextField(
                    key: const Key('acct-confirm-pw'),
                    controller: pw,
                    obscureText: true,
                    decoration: InputDecoration(labelText: tr('Your password', '你的密码'))),
              ]),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
                FilledButton(
                    key: const Key('acct-confirm-go'),
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('Approve', '批准'))),
              ],
            ));
    if (ok != true) return;
    try {
      await widget.api.approveAccount(id, role, pw.text);
      if (mounted) setState(() => _msg = tr('Approved ${r['name']}', '批准了 ${r['name']}'));
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = refusalText(e));
    }
    await _load();
  }

  Future<void> _reject(Map<String, dynamic> r) async {
    try {
      await widget.api.rejectAccount('${r['id']}');
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = refusalText(e));
    }
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final reqs = _reqs;
    const roles = <String>['viewer', 'guard', 'owner', 'admin'];
    return Scaffold(
      appBar: AppBar(title: Text(tr('Accounts', '账号'))),
      body: NwListView(children: [
        if (_msg != null) ...[NwNotice(text: _msg!), nwGap8],
        NwSectionLabel(tr('Waiting for approval', '等批准')),
        nwGap8,
        if (reqs == null)
          const Center(child: CircularProgressIndicator())
        else if (reqs.isEmpty)
          Text(tr('Nobody is waiting.', '没有等批准的。'), style: NwText.bodyMedium.copyWith(color: c.textSecondary))
        else
          for (final r in reqs)
            Padding(
              padding: const EdgeInsets.only(bottom: Nw.s8),
              child: NwCard(
                tone: NwTone.warning,
                child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  Text('${r['display']} · ${r['name']}', style: NwText.titleSmall.copyWith(color: c.text)),
                  nwGap8,
                  Row(children: [
                    Text(tr('Role', '权限'), style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
                    const SizedBox(width: Nw.s16),
                    DropdownButton<String>(
                        key: AccountsPage.roleKey('${r['id']}'),
                        value: _roles['${r['id']}'] ?? 'viewer',
                        items: [for (final x in roles) DropdownMenuItem(value: x, child: Text(roleName(x)))],
                        onChanged: (v) => setState(() => _roles['${r['id']}'] = v ?? 'viewer')),
                  ]),
                  Row(mainAxisAlignment: MainAxisAlignment.end, children: [
                    TextButton(
                        key: AccountsPage.rejectKey('${r['id']}'),
                        onPressed: () => _reject(r),
                        child: Text(tr('Turn down', '拒绝'))),
                    const SizedBox(width: Nw.s8),
                    NwButton(
                        buttonKey: AccountsPage.approveKey('${r['id']}'),
                        size: NwButtonSize.small,
                        label: tr('Approve', '批准'),
                        onPressed: () => _approve(r)),
                  ]),
                ]),
              ),
            ),
        const SizedBox(height: Nw.s16),
        Text(
            tr('To change roles, reset passwords or disable accounts, use Users on the web console.',
                '改权限、重设密码、停用账号，在网页值班台的「用户」里。'),
            style: NwText.bodySmall.copyWith(color: c.textSecondary)),
      ]),
    );
  }
}

// ------------------------------------------------------------ 日程（经理、管理员）

const List<String> _allDays = <String>['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

String dayName(String d) => switch (d) {
      'mon' => tr('Mon', '一'),
      'tue' => tr('Tue', '二'),
      'wed' => tr('Wed', '三'),
      'thu' => tr('Thu', '四'),
      'fri' => tr('Fri', '五'),
      'sat' => tr('Sat', '六'),
      'sun' => tr('Sun', '日'),
      _ => d,
    };

String daysText(List days) {
  final ds = [for (final d in days) '$d'];
  if (ds.length == 7) return tr('Every day', '每天');
  return tr(ds.map(dayName).join(' '), '周${ds.map(dayName).join('、')}');
}

/// 日程页：今天 / 每周。[embedded] 在底部栏里（经理的第 5 栏）不带返回。
class SchedulePage extends StatefulWidget {
  final SiteApi api;
  final bool embedded;
  const SchedulePage({super.key, required this.api, this.embedded = false});

  static const Key todayTabKey = Key('sched-today');
  static const Key weekTabKey = Key('sched-week');
  static const Key addKey = Key('sched-add');
  static Key rowKey(String id) => Key('sched-row-$id');

  @override
  State<SchedulePage> createState() => _SchedulePageState();
}

class _SchedulePageState extends State<SchedulePage> {
  bool _week = false;
  List<Map<String, dynamic>> _today = const [];
  Map<String, dynamic>? _sched;
  List<String> _routes = const [];
  List<String> _robots = const [];
  String? _msg;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final got = await Future.wait<Object>([
        widget.api.patrolsToday(),
        widget.api.routeSchedules(),
        widget.api.schedule(),
        widget.api.robots(),
      ]);
      if (!mounted) return;
      setState(() {
        _today = [for (final x in ((got[0] as Map)['items'] as List? ?? const [])) if (x is Map) x.cast<String, dynamic>()];
        _sched = (got[1] as Map).cast<String, dynamic>();
        _routes = [for (final m in ((got[2] as Map)['missions'] as List? ?? const [])) '$m'];
        _robots = [for (final r in got[3] as List) if (r is Map && r['revoked'] != true) '${r['robot_id']}'];
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = refusalText(e));
    } on FormatException catch (e) {
      if (mounted) setState(() => _msg = e.message);
    }
  }

  Future<void> _edit([Map<String, dynamic>? old]) async {
    final got = await showDialog<Map<String, dynamic>>(
        context: context,
        builder: (_) => _ScheduleEditor(api: widget.api, routes: _routes, robots: _robots, old: old));
    if (got == null) return;
    final id = old != null ? '${old['id']}' : 'app-${DateTime.now().millisecondsSinceEpoch}';
    try {
      await widget.api.saveSchedule(id, <String, dynamic>{
        ...got,
        'window_min': old?['window_min'] ?? 30,
        'on_missed': old?['on_missed'] ?? 'skip',
        'base_revision': old?['revision'] ?? 0,
      });
      if (mounted) setState(() => _msg = tr('Saved', '存好了'));
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = refusalText(e));
    }
    await _load();
  }

  Future<void> _delete(Map<String, dynamic> e) async {
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              content: Text(tr('Delete the ${e['at']} "${e['mission']}" schedule?', '删掉 ${e['at']}「${e['mission']}」这条日程？')),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
                FilledButton(
                    key: const Key('sched-delete-go'),
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('Delete', '删掉'))),
              ],
            ));
    if (ok != true) return;
    try {
      await widget.api.deleteSchedule('${e['id']}', (e['revision'] as num?)?.toInt() ?? 0);
    } on SiteError catch (err) {
      if (mounted) setState(() => _msg = refusalText(err));
    }
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final can = widget.api.session?.canSchedule ?? false;
    final rows = [for (final x in (_sched?['schedules'] as List? ?? const [])) if (x is Map) x.cast<String, dynamic>()]
      ..sort((a, b) => '${a['at']}'.compareTo('${b['at']}'));
    final body = RefreshIndicator(
      onRefresh: _load,
      child: NwListView(children: [
        SegmentedButton<bool>(
          segments: [
            ButtonSegment(value: false, label: Text(tr('Today', '今天'), key: SchedulePage.todayTabKey)),
            ButtonSegment(value: true, label: Text(tr('Weekly', '每周'), key: SchedulePage.weekTabKey)),
          ],
          selected: {_week},
          onSelectionChanged: (v) => setState(() => _week = v.first),
        ),
        nwGap8,
        if (_msg != null) ...[NwNotice(text: _msg!), nwGap8],
        if (!_week)
          TodayPatrolList(api: widget.api, items: _today, onChanged: _load)
        else ...[
          if (rows.isEmpty)
            Text(tr('No schedule yet.', '还没有日程。'), style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
          for (final e in rows)
            Padding(
              padding: const EdgeInsets.only(bottom: Nw.s8),
              child: NwCard(
                key: SchedulePage.rowKey('${e['id']}'),
                onTap: can ? () => _edit(e) : null,
                child: Row(children: [
                  SizedBox(
                      width: 56,
                      child: Text('${e['at']}', style: NwText.titleSmall.copyWith(color: c.text))),
                  Expanded(
                      child: Text(
                          [
                            '${e['mission']}',
                            if ('${e['robot'] ?? ''}'.isNotEmpty) '${e['robot']}' else tr('any robot', '任一台'),
                            daysText(e['days'] as List? ?? const []),
                            if ((e['every_min'] as num? ?? 0) > 0) scheduleWhen(e),
                          ].join(' · '),
                          style: NwText.bodyMedium.copyWith(color: c.text))),
                  if (can)
                    IconButton(
                        key: Key('sched-delete-${e['id']}'),
                        tooltip: tr('Delete', '删掉'),
                        icon: const Icon(Icons.delete_outline_rounded),
                        onPressed: () => _delete(e)),
                ]),
              ),
            ),
          if (can)
            NwButton(
                buttonKey: SchedulePage.addKey,
                icon: Icons.add_rounded,
                variant: NwButtonVariant.outline,
                label: tr('Add a schedule', '加一条'),
                onPressed: _routes.isEmpty ? null : () => _edit()),
          const SizedBox(height: Nw.s16),
          Text(
              tr('Arming times (when the site arms and disarms by itself) are set on the web console.',
                  '布防时间（几点自动布防、撤防）在网页值班台里排。'),
              style: NwText.bodySmall.copyWith(color: c.textSecondary)),
        ],
      ]),
    );
    if (widget.embedded) {
      return Scaffold(
          body: Column(children: [
        NwHeader(overline: 'Nexus Warden', title: tr('Schedule', '日程'), trailing: [
          IconButton(
              key: leaveSiteKey,
              tooltip: tr('Leave this site', '退出站点'),
              icon: const Icon(Icons.logout_rounded),
              onPressed: () => leaveSite(context)),
        ]),
        Expanded(child: body),
      ]));
    }
    return Scaffold(appBar: AppBar(title: Text(tr('Schedule', '日程'))), body: body);
  }
}

class _ScheduleEditor extends StatefulWidget {
  final SiteApi api;
  final List<String> routes;
  final List<String> robots;
  final Map<String, dynamic>? old;
  const _ScheduleEditor({required this.api, required this.routes, required this.robots, this.old});

  @override
  State<_ScheduleEditor> createState() => _ScheduleEditorState();
}

class _ScheduleEditorState extends State<_ScheduleEditor> {
  late String _route = widget.old?['mission'] as String? ?? widget.routes.first;
  late String _robot = widget.old?['robot'] as String? ?? '';
  late TimeOfDay _at = _parse('${widget.old?['at'] ?? '08:00'}');
  late final Set<String> _picked = {for (final d in (widget.old?['days'] as List? ?? _allDays)) '$d'};
  int? _est;

  static TimeOfDay _parse(String s) {
    final p = s.split(':');
    return TimeOfDay(hour: int.tryParse(p.first) ?? 8, minute: p.length > 1 ? int.tryParse(p[1]) ?? 0 : 0);
  }

  @override
  void initState() {
    super.initState();
    unawaited(_estimate());
  }

  Future<void> _estimate() async {
    final rid = _robot.isNotEmpty ? _robot : (widget.robots.isEmpty ? null : widget.robots.first);
    if (rid == null) return;
    try {
      final p = await widget.api.patrolPlan(rid, _route);
      if (mounted) setState(() => _est = (p['estimate_s'] as num?)?.toInt());
    } on SiteError {
      // 估不了就不写
    }
  }

  @override
  Widget build(BuildContext context) {
    String two(int v) => v.toString().padLeft(2, '0');
    return AlertDialog(
      title: Text(widget.old == null ? tr('Add a schedule', '加一条日程') : tr('Edit schedule', '改日程')),
      content: SingleChildScrollView(
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          DropdownButtonFormField<String>(
              key: const Key('sched-route'),
              initialValue: _route,
              decoration: InputDecoration(labelText: tr('Route', '路线')),
              items: [for (final r in widget.routes) DropdownMenuItem(value: r, child: Text(r))],
              onChanged: (v) {
                setState(() => _route = v ?? _route);
                unawaited(_estimate());
              }),
          DropdownButtonFormField<String>(
              key: const Key('sched-robot'),
              initialValue: _robot,
              decoration: InputDecoration(labelText: tr('Robot', '机器人')),
              items: [
                DropdownMenuItem(value: '', child: Text(tr('Any robot (the site picks)', '任一台（站点挑）'))),
                for (final r in widget.robots) DropdownMenuItem(value: r, child: Text(r)),
              ],
              onChanged: (v) {
                setState(() => _robot = v ?? '');
                unawaited(_estimate());
              }),
          const SizedBox(height: Nw.s8),
          OutlinedButton.icon(
              key: const Key('sched-time'),
              icon: const Icon(Icons.schedule_rounded),
              label: Text('${two(_at.hour)}:${two(_at.minute)}'),
              onPressed: () async {
                final t = await showTimePicker(context: context, initialTime: _at);
                if (t != null) setState(() => _at = t);
              }),
          const SizedBox(height: Nw.s8),
          Wrap(spacing: 4, children: [
            for (final d in _allDays)
              FilterChip(
                  key: Key('sched-day-$d'),
                  label: Text(dayName(d)),
                  selected: _picked.contains(d),
                  onSelected: (on) => setState(() => on ? _picked.add(d) : _picked.remove(d))),
          ]),
          if (_est != null) ...[
            const SizedBox(height: Nw.s8),
            Text(tr('${minutesText(_est!)} per patrol (including the walk back).', '一趟${minutesText(_est!)}（含走回去）。')),
          ],
        ]),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: Text(tr('Cancel', '算了'))),
        FilledButton(
            key: const Key('sched-save'),
            onPressed: _picked.isEmpty
                ? null
                : () => Navigator.pop(context, <String, dynamic>{
                      'mission': _route,
                      if (_robot.isNotEmpty) 'robot': _robot,
                      'at': '${two(_at.hour)}:${two(_at.minute)}',
                      'days': [for (final d in _allDays) if (_picked.contains(d)) d],
                    }),
            child: Text(tr('Save', '存'))),
      ],
    );
  }
}

// ------------------------------------------------------------ 我的（保安、只看）

class MeTab extends StatelessWidget {
  final SiteApi api;
  final SiteEntry? entry;
  const MeTab({super.key, required this.api, this.entry});

  static const Key leaveKey = Key('me-leave');

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = api.session;
    return Scaffold(
      body: Column(children: [
        NwHeader(overline: 'Nexus Warden', title: tr('Me', '我的')),
        Expanded(
          child: NwListView(children: [
            NwCard(
              child: Row(children: [
                Icon(Icons.account_circle_rounded, size: 40, color: c.textSecondary),
                const SizedBox(width: Nw.s16),
                Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(s?.displayName.isNotEmpty == true ? s!.displayName : (s?.name ?? ''),
                      style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                  Text('${s?.name ?? ''} · ${roleName(s?.role ?? '')}${entry == null ? '' : ' · ${entry!.name}'}',
                      style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                ])),
              ]),
            ),
            const SizedBox(height: Nw.s16),
            _Entry(
                entryKey: const Key('me-tour'),
                icon: Icons.school_rounded,
                title: tr('How to use', '使用教程'),
                hint: tr('Walk through the duty page again.', '再走一遍值守页的导览。'),
                onTap: () => restartTour(context, api)),
            if (s?.canHandleAlerts ?? false)
              _Entry(
                  entryKey: const Key('me-practice'),
                  icon: Icons.sports_score_rounded,
                  title: tr('Practice an alarm', '练一次报警'),
                  hint: tr("A pretend alarm only you see. Nothing is sent to the site.", '一条只有你看得到的假报警，不发给站点。'),
                  onTap: () => _push(context, const PracticeAlarmPage())),
            _Entry(
                entryKey: const Key('me-help'),
                icon: Icons.help_outline_rounded,
                title: tr('Common questions', '常见问题'),
                hint: tr('What to do when an alarm comes in, when a robot won\'t go, and more.',
                    '报警来了怎么办、派不出去怎么办……'),
                onTap: () => _push(context, HelpPage(role: s?.role ?? ''))),
            const SizedBox(height: Nw.s16),
            NwButton(
                buttonKey: leaveKey,
                variant: NwButtonVariant.outline,
                icon: Icons.logout_rounded,
                label: tr('Leave this site', '退出站点'),
                onPressed: () => leaveSite(context)),
          ]),
        ),
      ]),
    );
  }
}
