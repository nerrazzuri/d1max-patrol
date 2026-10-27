/// 巡检路线（2026-10-14，用户定：**站点存的路线、排程是唯一真理源**，网页和手机都能改）。
///
/// - [SiteRoutesPage]：选地图版本，列这一版上的路线；点一条进编辑页，管理员能新建。
/// - [SiteRouteEditorPage]：路线画在图上（折线 + 带号的点）；点图加点（「加点」）、点图挪点（「挪点」：先选中一个点，
///   再点它的新位置）、上移下移、删点、点上的动作（停留、拍照前 / 后、灯）、检查文字、朝向；存的时候站点逐点
///   校验，错的点标红、原因列出来。存好的路线能派给一只狗（走已有的 patrol 接口）。
///
/// 点下去的位置 Flutter 按图的转角、缩放反着算回图的像素（跟画区域一个办法，[MapGeo.toMap]），所以存下去的
/// 就是点的那一处的地图坐标。
library;

import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'map_view.dart';
import 'nw/nw_button.dart';
import 'nw/nw_map_raster.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'pose_marker.dart';
import 'widget/fields_dialog.dart';

/// 路线名、排程 id：ASCII 字母数字 . _ -（跟站点同一条规矩）。
final RegExp routeNameRe = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$');

double _num(Object? v, [double d = 0]) => v is num && v.isFinite ? v.toDouble() : d;

/// 路线上的一个点（站点的平格式）。
class RoutePt {
  String name;
  double x;
  double y;

  /// 朝向，弧度，逆时针（地图系）。
  double yaw;
  String check;
  List<Map<String, dynamic>> actions;
  RoutePt(this.name, this.x, this.y, this.yaw, {this.check = '', List<Map<String, dynamic>>? actions})
      : actions = actions ?? <Map<String, dynamic>>[];

  factory RoutePt.fromWire(Map<String, dynamic> m) => RoutePt('${m['name'] ?? ''}', _num(m['x']), _num(m['y']),
      _num(m['yaw']),
      check: '${m['check'] ?? ''}',
      actions: [for (final a in (m['actions'] as List? ?? const [])) if (a is Map) a.cast<String, dynamic>()]);

  RoutePt copy() => RoutePt(name, x, y, yaw, check: check, actions: [for (final a in actions) {...a}]);

  Map<String, dynamic> toWire() => <String, dynamic>{
        'name': name, 'x': x, 'y': y, 'yaw': yaw,
        if (check.isNotEmpty) 'check': check,
        if (actions.isNotEmpty) 'actions': actions,
      };

  double get dwell {
    for (final a in actions) {
      if (a['type'] == 'dwell') return _num(a['seconds']);
    }
    return 0;
  }

  set dwell(double s) {
    actions.removeWhere((a) => a['type'] == 'dwell');
    if (s > 0) actions.insert(0, <String, dynamic>{'type': 'dwell', 'seconds': s});
  }

  bool hasPhoto(String cam) => actions.any((a) => a['type'] == 'photo' && a['camera'] == cam);

  void setPhoto(String cam, bool on) {
    actions.removeWhere((a) => a['type'] == 'photo' && a['camera'] == cam);
    if (on) actions.add(<String, dynamic>{'type': 'photo', 'camera': cam});
  }

  bool get light => actions.any((a) => a['type'] == 'light');

  set light(bool on) {
    actions.removeWhere((a) => a['type'] == 'light');
    if (on) actions.add(<String, dynamic>{'type': 'light', 'on': true});
  }
}

/// 新点的名字：P1、P2…，不跟已有的重名（照片按点位名归档，重名会互相覆盖）。
String nextPointName(List<RoutePt> pts) {
  final have = {for (final p in pts) p.name};
  for (var i = pts.length + 1;; i++) {
    if (!have.contains('P$i')) return 'P$i';
  }
}

double _r2(double v) => (v * 100).roundToDouble() / 100;

// ------------------------------------------------------------------ 列表

class SiteRoutesPage extends StatefulWidget {
  final SiteApi api;
  const SiteRoutesPage({super.key, required this.api});

  static const Key mapPickKey = Key('routes-map');
  static const Key newKey = Key('routes-new');
  static const Key emptyKey = Key('routes-empty');
  static Key routeKey(String id) => Key('route-$id');

