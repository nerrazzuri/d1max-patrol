/// 地图管理（W54，管理员、工程师）：每张图的版本（显示名、什么时候建的、建图结论、哪台在用），下发给哪台、
/// 删旧版本、把某台退回上一版，新建（开建图向导）。数据是站点的地图管理总览（`/api/admin/maps`）。
/// 录包、拿录包重建这些工程师的活还在原来的地图目录页（最下面一行进）。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'map_wizard.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p4_admin.dart' show nwGap8, NwListView;
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';
import 'site_mapping.dart' show SiteMapPreviewPage;
import 'site_maps.dart' show SiteMapsPage;

class MapManagePage extends StatefulWidget {
  final SiteApi api;
  const MapManagePage({super.key, required this.api});

  static const Key newKey = Key('maps-new');
  static Key versionKey(String m, String v) => Key('mapver-$m-$v');
  static Key viewKey(String m, String v) => Key('mapver-view-$m-$v');
  static Key sendKey(String m, String v) => Key('mapver-send-$m-$v');
  static Key deleteKey(String m, String v) => Key('mapver-delete-$m-$v');
  static Key rollbackKey(String r) => Key('map-rollback-$r');
  static const Key bagsKey = Key('maps-bags');

  @override
  State<MapManagePage> createState() => _MapManagePageState();
}

String _when(Object? ms) {
  if (ms is! num) return '';
  final d = DateTime.fromMillisecondsSinceEpoch(ms.toInt());
  String two(int n) => n.toString().padLeft(2, '0');
  return '${d.year}-${two(d.month)}-${two(d.day)} ${two(d.hour)}:${two(d.minute)}';
}

