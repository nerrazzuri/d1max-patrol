/// 值守页（W53，用户 2026-10-30 定的）：打开 App 进站点就是这一页，**跟着一台机器人走**。
///
/// 从上往下：
/// 1. 布防模式 + 天气一行（保安只能切到布防；撤防是经理、管理员）。
/// 2. P1 报警：有就置顶红条，「去处理」进报警处理页；没有就不占地方。
/// 3. 机器人名字标签：默认第一台加入的；点名字切，或者在画面上左右滑。标签上带状态。
/// 4. 画面：**默认不播**，点 ▶ 才开始、全速；✕ 关。切到别的机器人，原来那路自动关。
/// 5. 状态：人话（巡逻中、待命、充电、离线……）、正在走哪条、预计几点走完、电量；被叫停的给「恢复」。
/// 6. 按角色的按钮：保安 派巡逻 / 遥控 / 叫停；经理 派巡逻 / 叫停；只看的没有。更多操作在「详情」。
/// 7. 这台最近的事件；全站今天的巡逻；其余没处理的报警收成一行（点开是原来的告警总览）。
///
/// 不放：钟差、硬盘、证据积压、P1/P2/P3 计数、排程正常、备份、诊断（W53 §9）。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../model/alert.dart';
import '../net/site_client.dart';
import 'map_wizard.dart' show openMapWizard, wizardSteps;
import 'nw/nw_button.dart';
import 'nw/nw_p2_duty.dart';
import 'nw/nw_p3_robots.dart';
import 'nw/nw_p4_admin.dart' show nwGap8;
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'patrol_dispatch.dart';
import 'robot_events.dart';
import 'site_alert_scene.dart';
import 'site_mode.dart';
import 'site_robot.dart';
import 'site_teleop.dart';
import 'site_video.dart';
import 'site_watch_page.dart';
import 'site_weather.dart';

/// 每个站点上次选的哪台（这一次打开 App 里记着；按站点地址分）。
final Map<String, String> _picked = <String, String>{};

/// 第一台加入的在前（站点给 `enrolled_at`；老站点没有就按编号）。
List<Map<String, dynamic>> dutyOrder(List<Map<String, dynamic>> robots) {
  final live = robots.where((r) => r['revoked'] != true).toList();
  live.sort((a, b) {
    final ea = a['enrolled_at'], eb = b['enrolled_at'];
    if (ea is num && eb is num && ea != eb) return ea.compareTo(eb);
    if (ea is num && eb is! num) return -1;
    if (eb is num && ea is! num) return 1;
    return '${a['robot_id']}'.compareTo('${b['robot_id']}');
  });
  return live;
}

/// `10-31 01:29:51` → `10-31 01:29`（值守页的最近事件只要到分钟）。
String _short(String t) => t.length > 11 ? t.substring(0, 11) : t;

/// 机器人给人看的名字（起了名用名字，没起用编号）。
String robotLabel(Map<String, dynamic> r) {
  final n = '${r['name'] ?? ''}';
  return n.isEmpty ? '${r['robot_id']}' : n;
}

class SiteDutyPage extends StatefulWidget {
  final SiteApi api;
  final String? title;
  /// 进来就要开始的上手导览（外壳给；W53 §8.2）。
  final void Function(BuildContext context, DutyTargets targets)? onReady;
  const SiteDutyPage({super.key, required this.api, this.title, this.onReady});

  static const Key modeKey = Key('duty-mode');
  static const Key modeChangeKey = Key('duty-mode-change');
  static Key p1Key(String key) => Key('duty-p1-$key');
  static Key chipKey(String id) => Key('duty-chip-$id');
  static const Key videoKey = Key('duty-video');
  static const Key playKey = Key('duty-play');
  static const Key closeKey = Key('duty-video-close');
  static const Key statusKey = Key('duty-status');
  static const Key patrolKey = Key('duty-patrol');
  static const Key teleopKey = Key('duty-teleop');
  static const Key stopKey = Key('duty-stop');
  static const Key detailKey = Key('duty-detail');
  static const Key resumeKey = Key('duty-resume');
  static const Key othersKey = Key('duty-others');
  static const Key noRobotsKey = Key('duty-no-robots');
  static const Key todayKey = Key('duty-today');
  static const Key noMapKey = Key('duty-no-map');
  static const Key mapWizardKey = Key('duty-map-wizard');

