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
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../model/alert.dart';
import '../model/site_watch.dart';
import '../net/site_client.dart';

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
  const SiteWatchPage({super.key, required this.api, this.now = DateTime.now});

  static const Key p1NoneKey = Key('watch-p1-none');
  static const Key p1LoadingKey = Key('watch-p1-loading');
  static const Key alertsErrorKey = Key('watch-alerts-error');
  static const Key summaryErrorKey = Key('watch-summary-error');
  static Key ackKey(String key) => Key('watch-ack-$key');
  static Key resolveKey(String key) => Key('watch-resolve-$key');
  static Key robotKey(String id) => Key('watch-robot-$id');
  static const Key loadedAtKey = Key('watch-loaded-at');
  static const Key liveLostKey = Key('watch-live-lost');

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
      if (mounted) setState(() => _alertsError = '告警读不到：$e');
    } on FormatException catch (e) {
      // 读不懂的名单不许当成「没有 P1」：那是这一屏能说的最坏的假话。
      if (mounted) setState(() => _alertsError = '告警读不懂：${e.message}');
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
      if (mounted) setState(() => _summaryError = '汇总读不到：$e');
    } on FormatException catch (e) {
      if (mounted) setState(() => _summaryError = '汇总读不懂：${e.message}');
    }
  }

  Future<void> _act(Future<Map<String, dynamic>> Function() call, String what) async {
    try {
      await call();
    } on SiteError catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$what 没成：$e')));
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

  Widget _alertTile(Alert a) {
    final can = widget.api.session?.canHandleAlerts ?? false;
    final state = <String>[
      if (a.ackedBy.isNotEmpty) '${a.ackedBy} 已确认' else '还没有人确认',
      if (a.escalated > 0) '已升级到 ${a.channel}',
      if (a.count > 1) '${a.count} 次',
    ].join(' · ');
    return Card(
      color: a.level == 'P1' ? Colors.red.shade50 : null,
      child: ListTile(
        title: Text('${a.level} ${a.robot} · ${a.title}'),
        subtitle: Text([if (a.detail.isNotEmpty) a.detail, state].join('\n')),
        isThreeLine: a.detail.isNotEmpty,
        trailing: can
            ? Wrap(spacing: 4, children: [
                if (a.ackedMs == null)
                  TextButton(
                      key: SiteWatchPage.ackKey(a.key),
                      onPressed: () => _act(() => widget.api.ackAlert(a.key), '确认'),
                      child: const Text('确认')),
                TextButton(
                    key: SiteWatchPage.resolveKey(a.key),
                    onPressed: () => _act(() => widget.api.resolveAlert(a.key), '解决'),
                    child: const Text('解决')),
              ])
            : null,
      ),
    );
  }

  String _num(Object? v, String unit, String why) => v == null ? '不知道（$why）' : '$v$unit';

  int? _pct(double? r) => r == null ? null : (r * 100).round();

  String _ago(int s) => s < 120 ? '$s 秒' : s < 7200 ? '${s ~/ 60} 分钟' : '${s ~/ 3600} 小时';

  /// 站点自己的备份（W00c5d）。
  String _backupText(SiteWatchSummary s) {
    final b = s.siteBackup;
    if (b == null || b['configured'] != true) return '备份：${s.siteWhy['backup'] ?? '没配'}';
    final ok = (b['last_ok_ms'] as num?)?.toInt();
    final when = ok == null ? '还没成功过' : '上次成功在 ${_ago(((s.nowMs - ok) ~/ 1000).clamp(0, 1 << 31))}前';
    final err = '${b['error'] ?? ''}';
    return '备份：$when${b['stale'] == true ? '（过期了）' : ''}${err.isEmpty ? '' : '；$err'}';
  }

  Widget _robotTile(SiteWatchRobot r) {
    final lines = <String>[
      r.online ? (r.fresh ? '在线' : '在线（状态过期）') : '掉线',
      '电量 ${_num(r.batteryPct?.toStringAsFixed(0), '%', r.whyFor('battery_pct'))}',
      '钟偏 ${_num(r.clockSkewS?.toStringAsFixed(1), ' 秒', r.whyFor('clock_skew_s'))}',
      r.alerts == null
          ? '未解决告警 不知道（站点没给）'
          : '未解决 P1 ${r.alerts!['P1']} · P2 ${r.alerts!['P2']} · P3 ${r.alerts!['P3']}',
      '盘水位 ${_num(_pct(r.diskUsedRatio), '%', r.whyFor('disk_used_ratio'))}',
      '证据积压 ${_num(r.uploadBacklog, ' 个文件', r.whyFor('upload_backlog'))}'
          '${(r.oldestBacklogS ?? 0) > 0 ? '（最老的等了 ${_ago(r.oldestBacklogS!)}）' : ''}',
    ];
    return ListTile(
        key: SiteWatchPage.robotKey(r.robotId),
        title: Text(r.robotId),
        subtitle: Text(lines.join('\n')),
        isThreeLine: true);
  }

  @override
  Widget build(BuildContext context) {
    final p1 = _alerts.where((a) => a.level == 'P1').toList();
    final rest = _alerts.where((a) => a.level != 'P1').toList();
    final s = _summary;
    return Scaffold(
      appBar: AppBar(title: const Text('值守')),
      body: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(children: [
          if (!_live)
            const ListTile(
                key: SiteWatchPage.liveLostKey,
                leading: Icon(Icons.sync_problem, color: Colors.orange),
                title: Text('实时更新断了，正在重连；下面的数可能是旧的，下拉可以再问一次')),
          ListTile(
              key: SiteWatchPage.loadedAtKey,
              dense: true,
              title: Text(_alertsAt == null
                  ? '告警还没读到过'
                  : '告警读于 ${_hms(_alertsAt!)}；下拉可以再问一次')),
          const ListTile(title: Text('要立刻动身的（P1）')),
          if (_alertsError != null)
            ListTile(
                key: SiteWatchPage.alertsErrorKey,
                title: Text(_alertsError!, style: const TextStyle(color: Colors.red)))
          else if (_alertsAt == null)
            // 还没读到过：**不许说「没有」** —— 那是这一屏能说的最坏的假话。
            const ListTile(key: SiteWatchPage.p1LoadingKey, title: Text('还没读到'))
          else if (p1.isEmpty)
            const ListTile(key: SiteWatchPage.p1NoneKey, title: Text('没有'))
          else
            for (final a in p1) _alertTile(a),
          if (rest.isNotEmpty) const ListTile(title: Text('其余未解决的')),
          for (final a in rest) _alertTile(a),
          const Divider(),
          const ListTile(title: Text('每台狗')),
          if (_summaryError != null)
            ListTile(
                key: SiteWatchPage.summaryErrorKey,
                title: Text(_summaryError!, style: const TextStyle(color: Colors.red)))
          else if (s != null) ...[
            for (final r in s.robots) _robotTile(r),
            ListTile(
                title: const Text('站点'),
                subtitle: Text('${s.scheduleOk == null
                    ? '排程：${s.siteWhy['schedule_ok'] ?? '不知道'}'
                    : s.scheduleOk!
                        ? '排程正常'
                        : '排程没办成：${s.scheduleError}'}\n${_backupText(s)}')),
          ],
        ]),
      ),
    );
  }
}
