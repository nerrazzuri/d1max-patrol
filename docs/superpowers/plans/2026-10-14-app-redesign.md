# Nexus Warden 手机 App 按 FlutterFlow 设计稿重做 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `mobile/` 的界面按用户给的 FlutterFlow 设计稿重做（主题、组件、导航、每一页的布局），行为、权限、和站点的通信一条不改。

**Architecture:** 只换表现层。设计稿的代码**不搬进来**（它没有数据、没有状态、导航是死的），只取它的主题、组件样式和页面布局，在现有页面的 `build` 里重排；每页的状态逻辑（计时器、租约、防迟到回复的代次）原样留着。颜色从「写死的常量」改成「从上下文取、跟着浅色 / 深色走」的一套新设计系统 `Nw*`，老的 `D1*` 在最后一批删掉。

**Tech Stack:** Flutter 3.47 / Dart 3.13；现有依赖不加新的运行时包（不用 `google_fonts`、`go_router`、`google_maps_flutter`、`flutter_animate`）；字体打进包里。

**Spec:**
- 设计稿导出：`/home/liang/Downloads/nexus_warden_mobile.zip`（解在 `<scratchpad>/ffexport/nexus_warden_mobile`，**只读，不执行**）。
- 设计稿逐项清单、现有 App 盘点：本计划「附录 A」「附录 B」（两个只读 agent 2026-10-14 出的，下面只摘了要用的）。
- 功能和页面关系：`docs/App重新设计-需求说明.md`、`docs/手机app.md`。

## 用户定了的（2026-10-14）

1. 浅色、深色两套都做，跟随系统。
2. 按设计稿用绿色表示正常（已连接、在线、电量、已同步）。红色仍只给 P1 和急停类，橙色只给 P2；优先级照旧「颜色 + 形状 + 文字」。**这改掉了原来「正常状态不上色」那一条**（只在手机 App 上；网页值班台不动）。
3. 登录保留账号加口令，只换样式。设计稿的数字键盘、指纹、「Emergency Station Reset」不做。
4. 字号按设计稿的比例，最小 12（设计稿里 10 的抬到 12）；告警页的标题、距离、按钮不低于 14。

## Global Constraints

- **不改行为**：`lib/net/` 不动；每页 `build` 之外的方法、字段、计时器原样保留。附录 B 第 5 节点了名的那些（`_gen`、`_asked`、`_alertApplied`、`_deterApplied`、遥控的 `_link` / `_tick` / `_halt`、监护开关的位置和 `ValueKey`）**一个字不动**。
- **Key 一个不许丢、不许改名**（附录 B 第 4 节的全表）。Key 落在哪一层、那一层是什么类型，测试是认的：`scene-ack` 是 `FilledButton`，`scene-false` / `scene-siren` / `health-bundle` / `health-share` 是 `OutlinedButton`，`scene-*` 状态行的 Key 在 `Text` 上，体检、值守、录像的行是 `ListTile`，告警框是 `scene-head` 上面的 `AnimatedOpacity`。自己做的按钮组件要把 Key 传到里面那个标准按钮上。
- **文案走 `tr(英文, 中文)`**，现有的中英文字符串不改（测试按整句认）。新加的字符串两种语言都给；英文那一半不许有中文和全角标点。
- **页面在默认主题下也要能渲染**：大多数测试是 `MaterialApp(home: page)`，不带我们的主题。`Nw.of(context)` 在上下文里没有扩展时回退到浅色那一套，不许抛。
- **不溢出**：390×844、800×360、568×360（遥控）、1280×720、1920×1080，中英文、浅色深色。不许有永远在动的动画（`pumpAndSettle` 会超时）；只有没确认的 P1 可以闪，照旧用那一个计时器。
- **不联网取东西**：字体打包；图片只用站点给的；地图用站点渲染的栅格图，不用 Google 地图。
- **设计稿里没有后端的东西不做**：重启站点、指纹、遥控页的速度 / 俯仰 / 航向 / 高度、网络延迟、固件运行时长、庄园下拉切换。对应的位置留给有真数据的内容，或者去掉。
- **测试只许这三类改动**，每一处写进 PR 说明：(a) 钉住的主题数值；(b) 入口换了位置（比如从顶栏挪到底部导航）；(c) 新设计系统自己的测试。断言行为的测试不许删、不许放松。
- 命令：`cd mobile && flutter analyze && flutter test --concurrency=1`（全量约 2 分钟）；Python 那几条设计测试 `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest -p no:cacheprovider tests/test_design_tokens.py tests/test_design_mobile.py tests/test_design_i18n.py -q`；`.venv/bin/ruff check packages src tests tools/make_release.py`。
- Git：从 master 开分支 → PR → 八项必过检查 → rebase 合并。提交信息和 PR 末尾的署名按仓库惯例。

