/// 站点模式的运行记录（W00c5d，决策 8：证据都在站点，狗上传完就删）。
///
/// - 列表：每一趟一行（哪台狗、哪个任务、什么时候、几张照片、判读结论、复核了几张）。
/// - 一趟：每张照片带模型的结论和人的复核；**人的复核优先**（模型判的是「先看哪几张」）。
/// - 保安、管理员能复核、让站点重判；业主只看。
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../net/site_client.dart';

/// 结论给人看的话。
String verdictText(String? v) => switch (v) {
      'normal' => '正常',
      'abnormal' => '异常',
      'unclear' => '看不清',
      'pending' => '待判读',
      null => '待判读',
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
  if (r['judged_ms'] == null || v is! Map || v.isEmpty) return '待判读';
  final parts = <String>[
    for (final k in ['abnormal', 'unclear', 'normal', 'pending'])
      if ((v[k] as num? ?? 0) > 0) '${verdictText(k)} ${v[k]}',
  ];
  return parts.join(' · ');
}

class SiteRunsPage extends StatefulWidget {
  final SiteApi api;
  final String? robotId;
  const SiteRunsPage({super.key, required this.api, this.robotId});

  static Key runKey(int id) => Key('run-$id');

  @override
  State<SiteRunsPage> createState() => _SiteRunsPageState();
}

class _SiteRunsPageState extends State<SiteRunsPage> {
  late Future<List<Map<String, dynamic>>> _rows = widget.api.runs(robotId: widget.robotId);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.robotId == null ? '记录' : '记录 ${widget.robotId}')),
      body: RefreshIndicator(
        onRefresh: () async {
          setState(() {
            _rows = widget.api.runs(robotId: widget.robotId);
          });
          await _rows.catchError((Object _) => <Map<String, dynamic>>[]);
        },
        child: FutureBuilder<List<Map<String, dynamic>>>(
          future: _rows,
          builder: (c, snap) {
            if (snap.hasError) {
              return ListView(children: [
                ListTile(
                    title: Text('记录拿不到：${snap.error}',
                        style: const TextStyle(color: Colors.red))),
              ]);
            }
            if (!snap.hasData) return const Center(child: CircularProgressIndicator());
            final rows = snap.data!;
            if (rows.isEmpty) {
              return ListView(children: const [ListTile(title: Text('还没有记录'))]);
            }
            return ListView(children: [
              for (final r in rows)
                ListTile(
                  key: SiteRunsPage.runKey((r['id'] as num).toInt()),
                  title: Text('${r['mission']} · ${stampText('${r['stamp']}')}'),
                  subtitle: Text('${r['robot_id']} · 照片 ${r['photos']} · '
                      '${r['finished'] == true ? '' : '没跑完 · '}${_verdictSummary(r)}'
                      ' · 已复核 ${r['reviewed'] ?? 0}'),
                  trailing: ((r['verdicts'] as Map?)?['abnormal'] as num? ?? 0) > 0
                      ? const Icon(Icons.report, color: Colors.red)
                      : null,
                  onTap: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) =>
                              SiteRunPage(api: widget.api, runId: (r['id'] as num).toInt()))),
                ),
            ]);
          },
        ),
      ),
    );
  }
}

class SiteRunPage extends StatefulWidget {
  final SiteApi api;
  final int runId;
  const SiteRunPage({super.key, required this.api, required this.runId});

  static Key photoKey(String name) => Key('photo-$name');
  static const Key judgeKey = Key('run-judge');

  @override
  State<SiteRunPage> createState() => _SiteRunPageState();
}

class _SiteRunPageState extends State<SiteRunPage> {
  late Future<Map<String, dynamic>> _detail = widget.api.run(widget.runId);
  final Map<String, Future<Uint8List>> _photos = <String, Future<Uint8List>>{};
  bool _judging = false;

  Future<Uint8List> _photo(String name) =>
      _photos.putIfAbsent(name, () => widget.api.runPhoto(widget.runId, name));

  void _reload() => setState(() {
        _detail = widget.api.run(widget.runId);
      });

