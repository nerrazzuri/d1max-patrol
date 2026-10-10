/// 中英文（App V2，视觉规范第十节）：**英文为主，中文可选**。
///
/// 写法是把两种文字并排写在用的地方：`tr('Stop all', '全部急停')`。带变量的照常插值，两边各写各的：
/// `tr('$n robots', '$n 只狗')`。这样词条不会跟界面漂开（没有 key、没有另一份表要对），也不需要
/// `BuildContext`（网络层报错的文字同样走它）。代价是加第三种语言要改每一处 —— 现在只定了两种。
///
/// 站点给的文字（告警标题、原因）是中文：告警标题按种类查 `design/alert_titles.g.dart`（跟网页值班台
/// 同一份词条），不认识的种类才用站点给的原文。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'design/alert_titles.g.dart';

enum AppLang { en, zh }

/// 现在用哪种文字。换了之后 `PatrolApp` 整棵重建。
final ValueNotifier<AppLang> appLang = ValueNotifier<AppLang>(AppLang.en);

/// 界面文字：英文、中文各写一遍，按当前语言取。
String tr(String en, String zh) => appLang.value == AppLang.zh ? zh : en;

/// 告警标题：英文界面按种类查词条；中文界面用站点给的原文（它带着当时的细节，比如「(授权已不适用)」）。
/// 种类不认识（站点比 app 新）也用站点原文。
String alertTitle(String kind, String siteTitle) {
  if (appLang.value == AppLang.zh && siteTitle.isNotEmpty) return siteTitle;
  final e = alertTitles[kind];
  if (e == null) return siteTitle;
  return appLang.value == AppLang.zh ? e.$2 : e.$1;
}

/// 语言选了哪种存在哪儿。只存这一项；文件坏了、没有，都当没选过（英文）。
class LangStore {
  final File file;
  const LangStore(this.file);

  Future<AppLang> load() async {
    try {
      if (!await file.exists()) return AppLang.en;
      final raw = jsonDecode(await file.readAsString());
      return raw is Map && raw['lang'] == 'zh' ? AppLang.zh : AppLang.en;
    } catch (_) {
      return AppLang.en;
    }
  }

  Future<void> save(AppLang lang) async {
    final tmp = File('${file.path}.tmp');
    await tmp.writeAsString(jsonEncode(<String, String>{'lang': lang.name}), flush: true);
    await tmp.rename(file.path);
  }
}
