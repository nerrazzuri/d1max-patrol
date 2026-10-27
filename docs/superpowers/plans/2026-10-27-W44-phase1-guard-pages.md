# W44 第一期：外壳、顶栏、保安五页、用词 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本项目用户禁止子代理（2026-10-15）：只能由主会话按 superpowers:executing-plans 一项一项做。

**Goal:** 站点网页换成保安五页（值班、实况、地图、录像、报警记录，深色），新顶栏，界面用词统一成「四足机器人 / 机器人 / robot」；后端补齐这五页要的接口。

**Architecture:** 后端（`packages/site-node`）加：告警分给谁、报警结论、交班留言、位置轨迹落库、最近的机器人、临时路线、录像抓帧，库结构升到 61。前端（`web/console`，Preact）换外壳和路由，新写五页，复用现有的 `CameraWell`、`MapCanvas`、`usePosePick`、`recordings.ts`；旧的 Live、Alarms 页删掉，定位、报表两页先挪进「管理」导航（第四期再重做）。管理各页这一期不动。

**Tech Stack:** Python 3（站点，`ThreadingHTTPServer` + SQLite），Preact 10 + `@preact/signals` + Vite + Vitest（happy-dom），Flutter（只改用词）。

**Spec:** `docs/superpowers/specs/2026-10-27-site-console-redesign-design.md`（下文「设计 §x」指它）

## Global Constraints

- 界面一律写「四足机器人」，挤的地方写「机器人」（如「机器人 A」）；英文写 robot；**不写「狗」「dog」**。代码注释、标识符不改。（设计 §2.1）
- 保安五页默认深色；「管理」默认浅色；手机 App 照旧跟系统。（设计 §2.2）
- 状态颜色：红 = 出事要人马上去；橙 = 机器人需要帮忙；绿 = 正常巡逻或布防；灰 = 待命、充电、撤防；蓝 = 选中、路线、摄像头。（设计 §2.2）
- 保安页面不出现：修订号、定位协方差、代价地图、点云、证书、MQTT、命令号。（设计 §2 做坏了的样子）
- 权限在站点后端判，网页只是不显示按钮。（设计 §9）
- 时间关口照旧：临时路线、「过去看看」都是会让机器人动的命令。（设计 §9）
- 网页 CSP 不许内联脚本、`<style>` 标签；样式进 `styles.css`（Preact 通过 `el.style` 设的动态位置可以用）。
- 界面文字走 `i18n.ts` 的 `t(key)`，英中两份都写；缺中文显示英文。
- 网页构建产物 `packages/site-node/src/d1max_site/web/` 要提交。
- 改 `SCHEMA_VERSION` 的提交要带上一版（60）的快照 `tests/fixtures/schema/site-v60.sql`。
- 不用 `dart format`；手机中文放 `tr()` 第二参数。
- 提交结尾两行：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`、`Claude-Session: https://claude.ai/code/session_01XrN14Jvh7JjXFNpNn91N2Z`。

## Review Focus

1. **轨迹和录像的钟对不上**：录像的 `start_ms` 是机器人的钟；轨迹也必须按遥测的 `stamp`（机器人的钟）存，不能用站点收到的时刻，否则录像页地图上的位置跟画面差几秒到几十秒。测试见 Task 4 `test_轨迹按机器人的钟记`。
2. **两件报警同时来**：横幅只显示最急的一件，写「还有 N 件」；画面位 1、2 分给头两件报警的现场。测试见 Task 9 `两件报警_两个画面位各给一件`。
3. **别人已经处理掉了**：横幅开着时别的保安点了「误报」，再点按钮回 404 —— 横幅消失，提示「已经有人处理了」，不弹错误循环。测试见 Task 9 `别人处理掉了_横幅消失`。
4. **最近的机器人在另一张图上**：只在同一张图、同一版上的机器人里挑；一个都没有就说「这张图上没有能派的机器人」。测试见 Task 5 `test_最近的_只挑同一张图同一版`。
5. **数据过期**：拿不到新数据时，值班页画面位、状态条照旧显示「状态未知」和过期横幅（现有外审 I5），不把旧的「巡逻中」当现在。测试见 Task 9 `数据过期_状态写未知`。

---

### Task 1: 用词统一（四足机器人 / robot）

**Files:**
- Modify: `web/console/src/i18n.ts`、`web/console/src/routes_i18n.ts`、`web/console/src/*.tsx` 里直接写的中文、`design/i18n/alerts.json`
- Modify: `packages/site-node/src/d1max_site/*.py` 里面向用户的字符串（`HttpError(...)` 的文字、告警 `title=`/`detail=`、推送文字、报表文字）
- Modify: `mobile/lib/**/*.dart` 里 `tr()` 第二参数和界面字符串；重新生成 `mobile/lib/design/alert_titles.g.dart`（`tools/gen_mobile_design.py`）
- Modify: 断言旧文字的测试（Python、vitest、flutter）
- Create: `tests/test_ui_wording.py`

**Interfaces:**
- Produces: 无代码接口；之后所有新文字都要过 `tests/test_ui_wording.py`。

