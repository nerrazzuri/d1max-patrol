/// 建图进程日志（W00c6g）：狗上录包、重建子进程的日志。以前录包、重建失败时站点只收到一句「退出码 N，
/// 日志见 ……」，要看原因只能 SSH 上狗。
///
/// - 管理员才有入口（单狗页「建图日志」，狗报了 `proc_log` 才显示）：日志里有路径、参数。
/// - 日志经站点现取、站点不存；只给尾巴（默认 64 KB，狗那头最多 128 KB）。点刷新再取一次，不实时跟。
library;

import 'package:flutter/material.dart';

import '../net/site_client.dart';

/// 字节数说人话。
String logSizeText(int n) {
  if (n < 1024) return '$n B';
  if (n < 1024 * 1024) return '${(n / 1024).round()} KB';
  return '${(n / 1024 / 1024).toStringAsFixed(1)} MB';
}

String _when(Object? ms) {
  if (ms is! int) return '';
  final t = DateTime.fromMillisecondsSinceEpoch(ms);
  String two(int v) => v.toString().padLeft(2, '0');
  return '${t.month}-${two(t.day)} ${two(t.hour)}:${two(t.minute)}';
}

class SiteProcLogsPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  const SiteProcLogsPage({super.key, required this.api, required this.robotId});

  static const Key refreshKey = Key('logs-refresh');

  @override
  State<SiteProcLogsPage> createState() => _SiteProcLogsPageState();
}

class _SiteProcLogsPageState extends State<SiteProcLogsPage> {
  late Future<Map<String, dynamic>> _data = widget.api.procLogs(widget.robotId);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('${widget.robotId} 的建图日志'), actions: [
        IconButton(
            key: SiteProcLogsPage.refreshKey,
            tooltip: '刷新',
            icon: const Icon(Icons.refresh),
            onPressed: () => setState(() {
                  _data = widget.api.procLogs(widget.robotId);
                })),
      ]),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _data,
        builder: (context, snap) {
          if (snap.hasError) {
            return Padding(
                padding: const EdgeInsets.all(16), child: Text('取不到日志列表：${snap.error}'));
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final logs = (snap.data!['logs'] as List? ?? const []).cast<Map<String, dynamic>>();
          if (logs.isEmpty) {
            return const Padding(
                padding: EdgeInsets.all(16), child: Text('还没有日志：这只狗还没录过包、没重建过图'));
          }
          return ListView(children: [
            for (final l in logs)
              ListTile(
                title: Text('${l['name']}'),
                subtitle: Text('${logSizeText((l['size'] as num? ?? 0).toInt())}'
                    '  ·  ${_when(l['mtime_ms'])}'),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => Navigator.push(
                    context,
                    MaterialPageRoute<void>(
                        builder: (_) => SiteProcLogPage(
                            api: widget.api, robotId: widget.robotId, name: '${l['name']}'))),
              ),
          ]);
        },
      ),
    );
  }
}

class SiteProcLogPage extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  final String name;
  const SiteProcLogPage(
      {super.key, required this.api, required this.robotId, required this.name});

  static const Key refreshKey = Key('log-refresh');

  @override
  State<SiteProcLogPage> createState() => _SiteProcLogPageState();
}

class _SiteProcLogPageState extends State<SiteProcLogPage> {
  late Future<Map<String, dynamic>> _data = _fetch();

  Future<Map<String, dynamic>> _fetch() => widget.api.procLog(widget.robotId, widget.name);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.name), actions: [
        IconButton(
            key: SiteProcLogPage.refreshKey,
            tooltip: '刷新',
            icon: const Icon(Icons.refresh),
            onPressed: () => setState(() {
                  _data = _fetch();
                })),
      ]),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _data,
        builder: (context, snap) {
          if (snap.hasError) {
            return Padding(padding: const EdgeInsets.all(16), child: Text('取不到：${snap.error}'));
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final d = snap.data!;
          final size = (d['size'] as num? ?? 0).toInt();
          final got = (d['bytes'] as num? ?? 0).toInt();
          return ListView(padding: const EdgeInsets.all(12), children: [
            Text(d['truncated'] == true
                ? '共 ${logSizeText(size)}，只显示最后 ${logSizeText(got)}（最新的在最底下）'
                : '共 ${logSizeText(size)}'),
            const Divider(),
            SelectableText('${d['text'] ?? ''}',
                style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
          ]);
        },
      ),
    );
  }
}
