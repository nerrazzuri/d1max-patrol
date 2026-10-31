/// 登录后提示 App 有新版本（W55）：底部一条提示带「更新」；点了下载（带进度）、核对、交给系统安装。
/// 站点没开 App 分发、没有更新、问不到：什么都不显示（不打扰值班）。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/app_update.dart';
import '../net/site_client.dart';

const Key appUpdateActionKey = Key('app-update-go');

Future<void> offerAppUpdate(BuildContext context, AppUpdater u) async {
  Map<String, dynamic>? info;
  try {
    info = await u.check();
  } on Object {
    return;
  }
  if (info == null || !context.mounted) return;
  final v = '${info['version'] ?? ''}';
  final found = info;
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(
    duration: const Duration(seconds: 12),
    content: Text(tr('A new app version $v is available', '有新版本 App：$v')),
    action: SnackBarAction(
        key: appUpdateActionKey, label: tr('Update', '更新'), onPressed: () => runAppUpdate(context, u, found)),
  ));
}

/// 下载（带进度）、核对、交给系统安装；最后底部一条说结果。
Future<void> runAppUpdate(BuildContext context, AppUpdater u, Map<String, dynamic> info) async {
  final progress = ValueNotifier<double>(0);
  final done = u.downloadAndInstall(info, progress: (p) => progress.value = p);
  showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (c) => AlertDialog(
            title: Text(tr('Downloading ${info['version']}', '正在下载 ${info['version']}')),
            content: ValueListenableBuilder<double>(
                valueListenable: progress,
                builder: (_, p, _) => Column(mainAxisSize: MainAxisSize.min, children: [
                      LinearProgressIndicator(value: p > 0 ? p : null),
                      const SizedBox(height: 8),
                      Text('${(p * 100).round()}%'),
                    ])),
          ));
  String msg;
  try {
    msg = await done;
  } on SiteError catch (e) {
    msg = refusalText(e);
  } on Object catch (e) {
    msg = tr('Update failed: $e', '更新失败：$e');
  }
  progress.dispose();
  if (!context.mounted) return;
  Navigator.of(context, rootNavigator: true).pop();
  ScaffoldMessenger.of(context)
      .showSnackBar(SnackBar(duration: const Duration(seconds: 10), content: Text(msg)));
}