- [ ] **Step 1: 写扫描测试 `tests/test_ui_wording.py`**

三条测试，各自扫文件里的字符串字面量（不扫注释）：
- `test_网页界面文字不写狗`：`web/console/src/i18n.ts`、`routes_i18n.ts` 的值，`*.tsx` 里的中文字符串字面量，`design/i18n/alerts.json` 的所有值 —— 不含「狗」，英文值不含 `\bdogs?\b`（不分大小写）。
- `test_站点给人看的文字不写狗`：用 `ast` 解析 `packages/site-node/src/d1max_site/*.py`，收集 `HttpError(...)` 调用里的字符串常量（含 f-string 的常量部分）和关键字参数 `title=`、`detail=` 的字符串常量 —— 不含「狗」。
- `test_手机界面文字不写狗`：`mobile/lib/**/*.dart` 里去掉 `//` 注释后，单引号、双引号字符串字面量不含「狗」。

每条失败时列出文件和行号。

- [ ] **Step 2: 跑，确认失败**

Run: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q tests/test_ui_wording.py`
Expected: 3 failed，列出约 44 + 5 + 22 处（网页）、50 处左右（站点）、约 150 处（手机）。

- [ ] **Step 3: 改文字**

「狗」→ 正式说法用「四足机器人」，句子里挤或重复出现的用「机器人」；「机器狗」→「四足机器人」；「每台狗」→「每台机器人」。英文已经是 robot 的不动。`alerts.json` 改完跑 `tools/gen_mobile_design.py` 重新生成手机的告警标题。

- [ ] **Step 4: 改断言旧文字的测试，跑三组**

Run: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts= -q tests/test_ui_wording.py` → PASS
Run: Python 全量（预计约 20 分钟，告诉用户）、`cd web/console && npx vitest run`、`cd mobile && flutter test --concurrency=1`（约 2 分钟）
Expected: 全过。

- [ ] **Step 5: 提交**

`git commit -m "界面用词:狗改成四足机器人 / 机器人(W44 第一期)"`

---

### Task 2: 告警分给谁（后端）

**Files:**
- Create: `packages/site-node/src/d1max_site/alert_audience.py`
- Modify: `packages/site-node/src/d1max_site/alerts.py`（`Alert.to_wire` 加 `audience`）
- Test: `packages/site-node/tests/test_site_alert_audience.py`

**Interfaces:**
- Produces:
  - `Audience = Literal["intrusion", "fire", "help", "offline", "owner"]`
  - `audience(kind: str) -> Audience`
  - `GUARD_AUDIENCES: frozenset[str] = {"intrusion", "fire", "help", "offline"}`
  - 告警回包每条多一个键 `"audience"`。

- [ ] **Step 1: 写测试**

```python
@pytest.mark.parametrize("kind, want", [
    ("intrusion", "intrusion"), ("intrusion_unanswered", "intrusion"), ("intrusion_person", "intrusion"),
    ("dog_sees_person", "intrusion"), ("force_lifted", "intrusion"),
    ("stuck", "help"), ("nav_blocked", "help"), ("fallen", "help"), ("force_flipped", "help"),
    ("deter_cornered", "help"), ("battery_abort", "help"), ("run_abort", "help"),
    ("charge_failed", "help"), ("dock_stuck", "help"), ("loc_lost_paused", "help"),
    ("loc_lost_idle", "help"), ("estop_pressed", "help"), ("nogo", "help"),
    ("payload_siren", "help"), ("payload_strobe", "help"), ("payload_spotlight", "help"),
    ("schedule_missed", "help"),
    ("robot_offline", "offline"), ("cctv_offline", "offline"),
    ("cert_expiring", "owner"), ("evidence_plain", "owner"), ("site_disk_low", "owner"),
    ("loc_degraded", "owner"), ("watchdog_died", "owner"), ("没见过的种类", "owner"),
])
def test_告警分给谁(kind, want): assert audience(kind) == want

def test_每个已知种类都分了():   # LEVEL_OF 里每个 kind 都在表里明写(不靠兜底)
def test_回包带audience():       # AlertBook 报一条 stuck,to_wire()["audience"] == "help"
```

`fire` 这一期没有 kind（热像温度告警等实物），表里留注释说明。`payload_*` 用前缀匹配；其余逐个写。

- [ ] **Step 2: 跑，确认失败**（`ModuleNotFoundError: d1max_site.alert_audience`）

Run: `<Python 测试命令> packages/site-node/tests/test_site_alert_audience.py`

- [ ] **Step 3: 写 `alert_audience.py`，`to_wire` 加键**

- [ ] **Step 4: 跑，PASS；再跑 `test_site_alerts*.py`、`test_site_alert_book.py`**（回包多一个键，快照式断言要补）

- [ ] **Step 5: 提交** `git commit -m "站点:告警分给保安四类、其余给业主(W44)"`

---

### Task 3: 库结构 61、报警结论、交班留言（后端）

