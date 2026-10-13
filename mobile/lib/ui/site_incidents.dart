/// 事件账（W00c5a）：站点派过狗的事件一行一条；顶栏去摄像头、拦截点。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_cameras.dart';
import 'site_intercepts.dart';
import 'widget/bar_actions.dart';

class SiteIncidentsPage extends StatelessWidget {
  final SiteApi api;

  /// 嵌在底部导航的记录栏里：不带自己的顶栏；去摄像头、拦截点的两个入口摆在内容最上面。
  final bool embedded;
  const SiteIncidentsPage({super.key, required this.api, this.embedded = false});

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    void cameras() =>
        Navigator.push(context, MaterialPageRoute<void>(builder: (_) => SiteCamerasPage(api: api)));
    void intercepts() =>
        Navigator.push(context, MaterialPageRoute<void>(builder: (_) => SiteInterceptsPage(api: api)));
    final body = FutureBuilder<List<Map<String, dynamic>>>(
      future: api.incidents(),
      builder: (_, snap) {
        // 嵌着的时候两个入口一直在（读不到、还没读到也能去）
        final entries = <Widget>[
          if (embedded) ...[
            Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
              NwButton(
                  buttonKey: const Key('btn-cameras'),
                  icon: Icons.videocam_rounded,
                  label: tr('Cameras…', '摄像头…'),
                  variant: NwButtonVariant.outline,
                  size: NwButtonSize.small,
                  onPressed: cameras),
              NwButton(
                  buttonKey: const Key('btn-intercepts'),
                  icon: Icons.fence_rounded,
                  label: tr('Intercept points…', '拦截点…'),
                  variant: NwButtonVariant.outline,
                  size: NwButtonSize.small,
                  onPressed: intercepts),
            ]),
            nwGap16,
          ],
        ];
        if (snap.hasError) {
          return NwListView(children: [...entries, NwNotice(tone: NwTone.error, text: '${snap.error}')]);
        }
        if (!snap.hasData) {
          return embedded
              ? NwListView(children: [...entries, const Center(child: CircularProgressIndicator())])
              : const Center(child: CircularProgressIndicator());
        }
        final rows = snap.data!;
        if (rows.isEmpty) {
          return NwListView(children: [
            ...entries,
            NwCard(
                child: Text(tr('No incidents', '没有事件'),
                    style: NwText.bodyMedium.copyWith(color: c.textSecondary))),
          ]);
        }
        return NwListView(children: [
          ...entries,
          NwListCard(children: [
            for (final r in rows)
              ListTile(
                leading: Icon(Icons.sensors_rounded, color: c.textSecondary),
                title: Text('${r['zone']} · ${incidentOutcomeText('${r['outcome']}')}',
                    style: NwText.titleSmall.copyWith(color: c.text)),
                subtitle: Text(
                    '${r['source']} ${r['event_id']} → '
                    '${r['robot_id'] ?? '-'} ${r['result'] ?? ''}',
                    style: NwText.bodySmall.copyWith(color: c.textSecondary)),
              ),
          ]),
        ]);
      },
    );
    if (embedded) return body;
    return Scaffold(
      appBar: AppBar(title: Text(tr('Incidents', '事件')), actions: barActions(context, [
        // 固定摄像头（W19）：自带的入侵检测报到站点
        BarAction(key: const Key('btn-cameras'), onPressed: cameras, label: tr('Cameras…', '摄像头…')),
        // 拦截点与防区（W16）：谁都能看，管理员能改
        BarAction(
            key: const Key('btn-intercepts'), onPressed: intercepts, label: tr('Intercept points…', '拦截点…')),
      ])),
      body: body,
    );
  }
}