## Review Focus

1. **没带主题的测试环境**：`Nw.of(context)` 回退浅色。测：不带主题泵每一页不抛（版面矩阵加一档「不带主题」）。
2. **深色下的对比度**：设计稿有几处「同色字压同色底」（`RobotBadge` 字和底一个色、`AlertItem` 图标和底一个色、P1 卡片白字压 5% 红底）。照抄就看不见。测：新令牌的对比度成对检查，正文 ≥ 4.5、非文字 ≥ 3。
3. **中文比英文宽、测试字体比真字体宽**：设计稿只有英文。测：版面矩阵中英文都过。
4. **360 宽、短屏**：设计稿固定宽度多（遥控 640+、键盘 288、地图框 300×200）。测：遥控 568×360 的几何断言保留；登录页带键盘不溢出。
5. **角色**：设计稿不分角色。业主看不到派狗、遥控、处理告警；只有管理员看得到管理入口。测：现有的角色按钮测试原样过；底部导航的「管理」一栏非管理员没有。

---

## 文件结构

**新建（P0）**

| 文件 | 管什么 |
|---|---|
| `design/mobile.json` | 手机 App 的设计令牌：浅色、深色两套颜色，字体，字号，间距，圆角。唯一来源 |
| `mobile/lib/design/nw_tokens.g.dart` | 从上面生成（`tools/gen_mobile_design.py` 加一段），别手改 |
| `mobile/lib/ui/nw/nw_theme.dart` | `NwColors`（`ThemeExtension`）、`Nw.of(context)`、`NwText`、`nwTheme(Brightness)` |
| `mobile/lib/ui/nw/nw_button.dart` | `NwButton`：primary / secondary / outline / ghost / destructive × small / medium / large |
| `mobile/lib/ui/nw/nw_widgets.dart` | 卡片、状态药丸、健康标签、分段、小节标题、数值格、诊断行、操作格、电量 |
| `mobile/lib/ui/nw/nw_cards.dart` | `NwSiteCard`、`NwDogCard`、`NwAlertItem`、`NwPatrolCard`、`NwVersionCard` |
| `mobile/lib/ui/nw/nw_shell.dart` | 登录之后的外壳：底部导航和它管的几页 |
| `mobile/assets/fonts/` | Space Grotesk、Inter、JetBrains Mono（可变字体，OFL）和各自的许可 |
| `mobile/test/nw_theme_test.dart`、`nw_widgets_test.dart`、`nw_shell_test.dart` | 新设计系统的测试 |
| `tests/test_design_mobile_tokens.py` | 令牌的规矩：对比度、红橙专用、浅深两套键一样 |

**拆（P0，不改行为）**：`mobile/lib/ui/site_page.dart`（1213 行，五个页面挤在一起）拆成 `site_list.dart`、`site_robots.dart`、`site_robot.dart`、`site_incidents.dart`、`site_schedule.dart`，`site_page.dart` 留着只做 `export`（测试都从它导入）。`test/l10n_source_test.dart` 的白名单按新路径改。