**Files:**
- Modify: `packages/site-node/src/d1max_site/db.py`（`SCHEMA_VERSION = 61`；`_ADDED_COLUMNS` 加 `("alerts", "verdict", "TEXT NOT NULL DEFAULT ''")`；`_DDL` 加 `shift_notes`、`pose_track` 两张表）
- Create: `packages/site-node/tests/fixtures/schema/site-v60.sql`（按测试文件头的办法从当前 master 的 `db.py` 生成）
- Modify: `alerts.py`（`Alert.verdict: str = ""`，`AlertBook.resolve(..., verdict: str = "")`，`to_wire` 带 `verdict`）、`alert_store.py`（`_COLS`、`_from_row`、写穿带 `verdict`）
- Create: `packages/site-node/src/d1max_site/shift_notes.py`
- Modify: `api.py`（`/resolve` 收 `verdict`；新路由 `/api/shift-notes`）
- Test: `tests/test_site_alerts_api.py`（加）、`tests/test_site_shift_notes.py`（新）、`tests/test_site_schema_upgrade.py`（自动覆盖）

**Interfaces:**
- Produces:
  - 表 `shift_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL)`
  - 表 `pose_track(robot_id TEXT NOT NULL, stamp_ms INTEGER NOT NULL, map_id TEXT NOT NULL, map_version TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL, PRIMARY KEY (robot_id, stamp_ms))`，索引 `pose_track_map ON pose_track(map_id, map_version, stamp_ms)`（Task 4 用）
  - `VERDICTS = ("intruder", "false_alarm", "handled")`（`alerts.py`）
  - `POST /api/alerts/{key}/resolve` 体 `{"verdict": "intruder"|"false_alarm"|"handled"}`；不给 = `""`（老客户端照旧能用）；别的值 400。
  - `ShiftNotes(db, now_ms)`：`add(author: str, text: str) -> dict`、`latest() -> dict | None`；`dict` 形如 `{"id", "at_ms", "author", "text"}`
  - `GET /api/shift-notes` → `{"latest": {...} | null}`（权限 `VIEW`）；`POST /api/shift-notes` 体 `{"text"}`（权限 `HANDLE_ALERTS`，作者取登录账号，`text` 去首尾空白后 1–500 字，否则 400），回 `{"note": {...}}`，进审计。

- [ ] **Step 1: 写测试**
  - `test_误报要记下结论和谁`：resolve 带 `false_alarm` → 回包 `verdict == "false_alarm"`、`resolved_by == 登录名`；重启站点（新 `AlertStore` 读回）还是 `false_alarm`。
  - `test_结论只认三种`：`"x"` → 400；不带 → 200、`verdict == ""`。
  - `test_家人账号也能下结论`：`owner` 角色有 `HANDLE_ALERTS` → 200。
  - `test_交班留言_最新一条`：先后写两条，`latest()` 是后一条；作者是登录名，不认体里的 `author`。
  - `test_交班留言_长度`：空白、501 字 → 400；500 字 → 200。
  - `test_交班留言_只看的人写不了`：没有 `HANDLE_ALERTS` 的会话 → 403。
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现；生成 `site-v60.sql` 快照**
- [ ] **Step 4: 跑这几份测试和 `test_site_schema_upgrade.py`，PASS**
- [ ] **Step 5: 提交** `git commit -m "站点:报警结论分开记、交班留言,库结构 61(W44)"`

---

### Task 4: 位置轨迹落库、某台机器人的轨迹、多久没巡过（后端）

**Files:**
- Create: `packages/site-node/src/d1max_site/pose_track.py`
- Modify: 遥测入口（`runviz.on_telemetry` 被调的那一处，同一处再喂 `PoseTrack.on_telemetry`）、站点每小时那一拍（删过期）、`api.py`
- Test: `packages/site-node/tests/test_site_pose_track.py`

**Interfaces:**
- Consumes: Task 3 的 `pose_track` 表。
- Produces:
  - `PoseTrack(db)`，常量 `STEP_M = 1.0`（挪了 1 m 才记）、`MAX_GAP_MS = 10_000`（没挪也每 10 s 记一个）、`KEEP_MS = 30 * 86_400_000`（跟录像一样 30 天）、`CELL_M = 2.0`
  - `on_telemetry(robot_id: str, t: Telemetry) -> None`：`t.pose is None`（定位不可信）不记；时刻用 `t.stamp`（**机器人的钟**，跟录像同一个钟）。
  - `track(robot_id: str, since_ms: int, until_ms: int) -> list[dict]`，每项 `{"t", "map_id", "map_version", "x", "y"}`，按时间排，最多 20 000 点，超了均匀抽稀并在回包里写 `"thinned": true`
  - `last_visit(map_id: str, version: str, since_ms: int) -> list[dict]`：按 `CELL_M` 的格子，每格 `{"i", "j", "t"}`（最后一次有机器人到过的时刻），`i,j` = `floor(x / CELL_M)`, `floor(y / CELL_M)`
  - `purge(now_ms: int) -> int`
  - `GET /api/robots/{id}/track?since=&until=`（`VIEW`；跨度最多 24 小时，否则 400）→ `{"points": [...], "thinned": bool}`
  - `GET /api/coverage?map_id=&map_version=&since=`（`VIEW`）→ `{"cell_m": 2.0, "cells": [...]}`