  @override
  State<SiteRoutesPage> createState() => _SiteRoutesPageState();
}

class _SiteRoutesPageState extends State<SiteRoutesPage> {
  List<Map<String, dynamic>> _maps = const [];
  Map<String, dynamic>? _sel;
  Map<String, dynamic>? _view;
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _loadMaps();
  }

  Future<void> _loadMaps() async {
    try {
      final cat = await widget.api.maps();
      final maps = (cat['maps'] as List? ?? const []).whereType<Map<String, dynamic>>().toList();
      if (!mounted) return;
      var pick = maps.isEmpty ? null : maps.first;
      for (final m in maps) {
        if (pick != null && (m['created_ms'] as num? ?? 0) >= (pick['created_ms'] as num? ?? 0)) pick = m;
      }
      setState(() {
        _maps = maps;
        _sel = pick;
      });
      await _loadRoutes();
    } on SiteError catch (e) {
      if (mounted) {
        setState(() {
          _error = e.message;
          _loading = false;
        });
      }
    }
  }

  Future<void> _loadRoutes() async {
    final s = _sel;
    if (s == null) {
      setState(() => _loading = false);
      return;
    }
    setState(() => _loading = true);
    try {
      final v = await widget.api.routes(mapId: '${s['map_id']}', version: '${s['version']}');
      if (mounted) {
        setState(() {
          _view = v;
          _error = null;
          _loading = false;
        });
      }
    } on SiteError catch (e) {
      if (mounted) {
        setState(() {
          _error = e.message;
          _loading = false;
        });
      }
    }
  }

  List<Map<String, dynamic>> get _routes =>
      (_view?['routes'] as List? ?? const []).whereType<Map<String, dynamic>>().toList();

  Future<void> _open({Map<String, dynamic>? route, String? newName}) async {
    final s = _sel!;
    await Navigator.push(
        context,
        MaterialPageRoute<void>(
            builder: (_) => SiteRouteEditorPage(
                api: widget.api,
                mapId: '${s['map_id']}',
                version: '${s['version']}',
                route: route,
                newName: newName,
                allRoutes: _routes)));
    if (mounted) await _loadRoutes();
  }

  Future<void> _new() async {
    final got = await showFieldsDialog(context,
        title: tr('New route', '新路线'),
        fields: [
          DialogField(tr('Route name (letters, digits, . _ -)', '路线名（字母、数字、. _ -）'),
              key: const Key('route-new-name'))
        ],
        confirm: tr('Create', '建'),
        confirmKey: const Key('route-new-go'));
    if (got == null || !mounted) return;
    final name = got[0].trim();
    if (!routeNameRe.hasMatch(name)) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(tr('Route name: letters, digits, . _ - only (1–64 characters)',
              '路线名只许字母、数字、. _ -（1–64 个字符）'))));
      return;
    }
    if (_routes.any((r) => r['id'] == name)) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(tr('A route with this name already exists', '已经有这个名字的路线'))));
      return;
    }
    await _open(newName: name);
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final admin = widget.api.session?.canManageRoutes ?? false;
    final sub = NwText.bodySmall.copyWith(color: c.textSecondary);
    return Scaffold(
        appBar: AppBar(title: Text(tr('Patrol routes', '巡检路线'))),
        body: _loading
            ? const Center(child: CircularProgressIndicator())
            : NwListView(children: [
                if (_error != null) NwNotice(tone: NwTone.error, text: tr('Routes unavailable: $_error', '路线读不到：$_error')),
                if (_maps.isEmpty && _error == null)
                  NwEmpty(icon: Icons.map_outlined, text: tr('No maps on the site yet', '站点上还没有图'))
                else if (_sel != null) ...[
                  Row(children: [
                    Text(tr('Map version', '地图版本'), style: sub),
                    const SizedBox(width: Nw.s8),
                    Expanded(
                        child: DropdownButton<String>(
                            key: SiteRoutesPage.mapPickKey,
                            isExpanded: true,
                            value: '${_sel!['map_id']}:${_sel!['version']}',
                            items: [
                              for (final m in _maps)
                                DropdownMenuItem(
                                    value: '${m['map_id']}:${m['version']}',
                                    child: Text('${m['map_id']} · v${m['version']}')),
                            ],
                            onChanged: (v) {
                              if (v == null) return;
                              setState(() => _sel = _maps.firstWhere((m) => '${m['map_id']}:${m['version']}' == v));
                              _loadRoutes();
                            })),
                  ]),
                  nwGap8,
                  if (admin)
                    NwButton(
                        buttonKey: SiteRoutesPage.newKey,
                        label: tr('New route', '新路线'),
                        icon: Icons.add_rounded,
                        onPressed: _new),
                  nwGap16,
                  if (_routes.isEmpty && _error == null)
                    NwEmpty(
                        key: SiteRoutesPage.emptyKey,
                        icon: Icons.route_rounded,
                        text: tr('No routes on this map version yet', '这一版地图上还没有路线'))
                  else
                    for (final r in _routes)
                      Padding(
                          padding: const EdgeInsets.only(bottom: Nw.s8),
                          child: NwCard(
                              key: SiteRoutesPage.routeKey('${r['id']}'),
                              radius: Nw.r8,
                              padding: const EdgeInsets.symmetric(horizontal: Nw.s16, vertical: Nw.s8),
                              onTap: () => _open(route: r),
                              child: Row(children: [
                                Expanded(
                                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                                  Text('${r['id']}',
                                      style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700)),
                                  nwGap4,
                                  Text(
                                      tr('${(r['waypoints'] as List? ?? const []).length} points · revision ${r['revision']}',
                                          '${(r['waypoints'] as List? ?? const []).length} 个点 · 第 ${r['revision']} 版'),
                                      style: sub),
                                ])),
                                Icon(Icons.chevron_right_rounded, size: 24, color: c.textSecondary),
                              ]))),
                ],
              ]));
  }
}