**逐批改**：`mobile/lib/ui/` 下各页的 `build`；`mobile/lib/main.dart`（主题、外壳）；`mobile/test/layout_matrix_test.dart`（加浅色 / 深色 / 不带主题三档）；`mobile/test/app_v2_test.dart:175-186`（钉住的主题数值）。

**最后删（P5）**：`mobile/lib/ui/theme.dart` 里的 `proTheme`、`D1Text`；`lib/design/tokens.g.dart` 里手机不再用的；Atkinson 两个字体文件。`design/tokens.json` 和网页值班台不动。

## 导航（改了的地方）

设计稿：底部五栏。落到我们的页面上：

| 栏 | 图标 | 是哪一页 | 谁看得到 |
|---|---|---|---|
| Duty 值守 | shield | 新的值守首页 = `SiteWatchPage` 的内容 + 布防模式分段 + 健康标签 | 都 |
| Dogs 狗 | pets | `SiteRobotsPage` 的狗列表 | 都 |
| Map 地图 | map | `SiteMapsPage` 当前这张图的预览，上面画狗、待命点、禁行区 | 都 |
| History 记录 | history | 记录页：巡检记录 / 事件 / 排程 三个分段 | 都 |
| Admin 管理 | admin_panel_settings | 体检页，上面有去版本、拦截点、摄像头、天气的入口 | 只有管理员；别的角色这一栏是 More：摄像头、天气 |

没有底部导航的：选站点、加站点、扫码、登录、单狗页、遥控、告警现场、驱离、录像、待命点、各管理子页（从上一页推进去，带返回）。

**外壳怎么搭**：`NwShell` 是一个 `StatefulWidget`，`IndexedStack` 套五页，底下 `NwBottomNav`。`SiteRobotsPage` 现在身上的站点级逻辑（事件流订阅、响铃、后台值守、401 退回登录）**留在它的 State 里不搬**——`NwShell` 把它当第二栏的内容，并且它在 `IndexedStack` 里一直活着，所以订阅不断。顶栏那排 `open-*` 入口移到底部导航和管理页；Key 跟着走（`open-watch` 给值守栏、`open-maps` 给地图栏、`open-runs` 给记录栏、`open-health` 给管理栏；`open-releases`、`open-incidents`、`open-schedule` 给各自分段或入口）。`app_v2_test.dart:705-719` 那几条「窄屏收进菜单」的断言按新位置重写（改动类别 b）。

---

### Task P0: 设计系统、字体、外壳、拆文件

**Files:** 见上面「新建」「拆」两张表；`mobile/pubspec.yaml`（字体）；`mobile/lib/main.dart`；`tools/gen_mobile_design.py`；`tests/test_design_mobile.py`。

**Interfaces — Produces（后面每一批都用这些名字）：**

