// 系统自带的文字（返回、关闭按钮的提示）跟着 App 选的语言走（2026-10-30 走查：中文界面里冒出英文 Back）。
import 'package:d1max_patrol/l10n.dart';
import 'package:d1max_patrol/main.dart';
import 'package:d1max_patrol/store/site_store.dart';
import 'package:d1max_patrol/ui/site_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Future<MaterialLocalizations> _under(WidgetTester t, AppLang lang) async {
  appLang.value = lang;
  await t.pumpWidget(PatrolApp(siteStore: MemorySiteStore()));
  await t.pumpAndSettle();
  return MaterialLocalizations.of(t.element(find.byType(SiteListPage)));
}

void main() {
  tearDown(() => appLang.value = AppLang.en);

  testWidgets('中文界面：返回、关闭的提示是中文', (t) async {
    final m = await _under(t, AppLang.zh);
    expect(m.backButtonTooltip, '返回');
    expect(m.closeButtonTooltip, '关闭');
  });

  testWidgets('英文界面：还是英文', (t) async {
    final m = await _under(t, AppLang.en);
    expect(m.backButtonTooltip, 'Back');
  });
}
