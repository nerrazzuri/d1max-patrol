// 区域页（2026-10-30 图铺满）：画的几样在右下的菜单里，比例、待命点、区域列表、确认在点开的说明栏里。
import 'package:d1max_patrol/ui/site_mapping.dart' show SiteMapPreviewPage;
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';

/// 开说明栏（开着就不动）。
Future<void> openZonePanel(WidgetTester t) async {
  if (find.byKey(SiteMapPreviewPage.panelKey).evaluate().isNotEmpty) return;
  await t.tap(find.byKey(SiteMapPreviewPage.statusKey));
  await t.pump();
}

/// 收起说明栏（收着就不动）：要点图的时候别让它盖着。
Future<void> closeZonePanel(WidgetTester t) async {
  if (find.byKey(SiteMapPreviewPage.panelKey).evaluate().isEmpty) return;
  await t.tap(find.byKey(SiteMapPreviewPage.panelCloseKey));
  await t.pump();
}

/// 菜单里的一样：先收起说明栏、弹开菜单，再点。
Future<void> tapZoneMenu(WidgetTester t, Key item) async {
  await closeZonePanel(t);
  if (find.byKey(item).evaluate().isEmpty) {
    await t.tap(find.byKey(SiteMapPreviewPage.menuKey));
    await t.pump();
  }
  await t.tap(find.byKey(item));
}
