/// 站点模式的运行记录（W00c5d，决策 8：证据都在站点，狗上传完就删）。
///
/// - 列表：每一趟一行（哪台狗、哪个任务、什么时候、几张照片、判读结论、复核了几张）。
/// - 一趟：每张照片带模型的结论和人的复核；**人的复核优先**（模型判的是「先看哪几张」）。
/// - 保安、管理员能复核、让站点重判；经理只看。
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_cards.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

/// 结论给人看的话。
String verdictText(String? v) => switch (v) {
      'normal' => tr('Normal', '正常'),
      'abnormal' => tr('Abnormal', '异常'),
      'unclear' => tr('Unclear', '看不清'),
      'pending' => tr('Pending', '待判读'),
      null => tr('Pending', '待判读'),
      _ => v,
    };

/// `20260925T010000Z` → 本地时间 `2026-09-25 09:00`。认不出来原样给。
String stampText(String stamp) {
  final m = RegExp(r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$').firstMatch(stamp);
  if (m == null) return stamp;
  final t = DateTime.utc(int.parse(m[1]!), int.parse(m[2]!), int.parse(m[3]!),
          int.parse(m[4]!), int.parse(m[5]!), int.parse(m[6]!))
      .toLocal();
  String two(int n) => n.toString().padLeft(2, '0');
  return '${t.year}-${two(t.month)}-${two(t.day)} ${two(t.hour)}:${two(t.minute)}';
}

String _verdictSummary(Map<String, dynamic> r) {
  final v = r['verdicts'];
  if (r['judged_ms'] == null || v is! Map || v.isEmpty) return tr('Pending', '待判读');
  final parts = <String>[
    for (final k in ['abnormal', 'unclear', 'normal', 'pending'])
      if ((v[k] as num? ?? 0) > 0) '${verdictText(k)} ${v[k]}',
  ];
  return parts.join(' · ');
}

/// 结果那一行里异常的那一段（和 [_verdictSummary] 拼出来的一样）。
String _abnormalPart(Map<String, dynamic> r) =>
    '${verdictText('abnormal')} ${(r['verdicts'] as Map)['abnormal']}';

/// 这一趟有没有判成异常的照片。
bool _abnormal(Map<String, dynamic> r) => ((r['verdicts'] as Map?)?['abnormal'] as num? ?? 0) > 0;

/// 这一趟是哪天跑的（当地时间）：按目录名那个 UTC 时刻。认不出来是 null。
DateTime? _stampTime(Object? stamp) {
  final m = RegExp(r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$').firstMatch('$stamp');
  if (m == null) return null;
  return DateTime.utc(int.parse(m[1]!), int.parse(m[2]!), int.parse(m[3]!), int.parse(m[4]!),
          int.parse(m[5]!), int.parse(m[6]!))
      .toLocal();
}

/// 记录栏顶上那两个数：今天（当地日期，按 [now]）跑了几趟、其中几趟有异常。只数已经读到的这一份列表，
/// 不另外问站点。
({int today, int anomalies}) runStats(List<Map<String, dynamic>> rows, DateTime now) {
  var today = 0, anomalies = 0;
  for (final r in rows) {
    final t = _stampTime(r['stamp']);
    if (t == null || t.year != now.year || t.month != now.month || t.day != now.day) continue;
    today++;
    if (_abnormal(r)) anomalies++;
  }
  return (today: today, anomalies: anomalies);
}

class SiteRunsPage extends StatefulWidget {
  final SiteApi api;
  final String? robotId;

  /// 嵌在底部导航的记录栏里：不带自己的顶栏（页头是记录栏的那排分段）。
  final bool embedded;

  /// 现在几点（「今天」按它算）。测试换掉它。
  final DateTime Function() now;
  const SiteRunsPage(
      {super.key, required this.api, this.robotId, this.embedded = false, this.now = DateTime.now});

  static Key runKey(int id) => Key('run-$id');
  static const Key todayKey = Key('runs-today');
  static const Key anomalyKey = Key('runs-anomalies');

  @override
  State<SiteRunsPage> createState() => _SiteRunsPageState();
}

class _SiteRunsPageState extends State<SiteRunsPage> {
  late Future<List<Map<String, dynamic>>> _rows = widget.api.runs(robotId: widget.robotId);

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final body = RefreshIndicator(
      onRefresh: () async {
        setState(() {
          _rows = widget.api.runs(robotId: widget.robotId);
        });
        await _rows.catchError((Object _) => <Map<String, dynamic>>[]);
      },
      child: FutureBuilder<List<Map<String, dynamic>>>(
        future: _rows,
        builder: (_, snap) {
          if (snap.hasError) {
            return NwListView(children: [
              NwNotice(
                  tone: NwTone.error,
                  text: tr("Can't load records: ${snap.error}", '记录拿不到：${snap.error}')),
            ]);
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final rows = snap.data!;
          final stats = runStats(rows, widget.now());
          return NwListView(children: [
            NwSectionLabel(tr('Today', '今天')),
            nwGap8,
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(
                  child: NwStatTile(
                      key: SiteRunsPage.todayKey,
                      icon: Icons.route_rounded,
                      label: tr('Rounds', '巡了几趟'),
                      value: '${stats.today}')),
              const SizedBox(width: Nw.s8),
              // 判成异常的照片是要人去看的：有就标红（跟下面卡片上的标记一个色），没有不上色
              Expanded(
                  child: NwStatTile(
                      key: SiteRunsPage.anomalyKey,
                      icon: Icons.warning_amber_rounded,
                      label: tr('With anomalies', '有异常的'),
                      value: '${stats.anomalies}',
                      tone: stats.anomalies > 0 ? NwTone.error : NwTone.neutral)),
            ]),
            nwGap24,
            NwSectionLabel(tr('Recent rounds', '最近的巡检')),
            nwGap8,
            if (rows.isEmpty)
              NwCard(child: Text(tr('No records yet', '还没有记录'), style: NwText.bodyMedium.copyWith(color: c.textSecondary))),
            for (final r in rows)
              Padding(
                  padding: const EdgeInsets.only(bottom: Nw.s8),
                  // 站点的列表里没有照片（只有张数）：不摆缩略图那一行，点进去才取照片
                  child: Stack(
                      key: SiteRunsPage.runKey((r['id'] as num).toInt()),
                      fit: StackFit.passthrough,
                      children: [
                        NwPatrolCard(
                            id: '${r['mission']}',
                            time: '${stampText('${r['stamp']}')} · ${r['robot_id']}',
                            status: tr(
                                '${r['robot_id']} · ${r['photos']} photos · '
                                    '${r['finished'] == true ? '' : 'not finished · '}${_verdictSummary(r)}'
                                    ' · ${r['reviewed'] ?? 0} reviewed',
                                '${r['robot_id']} · 照片 ${r['photos']} · '
                                    '${r['finished'] == true ? '' : '没跑完 · '}${_verdictSummary(r)}'
                                    ' · 已复核 ${r['reviewed'] ?? 0}'),
                            anomaly: _abnormal(r),
                            tone: _abnormal(r) ? NwTone.error : NwTone.neutral,
                            // 只把「异常 N」那一段标红，「正常 1 · 已复核 1」照常
                            flagged: _abnormal(r) ? _abnormalPart(r) : null,
                            onTap: () => Navigator.push(
                                context,
                                MaterialPageRoute<void>(
                                    builder: (_) =>
                                        SiteRunPage(api: widget.api, runId: (r['id'] as num).toInt())))),
                        if (_abnormal(r))
                          Positioned(
                              top: Nw.s16,
                              right: Nw.s16,
                              child: IgnorePointer(child: Icon(Icons.report, size: 24, color: c.error))),
                      ])),
          ]);
        },
      ),
    );
    if (widget.embedded) return body;
    return Scaffold(
      appBar: AppBar(
          title: Text(widget.robotId == null
              ? tr('Records', '记录')
              : tr('Records ${widget.robotId}', '记录 ${widget.robotId}'))),
      body: body,
    );
  }
}

