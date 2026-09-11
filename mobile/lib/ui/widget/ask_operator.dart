/// 问一句「现在是谁在开」的那个框。**全 app 只有这一份。**
///
/// 以前这件事写了两遍：`roster_page._operatorFor`（第一次连狗时问）和
/// `operator_chip._edit`（换班时改）。两份各自有一套
/// `TextEditingController` + `AlertDialog` + `labelText: '名字'` + helperText
/// + `trim()` + 空名字提示 + `dispose()`，**只有按钮上那几个字该不一样**
/// （「连上」/「就是我」）。
///
/// 分成两份的代价当场就兑现了（终审报告必修 5）：狗那头
/// (`app/identity.py::clean_operator`) 会把中间的连续空白压成一个、把不可
/// 打印字符换成空格、超长截断；**chip 那一份认了狗回来的规范名，名册那一份
/// 没认**。于是人第一次输「老  王」（中间两个空格），手机盘上和屏上留的是
/// 「老  王」，狗的审计环里从 unlock 那一刻起记的是「老 王」—— 事后拿手机
/// 那份跟审计对账，靠的就是字符串相等，而它们不相等。
///
/// 所以合并这件事**不是顺手整理**：两份各写一遍规范化处理，下次还会分家。
/// 措辞、Key、空名字那道守卫，从此只有一个出处。
///
/// **网络那一步不在这儿。** 第一次连狗走的是 `unlock`，换班走的是
/// `PUT /api/operator`，两条路回的规范名要各自由调用方接住 —— 这个框只管
/// 「人输了什么」，它不认识 `PatrolClient`。
///
/// **措辞是硬约束（§6.3）。** 狗记下这个名字但**从不核实**，所以这个框里
/// 不许出现「已登录 / 已认证 / 身份已验证 / 当前用户」这类说法。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符，
/// 中文只留在注释、docstring 和字符串字面量里。
library;

import 'package:flutter/material.dart';

/// 第一次连这只狗时框上的标题。
///
/// **狗记下这个名字但不核实**（§6.3）。它不是登录：它是别人来接管控制权时
/// 屏上显示的那个名字，也是留痕里记的那个名字。所以宁可问一句，也不要拿
/// 空名字去连 —— 空名字在狗的账上是「未具名(ref)」，事后谁也说不清那一趟
/// 是谁开的。
const String operatorAskTitle = '你是谁';

/// 换班时点开那枚常显 chip，框上的标题。
const String operatorEditTitle = '现在是谁在开';

/// 输入框底下那一句。**不许写成「登录」。**
///
/// **这一句以前有两份**（`operatorAskHint` / `operatorEditHint`），说的是
/// 同一件事，措辞却不一样 —— 合成一句之后两条路读到的是同一段话。
/// 「不核实」这三个字是 §6.3 那句实话在人读得到的地方唯一的落点，
/// `operator_chip_test` 有一条测试钉着它。
const String operatorAskHint = '狗记下这个名字，但不核实。别人来接管控制权时'
    '看到的就是这个名字；换班就在这儿改一下。';

/// 名字空着就按确认时说的那一句。
const String operatorEmptyHint = '填一个名字：狗的账上要记下是谁开的。';

/// 输入框上那个标签。
const String operatorNameLabel = '名字';

/// 放弃那个按钮上的字。
const String operatorCancelLabel = '算了';

/// 输入框。**两条路共用同一个 Key** —— 它们本来就是同一个框。
const Key operatorAskFieldKey = ValueKey<String>('operator-ask-field');

/// 确认那个按钮。字随调用方给的 `confirmLabel` 变，Key 不变。
const Key operatorAskConfirmKey = ValueKey<String>('operator-ask-confirm');

/// 放弃那个按钮。
const Key operatorAskCancelKey = ValueKey<String>('operator-ask-cancel');

/// 问一句名字。
///
/// 回一个**已经 `trim()` 过、并且非空**的名字；回 `null` 的意思是「别往下
/// 走」—— 人按了「算了」，或者名字是空的（空的那一种已经在屏上说过一句了，
/// 调用方不用再说）。
///
/// **空名字在这儿就拦住，不往下传。** 空名字在狗的账上是「未具名(ref)」，
/// 事后谁也说不清那一趟是谁开的 —— 而这是要拿去应付客户的一笔账。拦下来
/// 还得说一声：不说的话人按了按钮什么也没发生，会以为按钮坏了。
///
/// [initial] 给上就是预填（换班那条路预填的是此刻挂着的那个名字）。
/// [confirmLabel] 是确认按钮上的字：第一次连狗是「连上」，换班是「就是我」。
Future<String?> askOperator(
  BuildContext context, {
  required String confirmLabel,
  String initial = '',
  String title = operatorAskTitle,
}) async {
  final TextEditingController ctl = TextEditingController(text: initial);
  final String? asked = await showDialog<String>(
    context: context,
    builder: (BuildContext ctx) => AlertDialog(
      title: Text(title),
      content: TextField(
        key: operatorAskFieldKey,
        controller: ctl,
        autofocus: true,
        decoration: const InputDecoration(
          labelText: operatorNameLabel,
          helperText: operatorAskHint,
        ),
      ),
      actions: <Widget>[
        TextButton(
          key: operatorAskCancelKey,
          onPressed: () => Navigator.pop(ctx),
          child: const Text(operatorCancelLabel),
        ),
        TextButton(
          key: operatorAskConfirmKey,
          onPressed: () => Navigator.pop(ctx, ctl.text.trim()),
          child: Text(confirmLabel),
        ),
      ],
    ),
  );
  ctl.dispose();
  if (asked == null) return null;
  if (asked.isEmpty) {
    // **屏没了就别再往上贴话。** `showDialog` 这一等最长能等到人放下手机
    // 再拿起来，那期间这一屏可能已经被拆掉了。
    if (context.mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text(operatorEmptyHint)));
    }
    return null;
  }
  return asked;
}
