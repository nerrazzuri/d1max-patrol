/// 分级驱离（W22，决策 37）。
///
/// 入侵派出去的狗到了拦截点，站点开一场驱离：L0 观察 → L1 灯光 → L2 语音警告（中文、英文、马来文轮放）→ L3 警笛，
/// 每 30 秒自动升一级，自动最多到 L3；L4「人工」只能人进。保安、管理员能跳级、往回退（动过一次就不再自动升）；
/// 保安、管理员、业主都能「解除」（全关、狗回待命点）。最长 10 分钟（从开始或最后一次人动过算）自动收。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../design/tokens.g.dart';
import '../l10n.dart';
import '../net/site_client.dart';

/// 每一级的名字（跟站点 `deterrence.LABEL` 一样）。
List<String> get deterLevelNames => <String>[
      tr('Observe', '观察'),
      tr('Light', '灯光'),
      tr('Voice warning', '语音警告'),
      tr('Siren', '警笛'),
      tr('Manual', '人工'),
    ];

/// 一场驱离说人话：`L2 语音警告（自动，24 秒后升级）`。
String deterSessionText(Map<String, dynamic> s) {
  final lv = (s['level'] as num?)?.toInt() ?? 0;
  final name = lv >= 0 && lv < deterLevelNames.length ? deterLevelNames[lv] : '?';
  final next = s['next_in_s'];
  final tail = s['auto'] == true
      ? (next is num
          ? tr(' (auto, escalates in ${next.toInt()} s)', '（自动，${next.toInt()} 秒后升级）')
          : tr(' (auto, at the highest automatic level)', '（自动，已到最高的自动一级）'))
      : tr(' (${s['by'] ?? ''} in control)', '（${s['by'] ?? ''} 在管）');
  return 'L$lv $name$tail';
}

/// 驱离时狗上看到的人（W24）说人话：`看到 2 个人，最近 7.5 m` / `人走了` / 空（没检测或还没看到）。
String deterPersonsText(Map<String, dynamic> s) {
  final p = s['persons'];
  if (p is! Map) return '';
  if (p['gone'] == true) return tr('Person gone', '人走了');
  final n = p['count'];
  final near = p['nearest_m'];
  return tr('${n ?? '?'} person(s)${near is num ? ', nearest ${near.toStringAsFixed(1)} m' : ''}',
      '看到 ${n ?? '?'} 个人${near is num ? '，最近 ${near.toStringAsFixed(1)} m' : ''}');
}

/// 保持距离（W25）说人话：`守着` / `人太近，在往后退` / `无路可退，原地站定` / 空（狗不支持或没在守）。
String deterStandoffText(Map<String, dynamic> s) {
  switch (s['standoff']) {
    case 'hold':
      return tr('Holding position', '守着');
    case 'retreat':
      return tr('Person too close, backing away', '人太近，在往后退');
    case 'cornered':
      return tr('No room to back away, standing still', '无路可退，原地站定');
  }
  return '';
}

/// 一场驱离的现场（看到的人、保持距离）拼成一串，每样前面带「 · 」；都没有就是空串。
String deterSceneText(Map<String, dynamic> s) =>
    [deterPersonsText(s), deterStandoffText(s)].where((t) => t.isNotEmpty).map((t) => ' · $t').join();

class SiteDeterrencePage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  const SiteDeterrencePage({super.key, required this.api, required this.robotId});

  static const Key releaseKey = Key('deterrence-release');
  static Key levelKey(int l) => Key('deterrence-level-$l');

  @override
  State<SiteDeterrencePage> createState() => _SiteDeterrencePageState();
}