class _MapManagePageState extends State<MapManagePage> {
  SiteApi get api => widget.api;
  Map<String, dynamic>? _ov;
  bool _wizardActive = false;
  String _err = '';

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final got = await Future.wait([api.mapsOverview(), api.mapWizard()]);
      if (!mounted) return;
      setState(() {
        _ov = got[0];
        _wizardActive = got[1]['wizard'] is Map;
        _err = '';
      });
    } on SiteError catch (e) {
      if (mounted) setState(() => _err = refusalText(e));
    }
  }

  void _snack(String t) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(t)));
  }

  Future<void> _send(String mapId, String version) async {
    final robots = [
      for (final r in (_ov?['robots'] as List? ?? const []))
        if (r is Map && r['can_activate'] == true) '${r['robot_id']}'
    ];
    if (robots.isEmpty) {
      _snack(tr('No robot online can switch maps right now', '现在没有在线、能换图的机器人'));
      return;
    }
    final robot = await showDialog<String>(
      context: context,
      builder: (c) => SimpleDialog(title: Text(tr('Send to which robot', '下发给哪台机器人')), children: [
        for (final r in robots)
          SimpleDialogOption(key: Key('send-to-$r'), onPressed: () => Navigator.pop(c, r), child: Text(r)),
      ]),
    );
    if (robot == null || !mounted) return;
    try {
      Map<String, dynamic> r;
      try {
        r = await api.activateMap(robot, mapId, version);
      } on SiteError catch (e) {
        if (e.status != 409 || e.body['reason'] != 'no_home' || !mounted) rethrow;
        final go = await showDialog<bool>(
          context: context,
          builder: (c) => AlertDialog(
            title: Text(tr('$robot has no origin on this version', '$robot 在这一版上还没有原点')),
            content: Text(tr(
                'After loading it the robot takes no patrols until you drive it in front of the dock and tap "Mark origin here" on its page.',
                '换上以后机器人不接巡逻，要把它开到充电桩前，在机器人页点「在这儿标原点」。')),
            actions: [
              TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
              FilledButton(
                  key: const Key('send-without-home'),
                  onPressed: () => Navigator.pop(c, true),
                  child: Text(tr('Send anyway', '照样下发'))),
            ],
          ),
        );
        if (go != true) return;
        r = await api.activateMap(robot, mapId, version, withoutHome: true);
      }
      final ack = (r['ack'] as Map?) ?? const {};
      _snack(ack['result'] == 'accepted'
          ? tr('$robot is downloading and loading the map', '$robot 在下载、换图')
          : tr('The robot did not accept: ${ack['reason'] ?? ack['result']}', '机器人没接：${ack['reason'] ?? ack['result']}'));
    } on SiteError catch (e) {
      _snack(refusalText(e));
    }
    await _load();
  }

  Future<void> _delete(String mapId, String version) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Delete this version?', '删掉这一版？')),
        content: Text(tr("It can't be undone. Robots aren't using it.", '删了就没了。没有机器人在用它。')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
          FilledButton(key: const Key('delete-ok'), onPressed: () => Navigator.pop(c, true), child: Text(tr('Delete', '删'))),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await api.deleteMapVersion(mapId, version);
    } on SiteError catch (e) {
      _snack(refusalText(e));
    }
    await _load();
  }

  Future<void> _rollback(String robot, Map rb) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(tr('Switch $robot back to ${rb['map_id']}:${rb['version']}?', '把 $robot 退回 ${rb['map_id']}:${rb['version']}？')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '算了'))),
          FilledButton(key: const Key('rollback-ok'), onPressed: () => Navigator.pop(c, true), child: Text(tr('Switch back', '退回'))),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await api.rollbackMap(robot);
      _snack(tr('$robot is switching back', '$robot 在换回上一版'));
    } on SiteError catch (e) {
      _snack(refusalText(e));
    }
    await _load();
  }

  Widget _version(NwColors c, String mapId, Map<String, dynamic> v) {
    final version = '${v['version']}';
    final note = '${v['note'] ?? ''}';
    final q = (v['quality'] as Map?) ?? const {};
    final verdict = '${q['verdict'] ?? 'unknown'}';
    final reasons = [for (final r in (q['reasons'] as List? ?? const [])) qualityReasonText('$r')];
    final on = [for (final r in (v['loaded_on'] as List? ?? const [])) '$r'];
    final path = q['path_m'];
    final canDelete = on.isEmpty && v['latest'] != true;
    return Padding(
      padding: const EdgeInsets.only(bottom: Nw.s8),
      child: NwCard(
        key: MapManagePage.versionKey(mapId, version),
        tone: on.isNotEmpty ? NwTone.success : NwTone.neutral,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(
                child: Text(note.isNotEmpty ? '$note · $version' : version,
                    style: NwText.titleSmall.copyWith(color: c.text, fontWeight: FontWeight.w700))),
            if (verdict != 'unknown') NwStatusPill(label: verdictText(verdict), tone: verdictTone(verdict)),
            if (on.isNotEmpty) ...[
              const SizedBox(width: Nw.s4),
              NwStatusPill(label: tr('In use: ${on.join(', ')}', '${on.join('、')} 在用'), tone: NwTone.info),
            ],
          ]),
          const SizedBox(height: 2),
          Text(
              [
                _when(v['created_ms']),
                if (path is num) tr('walked ${path.round()} m', '走了 ${path.round()} 米'),
                ...reasons,
              ].where((s) => s.isNotEmpty).join(' · '),
              style: NwText.bodySmall.copyWith(color: c.textSecondary)),
          const SizedBox(height: Nw.s8),
          Wrap(spacing: Nw.s8, runSpacing: Nw.s4, children: [
            NwButton(
                buttonKey: MapManagePage.viewKey(mapId, version),
                label: tr('View', '看图'),
                size: NwButtonSize.small,
                variant: NwButtonVariant.outline,
                onPressed: () => Navigator.push(context,
                    MaterialPageRoute<void>(builder: (_) => SiteMapPreviewPage(api: api, mapId: mapId, version: version)))),
            NwButton(
                buttonKey: MapManagePage.sendKey(mapId, version),
                label: tr('Send to…', '下发给…'),
                size: NwButtonSize.small,
                variant: NwButtonVariant.outline,
                onPressed: () => _send(mapId, version)),
            if (canDelete)
              NwButton(
                  buttonKey: MapManagePage.deleteKey(mapId, version),
                  label: tr('Delete', '删掉'),
                  size: NwButtonSize.small,
                  variant: NwButtonVariant.ghost,
                  onPressed: () => _delete(mapId, version)),
          ]),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final c = Nw.of(context);
    final ov = _ov;
    final maps = [for (final m in (ov?['maps'] as List? ?? const [])) if (m is Map) m.cast<String, dynamic>()];
    final robots = [for (final r in (ov?['robots'] as List? ?? const [])) if (r is Map) r.cast<String, dynamic>()];
    return Scaffold(
      appBar: AppBar(title: Text(tr('Map management', '地图管理'))),
      body: RefreshIndicator(
        onRefresh: _load,
        child: NwListView(children: [
          NwButton(
              buttonKey: MapManagePage.newKey,
              label: _wizardActive
                  ? tr('Continue mapping', '接着建图')
                  : (maps.isEmpty ? tr('Build the first map', '建第一张图') : tr('Build a new version', '重新建一版')),
              icon: Icons.add_location_alt_rounded,
              fullWidth: true,
              size: NwButtonSize.large,
              onPressed: () async {
                await openMapWizard(context, api);
                await _load();
              }),
          if (_err.isNotEmpty) ...[nwGap8, NwNotice(tone: NwTone.error, text: _err)],
          if (ov == null && _err.isEmpty) const Padding(padding: EdgeInsets.all(Nw.s24), child: Center(child: CircularProgressIndicator())),
          for (final m in maps) ...[
            const SizedBox(height: Nw.s16),
            NwSectionLabel(tr('${m['map_id']} · ${(m['versions'] as List? ?? const []).length} version(s)',
                '${m['map_id']} · ${(m['versions'] as List? ?? const []).length} 个版本')),
            nwGap8,
            for (final v in (m['versions'] as List? ?? const []))
              if (v is Map) _version(c, '${m['map_id']}', v.cast<String, dynamic>()),
          ],
          if (robots.isNotEmpty) ...[
            const SizedBox(height: Nw.s16),
            NwSectionLabel(tr('Which version each robot uses', '机器人在用哪一版')),
            nwGap8,
            NwCard(
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                for (final r in robots)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: Nw.s4),
                    child: Row(children: [
                      Expanded(
                          child: Text(
                              '${r['robot_id']}  ${r['loaded'] is Map ? '${r['loaded']['map_id']}:${r['loaded']['version']}' : tr('(no map)', '（没有图）')}',
                              style: NwText.bodyMedium.copyWith(color: c.text))),
                      if (r['rollback'] is Map)
                        NwButton(
                            buttonKey: MapManagePage.rollbackKey('${r['robot_id']}'),
                            label: tr('Back to ${r['rollback']['version']}', '退回 ${r['rollback']['version']}'),
                            size: NwButtonSize.small,
                            variant: NwButtonVariant.outline,
                            onPressed: () => _rollback('${r['robot_id']}', r['rollback'] as Map)),
                    ]),
                  ),
              ]),
            ),
          ],
          const SizedBox(height: Nw.s16),
          ListTile(
            key: MapManagePage.bagsKey,
            leading: Icon(Icons.fiber_smart_record_rounded, color: c.textSecondary),
            title: Text(tr('Recordings and rebuild (engineer)', '录包、拿录包重建（工程师）'), style: NwText.bodyMedium.copyWith(color: c.text)),
            trailing: Icon(Icons.chevron_right_rounded, color: c.textSecondary),
            onTap: () => Navigator.push(context, MaterialPageRoute<void>(builder: (_) => SiteMapsPage(api: api))),
          ),
        ]),
      ),
    );
  }
}
