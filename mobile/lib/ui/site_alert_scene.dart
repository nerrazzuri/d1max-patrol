/// 告警页（App V2，视觉规范 2.5；前身是 W17 的「告警现场」）：一条告警的处理都在这一页 ——
/// 报警头（优先级、标题、没确认多久）、实时画面（手机一次只拉一路，前后可切）、在哪、四个大按钮
/// （我来处理 / 警笛 / 误报 / 对讲还没有）、现场细节（防区、拦截点、狗在哪、那一趟）、事件经过。
///
/// 只有**没确认的 P1** 闪框；确认了框稳住、头条换成谁在什么时候确认的。
///
/// 现场是站点记在告警上的（`context`）：狗最后一次报的地图位姿与收到的时刻、正在跑的那一趟、入侵的
/// 防区与拦截点。老站点不发：细节那一段只剩一句话。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../design/tokens.g.dart';
import '../l10n.dart';
import '../model/alert.dart';
import '../net/site_client.dart';
import 'site_mapping.dart';
import 'site_recordings.dart';
import 'site_runs.dart';
import 'site_video.dart';
import 'theme.dart';
import 'widget/alarm_widgets.dart';

class SiteAlertScenePage extends StatefulWidget {
  final SiteApi api;
  final Alert alert;
  final DateTime Function() now;
  const SiteAlertScenePage({super.key, required this.api, required this.alert, this.now = DateTime.now});

  static const Key mapKey = Key('scene-map');
  static const Key photoKey = Key('scene-photo');
  static const Key msgKey = Key('scene-msg');
  static const Key videoKey = Key('scene-video');
  static const Key deterKey = Key('scene-deter-here');
  static const Key ackKey = Key('scene-ack');
  static const Key sirenKey = Key('scene-siren');
  static const Key falseKey = Key('scene-false');
  static const Key falseGoKey = Key('scene-false-go');
  static const Key headKey = Key('scene-head');
  static const Key stateKey = Key('scene-state');
  static const Key nextKey = Key('scene-next');
  static const Key staleKey = Key('scene-deter-stale');

  @override
  State<SiteAlertScenePage> createState() => _SiteAlertScenePageState();
}

/// 告警现场里要在图上标的点：狗（红）、拦截点（紫）。没有位姿的不标；图不是同一张的（拦截点与狗报的
/// 不是一张图）各算各的。回 `(图 id, 版本, 点)`；一个都没有回 null。
(String, String, List<MapMark>)? sceneMarks(Alert a) {
  final ctx = a.context;
  final pose = ctx['pose'];
  final point = ctx['intercept'];
  String? mapId;
  String? version;
  final marks = <MapMark>[];
  void add(Object? p, String label, Color color) {
    if (p is! Map || p['x'] is! num || p['y'] is! num) return;
    final m = '${p['map_id'] ?? ''}';
    final v = '${p['map_version'] ?? ''}';
    if (m.isEmpty || v.isEmpty) return;
    if (mapId != null && (m != mapId || v != version)) return; // 别的图上的：不往这张图上画
    mapId = m;
    version = v;
    marks.add(MapMark(label, (p['x'] as num).toDouble(), (p['y'] as num).toDouble(), color));
  }

  add(pose, a.robot == 'site' ? tr('Robot', '狗') : tr('Robot ${a.robot}', '狗 ${a.robot}'), Colors.red);
  if (point is Map) add(point, tr('Intercept point ${point['name'] ?? ''}', '拦截点 ${point['name'] ?? ''}'), Colors.purple);
  if (marks.isEmpty) return null;
  return (mapId!, version!, marks);
}

/// 一场驱离里警笛的状态：狗接受了、要求了还没确认、没要求、这台狗没装、说不清。
enum SirenState { on, requested, off, notFitted, unknown }

/// 警笛在第几级、一场驱离里警笛现在算什么状态（C-B2 外审 I1）。
abstract final class SiteAlertScenePageSiren {
  /// 警笛在第 3 级（站点 `deterrence.RELAYS`）；第 2 级只有灯和语音警告。
  static const int level = 3;

