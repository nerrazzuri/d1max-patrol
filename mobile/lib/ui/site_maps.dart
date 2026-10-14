/// 站点模式的地图（W00c5d 第二部分，决策 8：站点是地图的唯一权威，狗上只有正在用的那一张）。
///
/// - 每张图一行（地图号:版本、从哪来、几个文件）；管理员点一张 → 选一台狗 → 下发。狗后台下载、核对、
///   载入，成了之后狗报的地图版本跟着变；没成站点出告警。
/// - 录包一行一个；管理员可以拿它在狗上重建一张新图（要给一个新的版本号）。
/// - 录包的开始、停止在单狗页（管理员）。
///
/// 底部导航的「地图」栏是 [SiteMapTab]（2026-10-14 重做）：狗正在用的那张图当主体，图上画狗、待命点、
/// 原点、禁行区和限速区；上面这个列表页从它的右上角进。
library;

import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_map_raster.dart';
import 'nw/nw_p3_robots.dart' show robotOnline;
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'map_view.dart';
import 'site_mapping.dart';
import 'site_robot.dart';
import 'widget/fields_dialog.dart';

export 'nw/nw_map_raster.dart' show darkMapRaster;

class SiteMapsPage extends StatefulWidget {
  final SiteApi api;
  const SiteMapsPage({super.key, required this.api});

  static Key mapKey(String id, String version) => Key('map-$id:$version');
  static Key bagKey(String robot, String name) => Key('bag-$robot/$name');
  static Key previewKey(String id, String version) => Key('preview-$id:$version');
  static const Key versionKey = Key('build-version');

  @override
  State<SiteMapsPage> createState() => _SiteMapsPageState();
}

class _SiteMapsPageState extends State<SiteMapsPage> {
  late Future<Map<String, dynamic>> _data = widget.api.maps();

