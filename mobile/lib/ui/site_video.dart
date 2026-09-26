/// 站点模式的画面（W00c5b）：画面经站点来，手机不直连狗。
///
/// 前、后两路切换；在线判定走站点的轮询（不看图片加载状态，理由见 `LiveVideo`）；
/// 取流用**钉住站点证书**的客户端。站点那头第一个观众来了才让狗开始推，最后一个走了才停 ——
/// 所以这一格只在页面上时连着（`LiveVideo` 被盖住时自己断开，见它的 `_onstage`）。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../net/site_client.dart';
import 'widget/live_video.dart';

/// 按固定间隔问站点「这台狗在不在线、新不新鲜」，变成 `LiveVideo` 要的那条流。
///
/// **不问画面健康**：站点那头要有人来拉流才会让狗开始推，画面健康在那之前永远是「没有」——
/// 拿它当开流的条件，两头就互相等，画面一次都起不来（W00c5b 内部评审阻断）。画面健康留给遥控的
/// 「没画面不许动」用。读不到当「都不在」。**上一发还没回来就不再发**（每发都带超时）。
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

  bool _inFlight = false;

  Future<void> _tick() async {
    if (_inFlight || _closed) return;
    _inFlight = true;
    VideoHealth h;
    try {
      final v = await _api.robot(robotId).timeout(period * 3);
      final up = v['fresh'] == true;
      h = VideoHealth(<String, bool>{'front': up, 'back': up});
    } on Object {
      h = const VideoHealth(<String, bool>{});
    } finally {
      _inFlight = false;
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
      // 横屏（app 只许横屏）：整宽的 16:9 比屏幕还高。最高到屏幕高的六成，居中、不拉伸。
      Center(
        child: ConstrainedBox(
          constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * 0.6),
          child: AspectRatio(
            aspectRatio: 16 / 9,
            child: LiveVideo(
              key: ValueKey<String>('site-live-$_camera'),
              baseUrl: widget.api.baseUrl,
              camera: _camera,
              streamPath: '/api/robots/$id/video/$_camera',
              token: widget.api.session?.token ?? '',
              openClient: widget.api.pinnedClient,
              firstFrameGrace: const Duration(seconds: 12),
              health: _poller.stream,
            ),
          ),
        ),
      ),
    ]);
  }
}