  /// `on` 只在狗**接受了**开警笛的命令时才算（站点回的 `active`）：级别到了只是要求，不等于响了。
  /// 老站点不发 `active` / `outputs`：到了级别也只算「要求了」。
  static SirenState of(Map<String, dynamic>? d) {
    if (d == null) return SirenState.unknown;
    final outputs = d['outputs'], active = d['active'], lv = d['level'];
    if (outputs is List && !outputs.contains('siren')) return SirenState.notFitted;
    if (active is List && active.contains('siren')) return SirenState.on;
    return lv is num && lv >= level ? SirenState.requested : SirenState.off;
  }
}

/// 过了多久：`0:42`、`12:05`、`1:02:03`。负数（两头的钟差一点）当 0。
abstract final class SiteAlertScenePageSpan {
  static String of(int ms) {
    final s = ms < 0 ? 0 : ms ~/ 1000;
    final h = s ~/ 3600, m = (s % 3600) ~/ 60;
    String two(int n) => n.toString().padLeft(2, '0');
    return h > 0 ? '$h:${two(m)}:${two(s % 60)}' : '$m:${two(s % 60)}';
  }
}

class _SiteAlertScenePageState extends State<SiteAlertScenePage> {
  late Alert _a = widget.alert;

  /// 在这一页点了「我来处理」之后站点回的（或者本地记的）确认人、时刻；刷新拿到新的告警就用新的。
  (String, int)? _ackedHere;

  /// 最后一次**问到**的驱离（这台狗没在驱离就是 null）。它只是一张快照：[_deterFresh] 不成立时
  /// （上一次没问到，或者太久没问到）不能当成现在的声光状态，见 [_d]（C-B2 外审 I2）。
  Map<String, dynamic>? _deter;
  bool _deterKnown = false;

  /// 问到 [_deter] 那一刻：第几拍（判新鲜用，不靠墙上的钟）、几点（给人看）。
  int _deterTick = 0;
  int _deterAtMs = 0;

  /// 隔这么多拍还没再问到，就算旧的（一拍 800 ms、每 4 拍问一次：连着三次没回来）。
  static const int _freshTicks = 12;

  /// 这一页现在是第几条告警：换了一条就加一。在途的请求回来时对不上就不许动页面（外审 I3）。
  int _gen = 0;

  /// 向站点问的编号：回来得比已经用上的还旧就丢掉（先发的后到）。
  int _asked = 0;
  int _alertApplied = 0;
  int _deterApplied = 0;

  bool _falseDialog = false;

  String _msg = '';
  bool _dim = false;
  int _ticks = 0;
  Timer? _timer;

  bool get _deterFresh => _deterKnown && _ticks - _deterTick <= _freshTicks;

  /// **现在**的驱离：只有新鲜的才算；旧快照、没问到都是 null。
  Map<String, dynamic>? get _d => _deterFresh ? _deter : null;

  bool get _isRobot => _a.robot != 'site' && _a.robot.isNotEmpty;
  bool get _acked => _a.ackedMs != null || _ackedHere != null;
  bool get _open => _a.resolvedMs == null;
  bool get _flashing => _a.level == 'P1' && _open && !_acked;

  @override
  void initState() {
    super.initState();
    unawaited(_refresh());
    // 一个钟管三件事：没确认多久（每拍重画）、闪框（半个周期翻一次）、隔几拍向站点再问一遍。
    _timer = Timer.periodic(const Duration(milliseconds: D1Motion.p1FlashMs ~/ 2), (_) {
      if (!mounted) return;
      _ticks++;
      if (_ticks % 4 == 0) unawaited(_refresh());
      if (_deterKnown && _ticks - _deterTick == _freshTicks + 1) setState(() {}); // 刚变旧：重画
      if (_flashing) {
        setState(() => _dim = !_dim);
      } else if (_dim) {
        setState(() => _dim = false);
      }
    });
  }

