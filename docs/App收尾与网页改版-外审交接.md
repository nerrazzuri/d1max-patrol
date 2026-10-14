# 手机 App 收尾（C-B5 后续）+ 网页值班台换外观（C-B6）：外审交接

> **2026-10-14 代码外审通过**（一轮，APPROVE；Minor「电量到期不主动撤下」随后补上）。下面是送审时的原文。
>
> 范围 **`2a37324..9ac2d0f`**（PR #151、#152，8 个提交）。两批合在一起送：都是外观改动，各带一处「在不在线」判断的修正。
> 来历：C-B5（手机 App 按设计稿重做）2026-10-14 代码外审通过后，用户先要收掉截图里的小毛病（PR #151），再要网页值班台跟手机用同一套外观（PR #152）。
> 用户这期间定的：**决策 64**——网页值班台跟手机用同一套设计（同配色、同三种字体、浅色深色跟着系统、正常状态用绿色），**只换外观，结构不动**；改掉决策 63 第 ② 条「网页不变、绿色只给手机」。台账 C-B6。
> 上一轮外审：`../reviews/2026-10-14-App-redesign-e0f829f-b20a8bc-review.md`、`2026-10-14-App-redesign-3dfd717-recheck.md`。

## 1. 请重点看的（按风险排）

| # | 内容 | 主要文件 | 我最不放心的地方 |
|---|---|---|---|
| 1 | **网页管理页「在线」改看新鲜度**：外观换成绿色后，原来只看 `status.online` 的那一格会把失联的狗显示成绿色在线（跟手机外审 I2 同一类）。改成也看 `/api/robots` 带的 `fresh`，跟值守页的 `robotState` 一个口径 | `web/console/src/admin.tsx`（`AdminRobot.fresh`、狗表「在线」那一格） | 只改了管理页狗表这一格；网页里还有没有别处自己判断在线、没走 `robotState` 的，我只按 `status?.online` 搜过一遍 |
| 2 | **狗卡电量过期就不写数**（上一轮外审的非阻断建议）：超过 10 分钟或没给读数时刻就显示「--」。多旧按**站点自己的钟**算——汇总带 `now_ms`，拿到时已旧多少用站点的钟，之后加本机钟上过去的时间 | `mobile/lib/ui/nw/nw_p3_robots.dart`（`NwFleet.batteryMaxAge`、`_loadBattery`、`_withBattery`） | ① 老站点没带 `now_ms` 时退回拿本机的钟比，手机钟不准就会误判；② 本机钟在两次读取之间被人改了（往回拨）会算短；③ 10 分钟是我定的，没有现场依据 |
| 3 | **网页主题跟系统实时切换**：`src/theme.ts` 读 `design/mobile.json`，用 CSSOM 挂 CSS 变量，`matchMedia` 监听切换；地图、时间轴的画布跟着重画 | `web/console/src/theme.ts`、`main.tsx`、`mapview.ts` 相关、`styles.css` | ① 有没有哪块画布或内联的颜色没跟着换（只在截图里看了 11 页）；② 去掉 `tokens.ts` 以后 `design/tokens.json` 网页不再读，其他读它的（Python 设计测试、手机生成脚本）照旧 |
| 4 | **字体打包**：三种字体从 `mobile/assets/fonts` 裁成拉丁加符号、转 woff2，随站点发；OFL 许可放在 `/assets/OFL-*.txt`；去掉 Atkinson 和两个 `@fontsource` 依赖 | `web/console/src/fonts/`、`fonts.css`、`public/assets/OFL-*.txt`、`package.json` | 裁剪后中文走系统字体（跟原来一样）；裁掉的字符集够不够（符号、箭头、`·`）只在截图里看过 |
| 5 | **深色下的地图底图**：手机用颜色矩阵反转亮度（空地 254→27、没探过 205→67、墙 0→235），网页用 CSS `invert(0.91)`，有单测核对两者差不到 5 级灰。两边都只滤底图，不滤上面画的狗、防区、待命点 | `mobile/lib/ui/site_maps.dart`（`darkMapRaster`、`_raster`）、`web/console/src/styles.css` | 测试站点的地图只看出大块灰，真实地图上墙、台阶边缘深色下是否清楚没在真图上看过（演示站点那张 `estate-1:7` 看着可以） |
| 6 | **手机其余六处外观**：告警页底栏按钮换比例字、内边距 8→4（390 宽一行放得下）；巡检历史只把「异常 N」标红；遥控两侧画静止的摇杆底圈、杆头和「前后/转向」；长地址折两行；值守首页高不到 480 时页头压成一行 | `site_alert_scene.dart`（只改 `_big` 的样式）、`nw_cards.dart`（`NwPatrolCard.flagged`）、`widget/joystick.dart`（`_idle`）、`site_list.dart`、`nw_widgets.dart`（`NwHeader.compact`）、`site_watch_page.dart` | ① 静止的摇杆画在中央，按下的位置仍是圆心——人会不会以为要按在画出来的那个圈里（规矩没变，只是看着像）；② 静止杆头不带 `knobKey`，老测试「没按下时没有杆头」照样过 |
| 7 | 文档 | 台账 C-B6、决策 64；`docs/视觉规范.md` 开头加了说明，老的颜色字体段落留作历史 | 事实有没有写错 |

