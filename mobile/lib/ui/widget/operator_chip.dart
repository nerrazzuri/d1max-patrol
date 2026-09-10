/// 「当前是谁」那一枚常显的 chip，点一下就换人。
///
/// **这枚 chip 是人拍的板 3（挂账 65）自带的两条补偿，不是可选项。**
/// 那块板选的是「姓名落盘记住、可随时改」，接受的代价写在挂账 65 里：
/// **甲忘了切换，乙的操作签在甲名下**。正因为接受了这个代价，两条补偿是硬的：
///
/// 1. **「当前是谁」必须常显** —— 不许埋进二级设置页。人只有一眼看得见屏上
///    挂着谁的名字，才会在那一眼里发现「这不是我」。埋进二级页之后，那一眼
///    没有了，代价就变成了纯亏损。
/// 2. **切换必须一步** —— 点一下这枚 chip 就能改。多一道「设置 → 账户 →
///    编辑 → 保存」，接班的人就不会去改了，而不改正是那笔代价发生的方式。
///
/// **措辞是硬约束（§6.3）。** 狗记下这个名字但**从不核实**
/// （`Session.operatorVerified` 恒为 `false`）。所以这枚 chip 上、编辑框里、
/// 任何地方都不许出现「已登录 / 已认证 / 身份已验证 / 当前用户」这类说法：
/// 它就是一个**自报的署名**。说成登录的那天，一次冒名操作在事后的记录里跟
/// 本人操作长得一模一样。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'dart:async';
import 'dart:developer' as developer;

import 'package:flutter/material.dart';

import '../../net/patrol_client.dart';

/// chip 上那个名字前面的小字。**不是「当前用户」**，见文件头。
const String signHint = '署名';

/// 编辑框上那一句。**不许写成「登录」。**
const String operatorEditHint = '狗记下这个名字，但不核实。换班就在这儿改一下。';

/// 编辑框的标题。
const String operatorEditTitle = '现在是谁在开';

/// 名字空着按保存时说的那一句。
const String operatorEmptyHint = '填一个名字：狗的账上要记下是谁开的。';

/// 常显的署名 chip：显示当前是谁，点一下换人。
class OperatorChip extends StatelessWidget {
  const OperatorChip({
    super.key,
    required this.name,
    required this.onChanged,
    this.client,
    this.dark = false,
  });

  /// 此刻挂在账上的那个名字。
  final String name;

  /// 换成了谁。**落盘是调用方的事** —— 这枚 chip 不认识存储。
  final ValueChanged<String> onChanged;

  /// 给了就顺手把「换人」这件事告诉狗（`PUT /api/operator`，留痕用）。
  ///
  /// **发不出去不回滚。** 名字的家在手机上：热点断着的时候人照样要能换班，
  /// 拿一次网络失败去把界面上的署名弹回上一个人，才是真的会让人签错名。
  final PatrolClient? client;

  /// 深色底（遥控屏那条黑带子）上要换一套颜色。
  final bool dark;

  /// 常显的那一枚。**测试拿它当「不用点开任何东西就看得见」的锚。**
  static const Key chipKey = ValueKey<String>('operator-chip');

  /// 点开之后那个输入框。
  static const Key editKey = ValueKey<String>('operator-edit');

  /// 编辑框上那个「就是我」。
  static const Key saveKey = ValueKey<String>('operator-save');

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    final Color ink = dark ? Colors.white : theme.colorScheme.onSurface;
    final Color faint =
        dark ? Colors.white70 : theme.colorScheme.onSurfaceVariant;
    return InkWell(
      key: chipKey,
      onTap: () => unawaited(_edit(context)),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Icon(Icons.badge_outlined, size: 16, color: faint),
            const SizedBox(width: 4),
            Text('$signHint：',
                style: theme.textTheme.bodySmall?.copyWith(color: faint)),
            // **名字自己一个 `Text`**，屏上找得到它本身 —— 拼进一句长话里的
            // 话，「常显的是这个名字」就只能靠扫文本去猜。
            Text(name,
                style: theme.textTheme.bodyMedium
                    ?.copyWith(color: ink, fontWeight: FontWeight.w600)),
            const SizedBox(width: 4),
            Icon(Icons.edit_outlined, size: 14, color: faint),
          ],
        ),
      ),
    );
  }

  /// 点一下弹出来的那个框。**一步：点开、改、按「就是我」。**
  Future<void> _edit(BuildContext context) async {
    final TextEditingController ctl = TextEditingController(text: name);
    final String? asked = await showDialog<String>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text(operatorEditTitle),
        content: TextField(
          key: editKey,
          controller: ctl,
          autofocus: true,
          decoration: const InputDecoration(
            labelText: '名字',
            helperText: operatorEditHint,
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('算了'),
          ),
          TextButton(
            key: saveKey,
            onPressed: () => Navigator.pop(ctx, ctl.text.trim()),
            child: const Text('就是我'),
          ),
        ],
      ),
    );
    ctl.dispose();
    if (asked == null) return;
    if (asked.isEmpty) {
      // 空名字在狗的账上是「未具名(ref)」，事后谁也说不清那一趟是谁开的。
      if (context.mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text(operatorEmptyHint)));
      }
      return;
    }
    if (asked == name) return;
    onChanged(asked);
    final PatrolClient? c = client;
    if (c == null) return;
    try {
      await c.setOperator(asked);
    } catch (e) {
      // 异常原文只进日志。屏上写 `$e` 给不出下一步该干什么。**界面上的名字
      // 不回滚**，见 [client]。
      developer.log('换人没告诉成狗：$e', name: 'operator_chip');
    }
  }
}
