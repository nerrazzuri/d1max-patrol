/// 排程（W00c5a）：站点上的任务包和每一条排程下一轮、上一次的结果。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';

class SiteSchedulePage extends StatelessWidget {
  final SiteApi api;
  const SiteSchedulePage({super.key, required this.api});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(tr('Schedule', '排程'))),
      body: FutureBuilder<Map<String, dynamic>>(
        future: api.schedule(),
        builder: (c, snap) {
          if (snap.hasError) return Center(child: Text('${snap.error}'));
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final d = snap.data!;
          final entries = (d['entries'] as List? ?? const []).whereType<Map>().toList();
          if (d['bundle'] == null) {
            return Center(child: Text(tr('No task bundle on the site yet', '站点上还没有任务包')));
          }
          return ListView(children: [
            ListTile(
                title: Text(tr(
                    'Task bundle ${(d['bundle'] as Map)['bundle_id']} v${(d['bundle'] as Map)['version']}',
                    '任务包 ${(d['bundle'] as Map)['bundle_id']} v${(d['bundle'] as Map)['version']}')),
                subtitle: Text(tr('Time zone ${d['timezone']}', '时区 ${d['timezone']}'))),
            for (final e in entries)
              ListTile(
                title: Text('${e['id']} · ${scheduleWhen(e.cast<String, dynamic>())} · ${e['mission']}'),
                subtitle: Text(tr('Next ${e['next_run']}', '下一轮 ${e['next_run']}') +
                    (e['last'] is Map
                        ? tr(
                            ' · last ${(e['last'] as Map)['outcome']} ${(e['last'] as Map)['result'] ?? ''}',
                            ' · 上一次 ${(e['last'] as Map)['outcome']} ${(e['last'] as Map)['result'] ?? ''}')
                        : '')),
              ),
          ]);
        },
      ),
    );
  }
}