class SiteRunPage extends StatefulWidget {
  final SiteApi api;
  final int runId;
  const SiteRunPage({super.key, required this.api, required this.runId});

  static Key photoKey(String name) => Key('photo-$name');
  static const Key judgeKey = Key('run-judge');
  static const Key keepKey = Key('run-keep');

  @override
  State<SiteRunPage> createState() => _SiteRunPageState();
}

class _SiteRunPageState extends State<SiteRunPage> {
  late Future<Map<String, dynamic>> _detail = widget.api.run(widget.runId);
  final Map<String, Future<Uint8List>> _photos = <String, Future<Uint8List>>{};
  bool _judging = false;

  /// 一张照片的字节。拿不到的不记着（下次再画的时候重拿），拿到的留在这一页里。
  Future<Uint8List> _photo(String name) => _photos.putIfAbsent(name, () {
        final f = widget.api.runPhoto(widget.runId, name);
        f.catchError((Object _) {
          _photos.remove(name);
          return Uint8List(0);
        });
        return f;
      });

  void _reload() => setState(() {
        _detail = widget.api.run(widget.runId);
      });

  Future<void> _judge() async {
    setState(() => _judging = true);
    try {
      // 站点在后台判（几十张照片要好几分钟）：这里只是开个头，过一会儿下拉刷新看结论。
      await widget.api.judgeRun(widget.runId);
      _snack(tr('The site has started reassessing. Refresh in a few minutes for the results',
          '站点开始重判了，过一会儿刷新看结论'));
    } on SiteError catch (e) {
      _snack(tr('Reassessment failed: $e', '判读没成：$e'));
    } finally {
      if (mounted) setState(() => _judging = false);
    }
  }