```dart
// nw_theme.dart
class NwColors extends ThemeExtension<NwColors> {
  final Color primary, onPrimary, secondary, tertiary, outline /*=alternate*/,
      text, textSecondary, textMuted /*=accent3*/, bg, surface, surfaceVariant,
      success, onSuccess, warning, onWarning, error, onError, info, onInfo,
      successSoft, warningSoft, errorSoft, infoSoft /*10–15% 的底*/,
      errorFrame /*P1 卡片的 2px 框*/, scrim, onScrim /*压在视频上的*/;
  static const light = NwColors(...); static const dark = NwColors(...);
}
abstract final class Nw {
  static NwColors of(BuildContext c);      // 没有扩展时按 Theme.brightness 回退,不抛
  static const double s4 = 4, s8 = 8, s16 = 16, s24 = 24, s32 = 32;
  static const double r2 = 2, r4 = 4, r8 = 8, r12 = 12, rPill = 9999;
}
abstract final class NwText {            // 都是 TextStyle,不带颜色;最小 12
  static const headlineMedium, headlineSmall,           // Space Grotesk 28/600, 24/600
      titleLarge, titleMedium, titleSmall,              // Inter 20/16/14, 600
      labelLarge, labelMedium, labelSmall,              // JetBrains Mono 14/12/12
      bodyLarge, bodyMedium, bodySmall;                 // Inter 16/14/12
}
ThemeData nwTheme(Brightness b);

// nw_button.dart
enum NwButtonVariant { primary, secondary, outline, ghost, destructive }
enum NwButtonSize { small, medium, large }
class NwButton extends StatelessWidget {
  const NwButton({super.key, this.buttonKey, required this.label, this.icon, this.onPressed,
      this.variant = NwButtonVariant.primary, this.size = NwButtonSize.medium, this.fullWidth = false});
  // buttonKey 落在里面的 FilledButton(primary/secondary/destructive)、OutlinedButton(outline)、TextButton(ghost)上
}

// nw_widgets.dart
class NwCard, NwSectionLabel, NwStatusPill(label, tone), NwHealthChip(icon, label, bad),
      NwSegment<T>(values, selected, onChanged, labelOf, keyOf), NwStatTile(label, value, icon, tone),
      NwDiagnosticRow(icon, label, value, tone), NwActionTile(icon, label, onTap, tileKey),
      NwBattery(percent), NwHeader(title, subtitle, leading, trailing), NwEmpty, NwNotice(text, tone);
enum NwTone { neutral, success, warning, error, info }

// nw_shell.dart
class NwShell extends StatefulWidget { /* 参数同现在的 SiteRobotsPage */ }
class NwBottomNav extends StatelessWidget { /* items, index, onTap;每一项有 Key */ }
```

- [ ] **Step 1** 写 `design/mobile.json`：颜色照附录 A 第 1 节的表（浅、深）；字号里 10 的写 12。设计稿里对比度不够的三处定成：`RobotBadge` / 状态药丸 = 软底（`*Soft`）+ 同色字；`AlertItem` 图标格 = 软底 + 同色图标；P1 卡片 = `errorSoft` 底 + `text` 色字 + `error` 色 2px 框。
- [ ] **Step 2** 写 `tests/test_design_mobile_tokens.py`（先红）：浅深两套键一样；`text` / `textSecondary` 压 `bg`、`surface` ≥ 4.5；`success` / `warning` / `error` / `info` 当文字压 `surface` 和各自的软底 ≥ 4.5（不够就在 JSON 里调深浅，不是放松测试）；`on*` 压各自实底 ≥ 4.5；`outline` 压 `bg` ≥ 1.5；红只出现在声明为 `p1`、`estop`、`offline`、`destructive` 的用途里，橙只在 `p2`、`lowBattery`。
- [ ] **Step 3** `tools/gen_mobile_design.py` 加生成 `nw_tokens.g.dart`；`tests/test_design_mobile.py` 加「生成的跟检入的一字节不差」。
- [ ] **Step 4** 取字体：从 `github.com/google/fonts` 的 `ofl/spacegrotesk`、`ofl/inter`、`ofl/jetbrainsmono` 取可变字体 TTF 和 `OFL.txt`，放 `mobile/assets/fonts/`，`pubspec.yaml` 登记，`main.dart` 里登记许可。中文回落系统字体（照旧）。取不到就停下来报告，不许换成运行时下载。
- [ ] **Step 5** 写 `nw_theme_test.dart`（先红）再写 `nw_theme.dart`：`Nw.of` 在没扩展时回退；`nwTheme` 浅深各一；正文 Inter 14；最小字号 12。
- [ ] **Step 6** 写 `nw_widgets_test.dart`（先红）再写 `nw_button.dart`、`nw_widgets.dart`、`nw_cards.dart`：每个组件浅深各泵一次；`NwButton` 的 `buttonKey` 落在对的标准按钮类型上；禁用态；390 宽、中文长文案不溢出。
- [ ] **Step 7** 拆 `site_page.dart`（纯搬家）；`l10n_source_test` 白名单改路径。跑全量手机测试，应当一条不变地过。
- [ ] **Step 8** 写 `nw_shell_test.dart`（先红）再写 `nw_shell.dart`，`SiteListPage._open` 登录后进 `NwShell`：五栏、管理员才有 Admin、切栏时狗列表的订阅不断（切走再切回，假站点的事件照样刷新列表）、401 退回选站点。这一步各栏里面还是老页面的样子。
- [ ] **Step 9** `main.dart`：`theme: nwTheme(light)`、`darkTheme: nwTheme(dark)`、`themeMode: system`。改 `app_v2_test.dart:175-186` 钉住的值（类别 a）、`:705-719` 的入口位置（类别 b）。版面矩阵加浅 / 深 / 不带主题三档。
- [ ] **Step 10** `flutter analyze`、手机全量、Python 设计测试、ruff 全过；提交；开 PR。

