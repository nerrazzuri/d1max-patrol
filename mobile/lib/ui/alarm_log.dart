/// 报警记录（W53：「记录」栏的一段）：最近的 P1、P2，谁确认的、解决了没有。点开是报警处理页。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../model/alert.dart';
import '../net/site_client.dart';
import 'nw/nw_p2_duty.dart';
import 'nw/nw_p4_admin.dart' show NwListView;
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'patrol_dispatch.dart' show hhmm;
import 'site_alert_scene.dart';

class AlarmLogPage extends StatefulWidget {
  final SiteApi api;
  const AlarmLogPage({super.key, required this.api});

  static Key rowKey(String key) => Key('alarm-log-$key');

  @override
  State<AlarmLogPage> createState() => _AlarmLogPageState();
}

class _AlarmLogPageState extends State<AlarmLogPage> {
  List<Alert>? _rows;
  String? _error;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final all = await widget.api.alerts(all: true);
      if (mounted) setState(() => _rows = all.where((a) => a.level != 'P3').toList());
    } on SiteError catch (e) {
      if (mounted) setState(() => _error = refusalText(e));
    } on FormatException catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  String _day(int ms) {
    final t = DateTime.fromMillisecondsSinceEpoch(ms);
    return '${t.month}/${t.day} ${hhmm(ms)}';
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final rows = _rows;
    return RefreshIndicator(
      onRefresh: _load,
      child: NwListView(children: [
        if (_error != null) NwNotice(text: _error!, tone: NwTone.error),
        if (rows == null && _error == null) const Center(child: CircularProgressIndicator()),
        if (rows != null && rows.isEmpty)
          Text(tr('No alarms.', '没有报警。'), style: NwText.bodyMedium.copyWith(color: c.textSecondary)),
        for (final a in rows ?? const <Alert>[])
          Padding(
            padding: const EdgeInsets.only(bottom: Nw.s8),
            child: NwCard(
              key: AlarmLogPage.rowKey(a.key),
              onTap: () => Navigator.push(context,
                  MaterialPageRoute<void>(builder: (_) => SiteAlertScenePage(api: widget.api, alert: a))),
              child: Row(children: [
                NwLevelBox(level: a.level),
                const SizedBox(width: Nw.s8),
                Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('${a.robot} · ${alertTitle(a.kind, a.title)}',
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: NwText.bodyMedium.copyWith(color: c.text, fontWeight: FontWeight.w600)),
                  Text(
                      a.resolvedMs != null
                          ? tr('Resolved${a.ackedBy.isEmpty ? '' : ' · handled by ${a.ackedBy}'}',
                              '已解决${a.ackedBy.isEmpty ? '' : ' · ${a.ackedBy} 处理'}')
                          : a.ackedBy.isNotEmpty
                              ? tr('${a.ackedBy} is handling it', '${a.ackedBy} 在处理')
                              : tr('Not handled yet', '未处理'),
                      style: NwText.bodySmall.copyWith(color: c.textSecondary)),
                ])),
                Text(_day(a.firstMs), style: NwText.labelSmall.copyWith(color: c.textSecondary)),
              ]),
            ),
          ),
      ]),
    );
  }
}