// ------------------------------------------------------------------ 编辑页

enum _Mode { add, move }

class SiteRouteEditorPage extends StatefulWidget {
  final SiteApi api;
  final String mapId;
  final String version;

  /// 站点上的这一条（`GET /api/routes` 里的一项）；新建的是 null，给 [newName]。
  final Map<String, dynamic>? route;
  final String? newName;
  final List<Map<String, dynamic>> allRoutes;
  const SiteRouteEditorPage(
      {super.key,
      required this.api,
      required this.mapId,
      required this.version,
      this.route,
      this.newName,
      this.allRoutes = const []});

  static const Key canvasKey = Key('route-canvas');
  static const Key saveKey = Key('route-save');
  static const Key deleteKey = Key('route-delete');
  static const Key deleteOkKey = Key('route-delete-ok');
  static const Key sendKey = Key('route-send');
  static const Key addModeKey = Key('route-mode-add');
  static const Key moveModeKey = Key('route-mode-move');
  static const Key nameKey = Key('route-pt-name');
  static const Key dwellKey = Key('route-pt-dwell');
  static const Key photoFrontKey = Key('route-pt-photo-front');
  static const Key photoBackKey = Key('route-pt-photo-back');
  static const Key lightKey = Key('route-pt-light');
  static const Key checkKey = Key('route-pt-check');
  static const Key yawKey = Key('route-pt-yaw');
  static const Key faceNextKey = Key('route-pt-face-next');
  static const Key messageKey = Key('route-message');
  static const Key reloadKey = Key('route-reload');
  static Key pointKey(int i) => Key('route-pt-$i');
  static Key upKey(int i) => Key('route-up-$i');
  static Key downKey(int i) => Key('route-down-$i');
  static Key removeKey(int i) => Key('route-rm-$i');
  static Key rowKey(int i) => Key('route-row-$i');

  @override
  State<SiteRouteEditorPage> createState() => _SiteRouteEditorPageState();
}

class _SiteRouteEditorPageState extends State<SiteRouteEditorPage> {
  late final Future<(Map<String, dynamic>, Uint8List, bool)> _data = _load();
  final TransformationController _zoom = TransformationController();
  late String _id;
  late int _revision;
  late Map<String, dynamic> _policy;
  List<RoutePt> _pts = [];
  String _saved = '[]';
  int? _sel;
  _Mode _mode = _Mode.add;
  final Map<int, String> _problems = {};
  String? _msg;
  bool _conflict = false;
  bool _busy = false;
  int _formGen = 0;