- [ ] **Step 1: 写测试**
  - `test_轨迹按机器人的钟记`：遥测 `stamp=1000` 而站点时钟是 `999_999` → 存的 `t == 1000`。
  - `test_没挪不记_挪了1米才记_但每10秒记一个`
  - `test_定位不可信那段不记`（`pose=None`）
  - `test_换图另记`：同一台机器人换了 `map_version`，`track` 里两段的 `map_version` 各是各的；`last_visit` 只算问的那一版。
  - `test_过期的删掉`：31 天前的点 `purge` 后不在。
  - `test_轨迹接口跨度超过24小时400`
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现，接进遥测入口和每小时那一拍**
- [ ] **Step 4: 跑，PASS**
- [ ] **Step 5: 提交** `git commit -m "站点:位置轨迹落库 30 天,轨迹和多久没巡过的接口(W44)"`

---

### Task 5: 最近的机器人过去看看、保安的临时路线（后端）

**Files:**
- Modify: `packages/site-node/src/d1max_site/intercept_reach.py`（加 `path_m`）
- Create: `packages/site-node/src/d1max_site/nearest.py`
- Modify: `packages/site-node/src/d1max_site/routes.py`（把 `_validate_route` 的校验拆出公开的 `build_mission`）
- Modify: `api.py`（`GET /api/robots/nearest`；`POST /api/robots/{id}/patrol` 多收 `points`）
- Test: `packages/site-node/tests/test_site_nearest.py`、`tests/test_site_temp_route.py`

**Interfaces:**
- Produces:
  - `InterceptReach.path_m(map_id: str, version: str, start: tuple[float, float], goal: tuple[float, float]) -> float | None`：用现有 `_costmap` + `astar.plan`，路程 = 相邻格距离之和 × 分辨率；走不通、栅格没有 → `None`
  - `NOMINAL_MPS = 0.5`（估到达时间用）
  - `nearest(reach: InterceptReach, candidates: list[tuple[str, str, str, float, float]], map_id: str, version: str, x: float, y: float) -> dict | None`；候选 = `(robot_id, map_id, map_version, x, y)`；回 `{"robot_id", "path_m", "eta_s"}`，`eta_s = round(path_m / NOMINAL_MPS)`
  - 候选由 api 层从派遣器取：在线、数据新鲜、不在驱离、没被手动接管、加载的图就是问的那一版、有可信位姿。
  - `GET /api/robots/nearest?map_id=&map_version=&x=&y=`（`DISPATCH`）→ `{"robot_id", "path_m", "eta_s"}`；没有 → 404 `{"error": "这张图上没有能派的机器人"}`。派出去照旧走现有 `POST /api/robots/{id}/goto`。
  - `RouteStore.build_mission(route_id: str, body: dict) -> Mission`（原 `_validate_route` 的内容；`_validate_route` 改成调它）
  - `POST /api/robots/{id}/patrol` 体 `{"points": [{"x", "y", "yaw"?}], "map_id", "map_version"}`：点 1–20 个，名字自动 `P1…Pn`，路线名 `temp-<毫秒>`；校验失败 400 并带 `errors`（第几个点、为什么）；**不进路线库**；优先级照旧 `MANUAL`；进审计 `detail` 写 `temp: true`。带 `mission_id` 的老用法不变。

- [ ] **Step 1: 写测试**（用现有测试里的小图夹具）
  - `test_路程_走得通回米数_走不通回None`
  - `test_最近的_按路程不按直线`：直线近但隔墙的那台不选。
  - `test_最近的_只挑同一张图同一版`：另一版上的机器人更近也不选；都不在 → `None`。
  - `test_最近的接口_没有能派的404`
  - `test_临时路线_派出去不进路线库`：派完 `GET /api/routes` 里没有 `temp-`。
  - `test_临时路线_走不到的点400带第几个`
  - `test_临时路线_家人账号403`（`owner` 没有 `DISPATCH`）
  - `test_临时路线_点数0或21个400`
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑，PASS；再跑 `test_site_routes*.py`、`test_site_intercept*.py`**
- [ ] **Step 5: 提交** `git commit -m "站点:最近的机器人、保安的临时路线(W44)"`

---

### Task 6: 录像抓一帧（后端）

**Files:**
- Modify: `packages/site-node/src/d1max_site/recordings.py`（加 `frame_at`）、`api.py`
- Test: `packages/site-node/tests/test_site_recording_frame.py`

**Interfaces:**
- Produces:
  - `RecordingStore.covering(robot_id: str, camera: str, t_ms: int) -> dict | None`：开头 ≤ t < 开头 + `SEGMENT_S`
  - `frame_at(store, robot_id: str, camera: str, t_ms: int, *, ffmpeg: str = "ffmpeg", timeout_s: float = 5.0) -> bytes | None`：`ffmpeg -ss <偏移秒> -i <段文件> -frames:v 1 -vf scale=320:-2 -f image2 -c:v mjpeg pipe:1`；没有那一段、ffmpeg 失败或超时 → `None`
  - 结果按 `(段 id, 偏移整秒)` 缓存在内存，最多 256 张
  - `GET /api/recordings/frame?robot=&camera=&t=`（`VIEW`，记看证据的审计）→ `image/jpeg`；没有 → 404
