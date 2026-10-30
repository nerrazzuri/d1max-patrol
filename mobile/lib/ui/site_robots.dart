/// 狗的列表（W00c4，实时）。这一屏在登录之后一直开着，站点级的事都在它身上：事件流、响铃、
/// 后台值守、登录过期退回站点列表。去别的页面的入口（值守、地图、记录、版本……）2026-10-14 起
/// 在底部导航上（`nw/nw_shell.dart`），不在这一屏的顶栏了。
///
/// **升到声音档、还没人确认的告警**（SSE `kind: alert`）：由这一屏响铃，不管人此刻在看哪一页。
/// App 没开时的系统推送不在 W00c5a 里。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../l10n.dart';
import '../net/background_watch.dart';
import '../net/push.dart';
import '../net/site_client.dart';
import '../store/site_store.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p3_robots.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'robot_bind_page.dart';
import 'site_deterrence.dart';
import 'site_mode.dart';
import 'site_robot.dart';
import 'site_watch_page.dart';
import 'site_weather.dart';

/// 响铃：系统提示音 + 震动。测试换成计数的。
void defaultRing() {
  unawaited(SystemSound.play(SystemSoundType.alert));
  unawaited(HapticFeedback.heavyImpact());
}

class SiteRobotsPage extends StatefulWidget {
  final SiteApi api;
  /// 标题（站点名）；不给就是「站点」（按当前语言）。
  final String? title;
  final void Function() ring;
  /// 后台值守（W17，决策 30）：登录进来就开（有 [entry] 且平台支持），离开这一页（注销）就停。
  final BackgroundWatch watch;
  final SiteEntry? entry;
  /// P1 推送登记（商业化 A6）：给了就在标题栏上显示推送就没就绪。
  final PushSession? push;
  /// 响铃那一档、没人确认：每隔这么久再响一次，直到确认（W17）。
  final Duration ringEvery;
  const SiteRobotsPage(
      {super.key,
      required this.api,
      this.title,
      this.ring = defaultRing,
      this.watch = const ChannelBackgroundWatch(),
      this.entry,
      this.push,
      this.ringEvery = const Duration(seconds: 4)});

  static const Key bgWatchKey = Key('btn-bg-watch');
  static const Key pushKey = Key('push-state');
  static const Key modeKey = Key('mode-banner');
  static const Key weatherKey = Key('weather-banner');

  @override
  State<SiteRobotsPage> createState() => _SiteRobotsPageState();
}

class _SiteRobotsPageState extends State<SiteRobotsPage> {
  List<Map<String, dynamic>> _robots = <Map<String, dynamic>>[];

  /// 布防模式（W20）；站点没开（老站点 404）就是 null，不显示那一行。
  Map<String, dynamic>? _mode;
  Map<String, dynamic>? _weather;

  /// 正在进行的驱离（W22），按狗。站点推 `deterrence` 帧当场换。
  Map<String, Map<String, dynamic>> _deter = <String, Map<String, dynamic>>{};
  String? _error;
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _debounce;
  Timer? _retry;

  /// 实时更新断了（换网、站点重启、令牌到期）：界面上要看得见，并且自己重连。
  bool _live = false;
  bool _disposed = false;