  bool get _isNew => widget.route == null;
  bool get _canEdit => widget.api.session?.canManageRoutes ?? false;
  bool get _canSend => widget.api.session?.canDispatch ?? false;
  String get _snapshot => jsonEncode([for (final p in _pts) p.toWire()]);
  bool get _dirty => _snapshot != _saved;

  @override
  void initState() {
    super.initState();
    _adopt(widget.route);
  }

  void _adopt(Map<String, dynamic>? r) {
    _id = r == null ? widget.newName ?? '' : '${r['id']}';
    _revision = r == null ? 0 : (r['revision'] as num).toInt();
    _policy = r == null ? <String, dynamic>{} : ((r['policy'] as Map?) ?? const {}).cast<String, dynamic>();
    _pts = [
      for (final w in (r?['waypoints'] as List? ?? const [])) if (w is Map) RoutePt.fromWire(w.cast<String, dynamic>())
    ];
    _saved = _snapshot;
    _sel = null;
    _problems.clear();
    _conflict = false;
    _formGen++;
  }

  Future<(Map<String, dynamic>, Uint8List, bool)> _load() async {
    final meta = await widget.api.mapPreview(widget.mapId, widget.version);
    final (png, plan) = await loadMapBase(widget.api, widget.mapId, widget.version, MapGeo.fromPreview(meta));
    return (meta, png, plan);
  }

  @override
  void dispose() {
    _zoom.dispose();
    super.dispose();
  }

  // ---- 图上的点击

  void _tap(Offset metres, double hitMetres) {
    // 点在某个点上：选中它
    int? near;
    var best = hitMetres;
    for (var i = 0; i < _pts.length; i++) {
      final d = (Offset(_pts[i].x, _pts[i].y) - metres).distance;
      if (d <= best) {
        best = d;
        near = i;
      }
    }
    setState(() {
      _msg = null;
      if (near != null) {
        _sel = near;
        _formGen++;
        return;
      }
      if (!_canEdit) return;
      final x = _r2(metres.dx), y = _r2(metres.dy);
      if (_mode == _Mode.move) {
        final i = _sel;
        if (i != null) {
          _pts[i].x = x;
          _pts[i].y = y;
          _problems.clear();
        }
        return;
      }
      var yaw = _pts.isEmpty ? 0.0 : _pts.last.yaw;
      if (_pts.isNotEmpty) {
        final d = Offset(x, y) - Offset(_pts.last.x, _pts.last.y);
        if (d.distance > 1e-6) yaw = math.atan2(d.dy, d.dx);
      }
      _pts.add(RoutePt(nextPointName(_pts), x, y, yaw));
      _sel = _pts.length - 1;
      _formGen++;
      _problems.clear();
    });
  }

  void _swap(int i, int j) => setState(() {
        final p = _pts.removeAt(i);
        _pts.insert(j, p);
        _sel = j;
        _formGen++;
        _problems.clear();
      });

  void _remove(int i) => setState(() {
        _pts.removeAt(i);
        _sel = null;
        _formGen++;
        _problems.clear();
      });

  // ---- 存、删、派