- [ ] **Step 1: 写测试**：`covering` 的边界（正好在开头、在结尾前 1 ms、在结尾）；ffmpeg 换成一个假的可执行脚本，回固定字节 → 接口回 200 和这些字节；假脚本退出码 1 → 404；第二次同一秒不再调脚本（缓存）。
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑，PASS**
- [ ] **Step 5: 提交** `git commit -m "站点:按时刻从录像抓一帧(报警记录的抓拍)(W44)"`

---

### Task 7: 外壳、顶栏、路由、主题（前端）

**Files:**
- Create: `web/console/src/shell.tsx`（新顶栏、用户菜单、页面切换）
- Modify: `web/console/src/console.tsx`（只留数据和对话框的总装，页面切换交给 `shell.tsx`）、`app.tsx`、`theme.ts`、`styles.css`、`i18n.ts`
- Delete: 旧 Live 墙的页面入口（`live.tsx` 里 `AlarmTile`、`JumpStrip`、视频墙外壳；`StateText`、`stateShape` 留着给新页用）、`queue.tsx` 底部报警队列
- Create: `web/console/src/notices.tsx`（业主的临时「提醒」页）
- Test: `web/console/src/shell.test.tsx`

**Interfaces:**
- Consumes: Task 2 的 `audience`、Task 3 的 `verdict`
- Produces:
  - `model.ts`：`Alert` 加 `audience?: string`、`verdict?: string`；`GUARD_AUDIENCES = new Set(["intrusion", "fire", "help", "offline"])`；`forGuard(a: Alert): boolean`（老站点没有 `audience` 的一律算给保安，宁可多看）
  - `#/admin/notices`：只有 `admin` 能进，列出 `forGuard` 为假的未解决告警（说人话的标题、时间、「知道了」= ack、「处理好了」= resolve `handled`）；「管理」导航的第一项，有未处理的显示个数。第二期换成「家里的状况」。底部报警队列删了以后，技术提醒在这里看，不会没地方看。
  - `type GuardPage = "duty" | "live" | "map" | "recordings" | "alarms"`；`type Page = GuardPage | "admin/..."`（管理各页名照旧，另加 `"admin/localization"`、`"admin/reports"` 指向现有 `LocHistoryPage`、`ReportsPage`）
  - 路由 `#/duty`（默认）、`#/live`、`#/map`、`#/recordings?robot=&t=`、`#/alarms`；旧的 `#/live` 以外的老地址（`#/localization`、`#/reports`）跳到新地址。
  - `roleLabel(role: string): Key`：`admin → roleOwner`（「业主（最高权限）」/ "Owner (full access)"）、`guard → roleGuard`（「保安」/ "Guard"）、`owner → roleFamily`（「家人 / 只看」/ "Family / view only"）
  - `pageScheme(p: Page): "dark" | "light"`：保安五页 `dark`，`admin/*` `light`；个人在用户菜单里选过「跟随系统 / 深色 / 浅色」就按个人的（存 `localStorage`，读写包 try/catch）
  - `TopBar` 从左到右：品牌 + 站点名 → 导航（值班、实况、地图、录像、报警记录；`admin` 多一个「管理」）→ 报警数（只数 `audience` 在 `GUARD_AUDIENCES` 里的未解决 P1、P2；业主另有管理页的提醒，不在这里数）→ 布防状态（只显示：布防 / 在家 / 访客到 hh:mm）→ 时钟 → 用户名下拉（语言、外观、改密码、退出）→ 全部急停（有 `DISPATCH` 或 `owner` 角色才有，跟现在一样）
- [ ] **Step 1: 写测试 `shell.test.tsx`**
  - `默认进值班页`：空地址 → 值班页的 `main` 有 `data-page="duty"`
  - `保安看不到管理`：`role: "guard"` 时导航没有「管理」；`admin` 有
  - `顶栏没有语言按钮_在用户菜单里`
  - `布防状态只显示不能点`：顶栏里布防状态不是 button
  - `报警数只数给保安的`：一条 `audience: "owner"` 的 P2 不算
  - `保安页深色_管理页浅色`：进 `#/map` 后 `document.documentElement.dataset.theme == "dark"`，进 `#/admin/users` 后是 `"light"`
  - `老地址跳新地址`：`#/reports` → `#/admin/reports`
  - `业主的提醒页_只列技术类`：一条 `audience: "owner"`、一条 `"help"` → 提醒页只有前一条；保安进 `#/admin/notices` 看不到
