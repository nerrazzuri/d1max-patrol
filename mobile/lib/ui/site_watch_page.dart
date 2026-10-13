/// 站点模式的值守屏（W00c5a）。告警与汇总都在站点上，狗上不留（决策 8）。
///
/// 从上往下是值班的人要问的三件事：
/// 1. **现在有没有要我动身的事？** P1 在最上面；**一条都没有也要明确写「没有」**，不留白——留白和
///    「还没加载出来」长得一样。
/// 2. 其余未解决的告警（P2、P3）。
/// 3. **每台狗、站点自己有没有什么在悄悄坏掉？** 值守汇总；缺的数一律印「不知道」和理由，不写 0。
///
/// **告警的顺序原样照登**：排法（先级别再最后一次触发倒序）只在站点说一次。
/// **两条路各读各的**：告警读不到的时候，汇总正是人要看的，反过来也一样。
/// **确认人是登录账号**：站点从令牌里取，手机不传名字。业主看得见，按钮不给。
/// **屏上写着「读于 几点几分」**，实时更新断了要看得见、并且自己重连：一屏停在旧数据上又不说，
/// 比明说「我停在这一刻」更危险——「没有 P1」这句话尤其如此。
///
/// 2026-10-14 重做（设计稿的 Duty Dashboard）：这是底部导航的第一栏。页头下面多了布防模式三段
/// （[SiteModeBar]，调用和权限跟模式页一样）；P1 是红框的卡片、没确认的带一个红色的「去处理」；
/// 汇总上面多了一排健康标签（只用汇总里有的数，没有的不摆）。上面那几条规矩一条没动。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../model/alert.dart';
import '../model/site_watch.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p2_duty.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_alert_scene.dart';
import 'site_mode.dart';

/// 这一帧 SSE 是不是「升到声音档、还没人确认」的告警——手机该响铃。
bool alertWantsSound(Map<String, dynamic> frame) {
  if (frame['kind'] != 'alert') return false;
  final a = frame['alert'];
  return a is Map && a['channel'] == 'sound' && a['acked_ms'] == null && a['resolved_ms'] == null;
}

class SiteWatchPage extends StatefulWidget {
  final SiteApi api;
  /// 墙上钟。测试换成固定的。
  final DateTime Function() now;

  /// 站点名，写在页头；不给就写「值守」。
  final String? title;
  const SiteWatchPage({super.key, required this.api, this.now = DateTime.now, this.title});

  static const Key p1NoneKey = Key('watch-p1-none');
  static const Key p1LoadingKey = Key('watch-p1-loading');
  static const Key alertsErrorKey = Key('watch-alerts-error');
  static const Key summaryErrorKey = Key('watch-summary-error');
  static Key ackKey(String key) => Key('watch-ack-$key');
  static Key alertKey(String key) => Key('watch-alert-$key');
  static Key resolveKey(String key) => Key('watch-resolve-$key');

  /// P1 卡片下面那个进告警页的按钮：没确认的是红色的「去处理」，确认了的是「看现场」。
  static Key respondKey(String key) => Key('watch-respond-$key');
  static Key viewKey(String key) => Key('watch-view-$key');
  static Key robotKey(String id) => Key('watch-robot-$id');
  static const Key loadedAtKey = Key('watch-loaded-at');
  static const Key liveLostKey = Key('watch-live-lost');
  static const Key alertsColumnKey = Key('watch-alerts-column');
  static const Key robotsColumnKey = Key('watch-robots-column');
  /// 窗口宽到这么多（逻辑像素）就分两栏（W15 桌面版）。横屏手机最宽也就 900 出头。
  static const double wideAt = 1100;

  @override
  State<SiteWatchPage> createState() => _SiteWatchPageState();
}

class _SiteWatchPageState extends State<SiteWatchPage> {
  List<Alert> _alerts = const <Alert>[];
  SiteWatchSummary? _summary;
  String? _alertsError;
  String? _summaryError;
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _debounce;
  Timer? _retry;
  Timer? _poll;
  DateTime? _alertsAt;
  bool _live = true;
  bool _disposed = false;

  @override
  void initState() {
    super.initState();
    unawaited(_reload());
    _listen();
    // 实时更新之外每 30 s 再问一次：事件流里错过的、半开没察觉的，都靠它兜住。
    _poll = Timer.periodic(const Duration(seconds: 30), (_) => unawaited(_reload()));
  }

