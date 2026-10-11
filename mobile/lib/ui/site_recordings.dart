/// 连续录像（W18，决策 31、32）：一只狗一分钟一段，按时间往前翻；点一段下载下来放。
///
/// 录像在站点上留 30 天；值班的人、管理员能把一段标成「留着」（过了 30 天也不删）。下载走 app 自己那条
/// 钉证书的连接（系统播放器不认站点的自签证书），下到 app 的临时目录再放：手机、Mac 在 app 里放，Linux、
/// Windows 桌面交给系统播放器。时刻是**狗的钟**（狗的钟不准站点会报告警）。
library;

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_player/video_player.dart';

import '../l10n.dart';
import '../net/site_client.dart';

/// 放一段录像。测试换成记下来的。
abstract class ClipPlayer {
  Future<void> play(BuildContext context, Uint8List bytes, String title);
}

class DefaultClipPlayer implements ClipPlayer {
  const DefaultClipPlayer();

  @override
  Future<void> play(BuildContext context, Uint8List bytes, String title) async {
    final dir = await getTemporaryDirectory();
    final f = File('${dir.path}/d1max-clip.mp4');
    await f.writeAsBytes(bytes, flush: true);
    if (Platform.isLinux || Platform.isWindows) {
      // video_player 没有 Linux、Windows 的实现：交给系统播放器
      await Process.start(Platform.isLinux ? 'xdg-open' : 'cmd', Platform.isLinux ? [f.path] : ['/c', 'start', '', f.path],
          mode: ProcessStartMode.detached);
      return;
    }
    if (!context.mounted) return;
    await Navigator.push(context, MaterialPageRoute<void>(builder: (_) => _ClipPage(file: f, title: title)));
  }
}

class _ClipPage extends StatefulWidget {
  final File file;
  final String title;
  const _ClipPage({required this.file, required this.title});

  @override
  State<_ClipPage> createState() => _ClipPageState();
}

class _ClipPageState extends State<_ClipPage> {
  late final VideoPlayerController _c = VideoPlayerController.file(widget.file);
  String? _error;

  @override
  void initState() {
    super.initState();
    _c.initialize().then((_) {
      if (mounted) {
        setState(() {});
        _c.play();
      }
    }, onError: (Object e) {
      if (mounted) setState(() => _error = tr("Can't play: $e", '放不了：$e'));
    });
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text(widget.title)),
        body: Center(
          child: _error != null
              ? Text(_error!)
              : !_c.value.isInitialized
                  ? const CircularProgressIndicator()
                  : GestureDetector(
                      onTap: () => setState(() => _c.value.isPlaying ? _c.pause() : _c.play()),
                      child: AspectRatio(aspectRatio: _c.value.aspectRatio, child: VideoPlayer(_c))),
        ),
      );
}

/// 时刻（毫秒）→ 本机时间 `MM-dd HH:mm:ss`。
String clipTime(int ms) {
  final t = DateTime.fromMillisecondsSinceEpoch(ms);
  String two(int v) => v.toString().padLeft(2, '0');
  return '${two(t.month)}-${two(t.day)} ${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
}

class SiteRecordingsPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  /// 从告警现场进来（W17 的现场页）：看这一刻前后各几分钟。不给就看最近的。
  final int? aroundMs;
  final ClipPlayer player;
  const SiteRecordingsPage(
      {super.key,
      required this.api,
      required this.robotId,
      this.aroundMs,
      this.player = const DefaultClipPlayer()});

  static const Key olderKey = Key('rec-older');
  static const Key newerKey = Key('rec-newer');
  static const Key msgKey = Key('rec-msg');
  static Key clipKey(int id) => Key('rec-$id');
  static Key keepKey(int id) => Key('rec-keep-$id');
  static Key cameraKey(String c) => Key('rec-cam-$c');

  /// 一屏看多长（分钟）。
  static const int windowMin = 30;

  @override
  State<SiteRecordingsPage> createState() => _SiteRecordingsPageState();
}

class _SiteRecordingsPageState extends State<SiteRecordingsPage> {
  List<Map<String, dynamic>> _rows = const [];
  String? _camera;
  String _msg = '';
  bool _loading = true;
  int? _busyId;
  /// 这一屏的窗口：[_until - windowMin, _until]；null = 最新的。
  int? _until;

