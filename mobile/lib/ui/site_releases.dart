/// 站点模式的版本（W00c5d 第三部分，决策 8：版本也由站点管）。
///
/// - 站点上登记过的每一版；每台狗在跑哪一版（狗自己报的）。
/// - 管理员：给一台狗**装**一版（狗后台下载、核对、落槽、建 venv，不切）→ **切**过去（狗要空闲；
///   重启代理，连上站点才算成，起不来自己退回）→ 不对劲就**退**回上一版。
/// - **查**（W00c6d 升级前检查）：切之前看一张清单 —— 狗那边（空闲、电量、盘、装没装、槽里的包对不对、
///   起不起得来、定位器/感知/外参）加站点这边（任务包 schema、备份）。切被狗拒的时候也把清单摆出来。
library;

import 'package:flutter/material.dart';

import '../net/site_client.dart';

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
        builder: (c) => SimpleDialog(title: const Text('哪一版'), children: [
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
      'install': '装',
      'activate': '切到',
      'rollback': '退回上一版',
    }[action];
    if (!mounted) return;
    if (action == 'precheck') {
      try {
        final r = await widget.api.releaseAction(robot, action, name: name);
        if (!mounted) return;
        await _showChecks('$robot 切到 $name 之前', r);
      } on SiteError catch (e) {
        _snack('查不了：$e');
      }
      return;
    }
    if (action != 'install') {
      final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
          title: Text('$robot $what $name'),
          content: const Text('狗要重启代理（几十秒）：跑着任务的时候狗会拒绝。确定？'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('算了')),
            FilledButton(
                key: const Key('rel-confirm'),
                onPressed: () => Navigator.pop(c, true),
                child: const Text('确定')),
          ],
        ),
      );
      if (ok != true) return;
    }
    try {
      final r = await widget.api.releaseAction(robot, action, name: name);
      final ack = (r['ack'] as Map?) ?? const {};
      _snack(ack['result'] == 'accepted'
          ? '$robot 收到了：$what $name（做完它会报，失败站点出告警）'
          : '$robot 没接：${ack['reason'] ?? ack['result']}');
      final data = ack['data'];
      if (mounted && ack['result'] != 'accepted' && data is Map<String, dynamic>) {
        await _showChecks('$robot 没切：为什么', data);          // 狗拒切时回执里带着清单
      }
    } on SiteError catch (e) {
      _snack('没发出去：$e');
    }
  }

  /// 升级前检查的清单：拦住的在前，只是提示的标出来。
  Future<void> _showChecks(String title, Map<String, dynamic> r) {
    final checks = (r['checks'] as List? ?? const []).whereType<Map>().toList()
      ..sort((a, b) => _rank(a).compareTo(_rank(b)));
    final blocking = (r['blocking'] as List? ?? const []).length;
    return showDialog<void>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(title),
        content: SizedBox(
          width: double.maxFinite,
          child: ListView(shrinkWrap: true, children: [
            Text(r['ok'] == true ? '能切：拦的都过了' : '不能切：$blocking 项拦着',
                key: const Key('precheck-verdict'),
                style: TextStyle(color: r['ok'] == true ? Colors.green : Colors.red)),
            for (final ch in checks)
              ListTile(
                key: Key('precheck-item-${ch['name']}'),
                dense: true,
                leading: Icon(ch['ok'] == true ? Icons.check_circle : Icons.cancel,
                    color: ch['ok'] == true
                        ? Colors.green
                        : (ch['blocking'] == true ? Colors.red : Colors.orange)),
                title: Text('${_label('${ch['name']}')}'
                    '${ch['ok'] != true && ch['blocking'] != true ? '（提示，不拦）' : ''}'),
                subtitle: Text('${ch['detail'] ?? ''}'),
              ),
          ]),
        ),
        actions: [TextButton(onPressed: () => Navigator.pop(c), child: const Text('知道了'))],
      ),
    );
  }

  /// 清单项的名字：狗与站点报的是英文键，给人看中文（不认识的原样显示）。
  static String _label(String name) =>
      const {
        'busy': '空闲',
        'battery': '电量',
        'disk': '盘',
        'version': '版本',
        'installed': '装好了',
        'package': '包没坏',
        'schema': '任务包格式',
        'agent_start': '切过去起得来',
        'localizer': '定位器',
        'perception': '感知',
        'extrinsic': '外参',
        'backup': '站点备份',
      }[name] ??
      name;

  /// 排序：拦住的不过项 → 提示的不过项 → 过了的。
  static int _rank(Map ch) => ch['ok'] == true ? 2 : (ch['blocking'] == true ? 0 : 1);

  @override
  Widget build(BuildContext context) {
    final admin = widget.api.session?.canManageMaps ?? false;
    return Scaffold(
      appBar: AppBar(title: const Text('版本')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _data,
        builder: (c, snap) {
          if (snap.hasError) {
            return Center(child: Text('拿不到：${snap.error}', style: const TextStyle(color: Colors.red)));
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
            child: ListView(children: [
              const ListTile(title: Text('每台狗在跑')),
              for (final e in robots.entries)
                ListTile(
                  key: SiteReleasesPage.robotKey(e.key),
                  title: Text(e.key),
                  subtitle: Text('${e.value ?? '不知道（狗没报：不在线，或者不支持站点下发版本）'}'),
                  // 狗没报在跑哪一版 = 没报发布能力（或者不在线）：按了也是 unsupported，不给按钮。
                  trailing: admin && e.value != null
                      ? Wrap(spacing: 4, children: [
                          for (final (a, label) in [
                            ('install', '装'),
                            ('precheck', '查'),
                            ('activate', '切'),
                            ('rollback', '退'),
                          ])
                            TextButton(
                                key: SiteReleasesPage.actionKey(e.key, a),
                                onPressed: () => _act(e.key, a, names),
                                child: Text(label)),
                        ])
                      : null,
                ),
              const Divider(),
              const ListTile(title: Text('站点上的版本')),
              if (rels.isEmpty) const ListTile(title: Text('还没有登记过版本')),
              for (final r in rels)
                ListTile(
                  title: Text('${r['name']}（${r['version']}）'),
                  subtitle: Text('${((r['size'] as num? ?? 0) / 1048576).toStringAsFixed(1)} MB'
                      '${(r['note'] ?? '') != '' ? ' · ${r['note']}' : ''}'),
                ),
            ]),
          );
        },
      ),
    );
  }
}
