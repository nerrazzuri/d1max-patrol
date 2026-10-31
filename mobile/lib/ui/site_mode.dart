/// 布防模式（W20）。
///
/// 全站一个当前模式，决定入侵来了派不派狗、响不响铃（巡检、狗自己的告警不看模式）：
/// - **布防**：所有防区都布防。谁都能切到布防。
/// - **留守**：标了「留守时撤防」的防区撤防，别的照常。经理、管理员能切。
/// - **临时撤防**：在「留守」的基础上再撤几个防区，必须有结束时间（默认 4 小时），到点自动回去。经理、管理员能切。
///
/// 下面列每个防区现在布不布防；管理员能改「留守时也布防」。撤防的防区来了入侵只记录（事件页看得见）。
///
/// 别的手机切了、临时撤防到点退回：站点推一帧 `mode`，这一页当场换（W20 外审）；断了 5 秒后重连并重新拉一次，
/// 另外每 30 秒再问一次兜底。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p2_duty.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

class SiteModePage extends StatefulWidget {
  final SiteApi api;
  const SiteModePage({super.key, required this.api});

  static const Key armKey = Key('mode-armed');
  static const Key homeKey = Key('mode-home');
  static const Key visitorKey = Key('mode-visitor');
  static const Key msgKey = Key('mode-msg');

  @override
  State<SiteModePage> createState() => _SiteModePageState();
}

/// 临时撤防能挑的时长（小时）。
const List<int> visitorHours = <int>[1, 2, 4, 8, 12, 24];

class _SiteModePageState extends State<SiteModePage> {
  Map<String, dynamic>? _mode;
  String _msg = '';
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _retry;
  Timer? _poll;
  bool _disposed = false;

  @override
  void initState() {
    super.initState();
    _load();
    _listen();
    _poll = Timer.periodic(const Duration(seconds: 30), (_) => unawaited(_load()));
  }

  void _listen() {
    _sub?.cancel();
    _sub = widget.api.events().listen((f) {
      if (f['kind'] == 'mode' && f['mode'] is Map && mounted) {
        setState(() => _mode = Map<String, dynamic>.from(f['mode'] as Map));
      } else if (f['kind'] == 'snapshot') {
        unawaited(_load()); // 连上（重连上）的第一帧：断线期间可能错过了切换
      }
    }, onError: (Object _) => _lost(), onDone: _lost, cancelOnError: true);
  }

