/// 排程（W00c5a）：站点上的任务包和每一条排程下一轮、上一次的结果。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

class SiteSchedulePage extends StatelessWidget {
  final SiteApi api;

  /// 嵌在底部导航的记录栏里：不带自己的顶栏。
  final bool embedded;
  const SiteSchedulePage({super.key, required this.api, this.embedded = false});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final title = NwText.titleSmall.copyWith(color: c.text);
    final sub = NwText.bodySmall.copyWith(color: c.textSecondary);
    final body = FutureBuilder<Map<String, dynamic>>(
      future: api.schedule(),
      builder: (_, snap) {
        if (snap.hasError) {
          return NwListView(children: [NwNotice(tone: NwTone.error, text: '${snap.error}')]);
        }
        if (!snap.hasData) return const Center(child: CircularProgressIndicator());
        final d = snap.data!;
        final entries = (d['entries'] as List? ?? const []).whereType<Map>().toList();
        if (d['bundle'] == null) {
          return NwEmpty(
              icon: Icons.event_busy_rounded, text: tr('No task bundle on the site yet', '站点上还没有任务包'));
        }
        return NwListView(children: [
          NwCard(
              child: Row(children: [
            Icon(Icons.inventory_2_rounded, size: 24, color: c.textSecondary),
            const SizedBox(width: Nw.s16),
            Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
              Text(
                  tr(
                      'Task bundle ${(d['bundle'] as Map)['bundle_id']} v${(d['bundle'] as Map)['version']}',
                      '任务包 ${(d['bundle'] as Map)['bundle_id']} v${(d['bundle'] as Map)['version']}'),
                  style: NwText.titleMedium.copyWith(color: c.text)),
              nwGap4,
              Text(tr('Time zone ${d['timezone']}', '时区 ${d['timezone']}'), style: sub),
            ])),
          ])),
          nwGap24,
          NwSectionLabel(tr('Scheduled rounds', '排好的巡检')),
          nwGap8,
          NwListCard(children: [
            if (entries.isEmpty) NwCardText(tr('Nothing scheduled', '没有排程')),
            for (final e in entries)
              ListTile(
                leading: Icon(Icons.schedule_rounded, color: c.textSecondary),
                title: Text('${e['id']} · ${scheduleWhen(e.cast<String, dynamic>())} · ${e['mission']}',
                    style: title),
                subtitle: Text(
                    tr('Next ${e['next_run']}', '下一轮 ${e['next_run']}') +
                        (e['last'] is Map
                            ? tr(
                                ' · last ${(e['last'] as Map)['outcome']} ${(e['last'] as Map)['result'] ?? ''}',
                                ' · 上一次 ${(e['last'] as Map)['outcome']} ${(e['last'] as Map)['result'] ?? ''}')
                            : ''),
                    style: sub),
              ),
          ]),
        ]);
      },
    );
    if (embedded) return body;
    return Scaffold(appBar: AppBar(title: Text(tr('Schedule', '排程'))), body: body);
  }
}