  void _snack(String t) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar() // 新的一条顶掉旧的：不让人等旧的那条 4 秒过完才看到结果
      ..showSnackBar(SnackBar(content: Text(t)));
  }

  String _ackText(Map<String, dynamic> r, String ok) {
    final ack = (r['ack'] as Map?) ?? const {};
    return ack['result'] == 'accepted'
        ? ok
        : tr('The robot did not accept: ${ack['result']} ${ack['reason'] ?? ''}',
            '狗没接：${ack['result']} ${ack['reason'] ?? ''}');
  }

  /// 选一台**报了这种能力**的狗（没这能力的狗会回 unsupported）。一台都没有就说一声、返回 null。
  Future<String?> _pickRobot(String task, String what) async {
    List<Map<String, dynamic>> robots;
    try {
      robots = (await widget.api.robots()).where((r) => robotCan(r, task)).toList();
    } on SiteError catch (e) {
      _snack(tr('Robot list unavailable: $e', '拿不到狗的列表：$e'));
      return null;
    }
    if (!mounted) return null;
    if (robots.isEmpty) {
      _snack(tr('No robot can $what (none is online reporting this capability, or it is not supported)',
          '没有能$what的狗（狗没在线报能力，或者这台狗不支持）'));
      return null;
    }
    return showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: Text(tr('Send to which robot', '下发给哪台狗')), children: [
        for (final r in robots)
          SimpleDialogOption(
              key: Key('pick-${r['robot_id']}'),
              onPressed: () => Navigator.pop(c, '${r['robot_id']}'),
              child: Text('${r['robot_id']}')),
      ]),
    );
  }

  Future<void> _activate(Map<String, dynamic> m) async {
    final robot = await _pickRobot('map_activate', tr('switch maps', '换图'));
    if (robot == null || !mounted) return;
    final label = '${m['map_id']}:${m['version']}';
    // 换图会换坐标系：点位、待命点都得是这张图上的。点错了狗就在错的图上走，所以要再确认一次。
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Switch $robot to $label?', '让 $robot 换成 $label？')),
        content: Text(tr(
            'The robot downloads and verifies the map first, then switches coordinate frames once its '
                'current task is done. It accepts no new tasks during the switch. '
                'Task packages and standby points must be on this map.',
            '狗先下载、核对，等手上的任务做完再切换坐标系；切换那一小会儿不接新任务。'
                '任务包、待命点要是这张图上的。')),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
          FilledButton(
              key: const Key('activate-go'),
              onPressed: () => Navigator.pop(c, true),
              child: Text(tr('Switch', '换'))),
        ],
      ),
    );
    if (ok != true) return;
    try {
      Map<String, dynamic> r;
      try {
        r = await widget.api.activateMap(robot, '${m['map_id']}', '${m['version']}');
      } on SiteError catch (e) {
        // W13a：这台狗在这张图上还没有原点。说清楚后果，管理员确认了才照样下发（标原点要狗先载上这张图）。
        if (e.status != 409 || e.body['reason'] != 'no_home' || !mounted) rethrow;
        final go = await showDialog<bool>(
          context: context,
          builder: (c) => AlertDialog(
            title: Text(tr('$robot has no origin on $label', '$robot 在 $label 上还没有原点')),
            content: Text(tr(
                'If you send it anyway, the robot accepts no goto or patrol after loading this map '
                    '(without an origin there is no safe place to return to). Remote control and '
                    'set position still work. Drive the robot to the origin (in front of the dock), '
                    'set its position, then tap "Mark origin here".',
                '照样下发的话，狗载上这张图之后不接 goto、巡检（没有原点就没有安全返航的目标）；'
                    '遥控、设位置照常。要先把狗开到原点（充电桩前），设好位置，再点「在这儿标原点」。')),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
              FilledButton(
                  key: const Key('activate-without-home'),
                  onPressed: () => Navigator.pop(c, true),
                  child: Text(tr('Send anyway', '照样下发'))),
            ],
          ),
        );
        if (go != true) {
          _snack(tr('Not sent: $robot has no origin on $label', '没下发：$robot 在 $label 上还没有原点'));
          return;
        }
        r = await widget.api.activateMap(robot, '${m['map_id']}', '${m['version']}',
            withoutHome: true);
      }
      _snack(_ackText(
          r,
          tr('$robot is downloading and loading $label. Its reported map version changes when done',
              '$robot 在下载、载入 $label，好了它报的地图版本会变')));
    } on SiteError catch (e) {
      _snack(tr('Send failed: $e', '下发没成：$e'));
    }
  }

  Future<void> _build(Map<String, dynamic> b) async {
    final robot = '${b['robot_id']}';
    try {
      if (!robotCan(await widget.api.robot(robot), 'map_build')) {
        _snack(tr('$robot cannot rebuild now (not online reporting this capability, or not supported)',
            '$robot 现在不能重建（没在线报能力，或者不支持）'));
        return;
      }
    } on SiteError catch (e) {
      _snack(tr('Status of $robot unavailable: $e', '拿不到 $robot 的状态：$e'));
      return;
    }
    if (!mounted) return;
    final got = await showFieldsDialog(context,
        title: tr('Rebuild from ${b['name']}', '拿 ${b['name']} 重建'),
        fields: [
          DialogField(tr('Map ID', '地图号'), initial: 'estate-1'),
          DialogField(tr('New version (must differ from existing ones)', '新的版本号（不能跟已有的重）'),
              key: SiteMapsPage.versionKey),
        ],
        confirm: tr('Rebuild', '重建'),
        confirmKey: const Key('build-go'));
    if (got == null || got[0].isEmpty || got[1].isEmpty) return;
    try {
      final r = await widget.api.buildMap(robot, '${b['name']}', got[0], got[1]);
      _snack(_ackText(
          r,
          tr('$robot started rebuilding. The new map appears here when done',
              '$robot 开始重建，好了新图会出现在这里')));
    } on SiteError catch (e) {
      _snack(tr('Rebuild failed: $e', '重建没成：$e'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final admin = widget.api.session?.canManageMaps ?? false;
    final c = Nw.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(tr('Maps', '地图'))),
      body: RefreshIndicator(
        onRefresh: () async {
          setState(() {
            _data = widget.api.maps();
          });
          await _data.catchError((Object _) => <String, dynamic>{});
        },
        child: FutureBuilder<Map<String, dynamic>>(
          future: _data,
          builder: (_, snap) {
            if (snap.hasError) {
              return NwListView(children: [
                NwNotice(
                    tone: NwTone.error,
                    text: tr('Map list unavailable: ${snap.error}', '地图目录拿不到：${snap.error}')),
              ]);
            }
            if (!snap.hasData) return const Center(child: CircularProgressIndicator());
            final maps = (snap.data!['maps'] as List).whereType<Map<String, dynamic>>();
            final bags = (snap.data!['bags'] as List).whereType<Map<String, dynamic>>();
            final title = NwText.titleSmall.copyWith(color: c.text);
            final sub = NwText.bodySmall.copyWith(color: c.textSecondary);
            return NwListView(children: [
              NwSectionLabel(tr('Maps', '图')),
              nwGap8,
              NwListCard(children: [
                if (maps.isEmpty) NwCardText(tr('No maps on the site yet', '站点上还没有图')),
                for (final m in maps)
                  ListTile(
                    key: SiteMapsPage.mapKey('${m['map_id']}', '${m['version']}'),
                    leading: Icon(Icons.map_rounded, color: c.textSecondary),
                    title: Text('${m['map_id']}:${m['version']}', style: title),
                    subtitle: Text(
                        tr(
                            'From ${m['source']} · ${(m['files'] as List).length} files'
                                '${(m['note'] ?? '') != '' ? ' · ${m['note']}' : ''}',
                            '来自 ${m['source']} · ${(m['files'] as List).length} 个文件'
                                '${(m['note'] ?? '') != '' ? ' · ${m['note']}' : ''}'),
                        style: sub),
                    // 看图（W00c6h）谁都能点；点这一行是下发（管理员）。
                    trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                      IconButton(
                          key: SiteMapsPage.previewKey('${m['map_id']}', '${m['version']}'),
                          tooltip: tr('View map', '看图'),
                          icon: Icon(Icons.map_outlined, color: c.text),
                          onPressed: () => Navigator.push(
                              context,
                              MaterialPageRoute<void>(
                                  builder: (_) => SiteMapPreviewPage(
                                      api: widget.api,
                                      mapId: '${m['map_id']}',
                                      version: '${m['version']}')))),
                      if (admin) Icon(Icons.send_to_mobile, color: c.textSecondary),
                    ]),
                    onTap: admin ? () => _activate(m) : null,
                  ),
              ]),
              nwGap24,
              NwSectionLabel(tr('Mapping recordings', '录包')),
              nwGap8,
              NwListCard(children: [
                if (bags.isEmpty) NwCardText(tr('No mapping recordings yet', '还没有录包')),
                for (final b in bags)
                  ListTile(
                    key: SiteMapsPage.bagKey('${b['robot_id']}', '${b['name']}'),
                    leading: Icon(Icons.fiber_smart_record_rounded, color: c.textSecondary),
                    title: Text('${b['name']}', style: title),
                    subtitle: Text(
                        '${b['robot_id']} · ${((b['bytes'] as num? ?? 0) / 1048576).toStringAsFixed(1)} MB',
                        style: sub),
                    trailing: admin ? Icon(Icons.build, color: c.textSecondary) : null,
                    onTap: admin ? () => _build(b) : null,
                  ),
              ]),
            ]);
          },
        ),
      ),
    );
  }
}