  void _lost() {
    if (_disposed) return;
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 5), () {
      if (_disposed) return;
      _listen();
      unawaited(_load());
    });
  }

  @override
  void dispose() {
    _disposed = true;
    _retry?.cancel();
    _poll?.cancel();
    _sub?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final m = await widget.api.mode();
      if (mounted) setState(() => _mode = m);
    } on SiteError catch (e) {
      if (mounted) {
        setState(() => _msg = e.status == 404
            ? tr('Arming modes are not enabled on this site', '此站点未启用布防模式')
            : '$e');
      }
    }
  }

  Future<void> _run(Future<Map<String, dynamic>> Function() f, String what) async {
    String msg;
    try {
      final m = await f();
      if (mounted) setState(() => _mode = m);
      msg = tr('$what: done', '$what：完成');
    } on SiteError catch (e) {
      msg = tr('$what failed: $e', '$what失败：$e');
    }
    if (mounted) setState(() => _msg = msg);
  }

  List<Map<String, dynamic>> get _zones =>
      ((_mode?['zones'] as List?) ?? const []).whereType<Map>().map((z) => z.cast<String, dynamic>()).toList();

  Future<void> _visitor() async {
    final picked = <String>{};
    var hours = 4;
    final zones = _zones.map((z) => '${z['zone']}').toList();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, set) => AlertDialog(
          scrollable: true, // 横屏
          title: Text(tr('Temp disarm: which zones to disarm in addition to Stay?',
              '临时撤防：在「留守」基础上，还要撤防哪些防区？')),
          content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            if (zones.isEmpty) Text(tr('No zones yet (disarms as in Stay only)', '还没有防区（仅按「留守」撤防）')),
            Wrap(spacing: 6, children: [
              for (final z in zones)
                FilterChip(
                  key: Key('visitor-zone-$z'),
                  label: Text(z),
                  selected: picked.contains(z),
                  onSelected: (v) => set(() => v ? picked.add(z) : picked.remove(z)),
                ),
            ]),
            Row(children: [
              Text(tr('Duration: ', '时长：')),
              DropdownButton<int>(
                key: const Key('visitor-hours'),
                value: hours,
                items: [for (final h in visitorHours) DropdownMenuItem(value: h, child: Text(tr('$h h', '$h 小时')))],
                onChanged: (v) => set(() => hours = v ?? hours),
              ),
              Text(tr(', then reverts automatically', '，到期自动恢复')),
            ]),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
            FilledButton(key: const Key('visitor-go'), onPressed: () => Navigator.pop(c, true), child: Text(tr('Start temp disarm', '开始临时撤防'))),
          ],
        ),
      ),
    );
    if (ok != true) return;
    final list = picked.toList()..sort();
    await _run(() => widget.api.setMode('visitor', zones: list, minutes: hours * 60),
        tr('Start temp disarm for $hours h', '开始临时撤防 $hours 小时'));
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final s = widget.api.session;
    final canArm = s?.canArm ?? false;
    final canSet = s?.canSetMode ?? false;
    final manage = s?.canManageMaps ?? false;
    final m = _mode;
    return Scaffold(
      appBar: AppBar(title: Text(tr('Arming mode', '布防模式'))),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          // 横屏的手机只有 360 高：内距、间距收紧一点，常用的那几样不用滚就点得到
          padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
          children: [
          if (_msg.isNotEmpty)
            Container(
                margin: const EdgeInsets.only(bottom: Nw.s8),
                padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
                decoration: BoxDecoration(color: c.infoSoft, borderRadius: BorderRadius.circular(Nw.r8)),
                child: Text(_msg, key: SiteModePage.msgKey, style: NwText.bodyMedium.copyWith(color: c.text))),
          if (m == null && _msg.isEmpty) const Center(child: CircularProgressIndicator()),
          if (m != null) ...[
            NwCard(
                padding: EdgeInsets.zero,
                child: ListTile(
                  key: const Key('mode-now'),
                  leading: Icon(modeIcon('${m['mode']}'), color: modeColor('${m['mode']}', c)),
                  title: Text(tr('Now: ${modeText(m)}', '现在：${modeText(m)}'),
                      style: NwText.titleMedium.copyWith(color: c.text)),
                  subtitle: Text(
                      tr(
                          '${m['source'] == 'schedule' ? 'Switched by the schedule' : m['set_by'] == 'site:visitor_expired' ? 'Restored automatically when temporary disarm ended' : 'Set by ${m['set_by']}'}${(m['visitor_zones'] as List? ?? const []).isNotEmpty ? ' · temporarily disarmed: ${(m['visitor_zones'] as List).join(', ')}' : ''}',
                          '${m['source'] == 'schedule' ? '按时间表切换' : m['set_by'] == 'site:visitor_expired' ? '临时撤防到期自动恢复' : '由 ${m['set_by']} 切换'}${(m['visitor_zones'] as List? ?? const []).isNotEmpty ? ' · 临时撤防：${(m['visitor_zones'] as List).join('、')}' : ''}'),
                      style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
                )),
            const SizedBox(height: Nw.s8),
            Wrap(spacing: Nw.s8, runSpacing: Nw.s8, crossAxisAlignment: WrapCrossAlignment.center, children: [
              if (canArm)
                NwButton(
                    buttonKey: SiteModePage.armKey,
                    onPressed: m['mode'] == 'armed' ? null : () => _run(() => widget.api.setMode('armed'), tr('Switch to Armed', '切换为布防')),
                    icon: Icons.shield_rounded,
                    label: tr('Armed', '布防')),
              if (canSet) ...[
                NwButton(
                    buttonKey: SiteModePage.homeKey,
                    variant: NwButtonVariant.outline,
                    onPressed: m['mode'] == 'home' ? null : () => _run(() => widget.api.setMode('home'), tr('Switch to Stay', '切换为留守')),
                    icon: Icons.home_rounded,
                    label: tr('Stay', '留守')),
                NwButton(
                    buttonKey: SiteModePage.visitorKey,
                    variant: NwButtonVariant.outline,
                    onPressed: _visitor,
                    icon: Icons.people_rounded,
                    label: tr('Temp disarm…', '临时撤防…')),
              ],
              if (!canSet)
                Text(tr('Disarming (Stay, Temp disarm) needs an administrator or manager', '撤防（留守、临时撤防）需管理员或经理操作'),
                    style: NwText.bodySmall.copyWith(color: c.textSecondary)),
            ]),
            const SizedBox(height: Nw.s16),
            Text(tr('Zones (zones that are not configured are always armed)', '防区（未配置的防区一律布防）'),
                style: NwText.labelMedium.copyWith(color: c.textSecondary)),
            const SizedBox(height: Nw.s8),
            if (_zones.isEmpty)
              Text(tr('No zones yet', '还没有防区'), style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
            if (_zones.isNotEmpty)
              NwCard(
                  padding: EdgeInsets.zero,
                  child: Column(mainAxisSize: MainAxisSize.min, children: [
                    for (final z in _zones)
                      ListTile(
                        key: Key('mode-zone-${z['zone']}'),
                        dense: true,
                        // 布防是正常状态：绿；撤防的不上色（空心）
                        leading: Icon(z['armed'] == true ? Icons.shield_rounded : Icons.shield_outlined,
                            color: z['armed'] == true ? c.success : c.textMuted),
                        title: Text(
                            '${z['zone']} · ${z['armed'] == true ? tr('Armed', '布防中') : tr('Disarmed', '撤防中')}',
                            style: NwText.titleSmall.copyWith(color: c.text)),
                        subtitle: Text(
                            z['home_armed'] == true
                                ? tr('Armed in Stay mode too', '留守时也布防')
                                : tr('Disarmed in Stay mode', '留守时撤防'),
                            style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                        trailing: manage
                            ? Switch(
                                key: Key('mode-zone-home-${z['zone']}'),
                                value: z['home_armed'] == true,
                                onChanged: (v) => _run(() => widget.api.setZoneHome('${z['zone']}', v),
                                    tr('${z['zone']} ${v ? 'armed' : 'disarmed'} in Home mode',
                                        '${z['zone']} 留守时${v ? '布防' : '撤防'}')))
                            : null,
                      ),
                  ])),
          ],
        ]),
      ),
    );
  }
}

