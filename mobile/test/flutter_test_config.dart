// 所有测试文件开跑前先过这里（flutter_test 的约定文件名）。
//
// App V2 起界面英文为主、中文可选。原有的测试按中文文字找控件，所以**测试默认把语言定成中文** ——
// 这同时证明了改成双语之后中文界面一个字没变。要测英文界面的测试自己切（见 `l10n_test.dart`），
// 切完在 tearDown 里切回来。
import 'dart:async';

import 'package:d1max_patrol/l10n.dart';

Future<void> testExecutable(FutureOr<void> Function() testMain) async {
  appLang.value = AppLang.zh;
  await testMain();
}
