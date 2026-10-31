/// 站点模式的版本（W00c5d 第三部分，决策 8：版本也由站点管）。
///
/// - 站点上登记过的每一版；每台狗在跑哪一版（狗自己报的）。
/// - 管理员：给一台狗**装**一版（狗后台下载、核对、落槽、建 venv，不切）→ **切**过去（狗要空闲；
///   重启代理，连上站点才算成，起不来自己退回）→ 不对劲就**退**回上一版。
/// - **查**（W00c6d 升级前检查）：切之前看一张清单 —— 狗那边（空闲、电量、盘、装没装、槽里的包对不对、
///   起不起得来、定位器/感知/外参）加站点这边（任务包 schema、备份）。切被狗拒的时候也把清单摆出来。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'nw/nw_button.dart';
import 'nw/nw_p4_admin.dart';
import 'nw/nw_theme.dart';
import 'nw/nw_widgets.dart';

class SiteReleasesPage extends StatefulWidget {
  final SiteApi api;
  const SiteReleasesPage({super.key, required this.api});

  static Key robotKey(String id) => Key('rel-robot-$id');
  static Key actionKey(String id, String action) => Key('rel-$action-$id');

  @override
  State<SiteReleasesPage> createState() => _SiteReleasesPageState();
}

class _SiteReleasesPageState extends State<SiteReleasesPage> {
  late Future<Map<String, dynamic>> _data = widget.api.releases();

