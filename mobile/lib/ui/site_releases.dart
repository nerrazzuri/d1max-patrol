/// 站点模式的版本（W00c5d 第三部分，决策 8：版本也由站点管）。
///
/// - 站点上登记过的每一版；每台狗在跑哪一版（狗自己报的）。
/// - 管理员：给一台狗**装**一版（狗后台下载、核对、落槽、建 venv，不切）→ **切**过去（狗要空闲；
///   重启代理，连上站点才算成，起不来自己退回）→ 不对劲就**退**回上一版。
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
    final what = {'install': '装', 'activate': '切到', 'rollback': '退回上一版'}[action];
    if (!mounted) return;
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
    } on SiteError catch (e) {
      _snack('没发出去：$e');
    }
  }

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
                  subtitle: Text('${e.value ?? '不知道（狗没报）'}'),
                  trailing: admin
                      ? Wrap(spacing: 4, children: [
                          for (final (a, label) in [
                            ('install', '装'),
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