IconData modeIcon(String mode) => switch (mode) {
      'home' => Icons.home_rounded,
      'visitor' => Icons.people_rounded,
      _ => Icons.shield_rounded,
    };

/// 布防、留守、临时撤防都是正常状态：不上色。认不出的模式是异常：提示色。颜色跟着浅色 / 深色走：把
/// `Nw.of(context)` 传进来（不传按浅色那一套）。
Color modeColor(String mode, [NwColors c = NwColors.light]) => switch (mode) {
      'armed' || 'home' || 'visitor' => c.textSecondary,
      _ => c.info,
    };

/// 值守首页上的布防模式三段（App 重做 P2）：布防 / 留守 / 临时撤防，下面一行写现在是什么。
///
/// **调用和权限跟 [SiteModePage] 一样**：布防 = `setMode('armed')`，谁都能；留守 = `setMode('home')`，
/// 经理、管理员；临时撤防要挑防区和时长，进 [SiteModePage] 去开。保安的留守、临时撤防两格是灰的。
/// 首页上一碰就撤防太容易：**切到留守先问一遍**（布防不用问）。
///
/// 跟模式页一样自己听站点推的 `mode`、断了 5 秒后重连并重新拉、每 30 秒再问一次。
/// **读不到、还没读到的时候不说现在是哪个模式**（哪一格都不亮）：上一次读到的不当成现在的。
/// 先发的读取后到不许盖掉更新的结果（按发出去的编号比）。老站点没有布防模式（404）：整条不显示。
class SiteModeBar extends StatefulWidget {
  final SiteApi api;
  const SiteModeBar({super.key, required this.api});

  static const Key armKey = Key('duty-mode-armed');
  static const Key homeKey = Key('duty-mode-home');
  static const Key homeGoKey = Key('duty-mode-home-go');
  static const Key visitorKey = Key('duty-mode-visitor');
  static const Key nowKey = Key('duty-mode-now');
  static const Key msgKey = Key('duty-mode-msg');

  @override
  State<SiteModeBar> createState() => _SiteModeBarState();
}

class _SiteModeBarState extends State<SiteModeBar> {
  Map<String, dynamic>? _mode;

  /// [_mode] 是不是**现在**的：最近一次读取没成就不是。
  bool _known = false;

  /// 这个站点没有布防模式（404）。
  bool _off = false;
  String _loadError = '';
  String _msg = '';
  bool _busy = false;

