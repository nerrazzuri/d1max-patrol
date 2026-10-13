/// 事件账（W00c5a）：站点派过狗的事件一行一条；顶栏去摄像头、拦截点。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'site_cameras.dart';
import 'site_intercepts.dart';
import 'widget/bar_actions.dart';

class SiteIncidentsPage extends StatelessWidget {
  final SiteApi api;
  const SiteIncidentsPage({super.key, required this.api});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(tr('Incidents', '事件')), actions: barActions(context, [
        // 固定摄像头（W19）：自带的入侵检测报到站点
        BarAction(
            key: const Key('btn-cameras'),
            onPressed: () => Navigator.push(
                context, MaterialPageRoute<void>(builder: (_) => SiteCamerasPage(api: api))),
            label: tr('Cameras…', '摄像头…')),
        // 拦截点与防区（W16）：谁都能看，管理员能改
        BarAction(
            key: const Key('btn-intercepts'),
            onPressed: () => Navigator.push(
                context, MaterialPageRoute<void>(builder: (_) => SiteInterceptsPage(api: api))),
            label: tr('Intercept points…', '拦截点…')),
      ])),
      body: FutureBuilder<List<Map<String, dynamic>>>(
        future: api.incidents(),
        builder: (c, snap) {
          if (snap.hasError) return Center(child: Text('${snap.error}'));
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final rows = snap.data!;
          if (rows.isEmpty) return Center(child: Text(tr('No incidents', '没有事件')));
          return ListView(children: [
            for (final r in rows)
              ListTile(
                title: Text('${r['zone']} · ${incidentOutcomeText('${r['outcome']}')}'),
                subtitle: Text('${r['source']} ${r['event_id']} → '
                    '${r['robot_id'] ?? '-'} ${r['result'] ?? ''}'),
              ),
          ]);
        },
      ),
    );
  }
}