  @override
  State<SiteDutyPage> createState() => _SiteDutyPageState();
}

/// 导览要圈的几块（值守页建好以后交给导览）。
class DutyTargets {
  final GlobalKey mode = GlobalKey(debugLabel: 'tour-mode');
  final GlobalKey chips = GlobalKey(debugLabel: 'tour-chips');
  final GlobalKey video = GlobalKey(debugLabel: 'tour-video');
  final GlobalKey status = GlobalKey(debugLabel: 'tour-status');
  final GlobalKey actions = GlobalKey(debugLabel: 'tour-actions');
  final GlobalKey today = GlobalKey(debugLabel: 'tour-today');
}

class _SiteDutyPageState extends State<SiteDutyPage> {
  final DutyTargets _t = DutyTargets();
  List<Map<String, dynamic>> _robots = const [];
  List<Alert> _alerts = const [];
  List<Map<String, dynamic>> _today = const [];
  int? _offsetMin;
  Map<String, dynamic>? _mode;
  Map<String, dynamic>? _weather;
  Map<String, dynamic>? _view;
  /// W54：站点有没有图、有没有人在建图（`/api/map-wizard`）；老站点没有是 null。
  Map<String, dynamic>? _mapWiz;
  String? _sel;
  bool _playing = false;
  bool _loaded = false;
  bool _ready = false;
  String? _error;
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _debounce;
  Timer? _poll;

  String get _site => widget.api.baseUrl;

  @override
  void initState() {
    super.initState();
    unawaited(_reload());
    _sub = widget.api.events().listen((f) {
      final k = f['kind'];
      // 推来的模式、天气当场换（不等重读）
      if (k == 'mode' && f['mode'] is Map && mounted) {
        setState(() => _mode = Map<String, dynamic>.from(f['mode'] as Map));
      }
      if (k == 'weather' && f['weather'] is Map && mounted) {
        setState(() => _weather = Map<String, dynamic>.from(f['weather'] as Map));
      }
      if (k == 'alert' || k == 'status' || k == 'mode' || k == 'weather' || k == 'event') _soon();
    }, onError: (Object _) {});
    _poll = Timer.periodic(const Duration(seconds: 30), (_) => unawaited(_reload()));
  }

  @override
  void dispose() {
    _sub?.cancel();
    _debounce?.cancel();
    _poll?.cancel();
    super.dispose();
  }