- [ ] **Step 2: 跑，确认失败** `cd web/console && npx vitest run src/shell.test.tsx`
- [ ] **Step 3: 实现**（页面先放占位，Task 8–13 换成真的）
- [ ] **Step 4: 跑这份和全部 vitest，PASS**（删掉的旧组件的测试一起删，留下的照旧过）
- [ ] **Step 5: 提交** `git commit -m "网页:新外壳和顶栏,保安页深色、管理页浅色(W44)"`

---

### Task 8: 画面位组件（前端）

**Files:**
- Create: `web/console/src/slot.tsx`
- Modify: `web/console/src/camera.tsx`（`CameraWell` 加 `variant: "main" | "pip"`、断流时的「画面断了」样式）、`styles.css`、`i18n.ts`
- Test: `web/console/src/slot.test.tsx`

**Interfaces:**
- Consumes: `CameraWell`、`robotCams`、`StateText`、`UnitInfo`
- Produces:
  - `VideoSlot(props: { u: UnitInfo; now: number; pinned: boolean; onPin: () => void; onZoom: () => void; forceLive?: boolean; scene?: { camera: string; src: string } })`
    - 大画面默认前摄像头，小窗后摄像头；点小窗两路互换。
    - 机器人有 `thermal`（`robotCams` 里有）才显示「热像」按钮：点了大画面换热像、前摄像头进小窗，再点换回。
    - 顶上状态栏：名字 + `StateText`、速度（遥测有就显示 `0.6 m/s`，没有不显示）、电量、钉住（`aria-pressed`）、放大。
    - `scene` 给了（报警现场：固定摄像头画面或抓拍），大画面放它，小窗放这台机器人前摄像头。
  - 断流：`CameraWell` 停在最后一帧、变灰，写 `t("feedLost", { t: hh:mm:ss, s: N })` =「画面断了，正在重连… 最后画面 {t} · 已断 {s} 秒」/ "Picture lost, reconnecting… last frame {t} · {s} s ago"
- [ ] **Step 1: 写测试**
  - `点小窗两路互换`
  - `没有热像不显示热像按钮` / `有热像_点了大画面换热像`
  - `断流写最后画面和已断几秒`：模拟 `img` 的 `onError`，过 5 秒 → 文字里有「已断 5 秒」
  - `报警现场_大画面放现场小窗放前摄像头`
- [ ] **Step 2–4: 失败 → 实现 → 通过**
- [ ] **Step 5: 提交** `git commit -m "网页:画面位(前后互换、热像、断流)(W44)"`

---

### Task 9: 值班页（前端）

**Files:**
- Create: `web/console/src/duty.tsx`、`web/console/src/dutyrank.ts`
- Modify: `styles.css`、`i18n.ts`
- Test: `web/console/src/dutyrank.test.ts`、`web/console/src/duty.test.tsx`

**Interfaces:**
- Consumes: Task 2 的 `audience`、Task 3 的 `verdict` 接口、Task 8 的 `VideoSlot`、`MapCanvas`、`usePosePick`
- Produces:
  - `rankUnits(units: UnitInfo[], alerts: Alert[], dispatched: Map<string, string>): UnitInfo[]`：排序 = 派去处理报警的（`dispatched` 里有，值是告警 key）→ 有 `help` 类未解决告警的 → 巡逻中 → 待命、充电 → 离线；同档按编号。
  - `pickSlots(ranked: UnitInfo[], pins: [string | null, string | null], alarms: Alert[]): [SlotPick | null, SlotPick | null]`；`SlotPick = { robot: string; alert?: Alert }`。有报警时：位 1 给最急的那件报警，位 2 给第二件报警；只有一件时位 2 给派去那件的机器人以外排第一的。钉住的位不被报警以外的东西换掉；报警来时钉住的位也让出来（报警优先）。
  - `DutyPage(props: { units; alerts; now; me; mode; onAct })`
    - 上半屏两个 `VideoSlot`；下半屏左边状态条列表（名字、状态、电量、在哪个防区），右边 `MapCanvas`。点状态条 = 放进没钉住的那个画面位。
    - 报警横幅：`audience` 在 `GUARD_AUDIENCES` 里的最急一件；三个按钮「我来处理」（ack）、「确认有人」（resolve `intruder`）、「误报，关掉」（resolve `false_alarm`，先确认一下）；`help` 类第三个按钮写「处理好了」（resolve `handled`）。多件写「还有 N 件」。
    - `loc_lost_paused`、`loc_lost_idle` 的横幅多一个「帮它找位置」，打开地图上的 `usePosePick` 设位置模式（现有，保安能用）。
    - 报警的防区在地图上标红，画派过去那台机器人的规划路径（现有 runviz 层）。
- [ ] **Step 1: 写测试**
  - `dutyrank.test.ts`：`派去处理报警的排第一`、`需要帮忙的排在巡逻前`、`钉住的不被巡逻的换掉`、`报警来时钉住的位让出来`、`两件报警_两个画面位各给一件`
  - `duty.test.tsx`：`确认有人发intruder`（断言请求体 `{"verdict": "intruder"}`）、`误报要先确认`、`别人处理掉了_横幅消失`（resolve 回 404 → 横幅不在、提示「已经有人处理了」，只提示一次）、`技术类不出横幅`（`audience: "owner"` 的 P1 不显示）、`迷路了有帮它找位置`、`数据过期_状态写未知`
