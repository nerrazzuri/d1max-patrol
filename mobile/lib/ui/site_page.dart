/// 站点模式的几屏（W00c4）：站点列表 → 登录 → 狗的列表（实时）→ 单狗页（派巡检、叫停、回待命点）；
/// 事件账、排程、值守（W00c5a）各一页。
///
/// **升到声音档、还没人确认的告警**（SSE `kind: alert`）：狗列表这一屏在栈底一直开着，由它响铃，
/// 不管人此刻在看哪一页。App 没开时的系统推送不在 W00c5a 里。
///
/// **按钮按角色显示**：业主（owner）只有「叫停」；保安（guard）与管理员（admin）能派单。真正的
/// 权限在站点上（站点回 403），这里只是不给人一个注定被拒的按钮。
library;

import 'dart:async';
import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../net/site_client.dart';
import '../store/site_store.dart';
import 'site_logs.dart';
import 'site_mapping.dart';
import 'site_maps.dart';
import 'site_releases.dart';
import 'site_runs.dart';
import 'site_supervise.dart';
import 'site_teleop.dart';
import 'site_video.dart';
import 'site_watch_page.dart';
import 'widget/fields_dialog.dart';

/// 造一个站点客户端。测试换成假的。
typedef SiteApiFactory = SiteApi Function(SiteEntry site);

SiteApi defaultSiteApi(SiteEntry s) => SiteClient(s.url, s.fingerprint);

String _statusLine(Map<String, dynamic> r) {
  final st = r['status'];
  if (r['revoked'] == true) return '已吊销';
  if (st is! Map) return '没有状态';
  final online = st['online'] == true && r['fresh'] == true;
  final task = st['task'];
  final taskText = task is Map ? '${task['kind']} ${task['state']}' : '空闲';
  final held = r['held'] is Map ? '已叫停 · ' : '';
  return held + (online ? '在线 · $taskText' : '离线');
}

// ------------------------------------------------------------ 站点列表

class SiteListPage extends StatefulWidget {
  final SiteStore store;
  final SiteApiFactory apiFactory;
  const SiteListPage(
      {super.key, required this.store, this.apiFactory = defaultSiteApi});

  @override
  State<SiteListPage> createState() => _SiteListPageState();
}

class _SiteListPageState extends State<SiteListPage> {
  List<SiteEntry> _sites = <SiteEntry>[];

  /// 站点文件读不出来（坏了）。这时**不许**添加或删除：一保存就把坏文件里的其余站点覆盖掉了。
  String? _loadError;

  @override
  void initState() {
    super.initState();
    widget.store.load().then((s) {
      if (mounted) setState(() => _sites = s);
    }, onError: (Object e) {
      if (mounted) setState(() => _loadError = '站点列表读不出来：$e');
    });
  }

  Future<void> _remove(SiteEntry s) async {
    final next = [..._sites]..remove(s);
    await widget.store.save(next);
    if (mounted) setState(() => _sites = next);
  }

