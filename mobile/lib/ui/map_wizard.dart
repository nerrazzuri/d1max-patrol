/// 建图向导（W54，用户 2026-10-31）：只买设备的客户自己把第一张图建出来。一个全屏页，做完回主页。
///
/// 七步：准备（选机器人、场地类型、显示名，自动检查）→ 怎么开（遥控器 / 手机）→ 走一圈（边走边建，看着图
/// 长、按规则提示）→ 建图（等站点收到这一版，给结论）→ 禁行区（确认了才能下发）→ 原点（下发给这台、
/// 在充电桩前标原点）→ 完成。
///
/// 向导做到哪一步记在站点上（`/api/map-wizard`），换手机、关了 App 都能接着做；地图号、版本站点起。
/// 禁行区排在原点前面：没确认禁行区的图下发不了（W10），标原点又要机器人先载上这一版。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p3_robots.dart' show robotActive, robotBattery, robotOnline;
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_mapping.dart' show PreviewPainter, SiteMapPreviewPage;
import 'site_maps.dart' show robotMapTag;
import 'site_robot.dart' show SiteRobotPage;
import 'site_routes.dart';
import 'site_tabs.dart' show SchedulePage;
import 'site_teleop.dart';

const List<String> wizardSteps = ['prepare', 'drive', 'walk', 'build', 'zones', 'origin', 'done'];

/// 开向导（全屏，推在最上面）。
Future<void> openMapWizard(BuildContext context, SiteApi api) =>
    Navigator.of(context, rootNavigator: true)
        .push(MaterialPageRoute<void>(fullscreenDialog: true, builder: (_) => MapWizardPage(api: api)));

/// 登录进站点以后（导览走完）：管理员、工程师，站点一张图都没有 → 弹「开始建图」。点过「稍后」记在账号上
/// （`guide.map_later`），不再每次弹；有没做完的向导就改问「接着做」（这个不记稍后）。
Future<void> maybeOfferMapping(BuildContext context, SiteApi api) async {
  if (!(api.session?.canManageMaps ?? false)) return;
  Map<String, dynamic> w, me;
  try {
    w = await api.mapWizard();
    me = await api.me();
  } on SiteError {
    return; // 老站点没有向导
  } on FormatException {
    return;
  }
  if ((w['maps'] as num? ?? 1) > 0 || !context.mounted) return;
  final wiz = w['wizard'] is Map ? w['wizard'] as Map : null;
  final g = me['guide'];
  if (wiz == null && g is Map && g['map_later'] == true) return;
  final step = wiz == null ? 0 : wizardSteps.indexOf('${wiz['step']}') + 1;
  final go = await showDialog<bool>(
    context: context,
    builder: (c) => AlertDialog(
      key: mapOfferKey,
      title: Text(wiz != null
          ? tr('Mapping stopped at step $step. Continue?', '上次建图停在第 $step 步，是否继续？')
          : tr('This site has no map yet', '此站点尚无地图')),
      content: Text(tr(
          'A map is needed before patrolling. Start mapping now? (about 20–40 minutes)',
          '巡逻前需先建图。现在开始建图？（约 20–40 分钟）')),
      actions: [
        TextButton(key: mapOfferLaterKey, onPressed: () => Navigator.pop(c, false), child: Text(tr('Later', '稍后'))),
        FilledButton(
            key: mapOfferStartKey,
            onPressed: () => Navigator.pop(c, true),
            child: Text(wiz != null ? tr('Continue', '继续') : tr('Start mapping', '开始建图'))),
      ],
    ),
  );
  if (!context.mounted) return;
  if (go == true) {
    await openMapWizard(context, api);
  } else if (go == false && wiz == null) {
    try {
      await api.setGuide(<String, dynamic>{'map_later': true});
    } on SiteError {
      // 下次还弹
    }
  }
}

const Key mapOfferKey = Key('map-offer');
const Key mapOfferLaterKey = Key('map-offer-later');
const Key mapOfferStartKey = Key('map-offer-start');

/// 建图结论的原因代号（站点给的）说人话。
String qualityReasonText(String code) => switch (code) {
      'no_loop' => tr("Loop not closed at the start: the map may be skewed",
          '未回到起点闭合：地图可能有偏差'),
      'big_shift' => tr('Large loop-closure correction: walls may show double lines',
          '闭合修正较大：墙边可能有重影'),
      'short_path' => tr('Path too short: some areas may be missing', '行走距离过短：可能有区域缺失'),
      _ => code,
    };

/// 结论三档（加「说不清」）。
String verdictText(String v) => switch (v) {
      'usable' => tr('Usable', '能用'),
      'problem' => tr('Has problems', '有问题'),
      _ => tr('Unknown', '无法评估'),
    };

NwTone verdictTone(String v) => switch (v) {
      'usable' => NwTone.success,
      'problem' => NwTone.warning,
      _ => NwTone.neutral,
    };

/// 站点拒向导某一步的原因（`reason`）说人话；别的照站点原话。
String wizardErrorText(SiteError e) => switch ('${e.body['reason'] ?? ''}') {
      'coming_soon' => tr('Mapping settings for this site type are not tuned yet. Contact your delivery engineer.',
          '此类场地的建图参数尚未调校，请联系交付工程师。'),
      'wizard_active' => tr('This site is already being mapped.', '此站点正在建图。'),
      'not_built' => tr('This version is not built yet.', '此版本尚未建成。'),
      'no_wizard' => tr('The mapping wizard was closed (possibly on another phone).', '建图向导已关闭（可能在其他手机上关闭）。'),
      _ => refusalText(e),
    };

/// 机器人没接录包命令的原因说人话。
String mappingAckText(Map<String, dynamic> r) {
  final ack = (r['ack'] as Map?) ?? const {};
  final reason = '${ack['reason'] ?? ack['result'] ?? ''}';
  return switch (reason) {
    'busy' => tr('The robot is busy (the previous recording or build is not finished). Try again later.',
        '机器人忙（上一次录制或建图尚未完成），请稍后重试。'),
    'overheat' => tr('The robot is too hot. Let it cool down before mapping.', '机器人温度过高，请降温后再建图。'),
    'storage_full' => tr("The robot's storage is full. Wait for pending uploads to finish.",
        '机器人存储已满，请等待已有文件上传完成后再建图。'),
    'not_recording' => tr('The robot is not recording.', '机器人未在录制。'),
    _ => tr('The robot rejected it: $reason', '机器人未接受：$reason'),
  };
}

/// 走的时候的提示（设计 §6，按规则，不用模型）。[speed] 米/秒、[turn] 弧度/秒，没有是 null。
String walkTip(
    {required double pathM,
    required double fromStartM,
    required Duration elapsed,
    double? speed,
    double? turn}) {
  if (speed != null && speed > 0.6) return tr('Slow down: high speed blurs the map', '请减速：速度过快会导致地图模糊');
  if (turn != null && turn > 0.8) return tr('Turn more slowly', '请放慢转向');
  if (elapsed.inMinutes >= 40) {
    return tr("Recording for a long time. You can build a version now to check.", '已录制较长时间，可先建一版查看效果。');
  }
  if (pathM > 15 && fromStartM < 1.0) {
    return tr("Back at the start. Tap \"Finish walking\" once every area is covered.", '已回到起点。全部区域走完后点「结束行走」。');
  }
  if (pathM > 20 && fromStartM > 5) {
    return tr('Finish at the start (green dot) so the map closes.',
        '请最后回到起点（绿点），以便地图闭合。');
  }
  return tr('Walk slowly, enter every room and corridor, and pause at doorways.',
      '请缓慢行走，进入每个房间和走廊，在门口稍作停留。');
}

