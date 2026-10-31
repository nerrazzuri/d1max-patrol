/// 固定摄像头（W19，决策 33、34）：现场固定的摄像头、NVR 自带的入侵检测，站点经 ONVIF 收进来当入侵派狗。
///
/// 这一页只看：每台的防区、连没连上（连不上的那一路入侵收不到）、最近一次报的什么；有画面地址的点进去看
/// 实时画面（站点拉 RTSP 转出来，走 app 钉证书的那条连接）。加、删摄像头在站点主机上用命令行（口令不经手机）。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_recordings.dart' show clipTime;
import 'widget/live_video.dart';

class SiteCamerasPage extends StatefulWidget {
  final SiteApi api;
  const SiteCamerasPage({super.key, required this.api});

  static Key camKey(String name) => Key('cam-$name');
  static const Key msgKey = Key('cam-msg');

  @override
  State<SiteCamerasPage> createState() => _SiteCamerasPageState();
}

class _SiteCamerasPageState extends State<SiteCamerasPage> {
  List<Map<String, dynamic>> _rows = const [];
  String _msg = '';
  bool _loading = true;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
    _poll = Timer.periodic(const Duration(seconds: 15), (_) => unawaited(_load()));
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final rows = await widget.api.cameras();
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
              ? tr('Cameras are not enabled on this site', '此站点未启用摄像头')
              : tr("Can't read cameras: $e", '无法读取摄像头：$e');
        });
      }
    }
  }

  String _state(Map<String, dynamic> c) {
    final since = (c['since_ms'] as num?)?.toInt();
    final when = since == null || since == 0 ? '' : tr(
        ' (since ${clipTime(since)})',
        '（${clipTime(since)} 起）');
    if (c['connected'] == true) return tr('Connected$when', '已连接$when');
    final err = '${c['error'] ?? ''}';
    return tr(
        'Not connected$when${err.isEmpty ? '' : ': $err'} — intrusions from this camera are not received',
        '未连接$when${err.isEmpty ? '' : '：$err'}——收不到此摄像头的入侵告警');
  }

  @override
  Widget build(BuildContext context) {
    final nw = Nw.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(tr('Fixed cameras', '固定摄像头'))),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : NwListView(children: [
              if (_msg.isNotEmpty) ...[
                // 读不到、没开：提示条里的那句话（Key 在 Text 上）
                Container(
                    padding: const EdgeInsets.all(Nw.s16),
                    decoration: BoxDecoration(color: nw.warningSoft, borderRadius: BorderRadius.circular(Nw.r8)),
                    child: Text(_msg,
                        key: SiteCamerasPage.msgKey, style: NwText.bodyMedium.copyWith(color: nw.text))),
                nwGap16,
              ],
              if (_msg.isEmpty && _rows.isEmpty)
                NwCard(
                    child: Text(
                        tr(
                            'No cameras yet. Register one on the site host with d1max-site camera-add.',
                            '暂无摄像头。请在站点主机上用 d1max-site camera-add 登记。'),
                        style: NwText.bodyMedium.copyWith(color: nw.textSecondary))),
              if (_rows.isNotEmpty)
                NwListCard(children: [
                  for (final c in _rows)
                    ListTile(
                      key: SiteCamerasPage.camKey('${c['name']}'),
                      // 连着是绿的；连不上是红的、换一个图标（这一路的入侵收不到）
                      leading: Icon(
                          c['connected'] == true ? Icons.videocam_rounded : Icons.videocam_off_rounded,
                          color: c['connected'] == true ? nw.success : nw.error),
                      title: Text(
                          tr('${c['name']} · Zone ${c['zone']}', '${c['name']} · 防区 ${c['zone']}'),
                          style: NwText.titleSmall.copyWith(color: nw.text)),
                      subtitle: Text(
                          [
                            _state(c),
                            if (c['last_event_ms'] != null)
                              tr(
                                  'Last intrusion reported: ${clipTime((c['last_event_ms'] as num).toInt())}',
                                  '最近一次报入侵：${clipTime((c['last_event_ms'] as num).toInt())}'),
                            if (c['motion'] == true)
                              tr('Video motion also counts as an intrusion', '画面移动也视为入侵'),
                          ].join('\n'),
                          style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
                      isThreeLine: c['last_event_ms'] != null || c['motion'] == true,
                      trailing:
                          c['live'] == true ? Icon(Icons.chevron_right_rounded, color: nw.textSecondary) : null,
                      onTap: c['live'] == true
                          ? () => Navigator.push(
                              context,
                              MaterialPageRoute<void>(
                                  builder: (_) => SiteCameraLivePage(api: widget.api, name: '${c['name']}')))
                          : null,
                    ),
                ]),
            ]),
    );
  }
}

class SiteCameraLivePage extends StatelessWidget {
  final SiteApi api;
  final String name;
  const SiteCameraLivePage({super.key, required this.api, required this.name});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(tr('$name · Live', '$name · 实时画面'))),
      body: Center(
        child: ConstrainedBox(
          constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * 0.8),
          child: AspectRatio(
            aspectRatio: 16 / 9,
            child: LiveVideo(
              baseUrl: api.baseUrl,
              camera: name,
              streamPath: '/api/cameras/${Uri.encodeComponent(name)}/live',
              token: api.session?.token ?? '',
              openClient: api.pinnedClient,
              // 站点要先拉 RTSP、等关键帧
              firstFrameGrace: const Duration(seconds: 15),
              health: Stream<VideoHealth>.value(VideoHealth(<String, bool>{name: true})),
            ),
          ),
        ),
      ),
    );
  }
}