  Future<void> _add() async {
    final name = TextEditingController();
    final url = TextEditingController(text: 'https://');
    final fp = TextEditingController();
    final user = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        // 横屏时键盘占掉一大半高：整个对话框（标题连输入框）能滚，不然溢出。
        scrollable: true,
        title: const Text('添加站点'),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(
              key: const Key('site-name'),
              controller: name,
              decoration: const InputDecoration(labelText: '名字')),
          TextField(
              key: const Key('site-url'),
              controller: url,
              decoration: const InputDecoration(labelText: '地址（https://…:8443）')),
          TextField(
              key: const Key('site-fp'),
              controller: fp,
              decoration: const InputDecoration(
                  labelText: '证书指纹（d1max-site fingerprint）')),
          TextField(
              key: const Key('site-user'),
              controller: user,
              decoration: const InputDecoration(labelText: '账号')),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('保存')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    final bad = checkSiteEntry(url.text, fp.text);
    if (bad != null || name.text.trim().isEmpty || user.text.trim().isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('没保存：${bad ?? '名字和账号要填'}')));
      return;
    }
    final entry = SiteEntry(
        name: name.text.trim(),
        url: url.text.trim(),
        fingerprint: normalizeFingerprint(fp.text),
        username: user.text.trim());
    final next = [..._sites, entry];
    await widget.store.save(next);
    if (mounted) setState(() => _sites = next);
  }

  Future<void> _open(SiteEntry s) async {
    final pw = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        scrollable: true, // 横屏弹键盘
        title: Text('登录 ${s.name}'),
        content: TextField(
            key: const Key('site-password'),
            controller: pw,
            obscureText: true,
            decoration: InputDecoration(labelText: '${s.username} 的口令')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('登录')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    final messenger = ScaffoldMessenger.of(context);
    final nav = Navigator.of(context);
    SiteApi? api;
    try {
      api = widget.apiFactory(s);
      await api.login(s.username, pw.text);
    } catch (e) {
      api?.close();
      messenger.showSnackBar(SnackBar(content: Text('登录不了：$e')));
      return;
    }
    final a = api;
    final why = await nav.push<String>(MaterialPageRoute<String>(
        builder: (_) => SiteRobotsPage(api: a, title: s.name)));
    // 离开就注销：不然令牌在站点上还能用半小时。
    try {
      if (a.session != null) await a.logout();
    } catch (_) {}
    a.close();
    if (why != null) messenger.showSnackBar(SnackBar(content: Text(why)));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('站点')),
      floatingActionButton: _loadError != null
          ? null
          : FloatingActionButton(
              key: const Key('site-add'), onPressed: _add, child: const Icon(Icons.add)),
      body: _loadError != null
          ? Center(child: Text(_loadError!, key: const Key('site-load-error')))
          : _sites.isEmpty
              ? const Center(child: Text('还没有站点。右下角添加。'))
              : ListView(children: [
                  for (final s in _sites)
                    ListTile(
                        title: Text(s.name),
                        subtitle: Text('${s.url} · ${s.username}'),
                        trailing: IconButton(
                            key: Key('site-remove-${s.name}'),
                            tooltip: '删掉（证书重签后删了重加）',
                            icon: const Icon(Icons.delete_outline),
                            onPressed: () => _remove(s)),
                        onTap: () => _open(s)),
                ]),
    );
  }
}

// ------------------------------------------------------------ 狗的列表

/// 响铃：系统提示音 + 震动。测试换成计数的。
void defaultRing() {
  unawaited(SystemSound.play(SystemSoundType.alert));
  unawaited(HapticFeedback.heavyImpact());
}

class SiteRobotsPage extends StatefulWidget {
  final SiteApi api;
  final String title;
  final void Function() ring;
  const SiteRobotsPage(
      {super.key, required this.api, this.title = '站点', this.ring = defaultRing});

  @override
  State<SiteRobotsPage> createState() => _SiteRobotsPageState();
}

class _SiteRobotsPageState extends State<SiteRobotsPage> {
  List<Map<String, dynamic>> _robots = <Map<String, dynamic>>[];
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
  }

  void _listen() {
    _sub?.cancel();
    _sub = widget.api.events().listen((f) {
      if (!_live && mounted) setState(() => _live = true);
      if (f['kind'] == 'snapshot') unawaited(_ringIfPending());
      if (alertWantsSound(f)) {
        widget.ring();
        final a = f['alert'] as Map;
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
              content: Text('${a['level']} ${a['robot']} · ${a['title']}（没人确认）')));
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
      if (pending.isEmpty || _disposed) return;
      widget.ring();
      final a = pending.first;
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text('${a.level} ${a.robot} · ${a.title}（没人确认）')));
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
    if (!_disposed && mounted) Navigator.of(context).pop('登录过期了，重新登录');
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
  }

  @override
  void dispose() {
    _disposed = true;
    _debounce?.cancel();
    _retry?.cancel();
    _sub?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final role = widget.api.session?.role ?? '';
    return Scaffold(
      appBar: AppBar(title: Text('${widget.title}（$role）'), actions: [
        IconButton(
            key: const Key('open-watch'),
            tooltip: '值守',
            icon: const Icon(Icons.notifications_active),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteWatchPage(api: widget.api)))),
        IconButton(
            key: const Key('open-releases'),
            tooltip: '版本',
            icon: const Icon(Icons.system_update),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteReleasesPage(api: widget.api)))),
        IconButton(
            key: const Key('open-maps'),
            tooltip: '地图',
            icon: const Icon(Icons.map),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteMapsPage(api: widget.api)))),
        IconButton(
            key: const Key('open-runs'),
            tooltip: '记录',
            icon: const Icon(Icons.photo_library),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteRunsPage(api: widget.api)))),
        IconButton(
            tooltip: '事件',
            icon: const Icon(Icons.warning_amber),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteIncidentsPage(api: widget.api)))),
        IconButton(
            tooltip: '排程',
            icon: const Icon(Icons.schedule),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute<void>(builder: (_) => SiteSchedulePage(api: widget.api)))),
      ]),
      body: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(children: [
          if (!_live)
            const ListTile(
                key: Key('live-lost'),
                leading: Icon(Icons.sync_problem, color: Colors.orange),
                title: Text('实时更新没连上，正在重连；下拉可以手动刷新')),
          if (_error != null)
            ListTile(title: Text(_error!, style: const TextStyle(color: Colors.red))),
          for (final r in _robots)
            ListTile(
              key: Key('robot-${r['robot_id']}'),
              title: Text('${r['robot_id']}'),
              subtitle: Text(_statusLine(r)),
              onTap: () => Navigator.push(
                  context,
                  MaterialPageRoute<void>(
                      builder: (_) => SiteRobotPage(api: widget.api, robotId: '${r['robot_id']}'))),
            ),
        ]),
      ),
    );
  }
}