// ------------------------------------------------------------ 地图栏

/// 这只狗报着哪张图（`地图号:版本`）：它载着、正在用的那一张。没报（老狗、不在线、没载图）是 null。
String? robotMapTag(Map<String, dynamic> robot) {
  final caps = robot['capabilities'];
  final tasks = caps is Map ? caps['tasks'] : null;
  final p = tasks is Map ? tasks['patrol'] : null;
  if (p is! Map || p['map_id'] == null || p['map_version'] == null) return null;
  return '${p['map_id']}:${p['map_version']}';
}

/// 图例的色块跟画布上画的是一个色。画布那几个 painter（[StandbyPainter]、[ZonesPainter]）的颜色是写死的、
/// 不跟主题走，图例只能照它们的来 —— 所以这四个不是主题色。字和底还是主题的。
const Color _standbyPaint = Colors.orange;
const Color _homePaint = Colors.blue;
const Color _nogoPaint = Colors.red;
const Color _slowPaint = Colors.amber;

/// 地图栏读到的一份：摆哪张图、图和它上面的东西、狗。
class _MapView {
  /// 摆的是哪张图；目录是空的就没有。
  final String? mapId;
  final String? version;

  /// 这张图是不是狗报着的那一张（不是：狗报的图站点上没有，摆的是站点上最新的）。
  final bool inUse;
  final Map<String, dynamic>? meta;

  /// 底图：栅格预览图，站点给了平面图就是平面图（[plan]）。
  final Uint8List? png;
  final bool plan;

  /// 这张图看不了的原因（没有栅格之类）。
  final String? previewError;
  final List<Map<String, dynamic>> zones;
  final List<Map<String, dynamic>> robots;
  final String? robotsError;

  const _MapView(
      {this.mapId,
      this.version,
      this.inUse = false,
      this.meta,
      this.png,
      this.plan = false,
      this.previewError,
      this.zones = const [],
      this.robots = const [],
      this.robotsError});

  String get tag => '$mapId:$version';
}

/// 底图（栅格预览图或者平面图）：浅色原样，深色过 [darkMapRaster]。
Widget _raster(BuildContext context, Uint8List png, {bool plan = false}) =>
    mapBaseImage(context, png, plan: plan, darkKey: SiteMapTab.darkRasterKey);

/// 底部导航的「地图」栏：狗正在用的那张图的预览当主体（能缩放），图上画狗、待命点、原点、禁行区和
/// 限速区，左上是图例、右下是回正和缩放；下面（横屏在右边）是这张图上的狗。右上进地图列表
/// （[SiteMapsPage]：管理员下发、重建都在那里）。
///
/// 摆哪张图：狗报着的那一张（报得最多的；要站点上有）。狗报的图站点上没有，就摆站点上最新的一张，并且
/// 写明没有狗在这张图上 —— 不把别的坐标系里的狗画上来。位置是打开、点刷新时读的，不自己跟着动。
class SiteMapTab extends StatefulWidget {
  final SiteApi api;
  const SiteMapTab({super.key, required this.api});

  static const Key canvasKey = Key('map-tab-canvas');