### Task P1: 进入（选站点、加站点、扫码、登录）

**Files:** `site_list.dart`、`site_add.dart`、`site_scan.dart`。
**设计稿：** 附录 A 3.1、3.2；组件 `site_card`。
**Interfaces — Consumes:** `Nw*`（P0）。

- [ ] 选站点：页头「NEXUS WARDEN」小标 + 「Site Selection」+ 语言切换（`lang-switch` 留着）+ 右上加号；每个站点一张 `NwSiteCard`（名字、地址行放站点地址、连接状态药丸现在没有数据就不显示、「Enter Station」）；删除收进卡片的菜单（`site-remove-<名>` 留着）；底下「Add New Station」带扫码图标（`site-add` 留着）。空状态照现在的文案，配一句引导。
- [ ] 登录：从弹窗改成一页（设计稿 3.2 的品牌块、站点条），中间是口令框（`site-password`，还是 `TextField`、还带 `controller`、自动聚焦、回车提交）和「Sign in」。没有键盘、指纹、重置。带键盘时不溢出（现有 `landscape_test` 那条按新页面改，类别 b）。
- [ ] 加站点、扫码：换成新组件，字段的 Key 和 `TextField` 不变。
- [ ] 测试、提交、PR；打一个包。

### Task P2: 值守首页、告警现场、布防、驱离

**Files:** `site_watch_page.dart`、`site_alert_scene.dart`、`site_mode.dart`、`site_deterrence.dart`、`site_deter.dart`。
**设计稿：** 附录 A 3.3、3.7；组件 `alert_item`、`health_chip`、`tab_item`、`alarm_status`、`action_tile`。

- [ ] 值守首页（设计稿的 Duty Dashboard）：页头站点名；布防模式三段（ARMED / HOME / VISITOR，点了走现在 `SiteModePage` 的那几条调用和权限，访客要选防区和时长的还是进 `SiteModePage`）；「ACTIVE INCIDENTS」= 没确认的 P1 各一张红框卡片，带「RESPOND TO INCIDENT」进告警现场；「SYSTEM HEALTH」= 健康标签（站点在线、几只狗在线、盘、备份——用值守汇总里现有的字段）；「UNRESOLVED EVENTS」= 其余告警的 `NwAlertItem`。**「没有」「还没读到」「读不到」三种话照旧分开**；`watch-alert-<key>`、`ackKey`、`resolveKey`、`robotKey`、`alertsColumnKey` / `robotsColumnKey`（≥1100 两栏）都留着，行还是 `ListTile`。
- [ ] 告警现场（设计稿 3.7）：页头级别标签（`PriorityShape` 留着）+ 标题 + 关闭；视频带「LIVE · D1-02 FRONT」和时间的叠字、前后切换；信息卡（位置、距离、事件经过，含「下一步」那一行）；小地图换成站点的栅格图缩略（没有图就不显示这一块），点了进地图（`scene-map`）；底栏「I'M HANDLING IT」（`scene-ack`，`FilledButton`）、「TRIGGER SIREN」（`scene-siren`，`OutlinedButton`）、「FALSE ALARM」（`scene-false`，`OutlinedButton`）、对讲置灰。`_stateLine`、`_whereLine`、警笛状态的分支、`_timeline` 的话一字不改；闪的还是 `scene-head` 上面那个 `AnimatedOpacity`。
- [ ] 布防页、驱离两页换成新组件。
- [ ] 测试、提交、PR；打一个包。