// ------------------------------------------------------------ 单狗页

class SiteRobotPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  const SiteRobotPage({super.key, required this.api, required this.robotId});

  @override
  State<SiteRobotPage> createState() => _SiteRobotPageState();
}

class _SiteRobotPageState extends State<SiteRobotPage> {
  Map<String, dynamic>? _view;
  String? _msg;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    try {
      final v = await widget.api.robot(widget.robotId);
      if (mounted) setState(() => _view = v);
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = e.toString());
    }
  }

  /// 发一条、把结果写在页面上。狗收下了（或站点没带回执）回真。
  Future<bool> _do(Future<Map<String, dynamic>> Function() f, String what) async {
    String msg;
    var ok = false;
    try {
      final r = await f();
      final ack = r['ack'];
      final reason = ack is Map ? '${ack['reason'] ?? ''}' : '';
      ok = ack is! Map || ack['result'] == 'accepted' || ack['result'] == 'duplicate';
      msg = '$what：${ack is Map ? '${ack['result']}' : 'ok'}'
          '${reason.isNotEmpty ? '（${ackReasonText(reason)}）' : ''}';
    } on SiteError catch (e) {
      msg = '$what 没成：$e';
    }
    if (!mounted) return ok; // 派单要等回执，人可能早就退出这一页了
    setState(() => _msg = msg);
    await _reload();
    return ok;
  }

  void _openTrail() => Navigator.push(context,
      MaterialPageRoute<void>(builder: (_) => SiteMappingTrailPage(api: widget.api, robotId: widget.robotId)));

  /// 录包（W00c5d 第二部分，管理员）：给包起个名，之后经站点遥控开着狗走一圈，再停。
  Future<void> _startRecord() async {
    final got = await showFieldsDialog(context,
        title: '开始录包',
        fields: [
          DialogField('包名（字母、数字、. _ -）',
              key: const Key('record-name'),
              initial:
                  'rec-${DateTime.now().toUtc().toIso8601String().substring(0, 16).replaceAll(':', '')}'),
        ],
        confirm: '开始',
        confirmKey: const Key('record-go'));
    if (got == null || got[0].isEmpty) return;
    final ok = await _do(() => widget.api.mapping(widget.robotId, 'start', name: got[0]), '开始录包');
    // 收下了就打开录包轨迹（W00c6h）：边开边看哪儿走过了。录包在狗上后台起，起来之后才开始记点。
    if (ok && mounted && robotCan(_view, 'mapping_trail')) _openTrail();
  }

  /// 设位置（W00c6e）：「狗在原点」一键，或者输地图坐标（朝向按度输）。
  Future<void> _relocalize() async {
    final how = await showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: const Text('狗现在在哪'), children: [
        SimpleDialogOption(
            key: const Key('reloc-home'),
            onPressed: () => Navigator.pop(c, 'home'),
            child: const Text('在原点（充电桩）')),
        SimpleDialogOption(
            key: const Key('reloc-xy'),
            onPressed: () => Navigator.pop(c, 'xy'),
            child: const Text('输地图坐标')),
      ]),
    );
    if (how == null || !mounted) return;
    if (how == 'home') {
      // 点错了狗就按错的位置走（W00c6e 内审）：再确认一次。
      final sure = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
          title: const Text('狗现在就在原点？'),
          content: const Text('要站在原点（充电桩）上、朝向跟标原点时一样。站在别处点了，狗会按错的位置走。'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
            FilledButton(
                key: const Key('reloc-home-go'),
                onPressed: () => Navigator.pop(c, true),
                child: const Text('在原点')),
          ],
        ),
      );
      if (sure != true) return;
      await _do(() => widget.api.relocalize(widget.robotId, atHome: true), '设位置');
      return;
    }
    final got = await showFieldsDialog(context,
        title: '狗现在在地图上哪儿',
        fields: const [
          DialogField('x（米）', key: Key('reloc-x')),
          DialogField('y（米）', key: Key('reloc-y')),
          // 地图的 x 轴不一定朝东（雷达建的图按建图起点定轴，W00c6e 内审）。
          DialogField('朝向（度，地图 x 轴方向为 0，逆时针为正）', key: Key('reloc-yaw'), initial: '0'),
        ],
        confirm: '设',
        confirmKey: const Key('reloc-go'));
    if (got == null) return;
    final x = double.tryParse(got[0]), y = double.tryParse(got[1]), deg = double.tryParse(got[2]);
    if (x == null || y == null || deg == null || !x.isFinite || !y.isFinite || !deg.isFinite) {
      if (mounted) setState(() => _msg = '坐标、朝向要是数字');
      return;
    }
    await _do(() => widget.api.relocalize(widget.robotId, x: x, y: y, yaw: deg * pi / 180),
        '设位置');
  }

  /// 在当前位置标原点（W00c6f，管理员）：确认之后狗用它此刻的位置当原点，站点登记成默认待命点。
  Future<void> _markHome() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('在这儿标原点'),
        content: Text('把 ${widget.robotId} 的默认待命点（原点，名字叫 home）换成它现在的位置？\n'
            '狗会先核定位：没设位置、偏差大就不标；在跑任务、在换图也不标。'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
          FilledButton(
              key: const Key('mark-home-go'),
              onPressed: () => Navigator.pop(c, true),
              child: const Text('标')),
        ],
      ),
    );
    if (ok != true) return;
    String msg;
    try {
      Map<String, dynamic> r;
      try {
        r = await widget.api.markHome(widget.robotId);
      } on SiteError catch (e) {
        final taken = e.body['name_taken'];
        if (e.status != 409 || taken is! Map || !mounted) rethrow;
        // 同名的点登记在别的图上（比如重建之前那一版）：问清楚再搬（W00c6f 内审）。
        final where = '${taken['map_id']}:${taken['map_version']}';
        final move = await showDialog<bool>(
          context: context,
          builder: (c) => AlertDialog(
            title: const Text('替换旧的待命点？'),
            content: Text('待命点 home 现在登记在 $where 上。换到狗现在用的这张图上（$where 上就没有它了）？'),
            actions: [
              TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
              FilledButton(
                  key: const Key('mark-home-replace'),
                  onPressed: () => Navigator.pop(c, true),
                  child: const Text('替换')),
            ],
          ),
        );
        if (move != true) {
          if (mounted) setState(() => _msg = '没标：待命点 home 还在 $where 上');
          return;
        }
        r = await widget.api.markHome(widget.robotId, replace: true);
      }
      final d = (r['ack'] as Map?)?['data'];
      msg = d is Map
          ? '原点标好了：(${(d['x'] as num).toStringAsFixed(1)}, ${(d['y'] as num).toStringAsFixed(1)})'
              '，偏差约 ${(d['sigma_m'] as num? ?? 0).toStringAsFixed(1)} m'
          : '原点标好了';
    } on SiteError catch (e) {
      final d = e.body['data'];
      if (e.status == 500 && d is Map && d['x'] is num && d['y'] is num) {
        // 狗上已经换了、站点没登记上：给坐标，好手工登记待命点。
        msg = '${e.message}：狗上的原点在 ${d['map_id']}:${d['map_version']} 的 '
            '(${(d['x'] as num).toStringAsFixed(1)}, ${(d['y'] as num).toStringAsFixed(1)})';
      } else if (e.status == 409) {
        // 站点回的是「狗没标:<狗的原因>」：原因照拒收那一套说人话。
        msg = '没标：${ackReasonText(e.message.replaceFirst(RegExp(r'^狗没标[:：]'), ''))}';
      } else {
        msg = e.toString(); // 504「狗可能已经标了」等：照站点说的
      }
    }
    if (!mounted) return;
    setState(() => _msg = msg);
    await _reload();
  }

  /// 叫停之前**现取**正在跑的任务：页面上的可能是几秒前的，排程早换了一趟。
  Future<void> _abort() async {
    Map<String, dynamic> v;
    try {
      v = await widget.api.robot(widget.robotId);
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = '叫停没成：$e');
      return;
    }
    final st = v['status'];
    final task = st is Map ? st['task'] : null;
    final tid = task is Map ? task['task_id'] : null;
    if (tid is! String || tid.isEmpty) {
      if (mounted) {
        setState(() {
          _view = v;
          _msg = '它现在没在跑任务';
        });
      }
      return;
    }
    await _do(() => widget.api.abort(widget.robotId, tid), '叫停');
  }

  Future<void> _patrol() async {
    List<String> missions;
    try {
      final s = await widget.api.schedule();
      missions = (s['missions'] as List? ?? const []).map((e) => '$e').toList();
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = '拿不到任务：$e');
      return;
    }
    if (!mounted) return;
    final pick = await showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: const Text('派哪趟巡检'), children: [
        if (missions.isEmpty) const Padding(padding: EdgeInsets.all(16), child: Text('站点上还没有任务包')),
        for (final m in missions)
          SimpleDialogOption(onPressed: () => Navigator.pop(c, m), child: Text(m)),
      ]),
    );
    if (pick == null) return;
    await _do(() => widget.api.patrol(widget.robotId, pick), '派巡检 $pick');
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.api.session;
    final v = _view;
    final st = v?['status'];
    final task = st is Map ? st['task'] : null;
    final taskId = task is Map && task['task_id'] is String ? task['task_id'] as String : null;
    final events = (v?['events'] as List? ?? const []).whereType<Map>().toList();
    return Scaffold(
      appBar: AppBar(title: Text(widget.robotId)),
      // 「我在现场监护」钉在列表外面（W00c6i 内审）：放在按需构建的列表里，上面插一行提示或者往下一翻，
      // 它的状态就被换掉、发出放租 —— 刚派出去的任务随即被中止。
      body: Column(children: [
        if ((s?.canDispatch ?? false) && robotNeedsSupervision(v))
          SupervisionSwitch(
              key: ValueKey('supervise-${widget.robotId}'), api: widget.api, robotId: widget.robotId),
        Expanded(
          child: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(padding: const EdgeInsets.all(12), children: [
          Text(v == null ? '加载中…' : _statusLine(v), key: const Key('robot-status')),
          if (v?['loc'] is Map)
            Text(locText((v!['loc'] as Map).cast<String, dynamic>()), key: const Key('robot-loc')),
          // 叫停之后站点不再派它（W00c5e）：说清楚，给能派单的人一个「恢复」。
          if (v?['held'] is Map)
            Card(
              key: const Key('robot-held'),
              color: Colors.orange.shade50,
              child: ListTile(
                title: Text('被 ${(v!['held'] as Map)['by']} 叫停了'),
                subtitle: const Text('站点不会再派它（排程、事件、回待命点、遥控都不派），点「恢复」之后才派'),
                trailing: (s?.canDispatch ?? false)
                    ? FilledButton(
                        key: const Key('btn-resume'),
                        onPressed: () => _do(() => widget.api.resume(widget.robotId), '恢复'),
                        child: const Text('恢复'))
                    : null,
              ),
            ),
          if (_msg != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_msg!)),
          const SizedBox(height: 12),
          Wrap(spacing: 8, runSpacing: 8, children: [
            if (s?.canDispatch ?? false)
              FilledButton(
                  key: const Key('btn-patrol'), onPressed: _patrol, child: const Text('派巡检')),
            if (s?.canTeleop ?? false)
              FilledButton.tonal(
                  key: const Key('btn-teleop'),
                  onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) =>
                              SiteTeleopPage(api: widget.api, robotId: widget.robotId))),
                  child: const Text('遥控')),
            if ((s?.canManageMaps ?? false) && robotCan(v, 'mapping')) ...[
              OutlinedButton(
                  key: const Key('btn-record-start'),
                  onPressed: _startRecord,
                  child: const Text('开始录包')),
              OutlinedButton(
                  key: const Key('btn-record-stop'),
                  onPressed: () => _do(() => widget.api.mapping(widget.robotId, 'stop'), '停止录包'),
                  child: const Text('停止录包')),
            ],
            if ((s?.canManageMaps ?? false) && robotCan(v, 'mark_home'))
              OutlinedButton(
                  key: const Key('btn-mark-home'),
                  onPressed: _markHome,
                  child: const Text('在这儿标原点')),
            if ((s?.canManageMaps ?? false) && robotCan(v, 'mapping_trail'))
              OutlinedButton(
                  key: const Key('btn-mapping-trail'), onPressed: _openTrail, child: const Text('录包轨迹')),
            // 建图进程日志（W00c6g）：管理员看录包、重建子进程的日志，不用再 SSH 上狗。
            if ((s?.canManageMaps ?? false) && robotCan(v, 'proc_log'))
              OutlinedButton(
                  key: const Key('btn-proc-logs'),
                  onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) =>
                              SiteProcLogsPage(api: widget.api, robotId: widget.robotId))),
                  child: const Text('建图日志')),
            if ((s?.canDispatch ?? false) && robotCan(v, 'relocalize'))
              OutlinedButton(
                  key: const Key('btn-relocalize'),
                  onPressed: _relocalize,
                  child: const Text('设位置')),
            if (s?.canDispatch ?? false)
              OutlinedButton(
                  key: const Key('btn-standby'),
                  onPressed: () => _do(() => widget.api.returnToStandby(widget.robotId), '回待命点'),
                  child: const Text('回待命点')),
            if ((s?.canAbort ?? false) && taskId != null)
              FilledButton(
                  key: const Key('btn-abort'),
                  style: FilledButton.styleFrom(backgroundColor: Colors.red),
                  onPressed: _abort,
                  child: const Text('叫停')),
          ]),
          const SizedBox(height: 12),
          // 画面经站点来（W00c5b）：谁都能看（业主也是），站点那头有人看才让狗推。
          // **放在按钮下面**：叫停要一眼就按得到，不许被画面挤出屏幕。
          SiteVideo(api: widget.api, robotId: widget.robotId),
          const Divider(),
          for (final e in events.take(30))
            ListTile(dense: true, title: Text('${e['kind']}'), subtitle: Text('${e['data']}')),
        ]),
          ),
        ),
      ]),
    );
  }
}

