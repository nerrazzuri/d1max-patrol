/// 填几个字的对话框。**输入框的控制器归对话框自己**，关掉（连退场动画）之后才释放 ——
/// 调用方在 `showDialog` 返回后就释放的话，退场动画还在用它（W00c5d 第二部分内部评审）。
library;

import 'package:flutter/material.dart';

import '../../l10n.dart';
import '../nw/nw_button.dart';
import '../nw/nw_theme.dart';

class DialogField {
  final Key? key;
  final String label;
  final String initial;
  const DialogField(this.label, {this.key, this.initial = ''});
}

/// 按确认返回每一栏去掉首尾空白的字；取消返回 null。
Future<List<String>?> showFieldsDialog(
  BuildContext context, {
  required String title,
  required List<DialogField> fields,
  required String confirm,
  Key? confirmKey,
}) {
  return showDialog<List<String>>(
    context: context,
    builder: (_) =>
        _FieldsDialog(title: title, fields: fields, confirm: confirm, confirmKey: confirmKey),
  );
}

class _FieldsDialog extends StatefulWidget {
  final String title;
  final List<DialogField> fields;
  final String confirm;
  final Key? confirmKey;
  const _FieldsDialog(
      {required this.title, required this.fields, required this.confirm, this.confirmKey});

  @override
  State<_FieldsDialog> createState() => _FieldsDialogState();
}

class _FieldsDialogState extends State<_FieldsDialog> {
  late final List<TextEditingController> _ctl = [
    for (final f in widget.fields) TextEditingController(text: f.initial),
  ];

  @override
  void dispose() {
    for (final c in _ctl) {
      c.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final nw = Nw.of(context);
    return AlertDialog(
      scrollable: true, // 横屏时键盘占掉一大半高：整个对话框能滚
      backgroundColor: nw.surface,
      elevation: 0,
      shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(Nw.r12), side: BorderSide(color: nw.outline)),
      title: Text(widget.title, style: NwText.titleLarge.copyWith(color: nw.text)),
      content: Column(mainAxisSize: MainAxisSize.min, children: [
        for (var i = 0; i < widget.fields.length; i++)
          Padding(
              padding: EdgeInsets.only(top: i == 0 ? 0 : Nw.s8),
              child: TextField(
                  key: widget.fields[i].key,
                  controller: _ctl[i],
                  style: NwText.bodyMedium.copyWith(color: nw.text),
                  decoration: InputDecoration(labelText: widget.fields[i].label))),
      ]),
      actions: [
        NwButton(
            variant: NwButtonVariant.ghost, onPressed: () => Navigator.pop(context), label: tr('Cancel', '取消')),
        NwButton(
            buttonKey: widget.confirmKey,
            onPressed: () => Navigator.pop(context, [for (final c in _ctl) c.text.trim()]),
            label: widget.confirm),
      ],
    );
  }
}