  @override
  void initState() {
    super.initState();
    final a = widget.aroundMs;
    if (a != null) _until = a + SiteRecordingsPage.windowMin * 60000 ~/ 2;
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    try {
      final until = _until;
      final rows = await widget.api.recordings(
          robotId: widget.robotId,
          camera: _camera,
          until: until,
          since: until == null ? null : until - SiteRecordingsPage.windowMin * 60000,
          limit: until == null ? 60 : null);
      if (!mounted) return;
      setState(() {
        _rows = rows;
        _loading = false;
        _msg = '';
      });
    } on SiteError catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          _msg = e.status == 404
              ? tr('Recording is not enabled on this site', '这个站点没开录像')
              : tr("Can't load recordings: $e", '录像读不到：$e');
        });
      }
    }
  }

  void _shift(int minutes) {
    final base = _until ??
        (_rows.isNotEmpty ? (_rows.first['start_ms'] as num).toInt() + 60000 : DateTime.now().millisecondsSinceEpoch);
    _until = base + minutes * 60000;
    unawaited(_load());
  }

  Future<void> _play(Map<String, dynamic> r) async {
    final id = (r['id'] as num).toInt();
    setState(() {
      _busyId = id;
      _msg = tr('Downloading…', '下载中…');
    });
    try {
      final bytes = await widget.api.recordingBytes(id);
      if (!mounted) return;
      setState(() => _msg = '');
      await widget.player.play(context, bytes, '${r['camera']} · ${clipTime((r['start_ms'] as num).toInt())}');
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr("Can't download: $e", '下载不了：$e'));
    } on Exception catch (e) {
      if (mounted) setState(() => _msg = tr("Can't play: $e", '放不了：$e'));
    } finally {
      if (mounted) setState(() => _busyId = null);
    }
  }

  Future<void> _keep(Map<String, dynamic> r) async {
    final id = (r['id'] as num).toInt();
    final want = r['keep'] != 1;
    try {
      await widget.api.setRecordingKeep(id, want);
      await _load();
      if (mounted) {
        setState(() => _msg = want
            ? tr('Marked to keep: not deleted after 30 days', '标了留着：过了 30 天也不删')
            : tr('No longer kept: deleted after 30 days as usual', '不留了：30 天后照常删'));
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr("Couldn't change the keep mark: $e", '没标上：$e'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final canKeep = widget.api.session?.canReview ?? false; // 站点按 review 查（W20：业主能确认告警，但不能标留着）
    final around = widget.aroundMs;
    return Scaffold(
      appBar: AppBar(title: Text(tr('${widget.robotId} · Recordings', '${widget.robotId} · 录像')), actions: [
        TextButton(key: SiteRecordingsPage.olderKey, onPressed: () => _shift(-SiteRecordingsPage.windowMin),
            child: Text(tr('← Earlier', '← 更早'))),
        TextButton(key: SiteRecordingsPage.newerKey, onPressed: () => _shift(SiteRecordingsPage.windowMin),
            child: Text(tr('Later →', '更晚 →'))),
      ]),
      body: ListView(padding: const EdgeInsets.all(8), children: [
        Wrap(spacing: 8, children: [
          for (final c in const <String?>[null, 'front', 'back'])
            ChoiceChip(
                key: SiteRecordingsPage.cameraKey(c ?? 'all'),
                label: Text(c == null
                    ? tr('All', '全部')
                    : c == 'front'
                        ? tr('Front', '前')
                        : tr('Back', '后')),
                selected: _camera == c,
                onSelected: (_) {
                  _camera = c;
                  unawaited(_load());
                }),
        ]),
        if (around != null) Text(tr('Alarm time: ${clipTime(around)} (times are by the robot clock)',
              '告警那一刻：${clipTime(around)}（时刻是狗的钟）')),
        if (_msg.isNotEmpty) Text(_msg, key: SiteRecordingsPage.msgKey),
        if (_loading) const Padding(padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator())),
        if (!_loading && _rows.isEmpty && _msg.isEmpty)
          Text(tr(
              'No recordings in this period (recording is off on the robot, not uploaded yet, or deleted after 30 days)',
              '这段时间没有录像（狗没开录像、还没传上来，或者过了 30 天删了）')),
        for (final r in _rows)
          ListTile(
            key: SiteRecordingsPage.clipKey((r['id'] as num).toInt()),
            dense: true,
            leading: _busyId == (r['id'] as num).toInt()
                ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.play_circle_outline),
            title: Text('${clipTime((r['start_ms'] as num).toInt())} · ${r['camera']}'),
            subtitle: Text('${((r['bytes'] as num) / 1048576).toStringAsFixed(1)} MB'
                '${r['keep'] == 1 ? tr(' · kept', ' · 留着') : ''}'),
            selected: around != null &&
                (r['start_ms'] as num) <= around &&
                around < (r['start_ms'] as num) + 60000,
            onTap: _busyId == null ? () => _play(r) : null,
            trailing: canKeep
                ? IconButton(
                    key: SiteRecordingsPage.keepKey((r['id'] as num).toInt()),
                    tooltip: r['keep'] == 1
                        ? tr('Stop keeping', '不留了')
                        : tr('Keep (not deleted after 30 days)', '留着（过了 30 天也不删）'),
                    icon: Icon(r['keep'] == 1 ? Icons.bookmark : Icons.bookmark_border),
                    onPressed: () => _keep(r))
                : null,
          ),
      ]),
    );
  }
}
