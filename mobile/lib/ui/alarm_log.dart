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

  /// 测试模式条幅多久重新问一次站点（外审 r1 #10：别人开了、结束了，这边也跟着变）。
  static const Duration testPoll = Duration(seconds: 15);

  @override
  State<AlarmLogPage> createState() => _AlarmLogPageState();
}

class _AlarmLogPageState extends State<AlarmLogPage> {
  List<Alert>? _rows;
  String? _error;

  /// POC 告警测试模式开着：到几点、谁开的。没开 / 老站点 = null。
  Map<String, dynamic>? _test;

  Timer? _poll;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
    _poll = Timer.periodic(AlarmLogPage.testPoll, (_) => unawaited(_loadTest()));
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _loadTest() async {
    Map<String, dynamic>? test;
    try {
      final m = await widget.api.alertTest();
      test = m['active'] == true ? m : null;
    } on SiteError {
      test = null; // 老站点没有这个接口
    } on FormatException {
      test = null;
    }
    if (mounted) setState(() => _test = test);
  }

  /// 到点就当结束了（不等下一次问）。
  bool get _testOn {
    final u = _test?['until_ms'];
    return _test != null && (u is! num || DateTime.now().millisecondsSinceEpoch < u);
  }

  Future<void> _load() async {
    try {
      final all = await widget.api.alerts(all: true);
      await _loadTest();
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
        if (_testOn)
          NwNotice(
              key: const Key('alarm-test-mode'),
              tone: NwTone.warning,
              text: tr(
                  'Alert test mode until ${_test!['until_ms'] is num ? hhmm((_test!['until_ms'] as num).toInt()) : '?'} (started by ${_test!['by']}): alerts raised now carry a test prefix in the title.',
                  '告警测试模式中，到 ${_test!['until_ms'] is num ? hhmm((_test!['until_ms'] as num).toInt()) : '?'} 结束（${_test!['by']} 开的）：这期间的告警标题带「测试：」。')),
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