  /// 向站点问的编号、已经用上的那一次的编号：回来得比已经用上的还旧就丢掉。
  int _asked = 0;
  int _applied = 0;
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _retry;
  Timer? _poll;
  bool _disposed = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
    _listen();
    _poll = Timer.periodic(const Duration(seconds: 30), (_) => unawaited(_load()));
  }

  void _got(Map<String, dynamic> m, int seq) {
    _mode = m;
    _known = true;
    _off = false;
    _loadError = '';
    _applied = seq;
  }

  void _listen() {
    _sub?.cancel();
    _sub = widget.api.events().listen((f) {
      if (f['kind'] == 'mode' && f['mode'] is Map && mounted) {
        setState(() => _got(Map<String, dynamic>.from(f['mode'] as Map), ++_asked));
      } else if (f['kind'] == 'snapshot') {
        unawaited(_load()); // 连上（重连上）的第一帧：断线期间可能错过了切换
      }
    }, onError: (Object _) => _lost(), onDone: _lost, cancelOnError: true);
  }

  void _lost() {
    if (_disposed) return;
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 5), () {
      if (_disposed) return;
      _listen();
      unawaited(_load());
    });
  }

  @override
  void dispose() {
    _disposed = true;
    _retry?.cancel();
    _poll?.cancel();
    _sub?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    final seq = ++_asked;
    try {
      final m = await widget.api.mode();
      if (!mounted || seq <= _applied) return;
      setState(() => _got(m, seq));
    } on SiteError catch (e) {
      if (!mounted || seq <= _applied) return;
      setState(() {
        _known = false;
        _off = e.status == 404;
        _loadError = '$e';
        _applied = seq;
      });
    }
  }

  /// 切模式。说的话跟模式页一样；切成了看亮的那一格就知道，只有没成才留一句。
  Future<void> _run(Future<Map<String, dynamic>> Function() f, String what) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _msg = '';
    });
    final seq = ++_asked;
    var msg = '';
    try {
      final m = await f();
      if (mounted && seq > _applied) setState(() => _got(m, seq));
    } on SiteError catch (e) {
      msg = tr('$what failed: $e', '$what失败：$e');
    }
    if (mounted) {
      setState(() {
        _busy = false;
        _msg = msg;
      });
    }
  }

  Future<void> _home() async {
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              content: Text(tr(
                  'Switch to Stay? Zones marked "Disarmed in Stay mode" stop dispatching robots and ringing.',
                  '切换到留守？标为「留守时撤防」的防区发生入侵时，不派机器人、不响铃。')),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
                FilledButton(
                    key: SiteModeBar.homeGoKey,
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('Switch to Stay', '切换为留守'))),
              ],
            ));
    if (ok != true || !mounted) return;
    await _run(() => widget.api.setMode('home'), tr('Switch to Stay', '切换为留守'));
  }

  Future<void> _visitor() async {
    await Navigator.push(context, MaterialPageRoute<void>(builder: (_) => SiteModePage(api: widget.api)));
    if (mounted) unawaited(_load());
  }

  @override
  Widget build(BuildContext context) {
    if (_off) return const SizedBox.shrink();
    final c = Nw.of(context);
    final s = widget.api.session;
    final canArm = s?.canArm ?? false;
    final canSet = s?.canSetMode ?? false;
    final m = _known ? _mode : null;
    final now = m == null ? null : '${m['mode']}';
    final small = NwText.bodySmall.copyWith(color: c.textSecondary);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
      NwChoiceBar(choices: [
        NwChoice(
            key: SiteModeBar.armKey,
            icon: Icons.shield_rounded,
            label: tr('Armed', '布防').toUpperCase(),
            selected: now == 'armed',
            onTap: !canArm || _busy || now == 'armed'
                ? null
                : () => _run(() => widget.api.setMode('armed'), tr('Switch to Armed', '切换为布防'))),
        NwChoice(
            key: SiteModeBar.homeKey,
            icon: Icons.home_rounded,
            label: tr('Stay', '留守').toUpperCase(),
            selected: now == 'home',
            onTap: !canSet || _busy || now == 'home' ? null : _home),
        NwChoice(
            key: SiteModeBar.visitorKey,
            icon: Icons.people_rounded,
            label: tr('Temp disarm…', '临时撤防…').toUpperCase(),
            selected: now == 'visitor',
            onTap: !canSet || _busy ? null : _visitor),
      ]),
      const SizedBox(height: Nw.s4),
      if (m != null)
        Text(tr('Now: ${modeText(m)}', '现在：${modeText(m)}'),
            key: SiteModeBar.nowKey, style: NwText.bodySmall.copyWith(color: c.text))
      else if (_loadError.isNotEmpty)
        Text(tr("Can't load the arming mode: $_loadError", '无法读取布防模式：$_loadError'),
            key: SiteModeBar.nowKey, style: small)
      else
        Text(tr('Arming mode not loaded yet', '布防模式未加载'), key: SiteModeBar.nowKey, style: small),
      if (!canSet) Text(tr('Disarming (Stay, Temp disarm) needs an administrator or manager', '撤防（留守、临时撤防）需管理员或经理操作'), style: small),
      if (_msg.isNotEmpty)
        Padding(
            padding: const EdgeInsets.only(top: Nw.s4),
            child: Text(_msg,
                key: SiteModeBar.msgKey,
                style: NwText.bodyMedium.copyWith(color: c.text, fontWeight: FontWeight.w600))),
    ]);
  }
}