  /// 标「留着」（W30）：过了留存期（默认 90 天）、按时间段删都不删。
  Future<void> _keep(bool want) async {
    try {
      await widget.api.setRunKeep(widget.runId, want);
      _snack(want
          ? tr('Kept: not deleted after the retention period', '留着：过了留存期也不删')
          : tr('No longer kept: deleted when the retention period ends', '不留了：到期照常删'));
      _reload();
    } on SiteError catch (e) {
      _snack(tr('Not changed: $e', '没改成：$e'));
    }
  }

  void _snack(String text) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _open(Map<String, dynamic> p) async {
    final changed = await Navigator.push<bool>(
        context,
        MaterialPageRoute<bool>(
            builder: (_) => SitePhotoPage(
                api: widget.api, runId: widget.runId, photo: p, bytes: _photo('${p['name']}'))));
    if (changed == true) _reload();
  }

  @override
  Widget build(BuildContext context) {
    final canReview = widget.api.session?.canReview ?? false;
    return Scaffold(
      appBar: AppBar(title: Text(tr('This run', '这一趟')), actions: [
        if (canReview)
          FutureBuilder<Map<String, dynamic>>(
              future: _detail,
              builder: (c, snap) {
                final kept = (snap.data?['run'] as Map?)?['keep'] == 1;
                return IconButton(
                    key: SiteRunPage.keepKey,
                    tooltip: kept
                        ? tr('Stop keeping', '不留了')
                        : tr('Keep (not deleted after the retention period)', '留着（过了留存期也不删）'),
                    icon: Icon(kept ? Icons.bookmark : Icons.bookmark_border),
                    onPressed: snap.hasData ? () => _keep(!kept) : null);
              }),
        if (canReview)
          IconButton(
              key: SiteRunPage.judgeKey,
              tooltip: tr('Reassess on the site', '让站点重判'),
              icon: _judging
                  ? const SizedBox(
                      width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.auto_awesome),
              onPressed: _judging ? null : _judge),
      ]),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _detail,
        builder: (c, snap) {
          if (snap.hasError) {
            return NwListView(children: [
              NwNotice(tone: NwTone.error, text: tr("Can't load: ${snap.error}", '拿不到：${snap.error}')),
            ]);
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final run = (snap.data!['run'] as Map?)?.cast<String, dynamic>() ?? {};
          final photos = (snap.data!['photos'] as List? ?? const [])
              .whereType<Map<String, dynamic>>()
              .toList();
          final nw = Nw.of(context);
          return NwListView(children: [
            NwCard(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
              Text('${run['mission']} · ${stampText('${run['stamp']}')}',
                  style: NwText.titleMedium.copyWith(color: nw.text)),
              nwGap4,
              Text(
                  tr(
                      '${run['robot_id']} · ${run['photos']} photos · '
                          '${run['finished'] == true ? 'finished (${run['result']})' : 'not finished'}',
                      '${run['robot_id']} · 照片 ${run['photos']} · '
                          '${run['finished'] == true ? '跑完了（${run['result']}）' : '没跑完'}'),
                  style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
            ])),
            nwGap24,
            NwSectionLabel(tr('Photos', '照片')),
            nwGap8,
            NwListCard(children: [
              if (photos.isEmpty) NwCardText(tr('No photos in this run', '这一趟没有照片')),
              for (final p in photos) _photoTile(p),
            ]),
          ]);
        },
      ),
    );
  }

  Widget _photoTile(Map<String, dynamic> p) {
    final nw = Nw.of(context);
    final f = (p['finding'] as Map?)?.cast<String, dynamic>();
    final r = (p['review'] as Map?)?.cast<String, dynamic>();
    final shown = r?['verdict'] ?? f?['verdict'];
    return ListTile(
      key: SiteRunPage.photoKey('${p['name']}'),
      leading: ClipRRect(
        borderRadius: BorderRadius.circular(Nw.r4),
        child: Container(
          width: 64,
          height: 48,
          color: nw.surfaceVariant,
          child: FutureBuilder<Uint8List>(
            future: _photo('${p['name']}'),
            builder: (c, s) => s.hasData && s.data!.isNotEmpty
                // 缩略图按小尺寸解码：几十张原图全尺寸解出来，手机内存吃不消。
                ? Image.memory(s.data!, fit: BoxFit.cover, gaplessPlayback: true, cacheWidth: 128)
                : Icon(s.hasError ? Icons.broken_image : Icons.photo, color: nw.textSecondary),
          ),
        ),
      ),
      title: Text(
          tr(
              '${p['waypoint']} · ${p['camera']} · ${verdictText(shown as String?)}'
                  '${r != null ? ' (reviewed)' : ''}',
              '${p['waypoint']} · ${p['camera']} · ${verdictText(shown)}'
                  '${r != null ? '（人已复核）' : ''}'),
          style: NwText.titleSmall.copyWith(color: nw.text)),
      subtitle: Text(
          r != null
              ? tr('Review: ${verdictText(r['verdict'] as String?)} ${r['note'] ?? ''}',
                  '复核：${verdictText(r['verdict'] as String?)} ${r['note'] ?? ''}')
              : f != null
                  ? tr('Assessment: ${f['reason'] ?? ''}', '判读：${f['reason'] ?? ''}')
                  : tr('Not assessed yet', '还没判读'),
          style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
      trailing: shown == 'abnormal' ? Icon(Icons.report, color: nw.error) : null,
      onTap: () => _open(p),
    );
  }
}