double _num(Object? v) => v is num ? v.toDouble() : 0;

List<Offset> _trail(Map<String, dynamic> pv) => [
      for (final p in (pv['trail'] as List? ?? const []))
        if (p is List && p.length >= 2 && p[0] is num && p[1] is num) Offset(_num(p[0]), _num(p[1])),
    ];

double _pathLen(List<Offset> t) {
  var s = 0.0;
  for (var i = 1; i < t.length; i++) {
    s += (t[i] - t[i - 1]).distance;
  }
  return s;
}

// ------------------------------------------------------------ 边走边建的预览

/// 问机器人边走边建的预览、画出来（走过的路、起点、现在在哪）。[onData] 每拿到一份调一次（算提示用）。
class MappingPreviewView extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  final Duration period;
  final void Function(Map<String, dynamic> pv)? onData;
  final bool compact;
  const MappingPreviewView(
      {super.key,
      required this.api,
      required this.robotId,
      this.period = const Duration(seconds: 3),
      this.onData,
      this.compact = false});

  static const Key canvasKey = Key('wizard-preview');

  @override
  State<MappingPreviewView> createState() => _MappingPreviewViewState();
}

class _MappingPreviewViewState extends State<MappingPreviewView> {
  Map<String, dynamic> _pv = const <String, dynamic>{};
  Uint8List? _png;
  int _seq = 0;
  String? _run;
  Timer? _timer;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_poll());
    _timer = Timer.periodic(widget.period, (_) => unawaited(_poll()));
  }

  @override
  void dispose() {
    _timer?.cancel();
    final old = _png;
    if (old != null) unawaited(MemoryImage(old).evict());
    super.dispose();
  }

  Future<void> _poll() async {
    if (_busy) return;
    _busy = true;
    try {
      var d = await widget.api.mappingPreview(widget.robotId, since: _seq);
      final run = d['run'];
      if (d['live'] == true && run is String && _run != null && run != _run) {
        _seq = 0; // 换了一趟：从头取
        d = await widget.api.mappingPreview(widget.robotId);
      }
      if (!mounted) return;
      setState(() {
        _pv = d;
        if (d['run'] is String) _run = d['run'] as String;
        _seq = (d['seq'] as num?)?.toInt() ?? _seq;
        final png = d['png'];
        if (png is String) {
          try {
            final old = _png;
            _png = base64Decode(png);
            if (old != null) unawaited(MemoryImage(old).evict());
          } on FormatException {
            // 坏的：留着上一张
          }
        }
      });
      widget.onData?.call(d);
    } on SiteError {
      // 连不上：留着上一张，下次再问
    } finally {
      _busy = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final d = _pv, png = _png;
    final w = _num(d['width']) == 0 ? 1.0 : _num(d['width']), h = _num(d['height']) == 0 ? 1.0 : _num(d['height']);
    final origin = d['origin'];
    final pose = d['pose'];
    final painter = PreviewPainter(
      res: _num(d['res']) == 0 ? 0.1 : _num(d['res']),
      origin: origin is List && origin.length >= 2 ? Offset(_num(origin[0]), _num(origin[1])) : Offset.zero,
      width: w,
      height: h,
      trail: _trail(d),
      pose: pose is List && pose.length >= 3 && pose.every((v) => v is num) ? [for (final v in pose) _num(v)] : null,
    );
    return DecoratedBox(
      key: MappingPreviewView.canvasKey,
      decoration: BoxDecoration(
          color: c.surfaceVariant, border: Border.all(color: c.outline), borderRadius: BorderRadius.circular(Nw.r8)),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(Nw.r8),
        child: png == null
            ? Center(
                child: Padding(
                padding: const EdgeInsets.all(Nw.s8),
                child: Text(tr('The map appears here once the robot starts moving', '机器人开始行走后，地图在此显示'),
                    textAlign: TextAlign.center, style: NwText.bodySmall.copyWith(color: c.textSecondary)),
              ))
            : InteractiveViewer(
                maxScale: widget.compact ? 1 : 8,
                panEnabled: !widget.compact,
                scaleEnabled: !widget.compact,
                child: Center(
                  child: AspectRatio(
                    aspectRatio: w / h,
                    child: Stack(fit: StackFit.expand, children: [
                      Image.memory(png, fit: BoxFit.fill, filterQuality: FilterQuality.none, gaplessPlayback: true),
                      CustomPaint(painter: painter),
                    ]),
                  ),
                ),
              ),
      ),
    );
  }
}

// ------------------------------------------------------------ 向导页

class MapWizardPage extends StatefulWidget {
  final SiteApi api;
  final Duration poll;
  const MapWizardPage({super.key, required this.api, this.poll = const Duration(seconds: 3)});

  static const Key exitKey = Key('wizard-exit');
  static const Key exitPauseKey = Key('wizard-exit-pause');
  static const Key exitAbandonKey = Key('wizard-exit-abandon');
  static const Key stepKey = Key('wizard-step');
  static const Key nextKey = Key('wizard-next');
  static const Key backKey = Key('wizard-back');
  static Key robotKey(String id) => Key('wizard-robot-$id');
  static Key siteTypeKey(String t) => Key('wizard-site-$t');
  static const Key labelKey = Key('wizard-label');
  static const Key checksKey = Key('wizard-checks');
  static Key driveKey(String d) => Key('wizard-drive-$d');
  static const Key startRecKey = Key('wizard-start-rec');
  static const Key walkStatusKey = Key('wizard-walk-status');
  static const Key controlKey = Key('wizard-control');
  static const Key tipKey = Key('wizard-tip');
  static const Key teleopKey = Key('wizard-teleop');
  static const Key doneWalkKey = Key('wizard-done-walk');
  static const Key walkBuiltKey = Key('wizard-walk-built');
  static const Key doneWalkOkKey = Key('wizard-done-walk-ok');
  static const Key buildStatusKey = Key('wizard-build-status');
  static const Key verdictKey = Key('wizard-verdict');
  static const Key useKey = Key('wizard-use');
  static const Key rewalkKey = Key('wizard-rewalk');
  static const Key zonesOpenKey = Key('wizard-zones-open');
  static const Key zonesEmptyKey = Key('wizard-zones-empty');
  static const Key zonesConfirmKey = Key('wizard-zones-confirm');
  static const Key sendMapKey = Key('wizard-send-map');
  static const Key markHomeKey = Key('wizard-mark-home');
  static const Key replaceHomeKey = Key('wizard-replace-home');
  static const Key relocKey = Key('wizard-reloc');
  static const Key routesKey = Key('wizard-routes');
  static const Key scheduleKey = Key('wizard-schedule');
  static const Key homeKey = Key('wizard-home');

  @override
  State<MapWizardPage> createState() => _MapWizardPageState();
}

class _MapWizardPageState extends State<MapWizardPage> {
  SiteApi get api => widget.api;

  /// 站点上的向导（没开始是 null）；做完了留一份给「完成」那一步。
  Map<String, dynamic>? _w;
  Map<String, dynamic>? _finished;
  bool _loading = true;
  bool _busy = false;
  String _err = '';
  Timer? _timer;

  // 准备
  List<Map<String, dynamic>> _robots = const [];
  String? _robot;
  String _siteType = 'indoor';
  final TextEditingController _label = TextEditingController();
  String _drive = 'remote';

  // 走一圈
  Map<String, dynamic>? _rv;
  Map<String, dynamic> _pv = const {};
  Offset? _lastPose;
  double? _lastYaw;
  DateTime? _lastAt;
  double? _speed, _turn;
  DateTime? _walkSince;

  // 禁行区
  Map<String, dynamic>? _zones;