  @override
  void didUpdateWidget(SiteAlertScenePage old) {
    super.didUpdateWidget(old);
    if (identical(old.alert, widget.alert) && identical(old.api, widget.api)) return;
    final same = old.alert.key == widget.alert.key && identical(old.api, widget.api);
    _a = widget.alert;
    if (!same) {
      // 同一个位置换了一条告警：这一页记的东西是上一条的，全部重来；上一条还在路上的请求、还开着的
      // 「记成误报？」都作废（外审 I3）
      _gen++;
      _ackedHere = null;
      _deter = null;
      _deterKnown = false;
      _msg = '';
      _dim = false;
      if (_falseDialog) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && _falseDialog) Navigator.of(context, rootNavigator: true).pop(false);
        });
      }
    }
    unawaited(_refresh());
  }

  /// 记下刚问到的驱离（[s] 是 null = 这台狗没在驱离）。在它之前发出去的查询回来了也不许盖掉它。
  void _gotDeter(Map<String, dynamic>? s) {
    _deter = s;
    _deterKnown = true;
    _deterTick = _ticks;
    _deterAtMs = widget.now().millisecondsSinceEpoch;
    _deterApplied = ++_asked;
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  /// 向站点再问一遍：这条告警现在什么样（别人可能确认了、解决了）、这台狗有没有在驱离。
  /// 告警没问到就留着旧的。驱离没问到记成「说不清」：旧快照留着给人看「最后问到时」，但不当成现在的。
  /// 发出去之后这一页换了告警、或者更新的结果已经用上了：这一次的结果丢掉。
  Future<void> _refresh() async {
    final gen = _gen, key = _a.key, robot = _a.robot, isRobot = _isRobot;
    final seq = ++_asked;
    bool current() => mounted && gen == _gen;
    try {
      final now = (await widget.api.alerts(all: true)).where((x) => x.key == key);
      if (!current()) return;
      if (now.isNotEmpty && seq > _alertApplied) {
        _alertApplied = seq;
        setState(() => _a = now.first);
      }
    } on Object {
      if (!current()) return; // 留着旧的
    }
    if (!isRobot) return;
    try {
      final rows = await widget.api.deterrence();
      if (!current() || seq <= _deterApplied) return;
      final mine = rows.where((r) => r['robot_id'] == robot);
      setState(() {
        _deter = mine.isEmpty ? null : mine.first;
        _deterKnown = true;
        _deterTick = _ticks;
        _deterAtMs = widget.now().millisecondsSinceEpoch;
        _deterApplied = seq;
      });
    } on Object {
      if (!current() || seq <= _deterApplied) return;
      setState(() {
        _deterKnown = false;
        _deterApplied = seq;
      });
    }
  }

  Future<void> _ack() async {
    final gen = _gen, key = _a.key;
    try {
      final r = await widget.api.ackAlert(key);
      if (!mounted || gen != _gen) return; // 这一页已经是另一条告警了：上一条的回包不许动它
      final got = r['alert'] is Map ? Alert.fromWire(Map<String, dynamic>.from(r['alert'] as Map)) : null;
      if (got != null && got.key != key) {
        // 站点明说回的是另一条：不能当成这一条确认上了
        setState(() => _msg = tr("Not acknowledged: the site answered about another alarm (${got.key})",
            '没确认上：站点回的是另一条告警（${got.key}）'));
        return;
      }
      // 站点回了确认之后的这一条：谁、几点以站点记的为准。老站点不回正文：先在本地记成「我、现在」，
      // 下一次刷新拿到站点的再换。
      setState(() {
        if (got != null && got.ackedMs != null) {
          _a = got;
          _ackedHere = (got.ackedBy, got.ackedMs!);
        } else {
          _ackedHere = (widget.api.session?.name ?? '', widget.now().millisecondsSinceEpoch);
        }
        _msg = '';
      });
    } on SiteError catch (e) {
      if (mounted && gen == _gen) setState(() => _msg = tr('Not acknowledged: $e', '没确认上：$e'));
    }
  }

  static const int sirenLevel = SiteAlertScenePageSiren.level;
  static SirenState sirenState(Map<String, dynamic>? d) => SiteAlertScenePageSiren.of(d);

  Future<void> _siren() async {
    final gen = _gen, robot = _a.robot;
    try {
      final r = await widget.api.deterLevel(robot, sirenLevel);
      if (!mounted || gen != _gen) return;
      final s = r['session'] is Map ? Map<String, dynamic>.from(r['session'] as Map) : null;
      setState(() {
        if (s != null) _gotDeter(s);
        _msg = switch (sirenState(s)) {
          SirenState.on => '',
          SirenState.notFitted => tr('Level $sirenLevel is set, but this robot has no siren.',
              '已到第 $sirenLevel 级，但这台狗没有警笛。'),
          _ => tr('Level $sirenLevel requested. The robot has not confirmed the siren yet.',
              '已要求第 $sirenLevel 级，狗还没确认警笛开了。'),
        };
      });
    } on SiteError catch (e) {
      if (mounted && gen == _gen) setState(() => _msg = tr("Siren didn't start: $e", '警笛没开：$e'));
    }
  }

  Future<void> _falseAlarm() async {
    final gen = _gen, key = _a.key;
    _falseDialog = true;
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              content: Text(tr(
                  'Mark this as a false alarm? It closes the alarm. Deterrence, if running, is not stopped here.',
                  '记成误报？这条告警就关了。正在进行的驱离不会在这儿停。')),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
                FilledButton(
                    key: SiteAlertScenePage.falseGoKey,
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('False alarm', '误报'))),
              ],
            ));
    _falseDialog = false;
    // 问的是那一条：这一页换了告警，这次的「是」不算数
    if (ok != true || !mounted || gen != _gen) return;
    try {
      await widget.api.resolveAlert(key);
      if (mounted && gen == _gen) Navigator.pop(context);
    } on SiteError catch (e) {
      if (mounted && gen == _gen) setState(() => _msg = tr('Not closed: $e', '没关上：$e'));
    }
  }

  Future<void> _photo(String taskId) async {
    final gen = _gen;
    List<Map<String, dynamic>> runs;
    try {
      runs = await widget.api.runs(mission: taskId);
    } on SiteError catch (e) {
      if (mounted && gen == _gen) {
        setState(() => _msg = tr("Can't find the record for that task: $e", '找不到那一趟的记录：$e'));
      }
      return;
    }
    if (!mounted || gen != _gen) return;
    if (runs.isEmpty) {
      setState(() => _msg = tr(
          'The record for that task has not reached the site yet (it uploads after the robot arrives and takes photos)',
          '那一趟的记录还没传到站点（狗到了、拍完会传上来）'));
      return;
    }
    await Navigator.push(
        context,
        MaterialPageRoute<void>(
            builder: (_) => SiteRunPage(api: widget.api, runId: (runs.first['id'] as num).toInt())));
  }

  /// 就地驱离（W33，决策 48）：狗在跑的任务先撤掉，就在它这儿开一场、从 L1（灯光）起。
  Future<void> _deterHere() async {
    final gen = _gen, robot = _a.robot;
    try {
      final s = await widget.api.deterStart(robot);
      if (!mounted || gen != _gen) return;
      final lv = s['session'] is Map ? (s['session'] as Map)['level'] : null;
      setState(() {
        if (s['session'] is Map) _gotDeter(Map<String, dynamic>.from(s['session'] as Map));
        _msg = tr('Deterrence started at $robot (L${lv ?? 1}). Escalate or end it on the deterrence page',
            '已在 $robot 这儿开驱离（L${lv ?? 1}），驱离页可以升级、解除');
      });
    } on SiteError catch (e) {
      if (mounted && gen == _gen) setState(() => _msg = tr("Can't start deterrence: $e", '开不了：$e'));
    }
  }

  String _ago(int ms) {
    final s = widget.now().difference(DateTime.fromMillisecondsSinceEpoch(ms)).inSeconds;
    if (s < 60) return tr('$s s ago', '$s 秒前');
    if (s < 3600) return tr('${s ~/ 60} min ago', '${s ~/ 60} 分钟前');
    return tr('${s ~/ 3600} h ago', '${s ~/ 3600} 小时前');
  }

  static String _two(int n) => n.toString().padLeft(2, '0');

  /// 当地时间 时:分:秒。
  static String hms(int ms) {
    final d = DateTime.fromMillisecondsSinceEpoch(ms);
    return '${_two(d.hour)}:${_two(d.minute)}:${_two(d.second)}';
  }

  static String span(int ms) => SiteAlertScenePageSpan.of(ms);

  String _stateLine() {
    if (!_open) return tr('Closed at ${hms(_a.resolvedMs!)}', '${hms(_a.resolvedMs!)} 已关闭');
    if (_acked) {
      final by = _a.ackedBy.isNotEmpty ? _a.ackedBy : (_ackedHere?.$1 ?? '');
      final at = hms(_a.ackedMs ?? _ackedHere!.$2);
      final me = by.isNotEmpty && by == widget.api.session?.name;
      return me
          ? tr('Acknowledged by you at $at', '你在 $at 确认了')
          : tr('Acknowledged by $by at $at', '$by 在 $at 确认了');
    }
    return tr('Unacknowledged for ${span(widget.now().millisecondsSinceEpoch - _a.firstMs)}',
        '没人确认已经 ${span(widget.now().millisecondsSinceEpoch - _a.firstMs)}');
  }

  Map? get _persons {
    final p = _a.context['persons'] ?? _d?['persons'];
    return p is Map ? p : null;
  }

  /// 「在哪」下面那一句：人离狗多远、驱离到第几级。
  String _whereLine() {
    final parts = <String>[];
    final m = _persons?['nearest_m'];
    if (m is num && _isRobot) {
      // 站点只给最近的距离，没给方向（前后相机汇总的）：不说「前方」（外审 M1）
      parts.add(tr('Nearest person ${m.toStringAsFixed(1)} m from ${_a.robot}.',
          '最近的人距 ${_a.robot} ${m.toStringAsFixed(1)} 米。'));
    }
    final d = _d;
    if (d != null) {
      parts.add(tr('Deterrence level ${d['level']}.', '驱离第 ${d['level']} 级。'));
    } else if (_isRobot && !_deterFresh) {
      final old = _deter;
      parts.add(old == null
          ? tr('Deterrence status unknown.', '说不清有没有在驱离。')
          : tr('Deterrence status unknown (level ${old['level']} when last checked at ${hms(_deterAtMs)}).',
              '说不清有没有在驱离（${hms(_deterAtMs)} 最后问到时是第 ${old['level']} 级）。'));
    }
    return parts.join(' ');
  }

  Widget _head() {
    final p1 = _a.level == 'P1';
    final solid = p1 && _open;
    final fg = solid ? D1Color.onSolid : D1Color.text;
    return Container(
        key: SiteAlertScenePage.headKey,
        color: solid ? D1Color.p1 : D1Color.raised,
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(crossAxisAlignment: CrossAxisAlignment.center, children: [
            PriorityShape(level: _a.level, color: solid ? D1Color.onSolid : priorityColor(_a.level)),
            const SizedBox(width: 6),
            Text(_a.level, style: D1Text.label.copyWith(color: solid ? D1Color.onSolid : priorityColor(_a.level))),
            const SizedBox(width: 10),
            Expanded(
                child: Text(alertTitle(_a.kind, _a.title), style: D1Text.heading.copyWith(color: fg))),
            const SizedBox(width: 8),
            Text(hms(_a.firstMs), style: D1Text.osd.copyWith(color: fg)),
          ]),
          const SizedBox(height: 2),
          Text(_stateLine(),
              key: SiteAlertScenePage.stateKey,
              style: D1Text.label.copyWith(color: solid ? D1Color.onSolid : D1Color.textSecondary)),
        ]));
  }

  /// 报警块：头 + 实时画面，外面一圈报警框（没确认的 P1 闪）。
  Widget _alarmBlock() {
    final p1 = _a.level == 'P1' && _open;
    final frame = p1 ? D1Color.p1Frame : D1Color.rule;
    return AnimatedOpacity(
        opacity: _dim ? D1Motion.p1FlashMinOpacity : 1.0,
        duration: const Duration(milliseconds: D1Motion.fast),
        child: Container(
            decoration: BoxDecoration(
                color: D1Color.surface, border: Border.all(color: frame, width: p1 ? D1Size.alarmFrame : 1)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              _head(),
              if (_isRobot && _open)
                Padding(
                    padding: const EdgeInsets.all(8), child: SiteVideo(api: widget.api, robotId: _a.robot)),
            ])));
  }

  Widget _big(Key? key, String label, VoidCallback? onPressed, {bool primary = false}) {
    final style = (primary ? FilledButton.styleFrom : OutlinedButton.styleFrom)(
        minimumSize: const Size.fromHeight(D1Size.buttonPhone), padding: const EdgeInsets.symmetric(horizontal: 8));
    final child = Text(label, textAlign: TextAlign.center, maxLines: 2, overflow: TextOverflow.ellipsis);
    return primary
        ? FilledButton(key: key, style: style, onPressed: onPressed, child: child)
        : OutlinedButton(key: key, style: style, onPressed: onPressed, child: child);
  }

  Widget _actions() {
    final s = widget.api.session;
    final handle = s?.canHandleAlerts ?? false;
    final dispatch = s?.canDispatch ?? false;
    // 新鲜的才按它的状态摆；只剩旧快照时不猜警笛开没开，让人能再要求一次（没在驱离了站点会说）
    final d = _d;
    final sound = tr('Sound siren (level $sirenLevel)', '开警笛（第 $sirenLevel 级）');
    Widget second;
    if (d != null && dispatch) {
      second = switch (sirenState(d)) {
        SirenState.on => _big(SiteAlertScenePage.sirenKey, tr('Siren is on', '警笛已开'), null),
        SirenState.notFitted =>
          _big(SiteAlertScenePage.sirenKey, tr('No siren on this robot', '这台狗没有警笛'), null),
        SirenState.requested =>
          _big(SiteAlertScenePage.sirenKey, tr('Siren not confirmed. Retry', '警笛没确认，再试'), _siren),
        _ => _big(SiteAlertScenePage.sirenKey, sound, _siren),
      };
    } else if (_deter != null && !_deterFresh && dispatch) {
      second = _big(SiteAlertScenePage.sirenKey, sound, _siren);
    } else if (_a.kind == 'dog_sees_person' && _isRobot && dispatch) {
      second = _big(SiteAlertScenePage.deterKey, tr('Deter here', '就地驱离'), _deterHere);
    } else {
      second = _big(null, tr('Siren: not available', '警笛：现在用不了'), null);
    }
    return Column(children: [
      Row(children: [
        Expanded(
            child: _big(SiteAlertScenePage.ackKey, tr("I'll handle it", '我来处理'),
                handle && _open && !_acked ? _ack : null,
                primary: true)),
        const SizedBox(width: 8),
        Expanded(child: second),
      ]),
      const SizedBox(height: 8),
      Row(children: [
        Expanded(
            child: _big(SiteAlertScenePage.falseKey, tr('False alarm', '误报'), handle && _open ? _falseAlarm : null)),
        const SizedBox(width: 8),
        Expanded(child: _big(null, tr('Talk: not available', '对讲：还没有'), null)),
      ]),
    ]);
  }

  /// 事件经过：站点记下来的时刻，一行一条；最后一行是「接下来会自动发生什么」（琥珀色）。
  List<Widget> _timeline() {
    final rows = <(int, String)>[
      (
        _a.firstMs,
        [alertTitle(_a.kind, _a.title), if (_a.detail.isNotEmpty && appLang.value == AppLang.zh) _a.detail]
            .join(' · ')
      ),
      if (_a.count > 1 && _a.lastMs > _a.firstMs)
        (_a.lastMs, tr('Raised again (${_a.count} times in total).', '又报了（一共 ${_a.count} 次）。')),
    ];
    // 开始、升级是带时刻的旧事，旧快照里的也照摆；「接下来」是现在的计划，只有新鲜的才说
    final d = _deter;
    if (d != null) {
      if (d['started_ms'] is num) {
        rows.add(((d['started_ms'] as num).toInt(), tr('Deterrence started.', '开始驱离。')));
      }
      if (d['level_ms'] is num && d['level'] is num && (d['level'] as num) > 1) {
        rows.add((
          (d['level_ms'] as num).toInt(),
          tr('Deterrence went to level ${d['level']}.', '驱离升到第 ${d['level']} 级。')
        ));
      }
    }
    final at = _a.ackedMs ?? _ackedHere?.$2;
    if (at != null) {
      final by = _a.ackedBy.isNotEmpty ? _a.ackedBy : (_ackedHere?.$1 ?? '');
      rows.add((at, tr('Acknowledged by $by.', '$by 确认了。')));
    }
    if (_a.resolvedMs != null) rows.add((_a.resolvedMs!, tr('Closed.', '已关闭。')));
    rows.sort((x, y) => x.$1.compareTo(y.$1));
    final staleNote = _isRobot && d != null && !_deterFresh
        ? tr('Deterrence status not updated since ${hms(_deterAtMs)}.', '驱离的状态从 ${hms(_deterAtMs)} 起没再问到。')
        : null;
    final next = _deterFresh && d != null && d['auto'] == true && d['next_in_s'] is num
        ? tr('Next: level ${(d['level'] as num? ?? 0) + 1} in ${d['next_in_s']} s if no one responds.',
            '接下来：没人处理的话 ${d['next_in_s']} 秒后升到第 ${(d['level'] as num? ?? 0) + 1} 级。')
        : null;
    return [
      Text(tr('What happened', '事件经过'), style: D1Text.heading),
      const SizedBox(height: 4),
      for (final r in rows)
        Padding(
            padding: const EdgeInsets.symmetric(vertical: 2),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(hms(r.$1), style: D1Text.osd.copyWith(color: D1Color.textSecondary)),
              const SizedBox(width: 12),
              Expanded(child: Text(r.$2, style: D1Text.caption.copyWith(color: D1Color.text))),
            ])),
      if (staleNote != null)
        Padding(
            padding: const EdgeInsets.symmetric(vertical: 2),
            child: Text(staleNote, key: SiteAlertScenePage.staleKey, style: D1Text.label.copyWith(color: D1Color.p2))),
      if (next != null)
        Padding(
            padding: const EdgeInsets.symmetric(vertical: 2),
            child: Text(next, key: SiteAlertScenePage.nextKey, style: D1Text.label.copyWith(color: D1Color.p2))),
    ];
  }

  List<Widget> _details(Map? pose, Map? point, String? taskId, (String, String, List<MapMark>)? marks) {
    final ctx = _a.context;
    final small = D1Text.caption.copyWith(color: D1Color.text);
    return [
      if (appLang.value != AppLang.zh && _a.detail.isNotEmpty)
        // 站点给的原因是中文：英文界面也摆出来（比没有强），放在细节里
        Text(_a.detail, style: small),
      if (ctx['zone'] != null) Text(tr('Zone: ${ctx['zone']}', '防区：${ctx['zone']}'), style: small),
      if (point != null)
        Text(
            tr(
                'Intercept point: ${point['name']}  (${(point['x'] as num).toStringAsFixed(1)}, '
                    '${(point['y'] as num).toStringAsFixed(1)})',
                '拦截点：${point['name']}  (${(point['x'] as num).toStringAsFixed(1)}, '
                    '${(point['y'] as num).toStringAsFixed(1)})'),
            style: small),
      if (pose != null)
        Text(
            tr(
                'Robot last at: (${(pose['x'] as num).toStringAsFixed(1)}, '
                    '${(pose['y'] as num).toStringAsFixed(1)})  map ${pose['map_id']}:${pose['map_version']}'
                    '${pose['at_ms'] is num ? '  · reported ${_ago((pose['at_ms'] as num).toInt())}' : ''}',
                '狗最后在：(${(pose['x'] as num).toStringAsFixed(1)}, '
                    '${(pose['y'] as num).toStringAsFixed(1)})  图 ${pose['map_id']}:${pose['map_version']}'
                    '${pose['at_ms'] is num ? '  · ${_ago((pose['at_ms'] as num).toInt())}报的' : ''}'),
            style: small),
      if (taskId != null) Text(tr('Task: $taskId', '那一趟：$taskId'), style: small),
      if (ctx.isEmpty)
        Text(
            tr('This alarm has no scene data (older site, or a site-level alarm)',
                '这条告警没带现场（老站点，或站点那一行的告警）'),
            style: small),
      const SizedBox(height: 8),
      Wrap(spacing: 8, runSpacing: 8, children: [
        if (marks != null)
          OutlinedButton.icon(
              key: SiteAlertScenePage.mapKey,
              icon: const Icon(Icons.map_outlined),
              label: Text(tr('Show on map', '在图上看')),
              onPressed: () => Navigator.push(
                  context,
                  MaterialPageRoute<void>(
                      builder: (_) => SiteMapPreviewPage(
                          api: widget.api, mapId: marks.$1, version: marks.$2, marks: marks.$3)))),
        if (_isRobot)
          OutlinedButton.icon(
              key: SiteAlertScenePage.videoKey,
              icon: const Icon(Icons.videocam_outlined),
              label: Text(tr('Scene recording', '现场录像')),
              onPressed: () => Navigator.push(
                  context,
                  MaterialPageRoute<void>(
                      builder: (_) =>
                          SiteRecordingsPage(api: widget.api, robotId: _a.robot, aroundMs: _a.firstMs)))),
        if (taskId != null)
          OutlinedButton.icon(
              key: SiteAlertScenePage.photoKey,
              icon: const Icon(Icons.photo_camera_outlined),
              label: Text(tr('Scene photos', '现场照片')),
              onPressed: () => _photo(taskId)),
      ]),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final ctx = _a.context;
    final pose = ctx['pose'] is Map ? ctx['pose'] as Map : null;
    final point = ctx['intercept'] is Map ? ctx['intercept'] as Map : null;
    final taskId = ctx['task_id'] is String ? ctx['task_id'] as String : null;
    final marks = sceneMarks(_a);
    final where = '${ctx['zone'] ?? (_isRobot ? _a.robot : tr('Site', '站点'))}';
    final whereLine = _whereLine();
    final rest = <Widget>[
      Text(where, style: D1Text.phoneTitle),
      if (whereLine.isNotEmpty) Text(whereLine, style: D1Text.body),
      const SizedBox(height: 12),
      _actions(),
      if (_msg.isNotEmpty)
        Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(_msg, key: SiteAlertScenePage.msgKey, style: D1Text.body.copyWith(color: D1Color.p2))),
      const SizedBox(height: 16),
      ..._details(pose, point, taskId, marks),
      const SizedBox(height: 16),
      ..._timeline(),
    ];
    // 横屏、平板、桌面：报警块在左、其余在右各滚各的；竖屏的手机一列到底。都不用懒加载的列表 ——
    // 四个大按钮、现场细节不管滚到哪儿都得在（无障碍、测试按 key 找）。
    final wide = MediaQuery.sizeOf(context).width >= 700;
    return Scaffold(
      appBar: AppBar(
          title: Text(tr('Alarm', '告警')),
          actions: [
            if (widget.api.session?.canDispatch ?? false)
              StopAllButton(api: widget.api, onResult: (m) => setState(() => _msg = m)),
          ]),
      body: wide
          ? Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(
                  flex: 5,
                  child: SingleChildScrollView(padding: const EdgeInsets.all(12), child: _alarmBlock())),
              Expanded(
                  flex: 4,
                  child: SingleChildScrollView(
                      padding: const EdgeInsets.fromLTRB(0, 12, 12, 12),
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: rest))),
            ])
          : SingleChildScrollView(
              padding: const EdgeInsets.all(12),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                _alarmBlock(),
                const SizedBox(height: 12),
                ...rest,
              ])),
    );
  }
}