  @override
  void initState() {
    super.initState();
    _reload();
    _listen();
    // 后台值守（决策 30）：登录进来就开
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) unawaited(_bgStart());
    });
  }

  void _listen() {
    _sub?.cancel();
    _sub = widget.api.events().listen((f) {
      if (!_live && mounted) setState(() => _live = true);
      if (f['kind'] == 'snapshot') unawaited(_ringIfPending());
      if (f['kind'] == 'alert' && f['alert'] is Map) {
        _track(Map<String, dynamic>.from(f['alert'] as Map));
      }
      if (f['kind'] == 'mode' && f['mode'] is Map && mounted) {
        setState(() => _mode = Map<String, dynamic>.from(f['mode'] as Map));
      }
      if (f['kind'] == 'weather' && f['weather'] is Map && mounted) {
        setState(() => _weather = Map<String, dynamic>.from(f['weather'] as Map));
      }
      if (f['kind'] == 'deterrence' && f['robot_id'] is String && mounted) {
        setState(() {
          final s = f['session'];
          if (s is Map) {
            _deter[f['robot_id'] as String] = Map<String, dynamic>.from(s);
          } else {
            _deter.remove(f['robot_id']);
          }
        });
      }
      if (alertWantsSound(f)) {
        widget.ring();
        final a = f['alert'] as Map;
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
              content: Text(tr(
                  '${a['level']} ${a['robot']} · '
                      '${alertTitle('${a['kind']}', '${a['title']}')} (unacknowledged)',
                  '${a['level']} ${a['robot']} · ${a['title']}（没人确认）'))));
        }
      }
      _soon();
    }, onError: (Object _) => _lost(), onDone: _lost, cancelOnError: true);
  }

  /// 连上（或重连上）时补一次：升档那一帧可能正好落在断线期间，错过了就永远不响。
  Future<void> _ringIfPending() async {
    try {
      final rows = await widget.api.alerts();
      final pending = rows.where((a) =>
          a.channel == 'sound' && a.ackedMs == null && a.resolvedMs == null);
      if (_disposed) return;
      _ringing
        ..clear()
        ..addAll(pending.map((a) => a.key));
      _bellOnOff();
      if (pending.isEmpty) return;
      widget.ring();
      final a = pending.first;
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(tr(
                '${a.level} ${a.robot} · ${alertTitle(a.kind, a.title)} (unacknowledged)',
                '${a.level} ${a.robot} · ${a.title}（没人确认）'))));
      }
    } on Exception {
      return; // 读不到就算了：值守屏自己会报读不到
    }
  }

  void _lost() {
    if (_disposed) return;
    if (mounted) setState(() => _live = false);
    if (widget.api.session == null) {
      _backToLogin();
      return;
    }
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 5), () {
      if (!_disposed) {
        _listen();
        _reload();
      }
    });
  }

  void _backToLogin() {
    if (!_disposed && mounted) {
      Navigator.of(context)
          .pop(tr('Your session has expired. Sign in again', '登录过期了，重新登录'));
    }
  }

  void _soon() {
    // 事件一来一串:半秒内的合成一次刷新。
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 500), _reload);
  }

  Future<void> _reload() async {
    try {
      final r = await widget.api.robots();
      if (mounted) {
        setState(() {
          _robots = r;
          _error = null;
        });
      }
    } on SiteError catch (e) {
      if (e.status == 401) return _backToLogin();
      if (mounted) setState(() => _error = e.toString());
    }
    try {
      final m = await widget.api.mode();
      if (mounted) setState(() => _mode = m);
    } on SiteError {
      // 老站点没有布防模式（404）；读不到就不显示那一行
    }
    try {
      final w = await widget.api.weather();
      if (mounted) setState(() => _weather = w);
    } on SiteError {
      // 老站点没有天气（404）
    }
    try {
      final d = await widget.api.deterrence();
      if (mounted) setState(() => _deter = {for (final s in d) '${s['robot_id']}': s});
    } on SiteError {
      return; // 老站点没有驱离（404）
    }
  }

  // ------------------------------------------------------------ 一直响到有人确认（W17）

  /// 正在响的：响铃那一档、还没人确认、没解决的告警的键。
  final Set<String> _ringing = <String>{};
  Timer? _bell;

  void _track(Map<String, dynamic> a) {
    final key = '${a['key']}';
    final want = a['channel'] == 'sound' && a['acked_ms'] == null && a['resolved_ms'] == null;
    if (want) {
      _ringing.add(key);
    } else {
      _ringing.remove(key);
    }
    _bellOnOff();
  }

  void _bellOnOff() {
    if (_ringing.isEmpty || _disposed) {
      _bell?.cancel();
      _bell = null;
    } else {
      _bell ??= Timer.periodic(widget.ringEvery, (_) {
        // 退到后台了由后台值守的通知响（闹钟声一直响），这边不叠着响
        final state = WidgetsBinding.instance.lifecycleState;
        if (_ringing.isNotEmpty && (state == null || state == AppLifecycleState.resumed)) {
          widget.ring();
        }
      });
    }
  }

  // ------------------------------------------------------------ 后台值守（W17）

  bool _bgOn = false;
  bool _bgBusy = false;

  Future<void> _bgStart() async {
    final e = widget.entry;
    if (e == null || !widget.watch.supported) return;
    setState(() => _bgBusy = true);
    String? why;
    try {
      final t = await widget.api.watchToken();
      await widget.watch.start(
          site: e.name, url: e.url, fingerprint: e.fingerprint, token: '${t['token']}');
      _bgOn = true;
    } on SiteError catch (err) {
      why = tr("Can't start background watch: $err", '后台值守开不了：$err');
    } on PlatformException catch (err) {
      why = tr("Can't start background watch: ${err.message}", '后台值守开不了：${err.message}');
    }
    if (!mounted) return;
    setState(() => _bgBusy = false);
    if (why != null) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(why)));
  }

  Future<void> _bgToggle() async {
    if (_bgOn) {
      setState(() => _bgBusy = true);
      await widget.watch.stop();
      if (mounted) {
        setState(() {
          _bgOn = false;
          _bgBusy = false;
        });
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(tr('Background watch off: no alarms arrive while the app is in the background',
                '后台值守关了：app 退到后台就收不到告警'))));
      }
    } else {
      await _bgStart();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _debounce?.cancel();
    _retry?.cancel();
    _sub?.cancel();
    _bell?.cancel();
    if (_bgOn) unawaited(widget.watch.stop()); // 离开就注销（站点列表那边）：后台值守跟着停
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final role = widget.api.session?.role ?? '';
    final title = widget.title ?? tr('Site', '站点');
    return Scaffold(
      body: Column(children: [
        NwHeader(
            overline: tr('Robot fleet', '机器人'),
            title: tr('$title ($role)', '$title（$role）'),
            // 能退就给返回：从站点列表登录进来的，返回 = 离开站点（注销）
            leading: Navigator.canPop(context) ? const BackButton() : null,
            trailing: [
              if (widget.push != null)
                ValueListenableBuilder<PushState>(
                    valueListenable: widget.push!.state,
                    builder: (_, st, _) => st == PushState.off
                        ? const SizedBox.shrink()
                        : Padding(
                            padding: const EdgeInsets.symmetric(horizontal: Nw.s8),
                            child: Tooltip(
                                key: SiteRobotsPage.pushKey,
                                message: st == PushState.ready
                                    ? tr('P1 push: ready', 'P1 推送：已就绪')
                                    : tr('P1 push: not ready (${widget.push!.reason})',
                                        'P1 推送：还没就绪（${widget.push!.reason}）'),
                                // 就绪是绿的；没就绪换一个图标（不只靠颜色）
                                child: Icon(
                                    st == PushState.ready
                                        ? Icons.notifications_on
                                        : Icons.notifications_paused,
                                    color: st == PushState.ready ? c.success : c.textSecondary)))),
              if (widget.watch.supported && widget.entry != null)
                IconButton(
                    key: SiteRobotsPage.bgWatchKey,
                    tooltip: _bgOn
                        ? tr('Background watch: on (tap to turn off)', '后台值守：开着（点了关）')
                        : tr('Background watch: off (tap to turn on)', '后台值守：关着（点了开）'),
                    icon: Icon(_bgOn ? Icons.shield : Icons.shield_outlined,
                        color: _bgOn ? c.success : c.textSecondary),
                    onPressed: _bgBusy ? null : _bgToggle),
            ]),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _reload,
            child: ListView(padding: const EdgeInsets.all(Nw.s16), children: [
              if (!_live)
                Padding(
                    padding: const EdgeInsets.only(bottom: Nw.s8),
                    child: NwNotice(
                        key: const Key('live-lost'),
                        tone: NwTone.neutral,
                        icon: Icons.sync_problem_rounded,
                        text: tr('Live updates disconnected. Reconnecting; pull down to refresh',
                            '实时更新没连上，正在重连；下拉可以手动刷新'))),
              if (_error != null)
                Padding(
                    padding: const EdgeInsets.only(bottom: Nw.s8),
                    child: NwNotice(text: _error!, tone: NwTone.error)),
              if (_mode != null)
                Padding(
                    padding: const EdgeInsets.only(bottom: Nw.s8),
                    child: NwBanner(
                      key: SiteRobotsPage.modeKey,
                      icon: modeIcon('${_mode!['mode']}'),
                      title: tr('Mode: ${modeText(_mode!)}', '模式：${modeText(_mode!)}'),
                      onTap: () async {
                        await Navigator.push(
                            context, MaterialPageRoute<void>(builder: (_) => SiteModePage(api: widget.api)));
                        await _reload();
                      },
                    )),
              if (_weather != null)
                Padding(
                    padding: const EdgeInsets.only(bottom: Nw.s8),
                    child: NwBanner(
                      key: SiteRobotsPage.weatherKey,
                      icon: weatherIcon('${_weather!['condition']}'),
                      title: weatherText(_weather!),
                      onTap: () async {
                        await Navigator.push(context,
                            MaterialPageRoute<void>(builder: (_) => SiteWeatherPage(api: widget.api)));
                        await _reload();
                      },
                    )),
              for (final e in _deter.entries)
                Padding(
                    padding: const EdgeInsets.only(bottom: Nw.s8),
                    child: NwBanner(
                      key: Key('deterrence-${e.key}'),
                      tone: NwTone.error,
                      icon: Icons.campaign_rounded,
                      title: tr('${e.key} deterring · ${deterSessionText(e.value)}',
                          '${e.key} 驱离中 · ${deterSessionText(e.value)}'),
                      subtitle: tr('Zone ${e.value['zone']}${deterSceneText(e.value)}',
                          '防区 ${e.value['zone']}${deterSceneText(e.value)}'),
                      onTap: () => Navigator.push(
                          context,
                          MaterialPageRoute<void>(
                              builder: (_) => SiteDeterrencePage(api: widget.api, robotId: e.key))),
                    )),
              if (_mode != null || _weather != null || _deter.isNotEmpty) const SizedBox(height: Nw.s8),
              // W42：等待批准加入的机器人（有管理权限的人才看得到、才批得了）
              if (widget.api.session?.canManageRobots == true)
                BindApprovals(key: const Key('bind-approvals-host'), api: widget.api),
              // 筛选、一只狗一张卡（Key 是 robot-<编号>）。**要带 Key**：上面的横幅多一条少一条，它在列表里
              // 的位置就变了，不带 Key 的话它的状态（选的哪一格）会被换掉。
              NwFleet(
                  key: const ValueKey<String>('robots-fleet'),
                  robots: _robots,
                  deterring: _deter.keys.toSet(),
                  activityOf: robotStatusLine,
                  loadSummary: widget.api.watchSummary,
                  onOpen: (id) => Navigator.push(context,
                      MaterialPageRoute<void>(builder: (_) => SiteRobotPage(api: widget.api, robotId: id)))),
              // W42：添加机器人（扫贴纸配网，谁都能做；站点上批准要管理权限）
              if (widget.entry != null)
                Padding(
                  padding: const EdgeInsets.only(top: Nw.s16),
                  child: Align(
                    alignment: Alignment.centerLeft,
                    child: NwButton(
                      buttonKey: const Key('robot-add'),
                      icon: Icons.add_rounded,
                      variant: NwButtonVariant.outline,
                      label: tr('Add robot', '添加机器人'),
                      onPressed: () async {
                        await Navigator.push(context, MaterialPageRoute<void>(
                            builder: (_) => RobotAddPage(api: widget.api, site: widget.entry!)));
                        await _reload();
                      },
                    ),
                  ),
                ),
            ]),
          ),
        ),
      ]),
    );
  }
}
