/// 站点模式的几屏（W00c4）：站点列表 → 登录 → 狗的列表（实时）→ 单狗页（派巡检、叫停、回待命点）；
/// 事件账、排程各一页。
///
/// **按钮按角色显示**：业主（owner）只有「叫停」；保安（guard）与管理员（admin）能派单。真正的
/// 权限在站点上（站点回 403），这里只是不给人一个注定被拒的按钮。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../net/site_client.dart';
import '../store/site_store.dart';

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
  return online ? '在线 · $taskText' : '离线';
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

  @override
  void initState() {
    super.initState();
    widget.store.load().then((s) {
      if (mounted) setState(() => _sites = s);
    });
  }

  Future<void> _add() async {
    final name = TextEditingController();
    final url = TextEditingController(text: 'https://');
    final fp = TextEditingController();
    final user = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('添加站点'),
        content: SingleChildScrollView(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
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
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('保存')),
        ],
      ),
    );
    if (ok != true) return;
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
    SiteApi api;
    try {
      api = widget.apiFactory(s);
      await api.login(s.username, pw.text);
    } on SiteError catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('登录不了：$e')));
      return;
    }
    await nav.push(MaterialPageRoute<void>(
        builder: (_) => SiteRobotsPage(api: api, title: s.name)));
    api.close();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('站点')),
      floatingActionButton: FloatingActionButton(
          key: const Key('site-add'), onPressed: _add, child: const Icon(Icons.add)),
      body: _sites.isEmpty
          ? const Center(child: Text('还没有站点。右下角添加。'))
          : ListView(children: [
              for (final s in _sites)
                ListTile(
                    title: Text(s.name),
                    subtitle: Text('${s.url} · ${s.username}'),
                    onTap: () => _open(s)),
            ]),
    );
  }
}

// ------------------------------------------------------------ 狗的列表

class SiteRobotsPage extends StatefulWidget {
  final SiteApi api;
  final String title;
  const SiteRobotsPage({super.key, required this.api, this.title = '站点'});

  @override
  State<SiteRobotsPage> createState() => _SiteRobotsPageState();
}

class _SiteRobotsPageState extends State<SiteRobotsPage> {
  List<Map<String, dynamic>> _robots = <Map<String, dynamic>>[];
  String? _error;
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _debounce;

  @override
  void initState() {
    super.initState();
    _reload();
    _sub = widget.api.events().listen((_) => _soon(), onError: (_) {}, cancelOnError: false);
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
      if (mounted) setState(() => _error = e.toString());
    }
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _sub?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final role = widget.api.session?.role ?? '';
    return Scaffold(
      appBar: AppBar(title: Text('${widget.title}（$role）'), actions: [
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

  Future<void> _do(Future<Map<String, dynamic>> Function() f, String what) async {
    try {
      final r = await f();
      final ack = r['ack'];
      final res = ack is Map ? '${ack['result']}' : 'ok';
      setState(() => _msg = '$what：$res');
    } on SiteError catch (e) {
      setState(() => _msg = '$what 没成：$e');
    }
    await _reload();
  }

  Future<void> _patrol() async {
    List<String> missions;
    try {
      final s = await widget.api.schedule();
      missions = (s['missions'] as List? ?? const []).map((e) => '$e').toList();
    } on SiteError catch (e) {
      setState(() => _msg = '拿不到任务：$e');
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
    final taskId = task is Map ? '${task['task_id']}' : null;
    final events = (v?['events'] as List? ?? const []).whereType<Map>().toList();
    return Scaffold(
      appBar: AppBar(title: Text(widget.robotId)),
      body: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(padding: const EdgeInsets.all(12), children: [
          Text(v == null ? '加载中…' : _statusLine(v), key: const Key('robot-status')),
          if (_msg != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_msg!)),
          const SizedBox(height: 12),
          Wrap(spacing: 8, runSpacing: 8, children: [
            if (s?.canDispatch ?? false)
              FilledButton(
                  key: const Key('btn-patrol'), onPressed: _patrol, child: const Text('派巡检')),
            if (s?.canDispatch ?? false)
              OutlinedButton(
                  key: const Key('btn-standby'),
                  onPressed: () => _do(() => widget.api.returnToStandby(widget.robotId), '回待命点'),
                  child: const Text('回待命点')),
            if ((s?.canAbort ?? false) && taskId != null)
              FilledButton(
                  key: const Key('btn-abort'),
                  style: FilledButton.styleFrom(backgroundColor: Colors.red),
                  onPressed: () => _do(() => widget.api.abort(widget.robotId, taskId), '叫停'),
                  child: const Text('叫停')),
          ]),
          const Divider(),
          for (final e in events.take(30))
            ListTile(dense: true, title: Text('${e['kind']}'), subtitle: Text('${e['data']}')),
        ]),
      ),
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