  void _snack(String t) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));
  }

  Future<String?> _pick(List<String> names) => showDialog<String>(
        context: context,
        builder: (c) => SimpleDialog(title: Text(tr('Which release', '哪一版')), children: [
          for (final n in names)
            SimpleDialogOption(
                key: Key('pick-rel-$n'), onPressed: () => Navigator.pop(c, n), child: Text(n)),
        ]),
      );

  Future<void> _act(String robot, String action, List<String> names) async {
    var name = '';
    if (action != 'rollback') {
      final got = await _pick(names);
      if (got == null) return;
      name = got;
    }
    final what = {
      'install': tr('install', '安装'),
      'activate': tr('activate', '启用'),
      'rollback': tr('Roll back to the previous release', '退回上一版'),
    }[action];
    if (!mounted) return;
    if (action == 'precheck') {
      try {
        final r = await widget.api.releaseAction(robot, action, name: name);
        if (!mounted) return;
        await _showChecks(tr('Before activating $name on $robot', '$robot 切到 $name 之前'), r);
      } on SiteError catch (e) {
        _snack(tr('Check failed: $e', '检查失败：$e'));
      }
      return;
    }
    if (action != 'install') {
      final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
          title: Text(tr('$robot: $what $name', '$robot $what $name')),
          content: Text(tr(
              'The robot restarts its agent (tens of seconds) and refuses while a task is running. Continue?',
              '机器人需重启代理（约数十秒），执行任务时会拒绝。确定继续？')),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(c, false), child: Text(tr('Cancel', '取消'))),
            FilledButton(
                key: const Key('rel-confirm'),
                onPressed: () => Navigator.pop(c, true),
                child: Text(tr('Confirm', '确定'))),
          ],
        ),
      );
      if (ok != true) return;
    }
    try {
      final r = await widget.api.releaseAction(robot, action, name: name);
      final ack = (r['ack'] as Map?) ?? const {};
      _snack(ack['result'] == 'accepted'
          ? tr('$robot accepted: $what $name (it reports when done; the site raises an alarm on failure)',
              '$robot 已接受：$what $name（完成后上报，失败时站点告警）')
          : tr('$robot did not accept: ${ack['reason'] ?? ack['result']}',
              '$robot 未接受：${ack['reason'] ?? ack['result']}'));
      final data = ack['data'];
      if (mounted && ack['result'] != 'accepted' && data is Map<String, dynamic>) {
        // 狗拒切时回执里带着清单
        await _showChecks(tr('$robot did not activate: reasons', '$robot 未切换：原因'), data);
      }
    } on SiteError catch (e) {
      _snack(tr('Not sent: $e', '发送失败：$e'));
    }
  }

  /// 升级前检查的清单：拦住的在前，只是提示的标出来。
  Future<void> _showChecks(String title, Map<String, dynamic> r) {
    final checks = (r['checks'] as List? ?? const []).whereType<Map>().toList()
      ..sort((a, b) => _rank(a).compareTo(_rank(b)));
    final blocking = (r['blocking'] as List? ?? const []).length;
    return showDialog<void>(
      context: context,
      builder: (c) {
        final nw = Nw.of(c);
        return AlertDialog(
          title: Text(title),
          content: SizedBox(
            width: double.maxFinite,
            child: ListView(shrinkWrap: true, children: [
              // 能切是绿的；不能切是红的（拦着的那几项也是红的，只是提示的是橙的）
              Text(
                  r['ok'] == true
                      ? tr('Can activate: all blocking checks passed', '可以切换：所有阻断检查均已通过')
                      : tr('Cannot activate: $blocking blocking check(s) failed', '不能切换：$blocking 项阻断检查未通过'),
                  key: const Key('precheck-verdict'),
                  style: NwText.titleSmall.copyWith(color: r['ok'] == true ? nw.success : nw.error)),
              nwGap8,
              for (final ch in checks)
                ListTile(
                  key: Key('precheck-item-${ch['name']}'),
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(
                      ch['ok'] == true
                          ? Icons.check_circle_rounded
                          : (ch['blocking'] == true ? Icons.cancel_rounded : Icons.warning_amber_rounded),
                      color: ch['ok'] == true ? nw.success : (ch['blocking'] == true ? nw.error : nw.warning)),
                  title: Text(
                      '${_label('${ch['name']}')}'
                      '${ch['ok'] != true && ch['blocking'] != true ? tr(' (advisory, not blocking)', '（仅提示，不阻断）') : ''}',
                      style: NwText.bodyMedium.copyWith(color: nw.text)),
                  subtitle: Text('${ch['detail'] ?? ''}', style: NwText.bodySmall.copyWith(color: nw.textSecondary)),
                ),
            ]),
          ),
          actions: [TextButton(onPressed: () => Navigator.pop(c), child: Text(tr('OK', '确定')))],
        );
      },
    );
  }

  /// 清单项的名字：狗与站点报的是英文键，给人看中文（不认识的原样显示）。
  static String _label(String name) =>
      {
        'busy': tr('Idle', '空闲'),
        'battery': tr('Battery', '电量'),
        'disk': tr('Disk', '磁盘'),
        'version': tr('Version', '版本'),
        'installed': tr('Installed', '已安装'),
        'package': tr('Package intact', '安装包完整'),
        'schema': tr('Task package format', '任务包格式'),
        'agent_start': tr('Agent starts after activation', '启用后可正常启动'),
        'localizer': tr('Localizer', '定位器'),
        'perception': tr('Perception', '感知'),
        'extrinsic': tr('Extrinsics', '外参'),
        'backup': tr('Site backup', '站点备份'),
      }[name] ??
      name;

  /// 排序：拦住的不过项 → 提示的不过项 → 过了的。
  static int _rank(Map ch) => ch['ok'] == true ? 2 : (ch['blocking'] == true ? 0 : 1);

  @override
  Widget build(BuildContext context) {
    final admin = widget.api.session?.canManageMaps ?? false;
    final nw = Nw.of(context);
    final title = NwText.titleSmall.copyWith(color: nw.text);
    final sub = NwText.bodySmall.copyWith(color: nw.textSecondary);
    return Scaffold(
      appBar: AppBar(title: Text(tr('Releases', '版本'))),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _data,
        builder: (_, snap) {
          if (snap.hasError) {
            return NwListView(children: [
              NwNotice(
                  tone: NwTone.error, text: tr('Releases unavailable: ${snap.error}', '无法加载：${snap.error}')),
            ]);
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final rels = (snap.data!['releases'] as List).whereType<Map<String, dynamic>>().toList();
          final names = [for (final r in rels) '${r['name']}'];
          final robots = (snap.data!['robots'] as Map).cast<String, dynamic>();
          return RefreshIndicator(
            onRefresh: () async {
              setState(() {
                _data = widget.api.releases();
              });
              await _data.catchError((Object _) => <String, dynamic>{});
            },
            child: NwListView(children: [
              NwSectionLabel(tr('Running on each robot', '各机器人运行版本')),
              nwGap8,
              for (final e in robots.entries)
                Padding(
                    padding: const EdgeInsets.only(bottom: Nw.s8),
                    child: NwCard(
                        key: SiteReleasesPage.robotKey(e.key),
                        child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Text(e.key, style: NwText.titleMedium.copyWith(color: nw.text, fontWeight: FontWeight.w700)),
                              nwGap4,
                              Text(
                                  '${e.value ?? tr('Unknown (not reported: the robot is offline, or does not support releases from the site)', '未知（机器人未上报：离线或不支持站点下发版本）')}',
                                  style: NwText.bodyMedium.copyWith(color: nw.textSecondary)),
                              // 狗没报在跑哪一版 = 没报发布能力（或者不在线）：按了也是 unsupported，不给按钮。
                              if (admin && e.value != null) ...[
                                nwGap8,
                                Wrap(spacing: Nw.s8, runSpacing: Nw.s8, children: [
                                  for (final (a, label) in [
                                    ('install', tr('Install', '安装')),
                                    ('precheck', tr('Check', '检查')),
                                    ('activate', tr('Activate', '启用')),
                                    ('rollback', tr('Roll back', '回退')),
                                  ])
                                    NwButton(
                                        buttonKey: SiteReleasesPage.actionKey(e.key, a),
                                        variant: NwButtonVariant.outline,
                                        size: NwButtonSize.small,
                                        onPressed: () => _act(e.key, a, names),
                                        label: label),
                                ]),
                              ],
                            ]))),
              nwGap16,
              NwSectionLabel(tr('Releases on the site', '站点上的版本')),
              nwGap8,
              NwListCard(children: [
                if (rels.isEmpty) NwCardText(tr('No releases registered yet', '暂无已登记版本')),
                for (final r in rels)
                  ListTile(
                    leading: Icon(Icons.inventory_2_rounded, color: nw.textSecondary),
                    title: Text(tr('${r['name']} (${r['version']})', '${r['name']}（${r['version']}）'), style: title),
                    subtitle: Text(
                        '${((r['size'] as num? ?? 0) / 1048576).toStringAsFixed(1)} MB'
                        '${(r['note'] ?? '') != '' ? ' · ${r['note']}' : ''}',
                        style: sub),
                  ),
              ]),
            ]),
          );
        },
      ),
    );
  }
}
