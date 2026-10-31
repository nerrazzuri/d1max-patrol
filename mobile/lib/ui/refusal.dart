/// 派单被站点拒了（W53 设计 D10）：不再一闪而过，弹一个框说**为什么**，再给一个**去处理**的按钮。
///
/// 站点在 409 里带 `reason_code`（十五种常见拒绝）和一句英文 `reason_en`；中文界面照站点原话。按代号给按钮：
/// 叫停了 → 「恢复」；禁行区没确认 → 管理员「去确认」、别人「请管理员确认」；要人监护、没定位、没就绪 → 「去机器人页」。
/// 没带代号的照旧只说原话。
library;

import 'package:flutter/material.dart';

import '../l10n.dart';
import '../net/site_client.dart';
import 'site_mapping.dart';
import 'site_robot.dart';

/// 被拒框里那个按钮（Key 测试用）。
const Key refusalActionKey = Key('refusal-action');
const Key refusalDialogKey = Key('refusal-dialog');

/// 禁行区没确认：从原话里抠出是哪张图哪一版（`estate-1:7 当前的区域修订…`）。
(String, String)? zonesMapOf(String message) {
  final m = RegExp(r'^(\S+?):(\S+?)\s*当前的区域修订').firstMatch(message);
  return m == null ? null : (m.group(1)!, m.group(2)!);
}

/// 按代号给一句「你可以做什么」（没有就空串）。
String refusalHint(String code, SiteSession? s) => switch (code) {
      'held' => (s?.canDispatch ?? false)
          ? tr('Tap Resume once it is safe.', '确认安全后点「恢复」。')
          : tr('Ask a guard or manager to resume it.', '请保安或经理点「恢复」。'),
      'zones_unconfirmed' => (s?.canManageMaps ?? false)
          ? tr('Review the no-go zones on the map and confirm this version.', '请在地图上核对禁行区并确认此版本。')
          : tr('An administrator must confirm the no-go zones first. Remote control is unaffected.',
              '需要管理员先确认禁行区。遥控不受影响。'),
      'supervision' => tr('Turn on "I\'m supervising" on the robot page.', '请在机器人页开启「我在现场监护」。'),
      'not_localized' => tr('Set the position on the robot page.', '请在机器人页「设位置」。'),
      'offline' => tr('Check the robot is powered on and connected.', '请检查机器人是否开机、网络是否连通。'),
      'teleop' => tr('Dispatch after remote control ends.', '请在遥控结束后再派。'),
      'map_mismatch' => tr('Ask an administrator to send the correct map to this robot.', '请管理员为此机器人下发正确的地图。'),
      _ => '',
    };

/// 弹被拒框。[robotId] 给了才有「恢复」「去机器人页」；[onChanged] 在按了「恢复」之后调（页面刷新）。
Future<void> showRefusal(BuildContext context, SiteApi api, SiteError e,
    {String? robotId, String? title, VoidCallback? onChanged}) async {
  final code = '${e.body['reason_code'] ?? ''}';
  final s = api.session;
  final hint = refusalHint(code, s);
  final zones = code == 'zones_unconfirmed' ? zonesMapOf(e.message) : null;
  String? label;
  Future<void> Function(BuildContext)? act;
  void open(BuildContext c, Widget page) =>
      Navigator.push(c, MaterialPageRoute<void>(builder: (_) => page));
  if (code == 'held' && robotId != null && (s?.canDispatch ?? false)) {
    label = tr('Resume', '恢复');
    act = (c) async {
      try {
        await api.resume(robotId);
        onChanged?.call();
      } on SiteError catch (err) {
        if (c.mounted) {
          ScaffoldMessenger.of(c).showSnackBar(SnackBar(content: Text(refusalText(err))));
        }
      }
    };
  } else if (zones != null && (s?.canManageMaps ?? false)) {
    label = tr('Confirm zones', '去确认');
    act = (c) async => open(c, SiteMapPreviewPage(api: api, mapId: zones.$1, version: zones.$2));
  } else if (robotId != null &&
      const {'supervision', 'not_localized', 'not_ready', 'obstacles', 'teleop'}.contains(code)) {
    label = tr('Open robot', '去机器人页');
    act = (c) async => open(c, SiteRobotPage(api: api, robotId: robotId));
  }
  await showDialog<void>(
    context: context,
    builder: (c) => AlertDialog(
      key: refusalDialogKey,
      title: Text(title ?? tr("Dispatch failed", '派出失败')),
      content: Text([refusalText(e), if (hint.isNotEmpty) hint].join('\n\n')),
      actions: [
        TextButton(onPressed: () => Navigator.pop(c), child: Text(tr('OK', '确定'))),
        if (label != null && act != null)
          FilledButton(
              key: refusalActionKey,
              onPressed: () async {
                Navigator.pop(c);
                await act!(context);
              },
              child: Text(label)),
      ],
    ),
  );
}
