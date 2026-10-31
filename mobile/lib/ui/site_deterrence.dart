/// 分级驱离（W22，决策 37）。
///
/// 入侵派出去的狗到了拦截点，站点开一场驱离：L0 观察 → L1 灯光 → L2 语音警告（中文、英文、马来文轮放）→ L3 警笛，
/// 每 30 秒自动升一级，自动最多到 L3；L4「人工」只能人进。保安、管理员能跳级、往回退（动过一次就不再自动升）；
/// 保安、管理员、经理都能「解除」（全关、狗回待命点）。最长 10 分钟（从开始或最后一次人动过算）自动收。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

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
          : tr(' (auto, at the highest automatic level)', '（自动，已达自动最高级）'))
      : tr(' (${s['by'] ?? ''} in control)', '（${s['by'] ?? ''} 正在控制）');
  return 'L$lv $name$tail';
}

/// 驱离时狗上看到的人（W24）说人话：`看到 2 个人，最近 7.5 m` / `人走了` / 空（没检测或还没看到）。
String deterPersonsText(Map<String, dynamic> s) {
  final p = s['persons'];
  if (p is! Map) return '';
  if (p['gone'] == true) return tr('Person gone', '人员已离开');
  final n = p['count'];
  final near = p['nearest_m'];
  return tr('${n ?? '?'} person(s)${near is num ? ', nearest ${near.toStringAsFixed(1)} m' : ''}',
      '检测到 ${n ?? '?'} 人${near is num ? '，最近 ${near.toStringAsFixed(1)} m' : ''}');
}

/// 保持距离（W25）说人话：`守着` / `人太近，在往后退` / `无路可退，原地站定` / 空（狗不支持或没在守）。
String deterStandoffText(Map<String, dynamic> s) {
  switch (s['standoff']) {
    case 'hold':
      return tr('Holding position', '原地警戒');
    case 'retreat':
      return tr('Person too close, backing away', '人员过近，正在后退');
    case 'cornered':
      return tr('No room to back away; holding position', '无法后退，原地保持');
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
        if (_ended && f['ended'] is String) _msg = tr('Ended: ${f['ended']}', '已结束：${f['ended']}');
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
        _msg = tr('Switched to L$l ${deterLevelNames[l]}', '已切换至 L$l ${deterLevelNames[l]}');
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr('Level change failed: $e', '切换级别失败：$e'));
    }
  }

  Future<void> _release() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr(
            'End deterrence on ${widget.robotId}? (Lights and sound off; the robot returns to standby)',
            '解除 ${widget.robotId} 的驱离？（关闭声光，机器人返回待命点）')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
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
          _msg = tr('Deterrence ended', '已解除');
        });
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr('End deterrence failed: $e', '解除失败：$e'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final sess = widget.api.session;
    final s = _s;
    final lv = (s?['level'] as num?)?.toInt();
    const size = Size(Nw.touch, 48);
    final shape = RoundedRectangleBorder(borderRadius: BorderRadius.circular(Nw.r8));
    // 现在这一级按不了，但它是「选中的」，不是「用不了的」：照样实底
    final current = FilledButton.styleFrom(
        disabledBackgroundColor: c.primary,
        disabledForegroundColor: c.onPrimary,
        minimumSize: size,
        textStyle: NwText.labelLarge,
        shape: shape);
    final other = OutlinedButton.styleFrom(
        foregroundColor: c.text,
        side: BorderSide(color: c.textMuted),
        minimumSize: size,
        textStyle: NwText.labelLarge,
        shape: shape);
    return Scaffold(
      appBar: AppBar(title: Text(tr('${widget.robotId} · Deterrence', '${widget.robotId} · 驱离'))),
      body: ListView(
          // 横屏的手机只有 360 高：内距、间距收紧一点，常用的那几样不用滚就点得到
          padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
          children: [
        if (_msg.isNotEmpty)
          Container(
              margin: const EdgeInsets.only(bottom: Nw.s8),
              padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
              decoration: BoxDecoration(color: c.infoSoft, borderRadius: BorderRadius.circular(Nw.r8)),
              child: Text(_msg,
                  key: const Key('deterrence-msg'), style: NwText.bodyMedium.copyWith(color: c.text))),
        if (s == null && _ended)
          Text(tr('No deterrence in progress', '未在驱离'), style: NwText.bodyLarge.copyWith(color: c.text)),
        if (s != null) ...[
          NwCard(
              padding: EdgeInsets.zero,
              child: ListTile(
                key: const Key('deterrence-now'),
                // 正在驱离 = 入侵（P1）还在处理：红
                leading: Icon(Icons.campaign_rounded, color: c.error),
                title: Text(deterSessionText(s), style: NwText.titleMedium.copyWith(color: c.text)),
                subtitle: Text(
                    tr('Zone ${s['zone']} · ends automatically in ${s['ends_in_s']} s${deterSceneText(s)}',
                        '防区 ${s['zone']} · ${s['ends_in_s']} 秒后自动结束${deterSceneText(s)}'),
                    key: const Key('deterrence-persons'),
                    style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
              )),
          const SizedBox(height: Nw.s16),
          if (sess?.canDispatch ?? false)
            Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
              for (var l = 0; l < deterLevelNames.length; l++)
                (l == lv ? FilledButton.new : OutlinedButton.new)(
                  key: SiteDeterrencePage.levelKey(l),
                  style: l == lv ? current : other,
                  onPressed: l == lv ? null : () => _level(l),
                  child: Text('L$l ${deterLevelNames[l]}'),
                ),
            ]),
          const SizedBox(height: Nw.s16),
          if (sess?.canAbort ?? false)
            FilledButton.icon(
              key: SiteDeterrencePage.releaseKey,
              style: FilledButton.styleFrom(
                  backgroundColor: c.primary,
                  foregroundColor: c.onPrimary,
                  minimumSize: const Size(Nw.touch, 52),
                  textStyle: NwText.labelLarge,
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(Nw.r12))),
              onPressed: _release,
              icon: const Icon(Icons.check_rounded),
              label: Text(tr('End (all off, return to standby)', '解除（关闭声光并返回待命点）')),
            ),
        ],
      ]),
    );
  }
}