  Future<void> _save() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _msg = null;
      _conflict = false;
      _problems.clear();
    });
    try {
      final r = await widget.api.saveRoute(_id, <String, dynamic>{
        'map_id': widget.mapId,
        'map_version': widget.version,
        'waypoints': [for (final p in _pts) p.toWire()],
        'policy': _policy,
        'base_revision': _revision,
      });
      if (!mounted) return;
      setState(() {
        _revision = (r['revision'] as num).toInt();
        _saved = _snapshot;
        _busy = false;
        _msg = tr('Route saved (revision $_revision)', '路线已保存（第 $_revision 版）');
      });
    } on SiteError catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _conflict = e.status == 409;
        for (final x in (e.body['errors'] as List? ?? const [])) {
          if (x is Map && x['index'] is num) _problems[(x['index'] as num).toInt()] = '${x['problem']}';
        }
        if (_problems.isNotEmpty) _sel = _problems.keys.first;
        _msg = tr('Not saved: ${e.message}', '没存上：${e.message}');
      });
    }
  }

  Future<void> _reload() async {
    try {
      final v = await widget.api.routes(mapId: widget.mapId, version: widget.version);
      final r = (v['routes'] as List? ?? const []).whereType<Map<String, dynamic>>().where((x) => x['id'] == _id);
      if (!mounted) return;
      setState(() {
        if (r.isEmpty) {
          _msg = tr('This route no longer exists', '这条路线已经没有了');
        } else {
          _adopt(r.first);
          _msg = tr('Reloaded from the site', '已从站点重新拉取');
        }
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr('Reload failed: ${e.message}', '拉取失败：${e.message}'));
    }
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
              title: Text(tr('Delete route $_id?', '删除路线 $_id？')),
              content: Text(tr('Schedules that use it must be removed first.', '还在用它的排程要先删掉。')),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
                FilledButton(
                    key: SiteRouteEditorPage.deleteOkKey,
                    onPressed: () => Navigator.pop(c, true),
                    child: Text(tr('Delete', '删除'))),
              ],
            ));
    if (ok != true || !mounted) return;
    try {
      await widget.api.deleteRoute(_id, _revision);
      if (mounted) Navigator.pop(context);
    } on SiteError catch (e) {
      if (mounted) {
        setState(() {
          _conflict = e.status == 409;
          _msg = tr('Not deleted: ${e.message}', '没删掉：${e.message}');
        });
      }
    }
  }

  Future<void> _send() async {
    List<Map<String, dynamic>> robots;
    try {
      robots = (await widget.api.robots()).where((r) => robotCan(r, 'patrol')).toList();
    } on SiteError catch (e) {
      setState(() => _msg = tr('Robot list unavailable: ${e.message}', '拿不到机器人的列表：${e.message}'));
      return;
    }
    if (!mounted) return;
    if (robots.isEmpty) {
      setState(() => _msg = tr('No robot can patrol right now', '现在没有能巡检的机器人'));
      return;
    }
    final id = await showDialog<String>(
        context: context,
        builder: (c) => SimpleDialog(title: Text(tr('Send $_id to which robot', '把 $_id 派给哪台机器人')), children: [
              for (final r in robots)
                SimpleDialogOption(
                    key: Key('route-send-${r['robot_id']}'),
                    onPressed: () => Navigator.pop(c, '${r['robot_id']}'),
                    child: Text('${r['robot_id']}')),
            ]));
    if (id == null || !mounted) return;
    try {
      final r = await widget.api.patrol(id, _id);
      final ack = (r['ack'] as Map?) ?? const {};
      if (mounted) {
        // 只有狗收下了才说派了（外审 W39-I3：没回执、被拒、过期、重复都不算）
        setState(() => _msg = ack['result'] == 'accepted'
            ? tr('Patrol sent to $id', '已派 $id 去巡检')
            : tr('The robot did not accept: ${ack['result']} ${ack['reason'] ?? ''}',
                '机器人没接：${ack['result']} ${ack['reason'] ?? ''}'));
      }
    } on SiteError catch (e) {
      if (mounted) setState(() => _msg = tr('Refused: ${e.message}', '被拒：${e.message}'));
    }
  }

  // ---- 画

  @override
  Widget build(BuildContext context) {
    return Scaffold(
        appBar: AppBar(title: Text(_id)),
        body: FutureBuilder<(Map<String, dynamic>, Uint8List, bool)>(
            future: _data,
            builder: (context, snap) {
              if (snap.hasError) {
                final e = snap.error;
                return SingleChildScrollView(
                    padding: const EdgeInsets.all(16),
                    child: NwNotice(
                        tone: NwTone.warning,
                        text: tr('Preview unavailable: ${e is SiteError ? e.message : e}',
                            '看不了：${e is SiteError ? e.message : e}')));
              }
              if (!snap.hasData) return const Center(child: CircularProgressIndicator());
              final (meta, png, plan) = snap.data!;
              final geo = MapGeo.fromPreview(meta);
              return LayoutBuilder(builder: (context, box) {
                final map = _map(context, geo, png, plan);
                final panel = _panel(context);
                if (box.maxWidth >= 700) {
                  return Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                    Expanded(child: map),
                    SizedBox(width: math.min(380, box.maxWidth * 0.42), child: panel),
                  ]);
                }
                return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  Expanded(flex: 5, child: map),
                  Expanded(flex: 5, child: panel),
                ]);
              });
            }));
  }

  Widget _map(BuildContext context, MapGeo geo, Uint8List png, bool plan) {
    final c = Nw.of(context);
    final w = geo.width, h = geo.height, rad = geo.radians;
    Offset px(double x, double y) => geo.toPx(x, y);
    final r = math.max(2.0, math.max(w, h) / 120);
    final others = [
      for (final o in widget.allRoutes)
        if (o['id'] != _id)
          [
            for (final p in (o['waypoints'] as List? ?? const []))
              if (p is Map) px(_num(p['x']), _num(p['y']))
          ]
    ];
    final line = [for (final p in _pts) px(p.x, p.y)];
    return LayoutBuilder(
        builder: (context, box) => ClipRect(
            child: InteractiveViewer(
                transformationController: _zoom,
                maxScale: 8,
                child: FittedBox(
                    child: NwMapRotation(
                        rad: rad,
                        size: Size(w, h),
                        child: GestureDetector(
                            key: SiteRouteEditorPage.canvasKey,
                            onTapUp: (d) {
                              // 点下去的位置已经按图的转角、缩放倒回图的像素；命中半径约 18 个屏上像素
                              final fit = geo.fitScale(box.biggest);
                              final z = _zoom.value.getMaxScaleOnAxis();
                              final hitPx = 18 / math.max(1e-6, fit * z);
                              _tap(geo.toMap(d.localPosition), hitPx * geo.mPerPx);
                            },
                            child: SizedBox(
                                width: w,
                                height: h,
                                child: Stack(clipBehavior: Clip.none, children: [
                                  SizedBox(width: w, height: h, child: mapBaseImage(context, png, plan: plan)),
                                  CustomPaint(
                                      size: Size(w, h),
                                      painter: _RoutePainter(
                                          others: others,
                                          line: line,
                                          color: c.tertiary,
                                          otherColor: c.textSecondary,
                                          width: r / 2)),
                                  // 路线的点：跟网页一样的定位箭头（pose_marker.dart），中间是序号，三角尖指着到点后的
                                  // 朝向；字跟着转回正
                                  for (var i = 0; i < _pts.length; i++)
                                    Positioned(
                                        left: line[i].dx - 2.2 * r,
                                        top: line[i].dy - 2.2 * r,
                                        child: IgnorePointer(
                                            child: SizedBox(
                                                key: SiteRouteEditorPage.pointKey(i),
                                                width: 4.4 * r,
                                                height: 4.4 * r,
                                                child: CustomPaint(
                                                    painter: PoseMarkerPainter(
                                                        at: Offset(2.2 * r, 2.2 * r),
                                                        heading: -_pts[i].yaw,
                                                        r: 2.2 * r,
                                                        label: '${i + 1}',
                                                        labelAngle: -rad,
                                                        selected: i == _sel,
                                                        accent: _problems.containsKey(i) ? c.error : c.tertiary,
                                                        surface: c.surface,
                                                        text: c.text))))),
                                ]))))))));
  }

  Widget _panel(BuildContext context) {
    final c = Nw.of(context);
    final sub = NwText.bodySmall.copyWith(color: c.textSecondary);
    final edit = _canEdit;
    final sel = _sel != null && _sel! < _pts.length ? _pts[_sel!] : null;
    return Material(
        color: c.surface,
        child: ListView(padding: const EdgeInsets.all(Nw.s16), children: [
          Text(tr('Points are measured on map version ${widget.version}. A new map version needs its own route.',
              '这些点是在地图第 ${widget.version} 版上量的。换了新版地图要重新画一条。'),
              style: sub),
          nwGap8,
          if (edit)
            NwSegment<_Mode>(
                values: _Mode.values,
                selected: _mode,
                onChanged: (v) => setState(() => _mode = v),
                labelOf: (v) => v == _Mode.add ? tr('Add points', '加点') : tr('Move point', '挪点'),
                keyOf: (v) => v == _Mode.add ? SiteRouteEditorPage.addModeKey : SiteRouteEditorPage.moveModeKey),
          if (edit)
            Padding(
                padding: const EdgeInsets.only(top: Nw.s4),
                child: Text(
                    _mode == _Mode.add
                        ? tr('Tap the map to add a point. Tap a point to select it.', '点地图加点；点一个点选中它。')
                        : tr('Select a point, then tap its new place on the map.', '先选中一个点，再点它的新位置。'),
                    style: sub)),
          nwGap8,
          if (_pts.isEmpty) Text(tr('No points yet. Tap the map.', '还没有点，点一下地图。'), style: sub),
          for (var i = 0; i < _pts.length; i++)
            Container(
                key: SiteRouteEditorPage.rowKey(i),
                decoration: BoxDecoration(
                    color: i == _sel ? c.infoSoft : null, border: Border(top: BorderSide(color: c.outline))),
                child: Row(children: [
                  Expanded(
                      child: InkWell(
                          onTap: () => setState(() {
                                _sel = i;
                                _formGen++;
                              }),
                          child: Padding(
                              padding: const EdgeInsets.symmetric(vertical: Nw.s8),
                              child: Text('${i + 1}. ${_pts[i].name}${_problems.containsKey(i) ? '  !' : ''}',
                                  style: NwText.bodyMedium.copyWith(
                                      color: _problems.containsKey(i) ? c.error : c.text))))),
                  if (edit) ...[
                    IconButton(
                        key: SiteRouteEditorPage.upKey(i),
                        tooltip: tr('Move up', '上移'),
                        icon: const Icon(Icons.arrow_upward_rounded),
                        onPressed: i == 0 ? null : () => _swap(i, i - 1)),
                    IconButton(
                        key: SiteRouteEditorPage.downKey(i),
                        tooltip: tr('Move down', '下移'),
                        icon: const Icon(Icons.arrow_downward_rounded),
                        onPressed: i == _pts.length - 1 ? null : () => _swap(i, i + 1)),
                    IconButton(
                        key: SiteRouteEditorPage.removeKey(i),
                        tooltip: tr('Remove point', '删除这个点'),
                        icon: const Icon(Icons.close_rounded),
                        onPressed: () => _remove(i)),
                  ],
                ])),
          if (_problems.isNotEmpty) ...[
            nwGap8,
            for (final e in _problems.entries)
              Text('${e.key + 1}. ${e.key < _pts.length ? _pts[e.key].name : ''}: ${e.value}',
                  style: NwText.bodySmall.copyWith(color: c.error)),
          ],
          if (sel != null) _pointForm(context, sel),
          nwGap16,
          if (_msg != null)
            Text(_msg!, key: SiteRouteEditorPage.messageKey, style: NwText.bodySmall.copyWith(color: c.text)),
          if (_conflict)
            TextButton(
                key: SiteRouteEditorPage.reloadKey,
                onPressed: _reload,
                child: Text(tr('Reload from the site (your edits are lost)', '从站点重新拉取（你的修改会丢）'))),
          nwGap8,
          Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
            if (edit)
              NwButton(
                  buttonKey: SiteRouteEditorPage.saveKey,
                  label: tr('Save route', '保存路线'),
                  onPressed: _busy || _pts.isEmpty || !(_isNew || _dirty) ? null : _save),
            if (_canSend && !_isNew)
              NwButton(
                  buttonKey: SiteRouteEditorPage.sendKey,
                  label: tr('Send to a robot', '派给一台机器人'),
                  icon: Icons.near_me_rounded,
                  variant: NwButtonVariant.outline,
                  onPressed: _dirty ? null : _send),
            if (edit && !_isNew)
              NwButton(
                  buttonKey: SiteRouteEditorPage.deleteKey,
                  label: tr('Delete route', '删除路线'),
                  variant: NwButtonVariant.destructive,
                  onPressed: _delete),
          ]),
          if (_dirty && _canSend && !_isNew)
            Padding(
                padding: const EdgeInsets.only(top: Nw.s4),
                child: Text(tr('Save first: a patrol runs the saved route.', '先保存：派出去的是站点上存着的那一条。'),
                    style: sub)),
        ]));
  }

  Widget _pointForm(BuildContext context, RoutePt p) {
    final edit = _canEdit;
    final i = _sel!;
    InputDecoration dec(String label) => InputDecoration(labelText: label, isDense: true);
    return Column(
        key: ValueKey('pf-$i-$_formGen'),
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          nwGap16,
          TextFormField(
              key: SiteRouteEditorPage.nameKey,
              initialValue: p.name,
              enabled: edit,
              maxLength: 64,
              decoration: dec(tr('Point name', '点位名')),
              onChanged: (v) => setState(() => p.name = v)),
          Row(children: [
            Expanded(
                child: TextFormField(
                    key: SiteRouteEditorPage.dwellKey,
                    initialValue: '${p.dwell == p.dwell.roundToDouble() ? p.dwell.toInt() : p.dwell}',
                    enabled: edit,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true),
                    decoration: dec(tr('Dwell (s)', '停留（秒）')),
                    onChanged: (v) {
                      final s = double.tryParse(v);
                      if (s != null && s >= 0) setState(() => p.dwell = s);
                    })),
            const SizedBox(width: Nw.s8),
            Expanded(
                child: TextFormField(
                    key: SiteRouteEditorPage.yawKey,
                    initialValue: yawText(p.yaw),
                    enabled: edit,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
                    decoration: dec(tr('Heading (°)', '朝向（度）')),
                    onChanged: (v) {
                      final d = double.tryParse(v);
                      if (d != null && d.isFinite) setState(() => p.yaw = d * math.pi / 180);
                    })),
          ]),
          if (edit && i < _pts.length - 1)
            Align(
                alignment: Alignment.centerLeft,
                child: TextButton(
                    key: SiteRouteEditorPage.faceNextKey,
                    onPressed: () => setState(() {
                          final n = _pts[i + 1];
                          final d = Offset(n.x, n.y) - Offset(p.x, p.y);
                          if (d.distance > 1e-6) p.yaw = math.atan2(d.dy, d.dx);
                          _formGen++;
                        }),
                    child: Text(tr('Face the next point', '朝向下一个点')))),
          SwitchListTile(
              key: SiteRouteEditorPage.photoFrontKey,
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text(tr('Photo (front)', '拍照（前）')),
              value: p.hasPhoto('front'),
              onChanged: edit ? (v) => setState(() => p.setPhoto('front', v)) : null),
          SwitchListTile(
              key: SiteRouteEditorPage.photoBackKey,
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text(tr('Photo (back)', '拍照（后）')),
              value: p.hasPhoto('back'),
              onChanged: edit ? (v) => setState(() => p.setPhoto('back', v)) : null),
          SwitchListTile(
              key: SiteRouteEditorPage.lightKey,
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text(tr('Light on', '开灯')),
              value: p.light,
              onChanged: edit ? (v) => setState(() => p.light = v) : null),
          TextFormField(
              key: SiteRouteEditorPage.checkKey,
              initialValue: p.check,
              enabled: edit,
              maxLength: 500,
              decoration: dec(tr('What to check here', '这里要看什么')),
              onChanged: (v) => setState(() => p.check = v)),
        ]);
  }
}