- [ ] **Step 2–4: 失败 → 实现 → 通过**
- [ ] **Step 5: 提交** `git commit -m "网页:值班页(两个画面位、地图、报警横幅)(W44)"`

---

### Task 10: 实况页（前端）

**Files:**
- Create: `web/console/src/livewall.tsx`
- Modify: `styles.css`、`i18n.ts`
- Test: `web/console/src/livewall.test.tsx`

**Interfaces:**
- Consumes: `rankUnits`（Task 9）、`VideoSlot` 的状态栏（Task 8 里抽出来的 `SlotBar`）、`CameraWell`
- Produces: `LiveWall(props: { units; alerts; now; onZoom })`
  - 2×2 卡片，每张一台机器人，前、后两路左右并排，一直在播；按 `rankUnits` 排；超过 4 台往下翻（卡片高度 = 视口高度减顶栏的一半）。
  - 每张卡片「热像」按钮（有热像才有）：后摄像头那一半换成热像。页面右上角「全部看热像」开关：有热像的卡片全换。
  - 放大：全屏看这一台（现有 `FullCamera`）。
- [ ] **Step 1: 写测试**：`按紧急程度排`、`全部看热像只换有热像的`、`离线的卡片写离线不拉流`
- [ ] **Step 2–4: 失败 → 实现 → 通过**
- [ ] **Step 5: 提交** `git commit -m "网页:实况页(2×2、前后并排、热像)(W44)"`

---

### Task 11: 地图页（前端）

**Files:**
- Modify: `web/console/src/mappage.tsx`（改成新样子）、`mapcanvas.tsx`（加图层开关和斜线层）、`styles.css`、`i18n.ts`
- Create: `web/console/src/maplayers.ts`
- Test: `web/console/src/maplayers.test.ts`、`web/console/src/mappage.test.tsx`

**Interfaces:**
- Consumes: Task 4 `/api/coverage`、Task 5 `/api/robots/nearest` 和临时路线、现有 `/api/mode/zones`、`/api/lists/authorizations`、`/api/cameras`、`/api/alerts?all=1`
- Produces:
  - `type Layer = "zones" | "cameras" | "route" | "standby" | "events" | "stale" | "nogo"`；`DEFAULT_LAYERS: Set<Layer>` = 除 `nogo` 外全开；开关存 `localStorage`（try/catch）
  - `STALE_AFTER_MS = 2 * 3_600_000`（设计 §11 第 4 条，第二期挪到「布防时间」页可改）
  - `staleCells(cells: {i: number; j: number; t: number}[], now: number, after: number): {i: number; j: number; age_ms: number}[]`
  - `tonightEvents(alerts: Alert[], since: number): Alert[]`：只要给保安的四类，按时间倒序
  - `shiftStart(now: number, hours: [number, number] = [7, 19]): number`（报警记录页也用）
  - `etaNext(path: Pt[] | null, at: Pt | null, mps = 0.5): number | null`：沿现有 runviz 规划路径（机器人报的）量到路径终点（就是下一个巡检点）的米数 ÷ 速度，回秒；没有路径回 `null`，界面就不写预计时间
  - 右栏：图层开关，「今晚的事」列表（点 = 跳到 `#/recordings?robot=&t=`）。
  - 点地图任意一处：弹一个小框「让最近的机器人过去看看（A，约 1 分钟）」，确认后发 `goto`；「按几个点走一趟」模式：依次点点，「开始」发临时路线，「取消」清掉。
  - 保安的「全部布防」按钮放在右栏防区图层下面（模式不是布防时才出现；设计 §11 第 14 条）。
  - 不显示：定位质量、点云、代价地图、近 7 天定位出问题的地方（挪到管理的定位页，第四期）。
- [ ] **Step 1: 写测试**
  - `maplayers.test.ts`：`etaNext_沿路径量不按直线`、`超过两小时的格子才算没巡过`、`今晚的事只要给保安的`、`交班时间_晚上8点算19点那一班_早上6点算前一天19点那一班`
  - `mappage.test.tsx`：`点图问最近的再确认才派`、`没有能派的说清楚`、`临时路线按顺序发点`、`禁行区默认关`
- [ ] **Step 2–4: 失败 → 实现 → 通过**
- [ ] **Step 5: 提交** `git commit -m "网页:地图页(图层、今晚的事、过去看看、临时路线)(W44)"`

---

### Task 12: 录像页（前端）

**Files:**
- Modify: `web/console/src/recpage.tsx`（改成新样子）、`recordings.ts`、`styles.css`、`i18n.ts`
- Test: `web/console/src/recordings.test.ts`（加）、`pages.test.tsx` 里录像页那几条按新界面改