class SitePhotoPage extends StatefulWidget {
  final SiteApi api;
  final int runId;
  final Map<String, dynamic> photo;
  final Future<Uint8List> bytes;
  const SitePhotoPage(
      {super.key,
      required this.api,
      required this.runId,
      required this.photo,
      required this.bytes});

  static Key reviewKey(String verdict) => Key('review-$verdict');
  static const Key noteKey = Key('review-note');

  @override
  State<SitePhotoPage> createState() => _SitePhotoPageState();
}

class _SitePhotoPageState extends State<SitePhotoPage> {
  final TextEditingController _note = TextEditingController();
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    final r = (widget.photo['review'] as Map?)?.cast<String, dynamic>();
    _note.text = '${r?['note'] ?? ''}';
  }

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  Future<void> _review(String verdict) async {
    setState(() => _busy = true);
    try {
      await widget.api
          .reviewPhoto(widget.runId, '${widget.photo['name']}', verdict, _note.text.trim());
      if (mounted) Navigator.pop(context, true);
    } on SiteError catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(tr('Review not saved: $e', '复核没记下：$e'))));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final f = (widget.photo['finding'] as Map?)?.cast<String, dynamic>();
    final r = (widget.photo['review'] as Map?)?.cast<String, dynamic>();
    final canReview = widget.api.session?.canReview ?? false;
    final c = Nw.of(context);
    final line = NwText.bodyMedium.copyWith(color: c.text);
    return Scaffold(
      appBar: AppBar(title: Text('${widget.photo['waypoint']} · ${widget.photo['camera']}')),
      body: NwListView(children: [
        FutureBuilder<Uint8List>(
          future: widget.bytes,
          builder: (_, s) => s.hasError
              ? NwNotice(
                  tone: NwTone.error, text: tr("Can't load photo: ${s.error}", '照片拿不到：${s.error}'))
              : s.hasData
                  ? ClipRRect(
                      borderRadius: BorderRadius.circular(Nw.r8),
                      child: InteractiveViewer(child: Image.memory(s.data!)))
                  : const SizedBox(height: 200, child: Center(child: CircularProgressIndicator())),
        ),
        nwGap16,
        NwCard(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
          Text(
              f == null
                  ? tr('Model: not assessed yet', '模型：还没判读')
                  : tr(
                      'Model: ${verdictText(f['verdict'] as String?)} (confidence '
                          '${((f['confidence'] as num? ?? 0) * 100).round()}%) ${f['reason'] ?? ''}'
                          '${(f['evidence'] ?? '') != '' ? '; evidence: ${f['evidence']}' : ''}',
                      '模型：${verdictText(f['verdict'] as String?)}（把握 '
                          '${((f['confidence'] as num? ?? 0) * 100).round()}%）${f['reason'] ?? ''}'
                          '${(f['evidence'] ?? '') != '' ? '；依据：${f['evidence']}' : ''}'),
              style: line),
          nwGap8,
          Text(
              r == null
                  ? tr('Reviewer: not reviewed yet', '人：还没复核')
                  : tr('Reviewer: ${verdictText(r['verdict'] as String?)} ${r['note'] ?? ''}',
                      '人：${verdictText(r['verdict'] as String?)} ${r['note'] ?? ''}'),
              style: line),
        ])),
        if (canReview) ...[
          nwGap16,
          TextField(
              key: SitePhotoPage.noteKey,
              controller: _note,
              decoration: InputDecoration(labelText: tr('Note (optional)', '说一句（可不填）')),
              maxLength: 200),
          Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
            for (final v in ['normal', 'abnormal', 'unclear'])
              NwButton(
                  buttonKey: SitePhotoPage.reviewKey(v),
                  variant: NwButtonVariant.secondary,
                  onPressed: _busy ? null : () => _review(v),
                  label: verdictText(v)),
          ]),
        ],
      ]),
    );
  }
}