class _SiteDeterrencePageState extends State<SiteDeterrencePage> {
  Map<String, dynamic>? _s;
  bool _ended = false;
  String _msg = '';
  StreamSubscription<Map<String, dynamic>>? _sub;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _load();
    _sub = widget.api.events().listen((f) {
      if (f['kind'] != 'deterrence' || f['robot_id'] != widget.robotId || !mounted) return;
      final s = f['session'];
      setState(() {
        _s = s is Map ? Map<String, dynamic>.from(s) : null;
        _ended = s is! Map;
        if (_ended && f['ended'] is String) _msg = tr('Ended: ${f['ended']}', '结束了：${f['ended']}');
      });
    }, onError: (Object _) {});
    _poll = Timer.periodic(const Duration(seconds: 5), (_) => unawaited(_load())); // 倒计时、兜底
  }

  @override
  void dispose() {
    _sub?.cancel();
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final rows = await widget.api.deterrence();
      final mine = rows.where((r) => r['robot_id'] == widget.robotId);
      if (!mounted) return;
      setState(() {
        _s = mine.isEmpty ? null : mine.first;
        _ended = mine.isEmpty;
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = '$e');
    }
  }

  Future<void> _level(int l) async {
    try {
      final r = await widget.api.deterLevel(widget.robotId, l);
      if (!mounted) return;
      setState(() {
        if (r['session'] is Map) _s = Map<String, dynamic>.from(r['session'] as Map);
        _msg = tr('Switched to L$l ${deterLevelNames[l]}', '切到 L$l ${deterLevelNames[l]}');
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr('Level change failed: $e', '切级没成：$e'));
    }
  }

  Future<void> _release() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr(
            'End deterrence on ${widget.robotId}? (Light and sound off, robot returns to standby)',
            '解除 ${widget.robotId} 的驱离？（声光全关、狗回待命点）')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
          FilledButton(key: const Key('deterrence-release-go'), onPressed: () => Navigator.pop(c, true), child: Text(tr('End deterrence', '解除'))),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await widget.api.deterRelease(widget.robotId);
      if (mounted) {
        setState(() {
          _s = null;
          _ended = true;
          _msg = tr('Deterrence ended', '解除了');
        });
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr('End deterrence failed: $e', '解除没成：$e'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final sess = widget.api.session;
    final s = _s;
    final lv = (s?['level'] as num?)?.toInt();
    return Scaffold(
      appBar: AppBar(title: Text(tr('${widget.robotId} · Deterrence', '${widget.robotId} · 驱离'))),
      body: ListView(padding: const EdgeInsets.all(8), children: [
        if (_msg.isNotEmpty) Text(_msg, key: const Key('deterrence-msg')),
        if (s == null && _ended) Text(tr('No deterrence in progress', '没在驱离')),
        if (s != null) ...[
          ListTile(
            key: const Key('deterrence-now'),
            leading: const Icon(Icons.campaign, color: D1Color.p1Text),
            title: Text(deterSessionText(s)),
            subtitle: Text(
                tr('Zone ${s['zone']} · ends automatically in ${s['ends_in_s']} s${deterSceneText(s)}',
                    '防区 ${s['zone']} · ${s['ends_in_s']} 秒后自动收${deterSceneText(s)}'),
                key: const Key('deterrence-persons')),
          ),
          if (sess?.canDispatch ?? false)
            Wrap(spacing: 6, children: [
              for (var l = 0; l < deterLevelNames.length; l++)
                (l == lv ? FilledButton.new : OutlinedButton.new)(
                  key: SiteDeterrencePage.levelKey(l),
                  onPressed: l == lv ? null : () => _level(l),
                  child: Text('L$l ${deterLevelNames[l]}'),
                ),
            ]),
          const SizedBox(height: 8),
          if (sess?.canAbort ?? false)
            FilledButton.icon(
              key: SiteDeterrencePage.releaseKey,
              style: FilledButton.styleFrom(backgroundColor: D1Color.text),
              onPressed: _release,
              icon: const Icon(Icons.check),
              label: Text(tr('End deterrence (all off, return to standby)', '解除（全关、回待命点）')),
            ),
        ],
      ]),
    );
  }
}