### Task P3: 狗（列表、单狗页、遥控、录像、待命点）

**Files:** `site_robots.dart`、`site_robot.dart`、`site_teleop.dart`、`site_recordings.dart`、`site_standby.dart`、`site_video.dart`、`widget/joystick.dart`（只改样子）。
**设计稿：** 附录 A 3.4、3.5、3.6；组件 `dog_card`、`action_btn`、`tab_group`、`robot_badge`、`joystick_base`。

- [ ] 狗列表：页头 + 筛选（全部 / 在跑 / 充电，本地筛）；`NwDogCard`（编号、状态药丸按真实状态取色：P1 红、在线绿、空闲中性、低电橙、离线红；在做什么；电量）；`robot-<id>` 留着；实时更新断了的那一行（`live-lost`）、布防 / 天气 / 驱离的横幅留着。
- [ ] 单狗页（设计稿 3.5）：页头返回 + 编号 + 状态 + 右上红色「STOP」（= `btn-abort`，没任务时置灰）；视频带前后切换；电量、定位两个数值格（定位那一行现有的话原样）；「Operations」格子 = 现有按钮按角色和能力出（`btn-patrol`、`btn-standby`、`btn-recordings`、`btn-teleop`、`btn-deter`、`btn-relocalize`、`btn-standby-list`），管理员的（录包、标原点、轨迹、日志）单独一组「Admin」；诊断卡放狗报的版本和能力。**监护开关还在滚动区外面、`ValueKey` 不变**；STOP 在 800×360 首屏内。
- [ ] 遥控（设计稿 3.6，横屏）：全屏画面 + 上下渐变；左上关闭、LIVE、编号；左右摇杆（顺序不变：0 是移动、1 是转向）；中间大「STOP」（`stopKey`，高 ≥ 56）；状态行（`statusKey`）。**不放**速度、俯仰、航向、高度、延迟、灯、喇叭、拍照。568×360 的几何断言全过。
- [ ] 录像、待命点换成新组件；录像行还是 `ListTile`、`selected` 的规矩不变。
- [ ] 测试、提交、PR；打一个包。

### Task P4: 地图、记录、管理

**Files:** `site_maps.dart`、`site_mapping.dart`（只改页面外壳，画布和 painter 不动）、`site_runs.dart`、`site_incidents.dart`、`site_schedule.dart`、`site_cameras.dart`、`site_weather.dart`、`site_health.dart`、`site_releases.dart`、`site_intercepts.dart`、`site_logs.dart`。
**设计稿：** 附录 A 3.8、3.9、3.10；组件 `patrol_card`、`version_card`、`diagnostic_row`、`legend_item`、`robot_marker`。

- [ ] 地图栏：当前图的预览做主体（现有画布），图例浮层、右下定位和缩放、底部「这一片的狗」；右上进地图列表（管理员的下发、重建在那里）。
- [ ] 记录栏：三个分段（巡检 / 事件 / 排程）；巡检是数值格（今天几趟、几趟有异常）+ `NwPatrolCard`（缩略图用站点给的照片，没有就不显示）；`run-<id>`、`Icons.report`、`Icons.broken_image` 留着。
- [ ] 管理栏（设计稿 3.10）：站点状态卡（体检各项，行还是 `ListTile`、排序和形状的规矩不变）、版本格子（站点、各只狗、各台手机）、「Diagnostic Package」（`health-bundle`、`health-share` 还是 `OutlinedButton`，`health-msg` 还是 `SelectableText`）；**没有重启站点**。下面一排入口：版本、拦截点、摄像头、天气。
- [ ] 其余管理子页换成新组件。
- [ ] 测试、提交、PR；打一个包。

### Task P5: 收尾