// ------------------------------------------------------------ 事件账、排程

class SiteIncidentsPage extends StatelessWidget {
  final SiteApi api;
  const SiteIncidentsPage({super.key, required this.api});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('事件')),
      body: FutureBuilder<List<Map<String, dynamic>>>(
        future: api.incidents(),
        builder: (c, snap) {
          if (snap.hasError) return Center(child: Text('${snap.error}'));
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final rows = snap.data!;
          if (rows.isEmpty) return const Center(child: Text('没有事件'));
          return ListView(children: [
            for (final r in rows)
              ListTile(
                title: Text('${r['zone']} · ${r['outcome']}'),
                subtitle: Text('${r['source']} ${r['event_id']} → '
                    '${r['robot_id'] ?? '-'} ${r['result'] ?? ''}'),
              ),
          ]);
        },
      ),
    );
  }
}

class SiteSchedulePage extends StatelessWidget {
  final SiteApi api;
  const SiteSchedulePage({super.key, required this.api});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('排程')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: api.schedule(),
        builder: (c, snap) {
          if (snap.hasError) return Center(child: Text('${snap.error}'));
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final d = snap.data!;
          final entries = (d['entries'] as List? ?? const []).whereType<Map>().toList();
          if (d['bundle'] == null) return const Center(child: Text('站点上还没有任务包'));
          return ListView(children: [
            ListTile(
                title: Text('任务包 ${(d['bundle'] as Map)['bundle_id']} '
                    'v${(d['bundle'] as Map)['version']}'),
                subtitle: Text('时区 ${d['timezone']}')),
            for (final e in entries)
              ListTile(
                title: Text('${e['id']} · ${e['at']} · ${e['mission']}'),
                subtitle: Text('下一轮 ${e['next_run']}'
                    '${e['last'] is Map ? ' · 上一次 ${(e['last'] as Map)['outcome']} ${(e['last'] as Map)['result'] ?? ''}' : ''}'),
              ),
          ]);
        },
      ),
    );
  }
}