  Future<void> _judge() async {
    setState(() => _judging = true);
    try {
      await widget.api.judgeRun(widget.runId);
      _reload();
    } on SiteError catch (e) {
      _snack('判读没成：$e');
    } finally {
      if (mounted) setState(() => _judging = false);
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
      appBar: AppBar(title: const Text('这一趟'), actions: [
        if (canReview)
          IconButton(
              key: SiteRunPage.judgeKey,
              tooltip: '让站点重判',
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
            return Center(child: Text('拿不到：${snap.error}'));
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final run = (snap.data!['run'] as Map?)?.cast<String, dynamic>() ?? {};
          final photos = (snap.data!['photos'] as List? ?? const [])
              .whereType<Map<String, dynamic>>()
              .toList();
          return ListView(children: [
            ListTile(
              title: Text('${run['mission']} · ${stampText('${run['stamp']}')}'),
              subtitle: Text('${run['robot_id']} · 照片 ${run['photos']} · '
                  '${run['finished'] == true ? '跑完了（${run['result']}）' : '没跑完'}'),
            ),
            for (final p in photos) _photoTile(p),
          ]);
        },
      ),
    );
  }

  Widget _photoTile(Map<String, dynamic> p) {
    final f = (p['finding'] as Map?)?.cast<String, dynamic>();
    final r = (p['review'] as Map?)?.cast<String, dynamic>();
    final shown = r?['verdict'] ?? f?['verdict'];
    return ListTile(
      key: SiteRunPage.photoKey('${p['name']}'),
      leading: SizedBox(
        width: 64,
        height: 48,
        child: FutureBuilder<Uint8List>(
          future: _photo('${p['name']}'),
          builder: (c, s) => s.hasData
              ? Image.memory(s.data!, fit: BoxFit.cover, gaplessPlayback: true)
              : const Icon(Icons.photo),
        ),
      ),
      title: Text('${p['waypoint']} · ${p['camera']} · ${verdictText(shown as String?)}'
          '${r != null ? '（人已复核）' : ''}'),
      subtitle: Text(r != null
          ? '复核：${verdictText(r['verdict'] as String?)} ${r['note'] ?? ''}'
          : f != null
              ? '判读：${f['reason'] ?? ''}'
              : '还没判读'),
      trailing: shown == 'abnormal' ? const Icon(Icons.report, color: Colors.red) : null,
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
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('复核没记下：$e')));
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
    return Scaffold(
      appBar: AppBar(title: Text('${widget.photo['waypoint']} · ${widget.photo['camera']}')),
      body: ListView(padding: const EdgeInsets.all(12), children: [
        FutureBuilder<Uint8List>(
          future: widget.bytes,
          builder: (c, s) => s.hasError
              ? Text('照片拿不到：${s.error}', style: const TextStyle(color: Colors.red))
              : s.hasData
                  ? InteractiveViewer(child: Image.memory(s.data!))
                  : const SizedBox(height: 200, child: Center(child: CircularProgressIndicator())),
        ),
        const SizedBox(height: 8),
        Text(f == null
            ? '模型：还没判读'
            : '模型：${verdictText(f['verdict'] as String?)}（把握 '
                '${((f['confidence'] as num? ?? 0) * 100).round()}%）${f['reason'] ?? ''}'
                '${(f['evidence'] ?? '') != '' ? '；依据：${f['evidence']}' : ''}'),
        const SizedBox(height: 4),
        Text(r == null
            ? '人：还没复核'
            : '人：${verdictText(r['verdict'] as String?)} ${r['note'] ?? ''}'),
        if (canReview) ...[
          const SizedBox(height: 12),
          TextField(
              key: SitePhotoPage.noteKey,
              controller: _note,
              decoration: const InputDecoration(labelText: '说一句（可不填）'),
              maxLength: 200),
          Wrap(spacing: 8, children: [
            for (final v in ['normal', 'abnormal', 'unclear'])
              FilledButton.tonal(
                  key: SitePhotoPage.reviewKey(v),
                  onPressed: _busy ? null : () => _review(v),
                  child: Text(verdictText(v))),
          ]),
        ],
      ]),
    );
  }
}