/// 朝向的度数文字（一位小数，整数不带小数点）。
String yawText(double yaw) {
  final d = (yaw * 180 / math.pi * 10).roundToDouble() / 10;
  return d == d.roundToDouble() ? '${d.toInt()}' : '$d';
}

class _RoutePainter extends CustomPainter {
  final List<List<Offset>> others;
  final List<Offset> line;
  final Color color;
  final Color otherColor;
  final double width;
  const _RoutePainter(
      {required this.others, required this.line, required this.color, required this.otherColor, required this.width});

  @override
  void paint(Canvas canvas, Size size) {
    void poly(List<Offset> pts, Color col, double w) {
      if (pts.length < 2) return;
      final path = Path()..moveTo(pts.first.dx, pts.first.dy);
      for (final p in pts.skip(1)) {
        path.lineTo(p.dx, p.dy);
      }
      canvas.drawPath(
          path,
          Paint()
            ..color = col
            ..style = PaintingStyle.stroke
            ..strokeWidth = w
            ..strokeJoin = StrokeJoin.round);
    }

    for (final o in others) {
      poly(o, otherColor.withValues(alpha: 0.7), width * 0.7);
    }
    poly(line, color, width);
  }

  @override
  bool shouldRepaint(_RoutePainter old) =>
      old.line != line || old.others != others || old.color != color || old.width != width;
}