- [ ] 删 `proTheme`、`D1Text`、手机不再用的 `D1*` 令牌、Atkinson 字体；`grep -rn "D1Color\|D1Text\|proTheme" mobile/lib` 应当为空。
- [ ] `docs/手机app.md`「现在有哪几屏」按新导航重写；`docs/视觉规范.md` 加一节说明手机 App 换了设计系统、网页值班台没换；台账记一行。
- [ ] `D1MAX_SHOTS=… flutter test test/shots_test.dart` 出一套浅 / 深截图给用户看。
- [ ] 全量、提交、PR；打包；送外审。

---

## 执行办法

P0 一个 agent 先做完、合进 master；P1–P4 文件不重叠，四个 agent 各在自己的 worktree 里并行，各开各的 PR，按 P1 → P2 → P3 → P4 的顺序合（后合的先 rebase）；P5 最后一个 agent。每个 PR 合之前由主会话看一遍 diff、跑一遍测试、打一个包装到手机上看。

## 附录 A：设计稿清单（摘要）

完整清单在 2026-10-14 的会话记录里；要用的数值：

- **颜色**（浅 / 深）：primary `#1A1C1E` / `#D1E4FF`；secondary `#44474E` / `#BEC6DC`；tertiary `#005FB7` / `#4796E6`；outline(alternate) `#C4C6D0` / `#2C313A`；text `#1A1C1E` / `#E2E2E6`；textSecondary `#44474E` / `#A1A1A6`；textMuted(accent3) `#74777F` / `#636366`；bg `#FDFDFD` / `#0A0C10`；surface `#FFFFFF` / `#1B2028`；surfaceVariant `#E1E2EC` / `#282E38`；success `#2E7D32` / `#4CAF50`；warning `#ED6C02` / `#FFA726`；error `#D32F2F` / `#FF5252`；info `#0288D1` / `#4FC3F7`；on* 白（深色的 onPrimary 黑）。软底 = 同色 10–15%。
- **字体**：标题 Space Grotesk（28/600、24/600）；正文和小标题 Inter（20/16/14 600；16/14/12 400）；标签 JetBrains Mono（14/600、12/600、10→12/500）。
- **形状**：卡片圆角 12、1px `outline` 边、内距 24 或 16；小块圆角 8；标签 4；药丸 9999。页内边距 24，间距 4/8/16/24。没有阴影（只有遥控 STOP 一处红色光晕），页头页脚用 1px 线分。图标 Material `*_rounded`，24。
- **按钮**：primary 实底 `primary`；secondary 实底 `secondary`；outline 透明 + 1px 边；ghost 透明、字 `primary`；destructive 实底 `error`。small 圆角 4、内距 16/4；medium 8、24/8；large 12、32/16。字 labelMedium。
- **各页布局**：见设计稿源码 `lib/pages/*/…_widget.dart`（agent 直接读）。
- **设计稿自己的毛病（不照抄）**：同色字压同色底三处；`SiteCard` 的 P1 徽标条件写反；`DogCard` 不认状态色、低电判断写死；`ActionBtn` 图标写死；底部导航各页不一致、竖着排；所有按钮没有动作；没有加载 / 空 / 出错 / 断线的样子；全是英文；没有 `SafeArea`。

## 附录 B：现有 App 盘点（摘要）

完整盘点在 2026-10-14 的会话记录里；要守的：

- 测试 389 处、跑起来约 490 条；`flutter_test_config.dart` 把语言定成中文，文字断言认的是 `tr()` 的中文那一半。
- 断点：520（顶栏收进菜单、值守卡片窄版）、700（告警页宽版）、1100（值守页两栏）。
- 各页要原样保留的逻辑：见正文 Global Constraints 第一条；逐页的方法名在盘点第 5 节。
- 几何断言：遥控（摇杆在画面左右两侧、宽 ≥ 144、STOP 高 ≥ 56、状态行不出屏）；单狗页 800×360 首屏看得到 `btn-abort`、视频不超过屏高六成；地图画布高 > 150。