**Interfaces:**
- Consumes: Task 4 `/api/robots/{id}/track`、现有 `/api/recordings`、`/api/recordings/{id}/video|keep`
- Produces:
  - `poseAt(points: TrackPt[], t: number): TrackPt | null`：取 ≤ t 的最后一个点，跟它差超过 `MAX_GAP_MS`（20 000）就算那一刻不知道在哪
  - `nearestTime(points: TrackPt[], x: number, y: number, map: {map_id; version}): number | null`：点轨迹 → 离点击处最近的那个点的时刻（只看同一张图）
  - `lanes(recs: Recording[], alerts: Alert[], day: {since; until}): Lane[]`；`Lane = { robot: string; segs: [number, number][]; marks: { t: number; kind: "alarm" | "help" }[] }`
  - 页面：上面日期、跳到时间、选机器人（一次一台）、选哪一路（前 / 后 / 热像，有才显示）；左边播放器（大画面 + 小窗另一路，左上角「机器人 A · 前 · 日期时间」，播放、倍速、±10 秒、截图、导出这一段（有 `EXPORT` 才有）、留作证据（有 `REVIEW` 才有））；右边地图（那一刻的位置，之前实线、之后虚线，点轨迹跳时刻）；下面时间轴每台一行，共用播放线，滚轮缩放（一整天 ↔ 5 分钟）。
- [ ] **Step 1: 写测试**：`poseAt_空档超过20秒算不知道`、`nearestTime_只看同一张图`、`lanes_报警和需要帮忙分开标`；页面：`点轨迹跳到那个时刻`、`保安没有导出按钮`、`换机器人旧的回包不串`（沿用现有外审 I1 那条思路）
- [ ] **Step 2–4: 失败 → 实现 → 通过**
- [ ] **Step 5: 提交** `git commit -m "网页:录像页(播放器、当时的位置、时间轴)(W44)"`

---

### Task 13: 报警记录页（前端）

**Files:**
- Create: `web/console/src/alarmlog.tsx`
- Modify: `styles.css`、`i18n.ts`
- Test: `web/console/src/alarmlog.test.tsx`

**Interfaces:**
- Consumes: Task 3 `verdict`、`/api/shift-notes`；Task 6 `/api/recordings/frame`；Task 11 `shiftStart`；现有 `/api/alerts?all=1&limit=`
- Produces: `AlarmLogPage(props: { now; me })`
  - 上面：本班出的事、还没处理、有人闯入三个数；上一班留的话（谁、几点）；「给下一班留话」（有 `HANDLE_ALERTS` 才有，500 字）。
  - 筛选：本班 / 今天 / 选日期 × 全部 / 有人闯入 / 机器人需要帮忙 / 机器人掉线（起火、过热有了 kind 再出现）× 只看没处理的。
  - 每行：抓拍（`/api/recordings/frame?robot=&camera=front&t=first_ms`，拿不到显示占位）、时间、什么事（`alertTitle` + 一行补充）、结论和谁（`verdict` → 「确认有人」「误报」「处理好了」，空的写「已处理」；没解决的写「还没处理」，左边橙色竖条）、按钮「看录像」（`#/recordings?robot=&t=`）或「去处理」（`#/duty`）。
  - 回包 `truncated` 为真时写「只显示最近 N 条」（不悄悄截）。
- [ ] **Step 1: 写测试**：`本班按19点算`、`只看没处理的`、`误报写误报和谁`、`留话写进去显示成上一班的`（重新拉一次）、`截了写明`、`技术类不列`
- [ ] **Step 2–4: 失败 → 实现 → 通过**
- [ ] **Step 5: 提交** `git commit -m "网页:报警记录页(本班、交班留言、筛选)(W44)"`

---

### Task 14: 构建、演示站点核对、文档、全量

**Files:**
- Modify: `packages/site-node/src/d1max_site/web/`（构建产物）
- Create: `docs/W44-第一期-完工报告.md`
- Modify: `docs/真机验证.md`（新增 §3aa 真机项）、`docs/庄园安防-差距核查与工单.md`（W44 行）

- [ ] **Step 1: 构建** `cd web/console && npm run build`，Expected: 无类型错误，产物写进站点包。
- [ ] **Step 2: 演示站点核对**：后台演示站点（`tools/site_demo.py`，已在跑的先 TaskStop 再起）上用截图脚本截五页（深色），对照设计 §4 逐条看：顶栏只有设计里的那几样、保安页没有技术字眼、没有「狗」。截图发给用户。
- [ ] **Step 3: 全量**：lint `.venv/bin/ruff check packages src tests tools/make_release.py`；Python 四组（约 20 分钟，先告诉用户）；vitest；flutter（约 2 分钟）。Expected: 全过。看日志尾再下结论。
- [ ] **Step 4: 写完工报告**：做了什么、没做什么（第二到四期、W45、热像温度）、真机项（实况页 4 台 × 2 路的站点 CPU 和机器人上行、4G 下的画面、热像按钮在真热像上、轨迹和录像对时）。没做的写清楚：机器人走 WiFi 还是 4G 的显示（机器人还不报，随 W45）、热像温度（等实物）、工程师临时账号和其它管理页（第二到四期）。
- [ ] **Step 5: 提交、开 PR、等九项检查、合并**