  /// 深色下栅格底图外面那层滤镜（浅色没有这一层）。
  static const Key darkRasterKey = Key('map-tab-dark-raster');
  static const Key zonesKey = Key('map-tab-zones');
  static const Key standbyKey = Key('map-tab-standby');
  static const Key legendKey = Key('map-tab-legend');
  static const Key listKey = Key('map-tab-list');
  static const Key refreshKey = Key('map-tab-refresh');
  static const Key emptyKey = Key('map-tab-empty');
  static const Key recenterKey = Key('map-tab-recenter');
  static const Key zoomInKey = Key('map-tab-zoom-in');
  static const Key zoomOutKey = Key('map-tab-zoom-out');

  /// 片区淡淡的底和边（`CustomPaint`，[AreasPainter]）；片区的名字（`Text`）。
  static const Key areasKey = Key('map-tab-areas');
  static Key areaLabelKey(String name) => Key('map-tab-area-$name');

  /// 比例尺那一段（`SizedBox`，宽 = 这一段在屏上的长度）。
  static const Key scaleKey = Key('map-tab-scale');

  /// 图例的标题行（`InkWell`）：点了收起、展开。
  static const Key legendToggleKey = Key('map-tab-legend-toggle');
  static Key robotKey(String id) => Key('map-tab-robot-$id');
  static Key markerKey(String id) => Key('map-tab-marker-$id');

  @override
  State<SiteMapTab> createState() => _SiteMapTabState();
}

class _SiteMapTabState extends State<SiteMapTab> {
  late Future<_MapView> _data = _load();
  final TransformationController _zoom = TransformationController();

  /// 画布那一块现在多大（缩放按钮绕它的中心缩放）。
  Size _viewport = Size.zero;

  /// 图例展开着没有：null = 人还没点过，一列摆得下就展开、摆不下（横屏的手机）收起来，免得挡住图。
  bool? _legendOpen;

  static const double _maxScale = 8;

  /// 左下比例尺那一块大约多高（图例往下排到这儿为止）。
  static const double _scaleBarHeight = 48;

  @override
  void dispose() {
    _zoom.dispose();
    super.dispose();
  }

  Future<_MapView> _load() async {
    final cat = await widget.api.maps();
    final maps = (cat['maps'] as List? ?? const []).whereType<Map<String, dynamic>>().toList();
    var robots = const <Map<String, dynamic>>[];
    String? robotsError;
    try {
      robots = await widget.api.robots();
    } on SiteError catch (e) {
      robotsError = '$e'; // 狗的列表读不到：图照样看
    }
    if (maps.isEmpty) return _MapView(robots: robots, robotsError: robotsError);
    // 狗报得最多的那一张（要站点上有）；没有就站点上最新的一张
    final votes = <String, int>{};
    for (final r in robots) {
      final tag = robotMapTag(r);
      if (tag != null) votes[tag] = (votes[tag] ?? 0) + 1;
    }
    Map<String, dynamic>? used;
    var best = 0;
    for (final m in maps) {
      final n = votes['${m['map_id']}:${m['version']}'] ?? 0;
      if (n > best) {
        best = n;
        used = m;
      }
    }
    var pick = used ?? maps.first;
    if (used == null) {
      for (final m in maps) {
        if ((m['created_ms'] as num? ?? 0) >= (pick['created_ms'] as num? ?? 0)) pick = m;
      }
    }
    final id = '${pick['map_id']}', version = '${pick['version']}';
    Map<String, dynamic>? meta;
    Uint8List? png;
    var plan = false;
    String? previewError;
    try {
      meta = await widget.api.mapPreview(id, version);
      (png, plan) = await loadMapBase(widget.api, id, version, MapGeo.fromPreview(meta));
    } on SiteError catch (e) {
      previewError = e.message;
    }
    // 区域：GET …/zones 那一份最新；取不到退回预览里带的（跟看图那一页一个口径）。
    var zones = (meta?['zones'] as List? ?? const []).whereType<Map<String, dynamic>>().toList();
    if (png != null) {
      try {
        final z = await widget.api.mapZones(id, version);
        zones = (z['zones'] as List? ?? const []).whereType<Map<String, dynamic>>().toList();
      } on SiteError {
        // 留着预览里带的
      }
    }
    return _MapView(
        mapId: id,
        version: version,
        inUse: used != null,
        meta: meta,
        png: png,
        plan: plan,
        previewError: previewError,
        zones: zones,
        robots: robots,
        robotsError: robotsError);
  }

  void _reload() {
    _zoom.value = Matrix4.identity();
    setState(() {
      _data = _load();
    });
  }

  /// 绕画布中心放大、缩小 [factor] 倍；不缩到比整张图还小，放大了不把图拖出画布。
  void _scale(double factor) {
    final cur = _zoom.value.getMaxScaleOnAxis();
    final next = (cur * factor).clamp(1.0, _maxScale);
    if (next <= 1.0 + 1e-9 || _viewport.isEmpty) {
      _zoom.value = Matrix4.identity();
      return;
    }
    final k = next / cur;
    final cx = _viewport.width / 2, cy = _viewport.height / 2;
    final m = Matrix4.translationValues(cx, cy, 0)
        .multiplied(Matrix4.diagonal3Values(k, k, 1))
        .multiplied(Matrix4.translationValues(-cx, -cy, 0))
        .multiplied(_zoom.value);
    final t = m.getTranslation();
    m.setTranslationRaw(t.x.clamp(_viewport.width * (1 - next), 0.0),
        t.y.clamp(_viewport.height * (1 - next), 0.0), 0);
    _zoom.value = m;
  }

