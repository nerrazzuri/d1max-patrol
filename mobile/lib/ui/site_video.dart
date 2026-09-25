/// 站点模式的画面（W00c5b）：画面经站点来，手机不直连狗。
///
/// 前、后两路切换；在线判定走站点的 `video/health` 轮询（跟直连那一版同一个规矩：不看图片加载状态）；
/// 取流用**钉住站点证书**的客户端。站点那头第一个观众来了才让狗开始推，最后一个走了才停 ——
/// 所以这一格只在页面上时连着（`LiveVideo` 被盖住时自己断开，见它的 `_onstage`）。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../net/site_client.dart';
import '../net/wire.dart';
import 'widget/live_video.dart';

/// 按固定间隔问站点这台狗的画面健康，变成 `LiveVideo` 要的那条流。读不到当「都没有」。
class SiteVideoHealthPoller {
  SiteVideoHealthPoller({required SiteApi api, required this.robotId,
      this.period = const Duration(seconds: 2)})
      // ignore: prefer_initializing_formals
      : _api = api {
    _timer = Timer.periodic(period, (_) => unawaited(_tick()));
    unawaited(_tick());
  }

  final SiteApi _api;
  final String robotId;
  final Duration period;
  final StreamController<VideoHealth> _ctl = StreamController<VideoHealth>.broadcast();
  Timer? _timer;
  bool _closed = false;

  Stream<VideoHealth> get stream => _ctl.stream;

  Future<void> _tick() async {
    VideoHealth h;
    try {
      h = VideoHealth.fromJson(await _api.videoHealth(robotId));
    } on SiteError {
      h = const VideoHealth(<String, bool>{});
    } on FormatException {
      h = const VideoHealth(<String, bool>{});
    }
    if (!_closed) _ctl.add(h);
  }

  void close() {
    _closed = true;
    _timer?.cancel();
    unawaited(_ctl.close());
  }
}

class SiteVideo extends StatefulWidget {
  final SiteApi api;
  final String robotId;
  const SiteVideo({super.key, required this.api, required this.robotId});

  static Key cameraKey(String cam) => Key('site-video-$cam');

  @override
  State<SiteVideo> createState() => _SiteVideoState();
}

class _SiteVideoState extends State<SiteVideo> {
  late final SiteVideoHealthPoller _poller =
      SiteVideoHealthPoller(api: widget.api, robotId: widget.robotId);
  String _camera = 'front';

  @override
  void dispose() {
    _poller.close();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final id = Uri.encodeComponent(widget.robotId);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SegmentedButton<String>(
        segments: [
          ButtonSegment(value: 'front', label: Text('前', key: SiteVideo.cameraKey('front'))),
          ButtonSegment(value: 'back', label: Text('后', key: SiteVideo.cameraKey('back'))),
        ],
        selected: {_camera},
        onSelectionChanged: (s) => setState(() => _camera = s.first),
      ),
      const SizedBox(height: 8),
      AspectRatio(
        aspectRatio: 16 / 9,
        child: LiveVideo(
          key: ValueKey<String>('site-live-$_camera'),
          baseUrl: widget.api.baseUrl,
          camera: _camera,
          streamPath: '/api/robots/$id/video/$_camera',
          token: widget.api.session?.token ?? '',
          openClient: widget.api.pinnedClient,
          health: _poller.stream,
        ),
      ),
    ]);
  }
}