  String get _step => _finished != null ? 'done' : '${_w?['step'] ?? 'prepare'}';
  String get _mapId => '${_w?['map_id'] ?? _finished?['map_id'] ?? ''}';
  String get _version => '${_w?['version'] ?? _finished?['version'] ?? ''}';
  String get _robotId => '${_w?['robot_id'] ?? _finished?['robot_id'] ?? _robot ?? ''}';

  @override
  void initState() {
    super.initState();
    unawaited(_load());
    _timer = Timer.periodic(widget.poll, (_) => unawaited(_tick()));
  }

  @override
  void dispose() {
    _timer?.cancel();
    _label.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final got = await Future.wait([api.mapWizard(), api.robots()]);
      if (!mounted) return;
      final w = got[0] as Map<String, dynamic>;
      final robots = [for (final r in got[1] as List<Map<String, dynamic>>) if (r['revoked'] != true) r];
      setState(() {
        _w = (w['wizard'] as Map?)?.cast<String, dynamic>();
        _robots = robots;
        _robot ??= '${_w?['robot_id'] ?? (robots.isNotEmpty ? robots.first['robot_id'] : '')}';
        if (_w != null) {
          _siteType = '${_w!['site_type'] ?? 'indoor'}';
          _drive = '${_w!['drive'] ?? 'remote'}';
          if (_label.text.isEmpty) _label.text = '${_w!['label'] ?? ''}';
        }
        _loading = false;
        _err = '';
      });
      await _enterStep();
    } on SiteError catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          _err = refusalText(e);
        });
      }
    }
  }

  /// 每隔一会儿：按这一步要的问一下。
  Future<void> _tick() async {
    if (!mounted || _busy) return;
    switch (_step) {
      case 'prepare':
        try {
          final robots = await api.robots();
          if (mounted) setState(() => _robots = [for (final r in robots) if (r['revoked'] != true) r]);
        } on SiteError {
          // 下次再问
        }
      case 'walk':
        await _pollRobot();
        await _reloadWizard(); // 机器人重启过、这一趟自己收尾了：站点上的建图结果会变
      case 'origin':
        await _pollRobot();
        if (_sent) await _checkActivation();
      case 'build':
        await _reloadWizard();
    }
  }

  Future<void> _pollRobot() async {
    if (_robotId.isEmpty) return;
    try {
      final r = await api.robot(_robotId);
      if (mounted) setState(() => _rv = r);
    } on SiteError {
      // 下次再问
    }
  }

  Future<void> _reloadWizard() async {
    try {
      final d = await api.mapWizard();
      if (!mounted) return;
      setState(() => _w = (d['wizard'] as Map?)?.cast<String, dynamic>());
    } on SiteError {
      // 下次再问
    }
  }

  /// 进到某一步时要先拿的。
  Future<void> _enterStep() async {
    switch (_step) {
      case 'walk':
        _walkSince ??= DateTime.now();
        await _pollRobot();
      case 'zones':
        await _loadZones();
      case 'origin':
        await _pollRobot();
    }
  }

  Future<void> _act(Map<String, dynamic> body) async {
    setState(() {
      _busy = true;
      _err = '';
    });
    final id = _w?['id'];
    if (body['action'] != 'start' && id != null) body = <String, dynamic>{...body, 'wizard_id': id};
    try {
      final d = await api.mapWizardAction(body);
      if (!mounted) return;
      final w = (d['wizard'] as Map?)?.cast<String, dynamic>();
      setState(() {
        if (body['action'] == 'finish') {
          _finished = w ?? _w;
          _w = null;
        } else {
          _w = w;
        }
      });
      await _enterStep();
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = wizardErrorText(e));
      rethrow;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _go(String step, [Map<String, dynamic> extra = const {}]) async {
    try {
      await _act(<String, dynamic>{'action': 'step', 'step': step, ...extra});
    } on SiteError {
      // 已经写在 _err 里
    }
  }

  // ---- 退出

  Future<void> _exit() async {
    if (_w == null) {
      if (mounted) Navigator.pop(context);
      return;
    }
    final choice = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Pause mapping?', '暂停建图？')),
        content: Text(tr('Progress is saved on the site; you can continue from this step later.',
            '进度已保存在站点，下次可从此步继续。')),
        actions: [
          TextButton(
              key: MapWizardPage.exitAbandonKey,
              onPressed: () => Navigator.pop(c, 'abandon'),
              child: Text(tr('Abandon mapping', '放弃建图'))),
          TextButton(onPressed: () => Navigator.pop(c), child: Text(tr('Continue', '继续'))),
          FilledButton(
              key: MapWizardPage.exitPauseKey,
              onPressed: () => Navigator.pop(c, 'pause'),
              child: Text(tr('Pause', '暂停'))),
        ],
      ),
    );
    if (!mounted || choice == null) return;
    if (choice == 'abandon') {
      // 在走一圈：先停录（预览可能还没回来，不看它）。机器人说本来就没在录也算停了；别的不放弃，说原因
      if (_step == 'walk') {
        try {
          final r = await api.mapping(_robotId, 'stop');
          final ack = (r['ack'] as Map?) ?? const {};
          if (ack['result'] != 'accepted' && '${ack['reason'] ?? ''}' != 'not_recording') {
            if (mounted) setState(() => _err = tr("Couldn't stop recording; mapping not abandoned: ", '无法停止录制，未放弃建图：') + mappingAckText(r));
            return;
          }
        } on SiteError catch (e) {
          if (mounted) setState(() => _err = tr("Couldn't stop recording; mapping not abandoned: ", '无法停止录制，未放弃建图：') + refusalText(e));
          return;
        }
      }
      try {
        await _act(<String, dynamic>{'action': 'abandon'});
      } on SiteError {
        return;
      }
    }
    if (mounted) Navigator.pop(context);
  }

  // ---- 1 准备

  List<(bool, String, String)> _checks(Map<String, dynamic>? r) {
    if (r == null) return const [];
    final online = robotOnline(r);
    final b = robotBattery(r);
    final st = r['status'];
    final ready = st is Map ? st['ready'] : null;
    final caps = r['capabilities'];
    final tasks = caps is Map ? caps['tasks'] : null;
    final canMap = tasks is Map && tasks['mapping'] is Map && (tasks['mapping'] as Map)['live'] == true;
    return [
      (online, tr('Online', '在线'), tr('Power on the robot and connect it to the network.', '请开机并连接网络。')),
      (b == null || b >= 50, b == null ? tr('Battery unknown', '电量未知') : tr('Battery ${b.round()}% (50% or more required)', '电量 ${b.round()}%（需 50% 以上）'),
          tr('Charge first: mapping takes about 20–40 minutes.', '请先充电：建图约需 20–40 分钟。')),
      (!online || !robotActive(r), tr('Idle (no task)', '空闲（无任务）'), tr('Wait for the current task to finish.', '请等待当前任务完成。')),
      (!(ready is Map && ready['estop_clear'] == false), tr('Emergency stop released', '急停已松开'),
          tr('Release the emergency stop.', '松开急停。')),
      (!online || canMap, tr('Can map', '能建图'),
          tr('This robot doesn\'t support live mapping (update its software).', '此机器人不支持边走边建图（请先升级）。')),
    ];
  }

  Future<void> _prepareNext() async {
    final robot = _robot ?? '';
    final label = _label.text.trim();
    final w = _w;
    try {
      if (w == null || w['robot_id'] != robot || w['label'] != label || w['site_type'] != _siteType) {
        await _act(<String, dynamic>{
          'action': 'start',
          'robot_id': robot,
          'site_type': _siteType,
          'drive': _drive,
          'label': label,
          if (w != null) 'replace': true,
        });
      }
      await _act(<String, dynamic>{'action': 'step', 'step': 'drive'});
    } on SiteError {
      // _err 里有
    }
  }

  Widget _prepare(NwColors c) {
    final r = _robots.where((x) => '${x['robot_id']}' == _robot).firstOrNull;
    final checks = _checks(r);
    final ok = r != null && checks.every((x) => x.$1);
    Widget siteCard(String id, String title, String sub, {bool enabled = true}) => Padding(
          padding: const EdgeInsets.only(bottom: Nw.s8),
          child: Opacity(
            opacity: enabled ? 1 : 0.45,
            child: NwCard(
              key: MapWizardPage.siteTypeKey(id),
              tone: _siteType == id ? NwTone.success : NwTone.neutral,
              onTap: enabled ? () => setState(() => _siteType = id) : null,
              child: Row(children: [
                Icon(_siteType == id ? Icons.radio_button_checked_rounded : Icons.radio_button_off_rounded,
                    color: _siteType == id ? c.success : c.textSecondary),
                const SizedBox(width: Nw.s16),
                Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(title, style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                  Text(sub, style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                ])),
              ]),
            ),
          ),
        );
    return _scroll([
      _h1(tr('Get ready', '准备')),
      if (_robots.length > 1) ...[
        _label2(tr('Which robot', '用哪台机器人')),
        Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
          for (final x in _robots)
            ChoiceChip(
                key: MapWizardPage.robotKey('${x['robot_id']}'),
                label: Text('${(x['name'] ?? '') != '' ? x['name'] : x['robot_id']}'),
                selected: '${x['robot_id']}' == _robot,
                onSelected: (_) => setState(() => _robot = '${x['robot_id']}')),
        ]),
        const SizedBox(height: Nw.s16),
      ],
      if (_robots.isEmpty)
        NwNotice(tone: NwTone.warning, text: tr('No robots on this site yet. Add one under Robots → Add.', '站点暂无机器人，请先在「机器人 → 添加」中添加。')),
      _label2(tr('Site type', '场地类型')),
      siteCard('indoor', tr('Indoor', '室内'), tr('Offices, warehouses, corridors, lobbies', '办公室、仓库、走廊、大堂')),
      siteCard('outdoor', tr('Outdoor', '室外'), tr('Coming soon. Contact your delivery engineer for now.', '即将支持，目前请联系交付工程师'),
          enabled: false),
      siteCard('glass', tr('Glass-heavy site', '玻璃较多的场地'),
          tr('Coming soon. Contact your delivery engineer for now.', '即将支持，目前请联系交付工程师'),
          enabled: false),
      const SizedBox(height: Nw.s8),
      TextField(
          key: MapWizardPage.labelKey,
          controller: _label,
          maxLength: 40,
          decoration: InputDecoration(
              labelText: tr('Map name (optional)', '地图名称（选填）'), hintText: tr('e.g. Ground floor', '比如：一楼'))),
      if (r != null) ...[
        _label2(tr('Checking ${r['robot_id']}', '机器人 ${r['robot_id']} 检查')),
        NwCard(
          key: MapWizardPage.checksKey,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (final x in checks)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 2),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Icon(x.$1 ? Icons.check_rounded : Icons.close_rounded, size: 18, color: x.$1 ? c.success : c.error),
                  const SizedBox(width: Nw.s8),
                  Expanded(
                      child: Text(x.$1 ? x.$2 : tr('${x.$2}: ${x.$3}', '${x.$2}：${x.$3}'),
                          style: NwText.bodyMedium.copyWith(color: x.$1 ? c.text : c.error))),
                ]),
              ),
          ]),
        ),
      ],
      const SizedBox(height: Nw.s16),
      NwNotice(
          tone: NwTone.info,
          text: tr('Place the robot where it will charge (in front of the dock). Start and finish there.',
              '请将机器人放在今后的充电位置（充电桩前），从此处出发并最终回到此处。')),
    ], [
      NwButton(
          buttonKey: MapWizardPage.nextKey,
          label: tr('Next', '下一步'),
          fullWidth: true,
          size: NwButtonSize.large,
          onPressed: ok && !_busy ? _prepareNext : null),
    ]);
  }

  // ---- 2 怎么开

  Widget _driveStep(NwColors c) {
    Widget card(String id, IconData icon, String title, String sub, {String? warn, bool rec = false}) => Padding(
          padding: const EdgeInsets.only(bottom: Nw.s16),
          child: NwCard(
            key: MapWizardPage.driveKey(id),
            tone: _drive == id ? NwTone.success : NwTone.neutral,
            onTap: () => setState(() => _drive = id),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Icon(icon, size: 30, color: _drive == id ? c.success : c.textSecondary),
                const SizedBox(width: Nw.s16),
                Expanded(child: Text(title, style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700))),
                if (rec) NwStatusPill(label: tr('Recommended', '推荐'), tone: NwTone.success),
              ]),
              const SizedBox(height: Nw.s8),
              Text(sub, style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
              if (warn != null) ...[
                const SizedBox(height: Nw.s4),
                Text(warn, style: NwText.bodyMedium.copyWith(color: c.fg(NwTone.warning), fontWeight: FontWeight.w600)),
              ],
            ]),
          ),
        );
    return _scroll([
      _h1(tr('Choose how to drive', '选择操控方式')),
      Text(tr('Both methods produce the same map.', '两种方式建出的地图相同。'),
          style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
      const SizedBox(height: Nw.s16),
      card('remote', Icons.sports_esports_rounded, tr("The robot's remote", '机器人的遥控器'),
          tr('Easier to control; the phone only shows mapping progress.', '操控更顺手，手机仅显示建图进度。'),
          warn: tr('Set the remote to LOW speed.', '请将遥控器档位调至「低速」。'), rec: true),
      card('phone', Icons.smartphone_rounded, tr('Phone remote control', '手机遥控'),
          tr('Landscape: joysticks, live video and mapping preview.', '横屏使用：摇杆、实时画面和建图预览。')),
    ], [
      Row(children: [
        Expanded(
            child: NwButton(
                buttonKey: MapWizardPage.backKey,
                label: tr('Back', '上一步'),
                variant: NwButtonVariant.outline,
                size: NwButtonSize.large,
                onPressed: _busy ? null : () => _go('prepare'))),
        const SizedBox(width: Nw.s8),
        Expanded(
            flex: 2,
            child: NwButton(
                buttonKey: MapWizardPage.nextKey,
                label: tr('Start recording', '开始录制'),
                size: NwButtonSize.large,
                onPressed: _busy
                    ? null
                    : () async {
                        await _go('walk', <String, dynamic>{'drive': _drive});
                        if (_step == 'walk') await _startRec();
                      })),
      ]),
    ]);
  }

  // ---- 3 走一圈

  Future<void> _startRec() async {
    setState(() {
      _busy = true;
      _err = '';
    });
    try {
      final r = await api.mapping(_robotId, 'start', name: 'map-$_version', mapId: _mapId, version: _version);
      final ack = (r['ack'] as Map?) ?? const {};
      if (ack['result'] != 'accepted') {
        if (mounted) setState(() => _err = mappingAckText(r));
      } else {
        _walkSince = DateTime.now();
        _lastPose = null;
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = refusalText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _onPreview(Map<String, dynamic> pv) {
    if (!mounted) return;
    final pose = pv['pose'];
    final now = DateTime.now();
    if (pose is List && pose.length >= 3 && pose.every((v) => v is num)) {
      final p = Offset(_num(pose[0]), _num(pose[1]));
      final yaw = _num(pose[2]);
      final last = _lastPose, at = _lastAt, ly = _lastYaw;
      if (last != null && at != null && ly != null) {
        final dt = now.difference(at).inMilliseconds / 1000;
        if (dt > 0.5) {
          _speed = (p - last).distance / dt;
          var dy = (yaw - ly).abs() % (2 * math.pi);
          if (dy > math.pi) dy = 2 * math.pi - dy;
          _turn = dy / dt;
        }
      }
      _lastPose = p;
      _lastYaw = yaw;
      _lastAt = now;
    }
    setState(() => _pv = pv);
  }

  (double, double) _walked() {
    final t = _trail(_pv);
    final pose = _pv['pose'];
    final here = pose is List && pose.length >= 2 && pose[0] is num && pose[1] is num
        ? Offset(_num(pose[0]), _num(pose[1]))
        : (t.isNotEmpty ? t.last : Offset.zero);
    return (_pathLen(t), t.isEmpty ? 0 : (here - t.first).distance);
  }

  Future<void> _doneWalking() async {
    final (path, from) = _walked();
    final warn = path < 15 || from > 2;
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Finish walking?', '结束行走？')),
        content: Text([
          tr('The robot stops recording and builds the map (a few minutes).', '机器人将停止录制并开始建图（约几分钟）。'),
          if (warn)
            tr('Walked ${path.round()} m, ${from.round()} m from the start. Return to the start before finishing.', '已行走 ${path.round()} 米，距起点 ${from.round()} 米。请回到起点后结束。'),
        ].join('\n\n')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Keep walking', '继续行走'))),
          FilledButton(
              key: MapWizardPage.doneWalkOkKey,
              onPressed: () => Navigator.pop(c, true),
              child: Text(tr('Build the map', '建图'))),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    setState(() {
      _busy = true;
      _err = '';
    });
    try {
      final r = await api.mapping(_robotId, 'stop');
      final ack = (r['ack'] as Map?) ?? const {};
      // 已经没在录（站点、App 重启过）也往下走：建图结果看站点
      if (ack['result'] != 'accepted' && '${ack['reason'] ?? ''}' != 'not_recording') {
        if (mounted) setState(() => _err = mappingAckText(r));
        return;
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = refusalText(e));
      return;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    await _go('build');
  }

  Future<void> _openTeleop() async {
    final done = await Navigator.of(context).push<bool>(MaterialPageRoute<bool>(
        builder: (ctx) => SiteTeleopPage(
              api: api,
              robotId: _robotId,
              overlay: SizedBox(
                width: 180,
                child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
                  SizedBox(
                      height: 120,
                      child: MappingPreviewView(api: api, robotId: _robotId, onData: _onPreview, compact: true)),
                  const SizedBox(height: Nw.s4),
                  FilledButton(
                      key: const Key('teleop-done-walk'),
                      onPressed: () => Navigator.pop(ctx, true),
                      child: Text(tr('Finish walking', '结束行走'))),
                ]),
              ),
            )));
    if (done == true && mounted) await _doneWalking();
  }

  Widget _walk(NwColors c) {
    final recording = _pv['recording'] == true;
    final live = _pv['live'] == true;
    final starting = _pv['starting'] == true;
    final (path, from) = _walked();
    final since = _walkSince;
    final mins = since == null ? 0 : DateTime.now().difference(since).inMinutes;
    final remote = _drive == 'remote' || _w?['drive'] == 'remote';
    final st = _rv?['status'];
    final ready = st is Map ? st['ready'] : null;
    final control = ready is Map ? ready['control'] : null;
    final built = '${(_w?['build'] as Map?)?['state'] ?? 'pending'}';
    final tip = walkTip(
        pathM: path, fromStartM: from, elapsed: Duration(minutes: mins), speed: _speed, turn: _turn);
    return _scroll([
      NwCard(
        key: MapWizardPage.walkStatusKey,
        padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
        child: Row(children: [
          Icon(Icons.fiber_manual_record_rounded, size: 14, color: recording ? c.error : c.textSecondary),
          const SizedBox(width: Nw.s8),
          Expanded(
              child: Text(
                  recording
                      ? tr('Recording', '录制中')
                      : (starting ? tr('Starting…', '正在启动…') : tr('Not recording', '未录制')),
                  style: NwText.bodyMedium.copyWith(color: c.text, fontWeight: FontWeight.w600))),
          Text(tr('$mins min · ${path.round()} m', '$mins 分钟 · ${path.round()} 米'),
              style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
        ]),
      ),
      if (remote && control is bool) ...[
        const SizedBox(height: Nw.s8),
        NwStatusPill(
            key: MapWizardPage.controlKey,
            label: control ? tr('Remote not in control', '遥控器未接管') : tr('Remote in control', '遥控器控制中'),
            tone: control ? NwTone.neutral : NwTone.success,
            dot: !control),
      ],
      const SizedBox(height: Nw.s8),
      SizedBox(
          height: 360,
          child: MappingPreviewView(api: api, robotId: _robotId, period: widget.poll, onData: _onPreview)),
      const SizedBox(height: Nw.s8),
      Text(tr('Blue: path walked · Grey: not scanned · Green dot: start',
              '蓝线：已走路径　灰色：未扫描　绿点：起点'),
          style: NwText.bodySmall.copyWith(color: c.textSecondary)),
      const SizedBox(height: Nw.s8),
      NwNotice(key: MapWizardPage.tipKey, tone: NwTone.info, text: tip),
      if (remote) ...[
        const SizedBox(height: Nw.s8),
        Text(tr('Keep the remote on LOW speed.', '请将遥控器档位调至低速。'),
            style: NwText.bodySmall.copyWith(color: c.fg(NwTone.warning))),
      ],
      if (!live && !starting && built != 'pending') ...[
        const SizedBox(height: Nw.s16),
        NwNotice(
            tone: NwTone.info,
            text: built == 'failed'
                ? tr('This run has ended and the map could not be built (the robot may have restarted).',
                    '本次录制已结束，建图失败（机器人可能重启过）。')
                : tr('This run has ended and the map is built (the robot may have restarted).',
                    '本次录制已结束，地图已建好（机器人可能重启过）。')),
        const SizedBox(height: Nw.s8),
        NwButton(
            buttonKey: MapWizardPage.walkBuiltKey,
            label: tr('See the result', '看建图结果'),
            fullWidth: true,
            onPressed: _busy ? null : () => _go('build')),
      ] else if (!live && !starting && !_busy) ...[
        const SizedBox(height: Nw.s16),
        NwButton(
            buttonKey: MapWizardPage.startRecKey,
            label: tr('Start recording', '开始录制'),
            icon: Icons.fiber_manual_record_rounded,
            fullWidth: true,
            onPressed: _startRec),
      ],
    ], [
      if (!remote) ...[
        NwButton(
            buttonKey: MapWizardPage.teleopKey,
            label: tr('Open phone remote control', '打开手机遥控'),
            icon: Icons.sports_esports_rounded,
            variant: NwButtonVariant.outline,
            fullWidth: true,
            size: NwButtonSize.large,
            onPressed: _busy ? null : _openTeleop),
        const SizedBox(height: Nw.s8),
      ],
      NwButton(
          buttonKey: MapWizardPage.doneWalkKey,
          label: tr('Finish walking', '结束行走'),
          fullWidth: true,
          size: NwButtonSize.large,
          onPressed: _busy || !live ? null : _doneWalking),
    ]);
  }

  // ---- 4 建图

  Future<Uint8List>? _mapPng;
  String _mapPngFor = '';

  Widget _mapImage(NwColors c) {
    final key = '$_mapId:$_version';
    if (_mapPngFor != key) {
      _mapPngFor = key;
      _mapPng = api.mapPreviewPng(_mapId, _version);
    }
    return FutureBuilder<Uint8List>(
      future: _mapPng,
      builder: (_, snap) => Container(
        height: 280,
        decoration: BoxDecoration(border: Border.all(color: c.outline), borderRadius: BorderRadius.circular(Nw.r8)),
        child: snap.hasData
            ? InteractiveViewer(maxScale: 8, child: Image.memory(snap.data!, filterQuality: FilterQuality.none))
            : Center(
                child: snap.hasError
                    ? Text(tr('No preview', '无预览'), style: NwText.bodySmall.copyWith(color: c.textSecondary))
                    : const CircularProgressIndicator()),
      ),
    );
  }

  Widget _build(NwColors c) {
    final b = (_w?['build'] as Map?)?.cast<String, dynamic>() ?? const <String, dynamic>{};
    final state = '${b['state'] ?? 'pending'}';
    if (state == 'pending') {
      return _scroll([
        _h1(tr('Building the map', '正在建图')),
        const SizedBox(height: Nw.s24),
        const Center(child: CircularProgressIndicator()),
        const SizedBox(height: Nw.s16),
        Text(
            tr('Usually a few minutes: the robot builds the map, then uploads it.',
                '通常需要几分钟：机器人建图完成后上传到站点。'),
            key: MapWizardPage.buildStatusKey,
            textAlign: TextAlign.center,
            style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
      ], const []);
    }
    if (state == 'failed') {
      return _scroll([
        _h1(tr("Mapping failed", '建图失败')),
        NwNotice(key: MapWizardPage.buildStatusKey, tone: NwTone.error, text: '${b['reason'] ?? ''}'),
        const SizedBox(height: Nw.s8),
        Text(tr('Walk again. If it fails again, send this message to your delivery engineer.',
                '请重新行走一次。若仍失败，请将此信息发给交付工程师。'),
            style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
      ], [
        NwButton(
            buttonKey: MapWizardPage.rewalkKey,
            label: tr('Map again', '重新建图'),
            fullWidth: true,
            size: NwButtonSize.large,
            onPressed: _busy ? null : _rewalk),
      ]);
    }
    final q = (b['quality'] as Map?)?.cast<String, dynamic>() ?? const <String, dynamic>{};
    final verdict = '${q['verdict'] ?? 'unknown'}';
    final reasons = [for (final r in (q['reasons'] as List? ?? const [])) '$r'];
    final good = verdict != 'problem';
    final path = q['path_m'], shift = q['max_shift_m'];
    return _scroll([
      Row(key: MapWizardPage.verdictKey, children: [
        Icon(
            switch (verdict) {
              'usable' => Icons.check_circle_rounded,
              'problem' => Icons.warning_rounded,
              _ => Icons.help_outline_rounded,
            },
            size: 28,
            color: switch (verdict) {
              'usable' => c.success,
              'problem' => c.fg(NwTone.warning),
              _ => c.textSecondary,
            }),
        const SizedBox(width: Nw.s8),
        Expanded(
            child: Text(
                switch (verdict) {
                  'usable' => tr('Map built: usable', '地图已建成：可用'),
                  'problem' => tr('Map built: has problems', '地图已建成：有问题'),
                  _ => tr("Map built: quality unknown", '地图已建成：无法评估'),
                },
                style: NwText.titleLarge.copyWith(color: c.text, fontWeight: FontWeight.w700))),
      ]),
      const SizedBox(height: Nw.s16),
      _mapImage(c),
      const SizedBox(height: Nw.s16),
      NwCard(
        tone: good ? NwTone.neutral : NwTone.warning,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (good && verdict == 'usable') Text(tr('✓ Loop closed at the start', '✓ 已回到起点闭合')),
          if (path is num) Text(tr('Walked ${path.round()} m', '走了 ${path.round()} 米')),
          if (verdict == 'unknown')
            Text(tr("No build record for this version, so its quality can't be assessed. Check the map before using it.",
                '此版本没有建图记录，无法评估质量，请查看地图后决定是否使用。')),
          if (shift is num && verdict == 'usable') Text(tr('Stable (max correction ${(shift * 100).round()} cm)', '拼接稳定（最大修正 ${(shift * 100).round()} 厘米）')),
          for (final r in reasons) Text('• ${qualityReasonText(r)}'),
          if (!good)
            Padding(
              padding: const EdgeInsets.only(top: Nw.s4),
              child: Text(
                  tr('Walk again more slowly and finish at the start. You can also use this version for now.',
                      '建议放慢速度重新行走，并在起点结束。也可先使用此版本。'),
                  style: NwText.bodySmall.copyWith(color: c.textSecondary)),
            ),
        ]),
      ),
      const SizedBox(height: Nw.s8),
      Text(tr("Check that walls are straight and no room is missing. If not, walk again.",
              '请检查墙体是否平直、房间是否完整，如有问题请重新行走。'),
          style: NwText.bodySmall.copyWith(color: c.textSecondary)),
    ], [
      Row(children: [
        Expanded(
            child: NwButton(
                buttonKey: good ? MapWizardPage.rewalkKey : MapWizardPage.useKey,
                label: good ? tr('Map again', '重新建图') : tr('Use for now', '暂用此版本'),
                variant: NwButtonVariant.outline,
                size: NwButtonSize.large,
                onPressed: _busy ? null : (good ? _rewalk : () => _go('zones')))),
        const SizedBox(width: Nw.s8),
        Expanded(
            child: NwButton(
                buttonKey: good ? MapWizardPage.useKey : MapWizardPage.rewalkKey,
                label: good ? tr('Use this version', '使用此版本') : tr('Map again', '重新建图'),
                size: NwButtonSize.large,
                onPressed: _busy ? null : (good ? () => _go('zones') : _rewalk))),
      ]),
    ]);
  }

  /// 重走一遍：站点换一个新版本号（旧的那一版留着），接着就开录（跟「开始录」一样）。
  Future<void> _rewalk() async {
    try {
      await _act(<String, dynamic>{'action': 'rewalk'});
    } on SiteError {
      return; // _err 里有
    }
    _pv = const {};
    _walkSince = null;
    if (_step == 'walk') await _startRec();
  }

  // ---- 5 禁行区

  Future<void> _loadZones() async {
    if (_mapId.isEmpty) return;
    try {
      final z = await api.mapZones(_mapId, _version);
      if (mounted) setState(() => _zones = z);
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = refusalText(e));
    }
  }

  Future<void> _confirmZones({required bool empty}) async {
    final z = _zones;
    if (z == null) return;
    setState(() => _busy = true);
    try {
      await api.confirmZones(_mapId, _version, (z['revision'] as num? ?? 0).toInt(), emptyOk: empty);
      await _loadZones();
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = refusalText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _zonesStep(NwColors c) {
    final z = _zones;
    final list = [for (final x in (z?['zones'] as List? ?? const [])) if (x is Map) x];
    final conf = z?['confirmed'];
    final confirmed = conf is Map && conf['current'] == true;
    final proposal = z?['proposal'];
    final hasProposal = proposal is Map && proposal.isNotEmpty;
    return _scroll([
      _h1(tr("Areas the robot must not enter", '机器人禁行区域')),
      Text(
          tr('Mark pools, stairwells, steep slopes and drops as no-go zones, then confirm.',
              '将水池、楼梯口、陡坡、落差处在图上圈为禁行区，然后确认。'),
          style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
      const SizedBox(height: Nw.s16),
      NwCard(
        child: Row(children: [
          Icon(confirmed ? Icons.check_circle_rounded : Icons.radio_button_unchecked_rounded,
              color: confirmed ? c.success : c.textSecondary),
          const SizedBox(width: Nw.s8),
          Expanded(
              child: Text(
                  z == null
                      ? tr('Loading…', '加载中…')
                      : (confirmed
                          ? tr('${list.length} zone(s), confirmed', '${list.length} 块，已确认')
                          : tr('${list.length} zone(s), not confirmed', '${list.length} 块，未确认')),
                  style: NwText.bodyMedium.copyWith(color: c.text))),
        ]),
      ),
      const SizedBox(height: Nw.s16),
      NwButton(
          buttonKey: MapWizardPage.zonesOpenKey,
          label: tr('Draw on the map', '在地图上绘制'),
          icon: Icons.edit_location_alt_rounded,
          variant: NwButtonVariant.outline,
          fullWidth: true,
          onPressed: () async {
            await Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteMapPreviewPage(api: api, mapId: _mapId, version: _version)));
            await _loadZones();
          }),
      if (z != null && !confirmed && !hasProposal) ...[
        const SizedBox(height: Nw.s8),
        if (list.isEmpty)
          NwButton(
              buttonKey: MapWizardPage.zonesEmptyKey,
              label: tr('This map has no no-go zones', '这张图没有禁区'),
              variant: NwButtonVariant.outline,
              fullWidth: true,
              onPressed: _busy ? null : () => _confirmZones(empty: true))
        else
          NwButton(
              buttonKey: MapWizardPage.zonesConfirmKey,
              label: tr('Confirm zones', '确认禁行区'),
              variant: NwButtonVariant.outline,
              fullWidth: true,
              onPressed: _busy ? null : () => _confirmZones(empty: false)),
      ],
      if (hasProposal) ...[
        const SizedBox(height: Nw.s8),
        NwNotice(
            tone: NwTone.warning,
            text: tr('Zones from the previous version need review: adopt or discard them on the map.',
                '上一版本的禁行区需要核对：请在地图上采纳或放弃。')),
      ],
    ], [
      NwButton(
          buttonKey: MapWizardPage.nextKey,
          label: tr('Next', '下一步'),
          fullWidth: true,
          size: NwButtonSize.large,
          onPressed: confirmed && !_busy ? () => _go('origin') : null),
    ]);
  }

  // ---- 6 原点

  bool _sent = false;

  Future<void> _sendMap() async {
    setState(() {
      _busy = true;
      _err = '';
    });
    try {
      final r = await api.activateMap(_robotId, _mapId, _version, withoutHome: true);
      final ack = (r['ack'] as Map?) ?? const {};
      if (ack['result'] == 'accepted') {
        _sent = true;
      } else if (mounted) {
        setState(() => _err = tr('The robot rejected it: ${ack['reason'] ?? ack['result']}', '机器人未接受：${ack['reason'] ?? ack['result']}'));
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = refusalText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    await _pollRobot();
  }

  /// 下发之后盯这一次换图：机器人报换图没成（下载、核对、载入、定位先验）就说原因，能再下发。
  Future<void> _checkActivation() async {
    try {
      final ov = await api.mapsOverview();
      for (final r in (ov['robots'] as List? ?? const [])) {
        if (r is! Map || '${r['robot_id']}' != _robotId) continue;
        final a = r['activation'];
        if (a is Map && '${a['map_id']}' == _mapId && '${a['version']}' == _version &&
            (a['state'] == 'failed' || a['state'] == 'timeout') && mounted) {
          setState(() {
            _sent = false;
            _err = a['state'] == 'timeout'
                ? tr('$_robotId timed out switching maps. Send it again.', '$_robotId 换图超时，请重新下发。')
                : tr('$_robotId failed to switch maps: ${a['reason']}', '$_robotId 换图失败：${a['reason']}');
          });
        }
      }
    } on SiteError {
      // 下次再问
    }
  }

  /// 换了图要重新知道自己在哪（换图会清掉原来的定位）：能自己找的让它拿看到的墙套地图；不能的去机器人页设。
  Future<void> _reloc() async {
    final r = _rv;
    if (r != null && robotCanFindPose(r)) {
      setState(() {
        _busy = true;
        _err = '';
      });
      try {
        final got = await api.relocalize(_robotId, global: true);
        final ack = (got['ack'] as Map?) ?? const {};
        if (ack['result'] != 'accepted' && mounted) {
          setState(() => _err = tr("Auto-localization failed: ${ack['reason'] ?? ack['result']}. Set the position on the robot page.",
              '自动定位失败：${ack['reason'] ?? ack['result']}。请到机器人页手动设位置。'));
        }
      } on SiteError catch (e) {
        if (mounted) setState(() => _err = refusalText(e));
      } finally {
        if (mounted) setState(() => _busy = false);
      }
    } else {
      await Navigator.push(context, MaterialPageRoute<void>(builder: (_) => SiteRobotPage(api: api, robotId: _robotId)));
    }
    await _pollRobot();
  }

  Future<void> _markHome({bool replace = false}) async {
    setState(() {
      _busy = true;
      _err = '';
    });
    try {
      final r = await api.markHome(_robotId, replace: replace);
      final ack = (r['ack'] as Map?) ?? const {};
      if (ack['result'] != null && ack['result'] != 'accepted') {
        if (mounted) setState(() => _err = tr('Failed to mark: ${ack['reason'] ?? ack['result']}', '标定失败：${ack['reason'] ?? ack['result']}'));
        return;
      }
    } on SiteError catch (e) {
      final taken = e.body['name_taken'];
      if (e.status == 409 && taken != null && taken != false && !replace && mounted) {
        final ok = await showDialog<bool>(
          context: context,
          builder: (c) => AlertDialog(
            content: Text(tr('The origin is registered on an older map version. Replace it with this version?',
                '原点已登记在旧版本地图上，是否替换为此版本？')),
            actions: [
              TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
              FilledButton(
                  key: MapWizardPage.replaceHomeKey,
                  onPressed: () => Navigator.pop(c, true),
                  child: Text(tr('Replace', '替换'))),
            ],
          ),
        );
        if (mounted) setState(() => _busy = false);
        if (ok == true) await _markHome(replace: true);
        return;
      }
      if (mounted) setState(() => _err = refusalText(e));
      return;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    try {
      await _act(<String, dynamic>{'action': 'finish'});
    } on SiteError {
      // _err 里有
    }
  }

  Widget _origin(NwColors c) {
    final r = _rv;
    final loaded = r != null && robotMapTag(r) == '$_mapId:$_version';
    final st = r?['status'];
    final ready = st is Map ? st['ready'] : null;
    final located = !(ready is Map && ready['loc_ok'] == false);
    return _scroll([
      _h1(tr('Mark the origin', '标原点')),
      NwCard(
        child: Row(children: [
          Icon(loaded ? Icons.check_circle_rounded : Icons.downloading_rounded,
              color: loaded ? c.success : c.textSecondary),
          const SizedBox(width: Nw.s8),
          Expanded(
              child: Text(
                  loaded
                      ? tr('The new map is loaded on $_robotId', '新地图已下发并加载到 $_robotId')
                      : (_sent
                          ? tr('$_robotId is downloading and loading the new map…', '$_robotId 正在下载并加载新地图…')
                          : tr('Send the new map to $_robotId first', '请先将新地图下发给 $_robotId')),
                  style: NwText.bodyMedium.copyWith(color: c.text))),
        ]),
      ),
      if (!loaded && !_sent) ...[
        const SizedBox(height: Nw.s8),
        NwButton(
            buttonKey: MapWizardPage.sendMapKey,
            label: tr('Send to $_robotId', '下发给 $_robotId'),
            fullWidth: true,
            onPressed: _busy ? null : _sendMap),
      ],
      if (loaded && !located) ...[
        const SizedBox(height: Nw.s8),
        NwNotice(
            tone: NwTone.warning,
            text: tr("$_robotId is not localized on the new map. Set its position first.",
                '$_robotId 尚未在新地图上定位，请先设位置。')),
        const SizedBox(height: Nw.s8),
        NwButton(
            buttonKey: MapWizardPage.relocKey,
            label: robotCanFindPose(r) ? tr('Auto-localize', '自动定位') : tr('Set position', '设位置'),
            icon: Icons.my_location_rounded,
            fullWidth: true,
            onPressed: _busy ? null : _reloc),
      ],
      const SizedBox(height: Nw.s16),
      Text(
          tr('Drive $_robotId to about 1.5 m in front of the dock, facing it, and keep it still. Then tap the button below.',
              '将 $_robotId 开到充电桩前约 1.5 米处，正对充电桩，保持静止，然后点下方按钮。'),
          style: NwText.bodyLarge.copyWith(color: c.text)),
      const SizedBox(height: Nw.s8),
      Text(
          tr('The robot verifies its position first and refuses if it is off. The origin is used for charging and return, and is the default standby point.',
              '机器人会先核验定位，偏差过大时拒绝。原点用于回充和返航，也是默认待命点。'),
          style: NwText.bodySmall.copyWith(color: c.textSecondary)),
    ], [
      NwButton(
          buttonKey: MapWizardPage.markHomeKey,
          label: tr('Mark the origin here', '在这儿标原点'),
          fullWidth: true,
          size: NwButtonSize.large,
          onPressed: loaded && located && !_busy ? _markHome : null),
    ]);
  }

  // ---- 7 完成

  Widget _done(NwColors c) {
    final f = _finished ?? const <String, dynamic>{};
    final label = '${f['label'] ?? ''}';
    final name = label.isNotEmpty ? label : '${f['map_id']}:${f['version']}';
    Widget next(Key key, String title, String sub, Widget page) => Padding(
          padding: const EdgeInsets.only(bottom: Nw.s8),
          child: NwCard(
            key: key,
            onTap: () => Navigator.pushReplacement(context, MaterialPageRoute<void>(builder: (_) => page)),
            child: Row(children: [
              Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(title, style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                Text(sub, style: NwText.bodySmall.copyWith(color: c.textSecondary)),
              ])),
              Icon(Icons.chevron_right_rounded, color: c.textSecondary),
            ]),
          ),
        );
    return _scroll([
      const SizedBox(height: Nw.s16),
      Icon(Icons.check_circle_outline_rounded, size: 64, color: c.success),
      const SizedBox(height: Nw.s8),
      Text(tr('Ready to patrol', '可以开始巡逻'),
          textAlign: TextAlign.center, style: NwText.headlineSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
      Text(tr('"$name" is active on ${f['robot_id']}', '「$name」已在 ${f['robot_id']} 上启用'),
          textAlign: TextAlign.center, style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
      const SizedBox(height: Nw.s16),
      NwCard(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(tr('✓ Map', '✓ 地图')),
        Text(tr('✓ Origin and standby point', '✓ 原点、待命点')),
        Text(tr('✓ No-go zones confirmed', '✓ 禁行区已确认')),
      ])),
      const SizedBox(height: Nw.s16),
      _label2(tr('Next', '接下来')),
      next(MapWizardPage.routesKey, tr('Draw patrol routes', '画巡逻路线'), tr('Tap patrol points on the map', '在地图上点选巡逻点'),
          SiteRoutesPage(api: api)),
      next(MapWizardPage.scheduleKey, tr('Schedule patrols', '排日程'), tr('Which route runs when', '各路线的执行时间'),
          SchedulePage(api: api)),
    ], [
      NwButton(
          buttonKey: MapWizardPage.homeKey,
          label: tr('Back to home', '返回首页'),
          fullWidth: true,
          size: NwButtonSize.large,
          onPressed: () => Navigator.pop(context)),
    ]);
  }

  // ---- 外框

  Widget _h1(String t) => Padding(
      padding: const EdgeInsets.only(bottom: Nw.s16),
      child: Text(t, style: NwText.titleLarge.copyWith(color: Nw.of(context).text, fontWeight: FontWeight.w700)));

  Widget _label2(String t) => Padding(
      padding: const EdgeInsets.only(top: Nw.s8, bottom: Nw.s8),
      child: Text(t, style: NwText.labelMedium.copyWith(color: Nw.of(context).textSecondary, letterSpacing: 0.6)));

  /// 中间能滚，底下一排按钮钉住。
  Widget _scroll(List<Widget> body, List<Widget> bottom) {
    final c = Nw.of(context);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Expanded(
          child: ListView(padding: const EdgeInsets.all(Nw.s16), children: [
        ...body,
        if (_err.isNotEmpty) ...[
          const SizedBox(height: Nw.s16),
          NwNotice(tone: NwTone.error, text: _err),
        ],
      ])),
      if (bottom.isNotEmpty)
        Container(
          decoration: BoxDecoration(color: c.bg, border: Border(top: BorderSide(color: c.outline))),
          padding: const EdgeInsets.fromLTRB(Nw.s16, Nw.s16, Nw.s16, Nw.s16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: bottom),
        ),
    ]);
  }

  String _title() => switch (_step) {
        'walk' => tr('Walk the site', '行走建图'),
        'zones' => tr('No-go zones', '禁行区'),
        'origin' => tr('Origin', '原点'),
        _ => tr('Mapping', '建图'),
      };

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final i = wizardSteps.indexOf(_step);
    final warnBuild = _step == 'build' && '${(_w?['build'] as Map?)?['quality']?['verdict'] ?? ''}' == 'problem';
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) unawaited(_step == 'done' ? Future.sync(() => Navigator.pop(context)) : _exit());
      },
      child: Scaffold(
        backgroundColor: c.bg,
        body: SafeArea(
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Container(
              padding: const EdgeInsets.fromLTRB(Nw.s8, Nw.s8, Nw.s16, Nw.s8),
              decoration: BoxDecoration(border: Border(bottom: BorderSide(color: c.outline))),
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                Row(children: [
                  if (_step != 'done')
                    TextButton.icon(
                        key: MapWizardPage.exitKey,
                        onPressed: _exit,
                        icon: const Icon(Icons.close_rounded),
                        label: Text(tr('Exit', '退出')))
                  else
                    const SizedBox(width: 48),
                  Expanded(
                      child: Text(_title(),
                          textAlign: TextAlign.center,
                          style: NwText.titleMedium.copyWith(color: c.text, fontWeight: FontWeight.w700))),
                  Text('${i + 1} / 7', key: MapWizardPage.stepKey, style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
                ]),
                const SizedBox(height: Nw.s8),
                Row(children: [
                  for (var k = 0; k < 7; k++) ...[
                    if (k > 0) const SizedBox(width: 4),
                    Expanded(
                        child: Container(
                            height: 4,
                            decoration: BoxDecoration(
                                borderRadius: BorderRadius.circular(2),
                                color: k < i || (k == i && !warnBuild)
                                    ? c.success
                                    : (k == i ? c.fg(NwTone.warning) : c.outline)))),
                  ],
                ]),
              ]),
            ),
            Expanded(
              child: _loading
                  ? const Center(child: CircularProgressIndicator())
                  : switch (_step) {
                      'drive' => _driveStep(c),
                      'walk' => _walk(c),
                      'build' => _build(c),
                      'zones' => _zonesStep(c),
                      'origin' => _origin(c),
                      'done' => _done(c),
                      _ => _prepare(c),
                    },
            ),
          ]),
        ),
      ),
    );
  }
}