  void _soon() {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 400), () => unawaited(_reload()));
  }

  Future<T?> _try<T>(Future<T> Function() f) async {
    try {
      return await f();
    } on SiteError catch (e) {
      if (e.status != 404 && mounted) setState(() => _error = refusalText(e));
      return null;
    } on FormatException {
      return null;
    }
  }

  Future<void> _reload() async {
    final got = await Future.wait<Object?>([
      _try(widget.api.robots),
      _try(() => widget.api.alerts()),
      _try(widget.api.patrolsToday),
      _try(widget.api.mode),
      _try(widget.api.weather),
      _try(widget.api.mapWizard),
    ]);
    if (!mounted) return;
    final robots = got[0] is List ? dutyOrder((got[0] as List).cast<Map<String, dynamic>>()) : _robots;
    setState(() {
      _robots = robots;
      if (got[1] is List) _alerts = (got[1] as List).cast<Alert>();
      if (got[2] is Map) {
        _offsetMin = ((got[2] as Map)['utc_offset_min'] as num?)?.toInt();
        _today = [
          for (final x in ((got[2] as Map)['items'] as List? ?? const [])) if (x is Map) x.cast<String, dynamic>(),
        ];
      }
      if (got[3] is Map) _mode = (got[3] as Map).cast<String, dynamic>();
      if (got[4] is Map) _weather = (got[4] as Map).cast<String, dynamic>();
      if (got[5] is Map) _mapWiz = (got[5] as Map).cast<String, dynamic>();
      final ids = robots.map((r) => '${r['robot_id']}').toList();
      final want = _sel ?? _picked[_site];
      _sel = want != null && ids.contains(want) ? want : (ids.isEmpty ? null : ids.first);
      _loaded = true;
    });
    await _loadView();
    if (!_ready && mounted) {
      _ready = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) widget.onReady?.call(context, _t);
      });
    }
  }

  Future<void> _loadView() async {
    final id = _sel;
    if (id == null) return;
    final v = await _try(() => widget.api.robot(id));
    if (mounted && v != null && _sel == id) setState(() => _view = v);
  }

  void _select(String id) {
    if (id == _sel) return;
    _picked[_site] = id;
    setState(() {
      _sel = id;
      _view = null;
      _playing = false; // 切台：原来那路关掉
    });
    unawaited(_loadView());
  }

  void _swipe(DragEndDetails d) {
    final v = d.primaryVelocity ?? 0;
    if (v.abs() < 200 || _robots.length < 2 || _sel == null) return;
    final ids = _robots.map((r) => '${r['robot_id']}').toList();
    final i = ids.indexOf(_sel!);
    final j = (i + (v < 0 ? 1 : -1)) % ids.length;
    _select(ids[j < 0 ? j + ids.length : j]);
  }

  void _say(String text) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _go(Widget page) async {
    await Navigator.push(context, MaterialPageRoute<void>(builder: (_) => page));
    unawaited(_reload());
  }

  Future<void> _patrol() async {
    final id = _sel;
    if (id == null) return;
    final msg = await showPatrolSheet(context, widget.api, id);
    if (msg != null) _say(msg);
    unawaited(_reload());
  }

  /// 叫停：**现问**站点它在跑哪一趟（页面上的可能是几秒前的）。
  Future<void> _stop() async {
    final id = _sel;
    if (id == null) return;
    try {
      final v = await widget.api.robot(id);
      final st = v['status'];
      final task = st is Map ? st['task'] : null;
      final tid = task is Map ? task['task_id'] : null;
      if (tid is! String || tid.isEmpty) {
        _say(tr('The robot isn\'t running a task', '机器人当前无任务'));
        return;
      }
      await widget.api.abort(id, tid);
      _say(tr('Stopped', '已叫停'));
    } on SiteError catch (e) {
      _say(tr('Stop failed: ${refusalText(e)}', '叫停失败：${refusalText(e)}'));
    }
    unawaited(_reload());
  }

  Future<void> _resume() async {
    final id = _sel;
    if (id == null) return;
    try {
      await widget.api.resume(id);
    } on SiteError catch (e) {
      _say(refusalText(e));
    }
    unawaited(_reload());
  }

  // ------------------------------------------------------------ 各块

  Widget _modeRow(NwColors c) {
    final s = widget.api.session;
    final m = _mode, w = _weather;
    if (m == null && w == null) return const SizedBox.shrink();
    final armed = m?['mode'] == 'armed' || m?['state'] == 'armed';
    final canChange = (s?.canArm ?? false) && !(s?.isViewer ?? true);
    return Padding(
      key: _t.mode,
      padding: const EdgeInsets.only(bottom: Nw.s8),
      child: NwCard(
        key: SiteDutyPage.modeKey,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
          if (m != null)
            Row(children: [
              Flexible(child: NwStatusPill(label: modeText(m), tone: armed ? NwTone.success : NwTone.warning)),
              const Spacer(),
              if (canChange)
                NwButton(
                    buttonKey: SiteDutyPage.modeChangeKey,
                    size: NwButtonSize.small,
                    variant: NwButtonVariant.outline,
                    label: tr('Change', '切换'),
                    onPressed: () => _go(SiteModePage(api: widget.api))),
            ]),
          if (m != null && w != null) const SizedBox(height: Nw.s8),
          if (w != null)
            InkWell(
              onTap: () => _go(SiteWeatherPage(api: widget.api)),
              child: Row(children: [
                Icon(weatherIcon('${w['condition']}'), size: 18, color: c.textSecondary),
                const SizedBox(width: 4),
                Expanded(
                    child: Text(weatherText(w),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: NwText.bodySmall.copyWith(color: c.textSecondary))),
              ]),
            ),
        ]),
      ),
    );
  }

  List<Widget> _p1(NwColors c) => [
        for (final a in _alerts.where((a) => a.level == 'P1' && a.resolvedMs == null))
          Padding(
            padding: const EdgeInsets.only(bottom: Nw.s8),
            child: NwCard(
              key: SiteDutyPage.p1Key(a.key),
              tone: NwTone.error,
              onTap: () => _go(SiteAlertScenePage(api: widget.api, alert: a)),
              child: Row(children: [
                NwLevelBox(level: a.level),
                const SizedBox(width: Nw.s8),
                Expanded(
                    child: Text('${a.robot} · ${alertTitle(a.kind, a.title)}',
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700))),
                const SizedBox(width: Nw.s8),
                NwButton(
                    size: NwButtonSize.small,
                    variant: a.ackedMs == null ? NwButtonVariant.destructive : NwButtonVariant.outline,
                    label: a.ackedMs == null ? tr('Respond', '去处理') : tr('View', '看现场'),
                    onPressed: () => _go(SiteAlertScenePage(api: widget.api, alert: a))),
              ]),
            ),
          ),
      ];

  Widget _chips(NwColors c) => SingleChildScrollView(
        key: _t.chips,
        scrollDirection: Axis.horizontal,
        child: Row(children: [
          for (final r in _robots)
            Padding(
              padding: const EdgeInsets.only(right: Nw.s8),
              child: ChoiceChip(
                key: SiteDutyPage.chipKey('${r['robot_id']}'),
                selected: r['robot_id'] == _sel,
                onSelected: (_) => _select('${r['robot_id']}'),
                label: Text('${robotLabel(r)} · ${robotPhaseLabel(robotPhase(r))}'),
              ),
            ),
        ]),
      );

  Widget _video(NwColors c) {
    final id = _sel!;
    return GestureDetector(
      key: _t.video,
      onHorizontalDragEnd: _swipe,
      child: _playing
          ? Column(key: SiteDutyPage.videoKey, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              Align(
                alignment: Alignment.centerRight,
                child: TextButton.icon(
                    key: SiteDutyPage.closeKey,
                    onPressed: () => setState(() => _playing = false),
                    icon: const Icon(Icons.close_rounded, size: 18),
                    label: Text(tr('Stop video', '关闭画面'))),
              ),
              SiteVideo(key: ValueKey<String>('duty-video-$id'), api: widget.api, robotId: id),
            ])
          : AspectRatio(
              key: SiteDutyPage.videoKey,
              aspectRatio: 16 / 9,
              child: Material(
                color: Colors.black,
                borderRadius: BorderRadius.circular(Nw.r8),
                child: InkWell(
                  key: SiteDutyPage.playKey,
                  borderRadius: BorderRadius.circular(Nw.r8),
                  onTap: () => setState(() => _playing = true),
                  child: Center(
                    child: Column(mainAxisSize: MainAxisSize.min, children: [
                      const Icon(Icons.play_circle_outline_rounded, size: 56, color: Colors.white70),
                      const SizedBox(height: 4),
                      Text(tr('Tap to watch ${robotLabel(_selRobot!)} live', '点击查看 ${robotLabel(_selRobot!)} 的实时画面'),
                          style: NwText.bodySmall.copyWith(color: Colors.white70)),
                    ]),
                  ),
                ),
              ),
            ),
    );
  }

  Map<String, dynamic>? get _selRobot {
    for (final r in _robots) {
      if (r['robot_id'] == _sel) return r;
    }
    return null;
  }

  Widget _status(NwColors c) {
    final s = widget.api.session;
    final r = _view ?? _selRobot!;
    final phase = robotPhase(r);
    final st = r['status'];
    final task = st is Map ? st['task'] : null;
    final tid = task is Map ? '${task['task_id'] ?? ''}' : null;
    final run = runningItem(_today, '${r['robot_id']}', tid);
    final battery = robotBattery(r);
    final doing = run != null
        ? tr('Patrolling "${run['label']}"${run['end_ms'] is num ? ' · done around ${hhmm((run['end_ms'] as num).toInt(), _offsetMin)}' : ''}',
            '正在巡逻「${run['label']}」${run['end_ms'] is num ? ' · 预计 ${hhmm((run['end_ms'] as num).toInt(), _offsetMin)} 结束' : ''}')
        : robotStatusLine(r);
    return Padding(
      key: _t.status,
      padding: const EdgeInsets.only(top: Nw.s8),
      child: NwCard(
        key: SiteDutyPage.statusKey,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(children: [
            NwStatusPill(label: robotPhaseLabel(phase), tone: robotPhaseTone(phase)),
            const SizedBox(width: Nw.s8),
            Expanded(
                child: Text(doing,
                    maxLines: 2, overflow: TextOverflow.ellipsis, style: NwText.bodyMedium.copyWith(color: c.text))),
            if (battery != null) ...[
              const SizedBox(width: Nw.s8),
              Text('${battery.round()}%', style: NwText.labelLarge.copyWith(color: c.text)),
            ],
          ]),
          if (r['held'] is Map) ...[
            nwGap8,
            Row(children: [
              Expanded(
                  child: Text(
                      tr('Stopped by ${(r['held'] as Map)['by']}. Not dispatched until resumed.',
                          '已被 ${(r['held'] as Map)['by']} 叫停，恢复前不会派出。'),
                      style: NwText.bodySmall.copyWith(color: c.textSecondary))),
              if (s?.canDispatch ?? false)
                NwButton(
                    buttonKey: SiteDutyPage.resumeKey,
                    size: NwButtonSize.small,
                    label: tr('Resume', '恢复'),
                    onPressed: _resume),
            ]),
          ],
        ]),
      ),
    );
  }

  Widget _actions() {
    final s = widget.api.session;
    final id = _sel!;
    final buttons = <Widget>[
      if (s?.canPatrol ?? false)
        NwButton(
            buttonKey: SiteDutyPage.patrolKey,
            icon: Icons.explore_rounded,
            variant: NwButtonVariant.outline,
            label: tr('Patrol', '派巡逻'),
            onPressed: _patrol),
      if (s?.canTeleop ?? false)
        NwButton(
            buttonKey: SiteDutyPage.teleopKey,
            icon: Icons.sports_esports_rounded,
            variant: NwButtonVariant.outline,
            label: tr('Drive', '遥控'),
            onPressed: () => _go(SiteTeleopPage(api: widget.api, robotId: id))),
      if (s?.canAbort ?? false)
        NwButton(
            buttonKey: SiteDutyPage.stopKey,
            icon: Icons.stop_circle_rounded,
            variant: NwButtonVariant.destructive,
            label: tr('Stop', '叫停'),
            onPressed: _stop),
    ];
    return Padding(
      key: _t.actions,
      padding: const EdgeInsets.only(top: Nw.s8),
      // 操作按钮等宽铺满一行；「详情」靠右单独一行。
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        if (buttons.isNotEmpty)
          Row(children: [
            for (var i = 0; i < buttons.length; i++) ...[
              if (i > 0) const SizedBox(width: Nw.s8),
              Expanded(child: buttons[i]),
            ],
          ]),
        Align(
            alignment: Alignment.centerRight,
            child: NwButton(
                buttonKey: SiteDutyPage.detailKey,
                variant: NwButtonVariant.ghost,
                label: tr('Details', '详情'),
                icon: Icons.chevron_right_rounded,
                onPressed: () => _go(SiteRobotPage(api: widget.api, robotId: id)))),
      ]),
    );
  }

  List<Widget> _events(NwColors c) {
    final ev = (_view?['events'] as List? ?? const []).whereType<Map>().take(3).toList();
    if (ev.isEmpty) return const [];
    return [
      const SizedBox(height: Nw.s16),
      NwSectionLabel(tr('${robotLabel(_selRobot!)} recent events', '${robotLabel(_selRobot!)} 最近事件')),
      nwGap8,
      NwCard(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          for (final e in ev)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Row(children: [
                SizedBox(
                    width: 100,
                    child: Text(_short(robotEventTime(e['stamp'])),
                        maxLines: 1, overflow: TextOverflow.fade, softWrap: false,
                        style: NwText.labelSmall.copyWith(color: c.textSecondary))),
                Expanded(
                    child: Text(
                        robotEventTitle('${e['kind']}', e['data'] is Map ? (e['data'] as Map).cast<String, dynamic>() : null),
                        style: NwText.bodySmall.copyWith(color: c.text))),
              ]),
            ),
        ]),
      ),
    ];
  }

  /// W54：站点上一张图都没有。管理员、工程师一行带「开始建图」；别人只说请管理员建。
  List<Widget> _noMap(NwColors c) {
    final w = _mapWiz;
    if (w == null || (w['maps'] as num? ?? 1) > 0) return const [];
    final admin = widget.api.session?.canManageMaps ?? false;
    final wiz = w['wizard'] is Map ? (w['wizard'] as Map) : null;
    final step = wiz == null ? 0 : wizardSteps.indexOf('${wiz['step']}') + 1;
    return [
      NwCard(
        key: SiteDutyPage.noMapKey,
        tone: NwTone.warning,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Text(tr('The site has no map yet', '站点还没有地图'),
              style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
          const SizedBox(height: 2),
          Text(
              wiz != null
                  ? tr('${wiz['by']} is mapping (step $step of 7).', '${wiz['by']} 正在建图（第 $step / 7 步）。')
                  : (admin
                      ? tr('A map is needed before patrolling. Video and remote control are available now.',
                          '巡逻前需先建图。当前可查看画面和遥控。')
                      : tr('Robots need a map before they can patrol. Ask an administrator to build it in the app.',
                          '机器人需要先建图才能巡逻，请管理员在 App 中建图。')),
              style: NwText.bodyMedium.copyWith(color: c.text)),
          if (admin) ...[
            const SizedBox(height: Nw.s8),
            Align(
              alignment: Alignment.centerLeft,
              child: NwButton(
                  buttonKey: SiteDutyPage.mapWizardKey,
                  label: wiz != null ? tr('Continue mapping', '继续建图') : tr('Start mapping', '开始建图'),
                  icon: Icons.add_location_alt_rounded,
                  size: NwButtonSize.small,
                  onPressed: () async {
                    await openMapWizard(context, widget.api);
                    await _reload();
                  }),
            ),
          ],
        ]),
      ),
      nwGap8,
    ];
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final others = _alerts.where((a) => a.level != 'P1' && a.resolvedMs == null).length;
    final hasRobot = _sel != null && _selRobot != null;
    return Scaffold(
      body: Column(children: [
        NwHeader(
            overline: tr('Nexus Warden', 'Nexus Warden'),
            title: widget.title ?? tr('Duty', '值守'),
            compact: MediaQuery.sizeOf(context).height < 480),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _reload,
            child: ListView(padding: const EdgeInsets.all(Nw.s16), children: [
              if (_error != null) ...[NwNotice(text: _error!, tone: NwTone.error), nwGap8],
              _modeRow(c),
              ..._p1(c),
              ..._noMap(c),
              if (!_loaded)
                const Padding(padding: EdgeInsets.all(Nw.s24), child: Center(child: CircularProgressIndicator()))
              else if (!hasRobot)
                NwNotice(
                    key: SiteDutyPage.noRobotsKey,
                    text: tr('No robots on this site yet. An administrator adds them under Robots → Add.',
                        '站点上还没有机器人，请管理员在「机器人 → 添加」中添加。'))
              else ...[
                _chips(c),
                nwGap8,
                _video(c),
                _status(c),
                if (!(widget.api.session?.isViewer ?? true)) _actions(),
                ..._events(c),
              ],
              const SizedBox(height: Nw.s16),
              NwSectionLabel(tr("Today's patrols", '今天的巡逻')),
              nwGap8,
              KeyedSubtree(
                  key: _t.today,
                  child: KeyedSubtree(
                      key: SiteDutyPage.todayKey,
                      child: TodayPatrolList(api: widget.api, items: _today, onChanged: _reload, offsetMin: _offsetMin))),
              if (others > 0) ...[
                nwGap8,
                NwCard(
                  key: SiteDutyPage.othersKey,
                  onTap: () => _go(SiteWatchPage(api: widget.api, title: tr('Alarms', '报警'))),
                  child: Row(children: [
                    Expanded(
                        child: Text(tr('$others other open alarm(s)', '其他未处理 $others 条'),
                            style: NwText.bodyMedium.copyWith(color: c.text))),
                    Icon(Icons.chevron_right_rounded, color: c.textSecondary),
                  ]),
                ),
              ],
            ]),
          ),
        ),
      ]),
    );
  }
}