  void _listen() {
    _sub?.cancel();
    _sub = widget.api.events().listen((f) {
      if (!_live && mounted) setState(() => _live = true);
      if (f['kind'] == 'alert' || f['kind'] == 'status') _soon();
    }, onError: (Object _) => _lost(), onDone: _lost, cancelOnError: true);
  }

  void _lost() {
    if (_disposed) return;
    if (mounted) setState(() => _live = false);
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 5), () {
      if (_disposed) return;
      _listen();
      unawaited(_reload());
    });
  }

  void _soon() {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 300), () => unawaited(_reload()));
  }

  Future<void> _reload() async {
    await Future.wait([_loadAlerts(), _loadSummary()]);
  }

  Future<void> _loadAlerts() async {
    try {
      final rows = await widget.api.alerts();
      if (!mounted) return;
      setState(() {
        _alerts = rows;
        _alertsError = null;
        _alertsAt = widget.now();
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _alertsError = tr("Can't load alarms: $e", '告警读不到：$e'));
    } on FormatException catch (e) {
      // 读不懂的名单不许当成「没有 P1」：那是这一屏能说的最坏的假话。
      if (mounted) setState(() => _alertsError = tr("Can't read the alarm list: ${e.message}", '告警读不懂：${e.message}'));
    }
  }

  Future<void> _loadSummary() async {
    try {
      final d = await widget.api.watchSummary();
      if (!mounted) return;
      setState(() {
        _summary = SiteWatchSummary.fromWire(d);
        _summaryError = null;
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _summaryError = tr("Can't load the summary: $e", '汇总读不到：$e'));
    } on FormatException catch (e) {
      if (mounted) setState(() => _summaryError = tr("Can't read the summary: ${e.message}", '汇总读不懂：${e.message}'));
    }
  }

  Future<void> _act(Future<Map<String, dynamic>> Function() call, String what) async {
    try {
      await call();
    } on SiteError catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(tr('$what failed: $e', '$what 没成：$e'))));
      }
    }
    await _loadAlerts();
  }

  @override
  void dispose() {
    _disposed = true;
    _debounce?.cancel();
    _retry?.cancel();
    _poll?.cancel();
    _sub?.cancel();
    super.dispose();
  }

  String _hms(DateTime t) =>
      [t.hour, t.minute, t.second].map((v) => v.toString().padLeft(2, '0')).join(':');

  void _openScene(Alert a) => Navigator.push(
      context, MaterialPageRoute<void>(builder: (_) => SiteAlertScenePage(api: widget.api, alert: a)));

  Widget _alertTile(Alert a) {
    final c = Nw.of(context);
    final can = widget.api.session?.canHandleAlerts ?? false;
    final state = <String>[
      if (a.ackedBy.isNotEmpty) tr('Acknowledged by ${a.ackedBy}', '${a.ackedBy} 已确认') else tr('Unacknowledged', '还没有人确认'),
      if (a.escalated > 0) tr('Escalated to ${a.channel}', '已升级到 ${a.channel}'),
      if (a.count > 1) tr('${a.count} times', '${a.count} 次'),
    ].join(' · ');
    final quiet = TextButton.styleFrom(
        foregroundColor: c.text, minimumSize: const Size(48, 48), textStyle: NwText.labelLarge);
    final actions = !can
        ? null
        : Wrap(spacing: 4, children: [
            if (a.ackedMs == null)
              TextButton(
                  key: SiteWatchPage.ackKey(a.key),
                  style: quiet,
                  onPressed: () => _act(() => widget.api.ackAlert(a.key), tr('Acknowledge', '确认')),
                  child: Text(tr('Acknowledge', '确认'))),
            TextButton(
                key: SiteWatchPage.resolveKey(a.key),
                style: quiet,
                onPressed: () => _act(() => widget.api.resolveAlert(a.key), tr('Resolve', '解决')),
                child: Text(tr('Resolve', '解决'))),
          ]);
    final p1 = a.level == 'P1';
    // 竖屏的手机一行摆不下「标题 + 两个按钮」（按钮会把标题挤成一个字一行）：按钮放到下面一行。
    return LayoutBuilder(builder: (context, box) {
      final narrow = box.maxWidth < 520;
      final text = [if (a.detail.isNotEmpty) a.detail, state].join('\n');
      final sub = Text(text, style: NwText.bodyMedium.copyWith(color: p1 ? c.text : c.textSecondary));
      return Padding(
          padding: const EdgeInsets.only(bottom: Nw.s8),
          // P1：红框、软红底、正文色的字（规矩：红只给 P1）
          child: NwCard(
              padding: EdgeInsets.zero,
              radius: p1 ? Nw.r12 : Nw.r8,
              tone: p1 ? NwTone.error : NwTone.neutral,
              child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                ListTile(
                  key: SiteWatchPage.alertKey(a.key),
                  // 点开看告警页（W17 的现场、App V2 的处理）：画面、在哪、处理按钮
                  onTap: () => _openScene(a),
                  // 三重编码：颜色 + 形状 + 文字
                  leading: NwLevelBox(level: a.level),
                  title: Text('${a.level} ${a.robot} · ${alertTitle(a.kind, a.title)}',
                      style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                  subtitle: narrow && actions != null
                      ? Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                          sub,
                          Align(alignment: Alignment.centerRight, child: actions),
                        ])
                      : sub,
                  isThreeLine: a.detail.isNotEmpty || (narrow && actions != null),
                  trailing: narrow ? null : actions,
                ),
                if (p1)
                  Padding(
                      padding: const EdgeInsets.fromLTRB(Nw.s16, 0, Nw.s16, Nw.s16),
                      child: a.ackedMs == null
                          ? NwButton(
                              buttonKey: SiteWatchPage.respondKey(a.key),
                              variant: NwButtonVariant.destructive,
                              size: NwButtonSize.large,
                              fullWidth: true,
                              icon: Icons.warning_rounded,
                              label: tr('RESPOND TO INCIDENT', '去处理'),
                              onPressed: () => _openScene(a))
                          : NwButton(
                              buttonKey: SiteWatchPage.viewKey(a.key),
                              variant: NwButtonVariant.outline,
                              size: NwButtonSize.large,
                              fullWidth: true,
                              label: tr('View incident', '看现场'),
                              onPressed: () => _openScene(a))),
              ])));
    });
  }

  String _num(Object? v, String unit, String why) => v == null ? tr('Unknown ($why)', '不知道（$why）') : '$v$unit';

  int? _pct(double? r) => r == null ? null : (r * 100).round();

  String _ago(int s) => s < 120
      ? tr('$s s', '$s 秒')
      : s < 7200
          ? tr('${s ~/ 60} min', '${s ~/ 60} 分钟')
          : tr('${s ~/ 3600} h', '${s ~/ 3600} 小时');

  /// 站点自己的备份（W00c5d）。
  String _backupText(SiteWatchSummary s) {
    final b = s.siteBackup;
    if (b == null || b['configured'] != true) return tr('Backup: ${s.siteWhy['backup'] ?? 'not configured'}', '备份：${s.siteWhy['backup'] ?? '没配'}');
    final ok = (b['last_ok_ms'] as num?)?.toInt();
    final ago = ok == null ? '' : _ago(((s.nowMs - ok) ~/ 1000).clamp(0, 1 << 31));
    final when = ok == null
        ? tr('never succeeded', '还没成功过')
        : tr('last succeeded $ago ago', '上次成功在 $ago前');
    final err = '${b['error'] ?? ''}';
    return tr('Backup: $when${b['stale'] == true ? ' (overdue)' : ''}${err.isEmpty ? '' : '; $err'}',
        '备份：$when${b['stale'] == true ? '（过期了）' : ''}${err.isEmpty ? '' : '；$err'}');
  }

  Widget _robotTile(SiteWatchRobot r) {
    final lines = <String>[
      r.online
          ? (r.fresh ? tr('Online', '在线') : tr('Online (status out of date)', '在线（状态过期）'))
          : tr('Offline', '掉线'),
      tr('Battery ${_num(r.batteryPct?.toStringAsFixed(0), '%', r.whyFor('battery_pct'))}',
          '电量 ${_num(r.batteryPct?.toStringAsFixed(0), '%', r.whyFor('battery_pct'))}'),
      tr('Clock offset ${_num(r.clockSkewS?.toStringAsFixed(1), ' s', r.whyFor('clock_skew_s'))}',
          '钟偏 ${_num(r.clockSkewS?.toStringAsFixed(1), ' 秒', r.whyFor('clock_skew_s'))}'),
      r.alerts == null
          ? tr('Open alarms: unknown (not reported by the site)', '未解决告警 不知道（站点没给）')
          : tr('Open P1 ${r.alerts!['P1']} · P2 ${r.alerts!['P2']} · P3 ${r.alerts!['P3']}',
              '未解决 P1 ${r.alerts!['P1']} · P2 ${r.alerts!['P2']} · P3 ${r.alerts!['P3']}'),
      tr('Disk used ${_num(_pct(r.diskUsedRatio), '%', r.whyFor('disk_used_ratio'))}',
          '盘水位 ${_num(_pct(r.diskUsedRatio), '%', r.whyFor('disk_used_ratio'))}'),
      tr(
          'Evidence backlog ${_num(r.uploadBacklog, ' files', r.whyFor('upload_backlog'))}'
              '${(r.oldestBacklogS ?? 0) > 0 ? ' (oldest waiting ${_ago(r.oldestBacklogS!)})' : ''}',
          '证据积压 ${_num(r.uploadBacklog, ' 个文件', r.whyFor('upload_backlog'))}'
              '${(r.oldestBacklogS ?? 0) > 0 ? '（最老的等了 ${_ago(r.oldestBacklogS!)}）' : ''}'),
    ];
    final c = Nw.of(context);
    final up = r.online && r.fresh;
    return Padding(
        padding: const EdgeInsets.only(bottom: Nw.s8),
        child: NwCard(
            padding: EdgeInsets.zero,
            child: ListTile(
                key: SiteWatchPage.robotKey(r.robotId),
                title: Row(children: [
                  Flexible(
                      child: Text(r.robotId,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700))),
                  const SizedBox(width: Nw.s8),
                  // 在线而且状态是新的才是绿的；状态过期不上色；掉线是红的
                  NwStatusPill(
                      dot: true,
                      label: up
                          ? tr('ONLINE', '在线')
                          : (r.online ? tr('OUT OF DATE', '状态过期') : tr('OFFLINE', '掉线')),
                      tone: up ? nwToneOf('online') : (r.online ? NwTone.neutral : nwToneOf('offline'))),
                ]),
                subtitle: Text(lines.join('\n'), style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                isThreeLine: true)));
  }

  /// 健康标签：只用汇总里**有**的数。没开排程、没配备份的不摆 —— 标签不坏的时候是个绿勾，
  /// 不许拿它冒充「正常」。
  List<Widget> _healthChips(SiteWatchSummary s) {
    final all = s.robots.length;
    final up = s.robots.where((r) => r.online && r.fresh).length;
    final b = s.siteBackup;
    final backup = b != null && b['configured'] == true;
    final stale = backup && b['stale'] == true;
    final failing = backup && (b['last_ok_ms'] is! num || '${b['error'] ?? ''}'.isNotEmpty);
    return [
      if (all > 0)
        NwHealthChip(
            icon: up == all ? Icons.check_circle_rounded : Icons.wifi_off_rounded,
            label: tr('Robots online $up/$all', '狗在线 $up/$all'),
            bad: up < all),
      if (s.scheduleOk != null)
        NwHealthChip(
            icon: s.scheduleOk! ? Icons.event_available_rounded : Icons.event_busy_rounded,
            label: s.scheduleOk! ? tr('Schedule OK', '排程正常') : tr('Schedule failed', '排程没办成'),
            bad: !s.scheduleOk!),
      if (backup)
        NwHealthChip(
            icon: stale || failing ? Icons.sync_problem_rounded : Icons.cloud_done_rounded,
            label: stale
                ? tr('Backup overdue', '备份过期了')
                : (failing ? tr('Backup failing', '备份没成') : tr('Backup OK', '备份正常')),
            bad: stale || failing),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final p1 = _alerts.where((a) => a.level == 'P1').toList();
    final rest = _alerts.where((a) => a.level != 'P1').toList();
    final s = _summary;
    const gap8 = SizedBox(height: Nw.s8), gap24 = SizedBox(height: Nw.s24);
    Widget line(Key key, String text, {IconData? icon, Color? iconColor}) => Padding(
        key: key,
        padding: const EdgeInsets.symmetric(vertical: Nw.s8),
        child: Row(children: [
          if (icon != null) ...[Icon(icon, size: 20, color: iconColor), const SizedBox(width: Nw.s8)],
          Expanded(child: Text(text, style: NwText.bodyLarge.copyWith(color: c.text))),
        ]));
    final top = <Widget>[
      if (!_live) ...[
        KeyedSubtree(
            key: SiteWatchPage.liveLostKey,
            child: NwNotice(
                icon: Icons.sync_problem_rounded,
                text: tr(
                    'Live updates lost, reconnecting. The figures below may be out of date; pull down to refresh',
                    '实时更新断了，正在重连；下面的数可能是旧的，下拉可以再问一次'))),
        gap8,
      ],
      Text(
          _alertsAt == null
              ? tr('Alarms not loaded yet', '告警还没读到过')
              : tr('Alarms loaded at ${_hms(_alertsAt!)}; pull down to refresh', '告警读于 ${_hms(_alertsAt!)}；下拉可以再问一次'),
          key: SiteWatchPage.loadedAtKey,
          style: NwText.bodySmall.copyWith(color: c.textSecondary)),
      gap8,
      SiteModeBar(api: widget.api),
      gap24,
    ];
    final incidents = <Widget>[
      NwSectionLabel(tr('Act now (P1)', '要立刻动身的（P1）')),
      gap8,
      if (_alertsError != null)
        KeyedSubtree(
            key: SiteWatchPage.alertsErrorKey, child: NwNotice(text: _alertsError!, tone: NwTone.error))
      else if (_alertsAt == null)
        // 还没读到过：**不许说「没有」** —— 那是这一屏能说的最坏的假话。
        line(SiteWatchPage.p1LoadingKey, tr('Not loaded yet', '还没读到'))
      else if (p1.isEmpty)
        line(SiteWatchPage.p1NoneKey, tr('None', '没有'), icon: Icons.check_circle_rounded, iconColor: c.success)
      else
        for (final a in p1) _alertTile(a),
      gap24,
    ];
    final health = <Widget>[
      if (_summaryError == null && s != null) ...[
        NwSectionLabel(tr('System health', '系统健康')),
        gap8,
        Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: _healthChips(s)),
        gap24,
      ],
    ];
    final events = <Widget>[
      if (rest.isNotEmpty) ...[
        NwSectionLabel(tr('Other open alarms', '其余未解决的')),
        gap8,
        for (final a in rest) _alertTile(a),
        gap24,
      ],
    ];
    final robots = <Widget>[
      NwSectionLabel(tr('Robots', '每台狗')),
      gap8,
      if (_summaryError != null)
        KeyedSubtree(
            key: SiteWatchPage.summaryErrorKey, child: NwNotice(text: _summaryError!, tone: NwTone.error))
      else if (s != null) ...[
        for (final r in s.robots) _robotTile(r),
        NwCard(
            padding: EdgeInsets.zero,
            child: ListTile(
                title: Text(tr('Site', '站点'),
                    style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                subtitle: Text(
                    '${s.scheduleOk == null
                        ? tr('Schedule: ${s.siteWhy['schedule_ok'] ?? 'unknown'}',
                            '排程：${s.siteWhy['schedule_ok'] ?? '不知道'}')
                        : s.scheduleOk!
                            ? tr('Schedule OK', '排程正常')
                            : tr('Schedule failed: ${s.scheduleError}', '排程没办成：${s.scheduleError}')}\n${_backupText(s)}',
                    style: NwText.bodySmall.copyWith(color: c.textSecondary)))),
      ],
    ];
    // 都不用懒加载的列表：告警、狗就那么几条，滚到哪儿都在（测试按 Key 找，读屏也读得全）。
    Widget column(List<Widget> children, {Key? key}) => RefreshIndicator(
        onRefresh: _reload,
        child: SingleChildScrollView(
            key: key,
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(Nw.s16),
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: children)));
    return Scaffold(
      body: Column(children: [
        NwHeader(overline: tr('Nexus Warden', 'Nexus Warden'), title: widget.title ?? tr('Watch', '值守')),
        Expanded(
          // 宽屏（桌面版、值守大屏，W15）：告警一栏、每台狗一栏并排，一眼都看得见；窄的照旧一栏往下排。
          child: LayoutBuilder(
            builder: (context, box) => box.maxWidth >= SiteWatchPage.wideAt
                ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                    Expanded(
                        child: column([...top, ...incidents, ...events],
                            key: SiteWatchPage.alertsColumnKey)),
                    const VerticalDivider(width: 1),
                    Expanded(child: column([...health, ...robots], key: SiteWatchPage.robotsColumnKey)),
                  ])
                : column([...top, ...incidents, ...health, ...events, ...robots]),
          ),
        ),
      ]),
    );
  }
}