## 2. 行为上的变化

- **网页管理页**：狗状态不新鲜时，「在线」那一格写离线（原来写在线）。
- **手机狗卡**：电量读数超过 10 分钟或没有读数时刻时显示「--」（原来照写）。
- **网页值班台**：浅色深色跟着系统；正常状态（在线、通过、已连）用绿色，离线改红（形状仍是 P2 三角）。
- 其余都是外观：页面结构、导航、id、`data-*`、`aria-*`、文案、请求、轮询、CSRF、数据过期横幅、告警队列、警笛和驱离状态逻辑都没动。`mobile/lib/net/`、站点、狗都没动。

## 3. 验证到哪一步

| 项 | 做了的 | 没做的 |
|---|---|---|
| 手机 | `flutter analyze` 干净；手机全量 **1180 条过**（送审前 1167）。新测试：`mobile/test/polish_test.dart`（按钮一行且字号≥14、深色底图滤镜、只标红异常那一段、矮屏页头）；`nw_fleet_battery_test.dart` 加 7 条，其中「站点钟慢+新读数」「站点钟快+旧读数」「钟对不上+后来放旧」3 条在只看手机钟的那一版上失败过 | 真机没看；遥控页只有深色截图 |
| 网页 | `npx tsc --noEmit` 干净；`npx vitest run` **74 条过**（原 60）。新测试 `src/theme.test.tsx`（跟系统实时切换、两套令牌都挂上、地图颜色和底图滤镜随主题、状态→颜色、样式表用到的变量两套都有）；`pages.test.tsx` 加管理页在线口径 3 条，其中「不新鲜」2 条在上一版上失败过 | 只在无头 Chromium 里看；没在真显示器、Windows/Mac 字体渲染、打印预览上看；窄屏（<720）、打开的对话框、数据过期横幅没截图 |
| Python | 设计测试 22 条过；`packages/site-node/tests/test_site_webui.py`（唯一碰网页资源和 CSP 的）15 条过 | Python 全量没在开发机上跑（靠 CI） |
| 真浏览器 | 无头 Chromium 连另起的演示站点：三种字体都加载、没有 CSP 报错、浅↔深切换就地生效；重启后的演示站点上字体按 `font/woff2` 发出 | — |
| 截图 | 手机 50 张（`~/Downloads/NexusWarden-polish-shots/`）；网页 11 页 × 1920/1280 × 浅/深 × 中/英，改前改后各 88 张（改后在 `~/Downloads/NexusWarden-web-shots/`） | 都是抽看，不是逐张审 |
| CI | 两个 PR 九项检查都过（含集成层），合并前确认过没有失败项 | — |

改到的老测试：`nw_fleet_battery_test.dart` 的 `_summary` 夹具加了读数时刻（新规矩要它），已有断言没变；`shots_test.dart`（只出截图）汇总夹具加了读数时刻。网页没有改老测试。

## 4. 这一批没解决的

- 手机告警页里的小地图预览在深色下仍是浅灰块（这次只改了地图栏）。
- 网页离线状态颜色变红了，图形仍是 P2 的三角。
- 网页 1280 宽时顶栏折行，「全部急停」掉到第二行——改之前就这样。
- 「布防」在网页、手机上都不显示成绿色，模式按钮保持中性。
- 真机、真显示器都没看过。

## 5. 复现用的

- 手机：`mobile/` 里 `flutter test --concurrency=1`；截图 `D1MAX_SHOTS=1 D1MAX_SHOTS_LANG=all flutter test test/shots_test.dart`（要中文字体和图标字体，见该文件开头）。
- 网页：`web/console/` 里 `npx tsc --noEmit`、`npx vitest run`、`npm run build`（产物在 `packages/site-node/src/d1max_site/web/`，已提交）。
- 演示站点：`tools/site_demo.py --host <局域网地址>`（每次启动清空数据、证书指纹会变）。开发机上现在开着一个（`https://192.168.5.12:8443`，账号 `demo` / `guard1` / `owner1`），已注入 P1 入侵告警；请不要停它。