  void _openList() => Navigator.push(
      context, MaterialPageRoute<void>(builder: (_) => SiteMapsPage(api: widget.api)));

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    return FutureBuilder<_MapView>(
        future: _data,
        builder: (context, snap) {
          final done = snap.connectionState == ConnectionState.done;
          final v = done ? snap.data : null;
          final onMap = v == null ? 0 : v.robots.where((r) => v.inUse && robotMapTag(r) == v.tag).length;
          return Column(children: [
            NwHeader(
                overline: tr('Map', '地图'),
                title: v?.mapId == null ? tr('Estate map', '庄园地图') : v!.tag,
                subtitle:
                    v?.mapId == null ? null : tr('$onMap robot(s) on this map', '这张图上 $onMap 只狗'),
                trailing: [
                  IconButton(
                      key: SiteMapTab.refreshKey,
                      tooltip: tr('Refresh', '刷新'),
                      icon: Icon(Icons.refresh_rounded, color: done ? c.text : c.textMuted),
                      onPressed: done ? _reload : null),
                  IconButton(
                      key: SiteMapTab.listKey,
                      tooltip: tr('All maps', '所有地图'),
                      icon: Icon(Icons.layers_rounded, color: c.text),
                      onPressed: _openList),
                ]),
            Expanded(child: _body(context, snap, v)),
          ]);
        });
  }

  Widget _body(BuildContext context, AsyncSnapshot<_MapView> snap, _MapView? v) {
    if (snap.connectionState != ConnectionState.done) {
      return const Center(child: CircularProgressIndicator());
    }
    if (snap.hasError || v == null) {
      return NwListView(children: [
        NwNotice(
            tone: NwTone.error,
            text: tr('Map list unavailable: ${snap.error}', '地图目录拿不到：${snap.error}')),
      ]);
    }
    if (v.mapId == null) {
      return NwEmpty(
          key: SiteMapTab.emptyKey,
          icon: Icons.map_outlined,
          text: tr('No maps on the site yet', '站点上还没有图'),
          hint: tr(
              'An administrator records a mapping run from a robot\'s page, then builds a map from it '
                  'in the map list.',
              '管理员在单狗页录一趟包，再到地图列表里拿它建图。'),
          action: NwButton(
              label: tr('Open map list', '去地图列表'),
              icon: Icons.layers_rounded,
              variant: NwButtonVariant.outline,
              onPressed: _openList));
    }
    final canvas = v.png == null
        ? SingleChildScrollView(
            padding: const EdgeInsets.all(Nw.s16),
            child: NwNotice(
                tone: NwTone.warning,
                text: tr('Preview unavailable: ${v.previewError}', '看不了：${v.previewError}')))
        : _canvas(context, v);
    // 横屏、桌面：图在左、狗在右一栏；竖屏：图在上、狗在下（最多占四成高）
    return LayoutBuilder(builder: (context, box) {
      if (box.maxWidth >= 700) {
        return Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Expanded(child: canvas),
          SizedBox(width: math.min(360, box.maxWidth * 0.4), child: _robots(context, v, side: true)),
        ]);
      }
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Expanded(child: canvas),
        ConstrainedBox(
            constraints: BoxConstraints(maxHeight: box.maxHeight * 0.4),
            child: _robots(context, v, side: false)),
      ]);
    });
  }

  /// 图：底图（栅格图或者平面图）上叠片区、区域、待命点和原点（还是看图那一页的 painter）、狗；整张图按站点
  /// 给的转角摆正、整张放进这一块（这就是「回到整张图」那个样子：缩放的变换是单位阵）；左上图例、左下
  /// 比例尺、右下回正和缩放。图上的字（狗的编号、片区名字）反着转回来，永远是正的。
  Widget _canvas(BuildContext context, _MapView v) {
    final c = Nw.of(context);
    final meta = v.meta!;
    final geo = MapGeo.fromPreview(meta);
    final w = geo.width, h = geo.height, mpp = geo.mPerPx, rad = geo.radians;
    final standby = (meta['standby'] as List? ?? const []).whereType<Map>().toList();
    final homes = (meta['homes'] as List? ?? const []).whereType<Map>().toList();
    final nogo = v.zones.any((z) => z['kind'] == 'nogo');
    final slow = v.zones.any((z) => z['kind'] != 'nogo');
    // 这张图上的狗：报着这张图、位置可信的才画（没设位置的画在哪儿都是错的）
    final placed = <(String, bool, Offset)>[
      if (v.inUse)
        for (final r in v.robots)
          if (robotMapTag(r) == v.tag && _pose(r) != null) ('${r['robot_id']}', _online(r), _pose(r)!),
    ];
    Widget swatch(Color color, {bool round = false}) => Container(
        width: round ? 10 : 12,
        height: 10,
        decoration: round
            ? BoxDecoration(color: color, shape: BoxShape.circle)
            : BoxDecoration(color: color.withValues(alpha: 0.35), border: Border.all(color: color, width: 2)));
    // 图例：只列图上有的（底图那三样总有）
    final legend = <NwLegendItem>[
      ...mapBaseLegend(context, plan: v.plan),
      if (placed.isNotEmpty)
        NwLegendItem(
            swatch: Container(
                width: 12,
                height: 12,
                decoration: BoxDecoration(
                    color: c.surface, shape: BoxShape.circle, border: Border.all(color: c.success, width: 2))),
            label: tr('Robot', '狗')),
      if (standby.isNotEmpty)
        NwLegendItem(swatch: swatch(_standbyPaint, round: true), label: tr('Standby point', '待命点')),
      if (homes.isNotEmpty)
        NwLegendItem(swatch: Container(width: 10, height: 10, color: _homePaint), label: tr('Origin', '原点')),
      if (nogo) NwLegendItem(swatch: swatch(_nogoPaint), label: tr('No-go zone', '禁行区')),
      if (slow) NwLegendItem(swatch: swatch(_slowPaint), label: tr('Speed-limit zone', '限速区')),
      if (geo.areas.isNotEmpty) mapAreaLegend(context),
    ];
    return Padding(
        padding: const EdgeInsets.all(Nw.s8),
        child: DecoratedBox(
            decoration: BoxDecoration(
                color: c.surfaceVariant,
                borderRadius: BorderRadius.circular(Nw.r12),
                border: Border.all(color: c.outline)),
            child: ClipRRect(
                borderRadius: BorderRadius.circular(Nw.r12),
                child: LayoutBuilder(builder: (context, box) {
                  _viewport = box.biggest;
                  // 转过的整张图按比例放进这一块；图上的点（图的像素）跟着同一个比例
                  final k = geo.fitScale(box.biggest);
                  Offset px(double x, double y) => geo.toPx(x, y) * k;
                  final r = math.max(3.0, math.max(w, h) * k / 120);
                  // 短屏（横屏手机）右下那三个按钮竖着摆不下：横着摆
                  final short = box.maxHeight < 3 * Nw.touch + 4 * Nw.s8 + 96;
                  // 图例摆在左上，下面给比例尺留地方；矮了就排成几列。人没点过的话，一列摆得下、展开了也
                  // 不压着整张图（转过的外接框）才展开，不然收成一个「图例」，点了再看
                  final legendMax = math.max(48.0, box.maxHeight - 3 * Nw.s8 - _scaleBarHeight);
                  final legendOpen = _legendOpen ?? _legendFits(context, legend, box.biggest, geo, k, legendMax);
                  final areas = [
                    for (final a in geo.areas) (a.name, [for (final p in a.points) px(p.dx, p.dy)]),
                  ];
                  return Stack(children: [
                    Positioned.fill(
                        child: InteractiveViewer(
                            transformationController: _zoom,
                            maxScale: _maxScale,
                            child: Center(
                                child: NwMapRotation(
                                    rad: rad,
                                    size: Size(w * k, h * k),
                                    child: SizedBox(
                                        key: SiteMapTab.canvasKey,
                                        width: w * k,
                                        height: h * k,
                                        child: Stack(clipBehavior: Clip.none, children: [
                                          Positioned.fill(child: _raster(context, v.png!, plan: v.plan)),
                                          if (areas.isNotEmpty)
                                            Positioned.fill(
                                                child: CustomPaint(
                                                    key: SiteMapTab.areasKey,
                                                    painter: AreasPainter([for (final a in areas) a.$2],
                                                        fill: mapAreaFill(c), stroke: mapAreaStroke(c), strokeWidth: 1.5))),
                                          Positioned.fill(
                                              child: CustomPaint(
                                                  key: SiteMapTab.zonesKey,
                                                  painter: ZonesPainter([
                                                    for (final z in v.zones)
                                                      ZoneShape(z['kind'] == 'nogo', [
                                                        for (final p in (z['polygon'] as List? ?? const []))
                                                          if (p is List && p.length >= 2 && p[0] is num && p[1] is num)
                                                            px((p[0] as num).toDouble(), (p[1] as num).toDouble()),
                                                      ]),
                                                  ], const <Offset>[], radius: r))),
                                          Positioned.fill(
                                              child: CustomPaint(
                                                  key: SiteMapTab.standbyKey,
                                                  painter: StandbyPainter([
                                                    for (final p in standby)
                                                      if (p['x'] is num && p['y'] is num)
                                                        px((p['x'] as num).toDouble(), (p['y'] as num).toDouble()),
                                                  ], radius: r, homes: [
                                                    for (final p in homes)
                                                      if (p['x'] is num && p['y'] is num)
                                                        px((p['x'] as num).toDouble(), (p['y'] as num).toDouble()),
                                                  ]))),
                                          if (areas.isNotEmpty)
                                            Positioned.fill(
                                                child: MapAreaLabels(
                                                    areas: areas,
                                                    rad: rad,
                                                    unit: 1,
                                                    zoom: _zoom,
                                                    labelKey: SiteMapTab.areaLabelKey)),
                                          for (final (id, online, at) in placed)
                                            Positioned(
                                                left: px(at.dx, at.dy).dx,
                                                top: px(at.dx, at.dy).dy,
                                                child: FractionalTranslation(
                                                    translation: const Offset(-0.5, -0.5),
                                                    // 牌子反着转回来：编号正着读
                                                    child: Transform.rotate(
                                                        angle: -rad,
                                                        child: NwRobotMarker(
                                                            markerKey: SiteMapTab.markerKey(id),
                                                            id: id,
                                                            // 不在线的（过期、吊销）：中性色，不是绿的
                                                            tone: online ? nwToneOf('online') : NwTone.neutral)))),
                                        ])))))),
                    Positioned(
                        left: Nw.s8,
                        top: Nw.s8,
                        child: ConstrainedBox(
                            constraints: BoxConstraints(
                                maxWidth: math.max(0, box.maxWidth - 2 * Nw.s8), maxHeight: legendMax),
                            child: NwMapOverlay(
                                overlayKey: SiteMapTab.legendKey,
                                child: Column(
                                    mainAxisSize: MainAxisSize.min,
                                    crossAxisAlignment: CrossAxisAlignment.start,
                                    children: [
                                      InkWell(
                                          key: SiteMapTab.legendToggleKey,
                                          onTap: () => setState(() => _legendOpen = !legendOpen),
                                          child: Padding(
                                              padding: const EdgeInsets.symmetric(vertical: Nw.s4),
                                              child: Row(mainAxisSize: MainAxisSize.min, children: [
                                                Text(tr('Legend', '图例'),
                                                    style: NwText.labelSmall
                                                        .copyWith(color: c.text, fontWeight: FontWeight.w700)),
                                                const SizedBox(width: Nw.s4),
                                                Icon(legendOpen ? Icons.expand_less_rounded : Icons.expand_more_rounded,
                                                    size: 16, color: c.textSecondary),
                                              ]))),
                                      if (legendOpen)
                                        Flexible(
                                            child: Wrap(direction: Axis.vertical, runSpacing: Nw.s16, children: [
                                          for (final item in legend)
                                            ConstrainedBox(
                                                constraints: const BoxConstraints(maxWidth: 176), child: item),
                                        ])),
                                    ])))),
                    Positioned(
                        left: Nw.s8,
                        bottom: Nw.s8,
                        child: MapScaleBar(pxPerMeter: k / mpp, zoom: _zoom, barKey: SiteMapTab.scaleKey)),
                    Positioned(
                        right: Nw.s8,
                        bottom: Nw.s8,
                        child: Flex(
                            direction: short ? Axis.horizontal : Axis.vertical,
                            mainAxisSize: MainAxisSize.min,
                            spacing: Nw.s8,
                            children: [
                              NwMapButton(
                                  buttonKey: SiteMapTab.recenterKey,
                                  icon: Icons.my_location_rounded,
                                  tooltip: tr('Fit the whole map', '回到整张图'),
                                  onPressed: () => _zoom.value = Matrix4.identity()),
                              NwMapButton(
                                  buttonKey: SiteMapTab.zoomInKey,
                                  icon: Icons.add_rounded,
                                  tooltip: tr('Zoom in', '放大'),
                                  onPressed: () => _scale(1.5)),
                              NwMapButton(
                                  buttonKey: SiteMapTab.zoomOutKey,
                                  icon: Icons.remove_rounded,
                                  tooltip: tr('Zoom out', '缩小'),
                                  onPressed: () => _scale(1 / 1.5)),
                            ])),
                  ]);
                }))));
  }

  /// 图例一列摆得下（[legendMax] 那么高），左上角展开了也不压着整张图（按 [k] 放进 [box] 的、转过的
  /// 外接框）。宽、高按图例的字量出来（跟 [NwLegendItem] 一个字号）。
  static bool _legendFits(
      BuildContext context, List<NwLegendItem> items, Size box, MapGeo geo, double k, double legendMax) {
    const header = 24.0, row = 16.0;
    final h = 2 * Nw.s8 + header + items.length * row;
    if (h > legendMax) return false;
    var textW = 0.0;
    final scaler = MediaQuery.textScalerOf(context);
    for (final i in items) {
      final tp = TextPainter(
          text: TextSpan(text: i.label, style: NwText.labelSmall),
          textDirection: Directionality.of(context),
          textScaler: scaler,
          maxLines: 1)
        ..layout();
      textW = math.max(textW, tp.width);
      tp.dispose();
    }
    final legend = Rect.fromLTWH(Nw.s8, Nw.s8, 2 * Nw.s8 + row + Nw.s4 + textW, h);
    final map = Rect.fromCenter(
        center: box.center(Offset.zero),
        width: nwRotatedBounds(geo.size, geo.radians).width * k,
        height: nwRotatedBounds(geo.size, geo.radians).height * k);
    return !legend.overlaps(map);
  }

  /// 在线 = 没吊销、狗说在线、**站点说这份状态还新鲜**，跟狗列表一个口径（`robotOnline`；App 重做外审
  /// I2：原来只看狗最后一次说的 `online`，狗失联以后那一格还是真的，站点用 `fresh=false` 说它过期了 ——
  /// 过期的、吊销的都被画成了绿色的在线）。
  static bool _online(Map<String, dynamic> r) => robotOnline(r);

  /// 状态药丸上写什么、什么色：在线绿；吊销、离线照规矩（离线红）。
  static (String, NwTone) _state(Map<String, dynamic> r) => r['revoked'] == true
      ? (tr('Revoked', '已吊销'), NwTone.neutral)
      : _online(r)
          ? (tr('Online', '在线'), nwToneOf('online'))
          : (tr('Offline', '离线'), nwToneOf('offline'));

  /// 这只狗在图上的位置（米）；没设位置、没报的是 null。
  static Offset? _pose(Map<String, dynamic> r) {
    final loc = r['loc'];
    final p = loc is Map && loc['anchored'] == true ? loc['pose'] : null;
    if (p is! Map || p['x'] is! num || p['y'] is! num) return null;
    return Offset((p['x'] as num).toDouble(), (p['y'] as num).toDouble());
  }

  /// 这张图上的狗；不在这张图上的排在后面，写明它在哪张图上。[side] = 摆在图的右边（横屏）。
  Widget _robots(BuildContext context, _MapView v, {required bool side}) {
    final c = Nw.of(context);
    bool here(Map<String, dynamic> r) => v.inUse && robotMapTag(r) == v.tag;
    final rows = [...v.robots.where(here), ...v.robots.where((r) => !here(r))];
    final sub = NwText.bodySmall.copyWith(color: c.textSecondary);
    String where(Map<String, dynamic> r) {
      final tag = robotMapTag(r);
      if (!here(r)) {
        return tag == null ? tr('Reports no map', '没报在用哪张图') : tr('On map $tag', '在 $tag 这张图上');
      }
      final p = _pose(r);
      return p == null
          ? tr('Position not set: not drawn on the map', '没设位置：图上不画')
          : tr('At (${p.dx.toStringAsFixed(1)}, ${p.dy.toStringAsFixed(1)}) m',
              '在 (${p.dx.toStringAsFixed(1)}, ${p.dy.toStringAsFixed(1)}) m');
    }

    return Material(
        color: c.surface,
        shape: Border(
            top: side ? BorderSide.none : BorderSide(color: c.outline),
            left: side ? BorderSide(color: c.outline) : BorderSide.none),
        child: ListView(shrinkWrap: true, padding: const EdgeInsets.all(Nw.s16), children: [
          NwSectionLabel(tr('Robots on this map', '这张图上的狗')),
          nwGap8,
          if (v.robotsError != null)
            NwNotice(
                tone: NwTone.warning,
                text: tr('Robot list unavailable: ${v.robotsError}', '拿不到狗的列表：${v.robotsError}'))
          else if (rows.isEmpty)
            Text(tr('No robots yet.', '还没有狗。'), style: sub)
          else if (!rows.any(here))
            Padding(
                padding: const EdgeInsets.only(bottom: Nw.s8),
                child: Text(
                    tr('No robot is on this map. It is the newest map on the site.',
                        '没有狗在这张图上。这是站点上最新的一张。'),
                    style: sub)),
          for (final r in rows)
            Padding(
                padding: const EdgeInsets.only(bottom: Nw.s8),
                child: NwCard(
                    key: SiteMapTab.robotKey('${r['robot_id']}'),
                    radius: Nw.r8,
                    padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
                    onTap: () => Navigator.push(
                        context,
                        MaterialPageRoute<void>(
                            builder: (_) => SiteRobotPage(api: widget.api, robotId: '${r['robot_id']}'))),
                    child: Row(children: [
                      Expanded(
                          child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              mainAxisSize: MainAxisSize.min,
                              children: [
                            Row(children: [
                              Flexible(
                                  child: Text('${r['robot_id']}',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style:
                                          NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700))),
                              const SizedBox(width: Nw.s8),
                              NwStatusPill(label: _state(r).$1, tone: _state(r).$2),
                            ]),
                            nwGap4,
                            // 不在线的：图上那个点是它最后报的位置，不是现在的，说出来
                            Text(
                                _online(r) || _pose(r) == null
                                    ? where(r)
                                    : tr('${where(r)} · last known position', '${where(r)} · 最后知道的位置'),
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: sub),
                          ])),
                      Icon(Icons.chevron_right_rounded, size: 24, color: c.textSecondary),
                    ]))),
        ]));
  }
}
