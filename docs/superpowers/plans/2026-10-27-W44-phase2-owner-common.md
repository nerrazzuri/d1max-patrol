# W44 第二期：业主「管理」的常用组 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本项目用户禁止子代理（2026-10-15）：只能由主会话按 superpowers:executing-plans 一项一项做。

**Goal:** 业主打开「管理」先看到「家里的状况」，常用组五页（布防时间、授权和访客、用户、机器人、报表）按设计 §5.0、§5.1 重做；后端补上布防时间表、工程师临时账号、机器人概览和改名、按天汇总的报表。

**Architecture:** 后端（`packages/site-node`）：库结构 62；新模块 `sitetime.py`（站点用哪个时区）、`arming_schedule.py`（布防时间表、例外日期、交班时间和「多久没巡过」）、`robot_overview.py`（机器人卡片要的数）、`reports.summary`（按天汇总）；`modes.ArmingDesk` 接上时间表；`accounts.py` 加工程师临时账号、删除、最后登录、再输一次密码。前端（`web/console`）：「管理」左边按 常用 / 布置 / 高级 三组排；常用六页新写，旧的提醒页、名单页、用户页、机器人页、报表页删掉；布置、高级两组先挂现有页面（第三、四期重做）。

**Tech Stack:** Python 3（站点，`ThreadingHTTPServer` + SQLite），Preact 10 + `@preact/signals` + Vite + Vitest（happy-dom），Flutter（只动布防模式那一行字）。

**Spec:** `docs/superpowers/specs/2026-10-27-site-console-redesign-design.md`（下文「设计 §x」指它）。第一期计划和完工报告：`docs/superpowers/plans/2026-10-27-W44-phase1-guard-pages.md`、`docs/W44-第一期-完工报告.md`。

## Global Constraints

- 界面一律写「四足机器人」，挤的地方写「机器人」；英文写 robot；**不写「狗」「dog」**（`tests/test_ui_wording.py` 看着）。（设计 §2.1）
- 「管理」默认浅色；每页开头一句话说这一页干什么。（设计 §2.2、§5）
- 界面名字：业主（最高权限）= `admin`，保安 = `guard`，家人 / 只看 = `owner`，工程师（临时）= 新角色 `engineer`。后端名字不改（第一期 Ruling 同口径），旧库照样认。（设计 §3）
- 权限在站点后端判，网页只是不显示按钮。工程师 = 业主的全部权限减去管账号（`manage_accounts`）。（设计 §3、§9）
- 最后一个启用的业主账号删不掉、停不了、降不了（现有规矩，删除也算）。（设计 §3、§9）
- 「再输一次密码」这一期管两样：开工程师账号、解绑机器人。（设计 §5.2 第 4 条）
- 时间按**站点时区**算：路线库设过时区（`route_store.timezone`）就用它，没设用站点主机的本地时区。网页不自己算星期、日子、时区（第一期 Ruling 7 同口径）。
- 断网不影响看家：时间表、工程师账号到点都在站点本机判，不依赖外网。（设计 §2.3、§9）
- 网页 CSP 不许内联脚本、`<style>` 标签；样式进 `styles.css`。
- 界面文字走 `i18n.ts` 的 `t(key)`，英中两份都写。
- 网页构建产物 `packages/site-node/src/d1max_site/web/` 要提交。
- 改 `SCHEMA_VERSION` 的提交要带上一版（61）的快照 `packages/site-node/tests/fixtures/schema/site-v61.sql`（Python `iterdump`，去掉 `sqlite_sequence`，首行注释）。
- 告警、模式回包形状变了要重生成手机夹具（`D1MAX_WRITE_FIXTURES=1` 跑 `test_site_mobile_fixtures.py`）。
- 不用 `dart format`；手机中文放 `tr()` 第二参数。
- 提交结尾两行：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`、`Claude-Session: https://claude.ai/code/session_01XrN14Jvh7JjXFNpNn91N2Z`。

## Review Focus

1. **手动切模式和时间表谁说了算**：业主手动切了，到下一个时间段的边界之前以手动为准，过了边界回到时间表；访客模式到点也回到时间表（不是回到开访客之前的手动模式）。测试见 Task 2 `test_手动切到下一个边界为止`、`test_访客到点回时间表`。
2. **站点主机是 UTC、路线库时区是吉隆坡**：时间表、「今天」、报表按天分都按吉隆坡算。测试见 Task 1 `test_站点时区_路线库设了用它`、Task 2 `test_时间表按站点时区`、Task 7 `test_按天分_按站点时区`。
3. **工程师账号到点**：到点以后已经登录着的那个令牌下一次请求就 401，不等闲置期；登录回「账号或口令不对」。测试见 Task 3 `test_工程师到点_令牌立刻失效`。
4. **工程师想给自己续命**：工程师调 `/api/accounts*` 一律 403；业主也不能把工程师账号改成别的角色（只能提前关、删）。测试见 Task 3 `test_工程师不能管账号`、`test_工程师账号不能改角色`。
5. **时间段到 24:00、例外跨月**：一天的时间段可以写到 24:00（=1440）；例外「10 月 30 日到 11 月 2 日」两头都算。测试见 Task 2 `test_时间段到2400`、`test_例外两头都算`。

---

### Task 1: 库结构 62、站点时区、值班设置（后端 + 两处网页读设置）

**Files:**
- Modify: `packages/site-node/src/d1max_site/db.py`（`SCHEMA_VERSION = 62`；`_ADDED_COLUMNS` 加 `("accounts", "expires_ms", "INTEGER")`、`("accounts", "last_login_ms", "INTEGER")`、`("site_mode", "manual_until_ms", "INTEGER")`、`("robots", "display_name", "TEXT NOT NULL DEFAULT ''")`、`("authorizations", "ended_until_ms", "INTEGER")`；`_DDL` 加 `arming_slots`、`arming_exceptions`、`site_settings` 三张表）
- Create: `packages/site-node/tests/fixtures/schema/site-v61.sql`
- Create: `packages/site-node/src/d1max_site/sitetime.py`
- Create: `packages/site-node/src/d1max_site/site_settings.py`
- Modify: `api.py`（`GET/POST /api/duty-settings`）、`main.py`（接上）
- Modify: `web/console/src/maplayers.ts`、`alarmlog.tsx`、`mappage.tsx`（交班时间、「多久没巡过」改读站点）
- Test: `packages/site-node/tests/test_site_sitetime.py`、`test_site_settings.py`（新）；`test_site_schema_upgrade.py`（自动覆盖）；`web/console/src/maplayers.test.ts`、`alarmlog.test.tsx`（补）

**Interfaces:**
- Produces:
  - 表 `arming_slots(zone TEXT NOT NULL, day INTEGER NOT NULL, start_min INTEGER NOT NULL, end_min INTEGER NOT NULL, PRIMARY KEY (zone, day, start_min))` —— `zone=''` 是「全部防区一样」那一份；`day` 0=星期一 … 6=星期日；`0 <= start_min < end_min <= 1440`。
  - 表 `arming_exceptions(id INTEGER PRIMARY KEY AUTOINCREMENT, first_day TEXT NOT NULL, last_day TEXT NOT NULL, state TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_ms INTEGER NOT NULL)` —— 日期 `YYYY-MM-DD`，`state` 是 `armed` / `disarmed`。
  - 表 `site_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)`（值是 JSON）。
  - `sitetime.site_tz(db: SiteDB) -> tzinfo`：`route_store.timezone` 有就 `ZoneInfo(它)`，没有、认不出来就本地时区（`datetime.now().astimezone().tzinfo`）。
  - `sitetime.local_day_start(ms: int, tz: tzinfo) -> int`（那一天 0 点的毫秒）、`sitetime.day_text(ms, tz) -> "YYYY-MM-DD"`。
  - `SiteSettings(db)`：`duty() -> {"shift_hours": [7, 19], "stale_hours": 2}`；`set_duty(d, *, by) -> dict`。`shift_hours` 两个不同的 0–23 整数；`stale_hours` 1–48 整数；错了抛 `SettingsError`（400）。
  - `GET /api/duty-settings`（`view`，保安页面也要）、`POST /api/duty-settings`（`manage`，进审计）。
  - 网页：`shiftStart(now, hours)` 已有；`STALE_AFTER_MS` 换成按 `stale_hours` 算；`useDutySettings()`（`data.ts`）读一次、拿不到用默认 [7,19] / 2。

- [ ] **Step 1: 写测试**

```python
def test_站点时区_路线库设了用它(tmp_db):            # route_store.timezone='Asia/Kuala_Lumpur' → site_tz 是它
def test_站点时区_没设用本地(tmp_db):                # 没有 route_store 那一行 → 本地时区
def test_站点时区_认不出来用本地(tmp_db):            # 'Mars/Base' → 本地,不抛
def test_一天的开始(tz=ZoneInfo("Asia/Kuala_Lumpur")):  # 2026-10-27 03:00 吉隆坡 → 当天 00:00 吉隆坡
def test_值班设置_默认():          assert SiteSettings(db).duty() == {"shift_hours": [7, 19], "stale_hours": 2}
def test_值班设置_存了读回来():     set_duty({"shift_hours": [8, 20], "stale_hours": 3}) 再 duty()
@pytest.mark.parametrize("bad", [{"shift_hours": [7, 7]}, {"shift_hours": [7, 24]}, {"shift_hours": [7]},
                                 {"stale_hours": 0}, {"stale_hours": 49}, {"stale_hours": True}])
def test_值班设置_不合规矩(bad):    pytest.raises(SettingsError)
def test_值班设置_接口权限():       # 保安 GET 200、POST 403;业主 POST 200 并进审计
```

网页：`maplayers.test.ts` 加 `staleAfter(3) === 3 * 3_600_000`；`alarmlog.test.tsx` 加「交班时间按站点设置」（站点回 [8,20]，8:30 时本班从 8:00 算）。

- [ ] **Step 2: 跑，确认失败**（`ModuleNotFoundError: d1max_site.sitetime`；vitest 找不到 `staleAfter`）

- [ ] **Step 3: 写库结构、v61 快照、两个模块、接口、网页读设置**

- [ ] **Step 4: 跑这几个文件 + `test_site_schema_upgrade.py` + vitest，PASS**

- [ ] **Step 5: 提交** `git commit -m "站点:库结构 62、站点时区、交班时间和多久没巡过可设(W44 第二期)"`

---

### Task 2: 布防时间表（后端）

**Files:**
- Create: `packages/site-node/src/d1max_site/arming_schedule.py`
- Modify: `packages/site-node/src/d1max_site/modes.py`（`ArmingDesk` 接时间表；`view()` 多几个键；`tick()` 管边界）
- Modify: `api.py`（`GET/POST /api/arming/schedule`、`POST /api/arming/exceptions`、`POST /api/arming/exceptions/{id}/remove`）、`main.py`
- Modify: `mobile/lib/ui/site_mode.dart`（按时间表切的那一行字）
- Test: `packages/site-node/tests/test_site_arming_schedule.py`（新）、`test_site_modes.py`（补）、手机 `test/site_mode_test.dart`（补一条）

**Interfaces:**
- Consumes: Task 1 的表、`site_tz`。
- Produces:
  - `ArmingSchedule(db, *, now_ms, tz: Callable[[], tzinfo])`：
    - `view() -> {"enabled": bool, "slots": [{"zone", "day", "start", "end"}], "exceptions": [{"id", "first_day", "last_day", "state", "note"}], "today": [{"zone", "windows": [["HH:MM","HH:MM"]]}]}`（`"24:00"` 写成 `"24:00"`）
    - `put(d, *, by)`：整份换 `{"enabled": bool, "slots": [...]}`；每条 `start < end`，`"HH:MM"`，`end` 可以 `"24:00"`；同一防区同一天的段不许重叠；最多 500 段；错了抛 `ScheduleError`。
    - `add_exception(d, *, by) -> int`、`remove_exception(id) -> bool`；`first_day <= last_day`，跨度最多 366 天，最多 100 条。
    - `scheduled(zone: str, now_ms: int) -> bool | None`：没开时间表回 `None`。例外日期整天按例外（全站）；否则这个防区有自己的段用自己的，没有用 `zone=''` 的，都没有回 `True`（没配过一律布防，跟 `modes` 同一条规矩）。
    - `next_boundary(now_ms: int) -> int | None`：下一个可能变的时刻（今天起 8 天内每个段的头尾 + 每个 0 点），没开回 `None`。
  - `ArmingDesk.schedule: ArmingSchedule | None`（站点主程序接上）。
  - `ArmingDesk.armed(zone)`：时间表开着、而且没有在手动期内（`manual_until_ms` 为空或已过）→ 按 `scheduled()`；否则按现有模式规矩。
  - `set_mode(...)`：时间表开着时写 `manual_until_ms`：访客 = 访客的 `until_ms`；别的 = `next_boundary(now)`。没开时间表写 `NULL`。
  - `tick()`：访客到点 → 时间表开着就清掉手动期（回时间表），没开照旧退回 `prev_mode`；手动期过了 → 清掉、推一次；时间表管着时任何防区的布防状态变了 → 推一次、审计一条（`set_by='site:schedule'`，审计动作 `arming.schedule`）。
  - `view()` 多：`"source": "schedule" | "manual"`、`"state": "armed" | "partial" | "disarmed"`（所有已知防区都布防 / 部分 / 都撤防）、`"manual_until_ms"`、`"next_change_ms"`。`label` 在时间表管着时写「按时间表：布防 / 部分布防 / 撤防」。
  - 接口权限：`GET /api/arming/schedule` `view`；改 `manage`；都进审计。

- [ ] **Step 1: 写测试**（`tz` 一律 `ZoneInfo("Asia/Kuala_Lumpur")`，钟用假的）

```python
def test_没开时间表_照旧按模式():          # scheduled() is None;armed() 跟现在一样
def test_时间段内布防段外撤防():           # 星期一 22:00–24:00 全部防区;星期一 23:00 armed,21:00 不
def test_时间段到2400():                   # end "24:00" → 23:59 布防,第二天 00:00 看第二天的段
def test_防区有自己的段用自己的():         # '' 全天布防;'后花园' 只 20:00–06:00 → 后花园 12:00 撤防,前门 12:00 布防
def test_没配过的防区一律布防():           # 只有 '后花园' 的段、没有 '' 的段 → '车库' 布防
def test_例外两头都算():                   # 10-30 到 11-02 armed:10-30 00:00 和 11-02 23:59 都布防,11-03 00:00 回时间表
def test_例外撤防盖过时间段():
def test_时间表按站点时区():               # 主机 UTC、tz=吉隆坡:吉隆坡 22:30 那一刻在段内
def test_下一个边界():                     # 星期一 21:00 → 22:00;没开 → None
def test_段重叠不让存(): / test_开始不早于结束不让存(): / test_格式不对不让存():
def test_手动切到下一个边界为止():         # 段内(布防)业主切在家 → manual,到 22:00 边界前按在家;过了边界 source 回 schedule
def test_访客到点回时间表():               # 时间表开着,开 30 分钟访客 → 到点 source=schedule,不是 prev_mode
def test_保安全部布防也算手动():           # arm → manual 到下一个边界
def test_时间表变了推一次记一条审计():     # tick 跨过 22:00 → publish 一次 {"kind":"mode"},audit 一条 arming.schedule
def test_view_state():                     # 三个防区两个布防 → "partial"
def test_接口权限():                       # 保安 GET 200、POST 403;业主 POST 200
```

手机：`site_mode_test.dart` 加一条：`set_by == 'site:schedule'` 时显示「按时间表切的」/ "Switched by the schedule"。

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写 `arming_schedule.py`，改 `modes.py`、接口、主程序；手机那一行**

模式回包多了键：重生成手机夹具。

- [ ] **Step 4: 跑 `test_site_arming_schedule.py`、`test_site_modes.py`、`test_site_incidents.py`（入侵派不派看 `armed()`）、手机 `flutter test test/site_mode_test.dart`，PASS**

- [ ] **Step 5: 提交** `git commit -m "站点:布防时间表、例外日期,手动切到下一个边界为止(W44 第二期)"`

---

### Task 3: 工程师临时账号、删账号、最后登录、再输一次密码（后端）

**Files:**
- Modify: `packages/site-node/src/d1max_site/permissions.py`（`ROLES["engineer"] = ALL - {MANAGE_ACCOUNTS}`）
- Modify: `packages/site-node/src/d1max_site/accounts.py`
- Modify: `packages/site-node/src/d1max_site/audit.py`（`list(limit, actor=None)`，每行带 `actor_role`）
- Modify: `api.py`（`POST /api/accounts/engineer`、`POST /api/accounts/{name}/remove`、`GET /api/audit?actor=`；`_confirm(user, d)`）
- Test: `packages/site-node/tests/test_site_accounts.py`（补）、`test_site_engineer.py`（新）

**Interfaces:**
- Produces:
  - `Accounts.add_engineer(display_name: str, hours: int, *, by: str) -> tuple[str, str, int]` → `(账号名, 口令, 到期时刻)`。`hours` 只收 4 或 24。账号名 `eng-YYYYMMDD-HHMM`（站点时区；重名加 `-2`、`-3`）；口令 `secrets.token_urlsafe(12)`，只回这一次。
  - `Accounts.check()`、`login()`：`expires_ms` 过了 → 当停用（令牌删掉、回 None；登录回「账号或口令不对」）。
  - `Accounts.add()` / `update()` / `set_role()` 不收 `engineer`（只能走 `add_engineer`）；工程师账号 `update` 只许 `disabled=True` 和改显示名。
  - `Accounts.remove(name: str, *, by: str) -> None`：最后一个启用的业主不许删；不许删自己（接口那层判）；连 `sessions`、`push_devices` 一起删。
  - `Accounts.verify(name: str, password: str) -> bool`：跟登录同一个失败计数和锁。
  - `Accounts.list()` 每条多 `expires_ms`、`last_login_ms`、`expired: bool`。登录成功写 `last_login_ms`。
  - `ApiHandler._confirm(user, d)`：`d["confirm_password"]` 验不过回 403「密码不对」；锁了回 429。
  - `AuditLog.list(limit=200, actor: str | None = None)`，每行多 `actor_role`（`LEFT JOIN accounts`，删了的账号是 `null`）。

- [ ] **Step 1: 写测试**

```python
def test_开工程师账号(api):           # 业主带 confirm_password POST /api/accounts/engineer {display_name:"李工", hours:4}
                                      # → 200,{name, password, expires_ms};能用这个口令登录;角色 engineer
def test_开工程师账号要再输密码():     # 没带、带错 → 403;带错 5 次 → 429
def test_工程师只收4或24小时():        # hours 1 / 48 / "4" → 400
def test_工程师到点_令牌立刻失效():    # 登录拿令牌,钟拨过 expires_ms → 下一次 GET /api/robots 401;再登录 401
def test_工程师能做业主的事():         # 工程师 POST /api/duty-settings 200;GET /api/audit 200
def test_工程师不能管账号():           # 工程师 GET/POST /api/accounts、/api/accounts/engineer → 403
def test_工程师账号不能改角色():       # 业主 POST /api/accounts/eng-… {role:"admin"} → 400;{disabled:true} → 200
def test_普通加账号不收engineer角色(): # POST /api/accounts {role:"engineer"} → 400
def test_删账号():                     # 删保安 → 列表里没了、它的令牌 401
def test_最后一个业主删不掉():         # → 400 「这是最后一个启用的业主」
def test_不能删自己():                 # → 400
def test_最后登录时间():               # 登录后 list() 那一行 last_login_ms == 钟
def test_审计按人查():                 # ?actor=guard1 只回 guard1 的;工程师那几行 actor_role == "engineer"
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**（`HttpError` 文字里业主叫「业主」，不叫 admin）

- [ ] **Step 4: 跑 `test_site_accounts.py`、`test_site_engineer.py`、`test_site_api*.py` 里管账号的几个，PASS**

- [ ] **Step 5: 提交** `git commit -m "站点:工程师临时账号、删账号、最后登录、敏感操作再输一次密码(W44 第二期)"`

---

### Task 4: 机器人概览、改名、解绑要再输密码（后端）

**Files:**
- Create: `packages/site-node/src/d1max_site/robot_overview.py`
- Modify: `registry.py`（`display_name` 读写）、`dispatcher.robot_view`（带 `name`）、`api.py`（`GET /api/robots/overview`、`POST /api/admin/robots/{id}/name`；`…/revoke` 要 `_confirm`）
- Test: `packages/site-node/tests/test_site_robot_overview.py`（新）、`test_site_admin_robots.py`（补）

**Interfaces:**
- Consumes: Task 1 `site_tz`、`local_day_start`；Task 3 `_confirm`；第一期 `alert_audience.audience`。
- Produces:
  - `overview(dispatcher, db, *, now_ms, tz) -> list[dict]`，每台：`robot_id`、`name`（没起名是 `""`）、`online`（在线且新鲜）、`battery_pct`（不新鲜是 `null`）、`patrols_today`（今天 0 点起 `commands.kind='patrol'` 且 `ack_result` 不是 `rejected` / `expired` / `timeout`）、`problems`（这台没解决的 `help` / `offline` 告警 `[{key, kind, first_ms}]` + 设备不在线的 `[{device, state}]`）、`recent_problems`（近 30 天这台的 `help` / `offline` 告警，最多 50 条，新的在前）、`agent`（`capabilities.agent`）、`compat`（`/api/versions` 的结论）、`expires_at`、`revoked`。
  - `GET /api/robots/overview`（`manage`）。
  - `POST /api/admin/robots/{id}/name {name}`（`manage`）：1–32 字，不许控制字符；空串 = 不起名。
  - `robot_view()` 多 `name`；告警标题里的机器人这一期还用编号（不动告警）。
  - 吊销（解绑）要 `confirm_password`。

- [ ] **Step 1: 写测试**

```python
def test_概览_今天巡了几趟():        # 昨天 23:00 一条、今天 08:00 两条(一条 rejected)→ 1
def test_概览_今天按站点时区():      # 主机 UTC、tz 吉隆坡:吉隆坡 00:30 那一条算今天
def test_概览_毛病():                # 一条没解决的 stuck、一条已解决的 fallen、devices 里前摄像头 offline
                                     # → problems 有 stuck 和前摄像头;recent_problems 有两条
def test_概览_技术告警不算毛病():    # cert_expiring 不进 problems
def test_概览_离线电量是null():
def test_改名(): / test_改名_太长和控制字符(): / test_改名_空串清掉():
def test_robots回包带name():
def test_解绑要再输密码():           # 没带 confirm_password → 403,带对 → 200
def test_概览权限():                 # 保安 403、业主 200、工程师 200
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: 跑这两个文件 + `test_site_dispatcher*.py` 里看 `robot_view` 的，PASS**

- [ ] **Step 5: 提交** `git commit -m "站点:机器人概览、起名、解绑再输密码(W44 第二期)"`

---

### Task 5: 按天汇总的报表、授权提前结束和「今天有没有」（后端）

**Files:**
- Modify: `packages/site-node/src/d1max_site/reports.py`（加 `summary`）
- Modify: `packages/site-node/src/d1max_site/authz.py`（`end_now(id, *, by)`；`list(with_active=True)` 每条多 `on_today`；`_live` 看 `ended_until_ms`）
- Modify: `api.py`（`GET /api/reports/summary?since=&until=`；`POST /api/lists/authorizations/{id}/end`）
- Test: `packages/site-node/tests/test_site_reports.py`（补）、`test_site_lists.py`（补）

**Interfaces:**
- Consumes: Task 1 `site_tz`、`day_text`；第一期 `audience`、`alerts.verdict`。
- Produces:
  - `summary(db, since_ms, until_ms, *, tz) -> dict`（最长 31 天，同 `daily` 的校验）：
    - `totals`: `{"patrols", "incidents", "false_alarms", "robot_problems"}`。巡逻 = `commands.kind='patrol'` 且没被拒；出事 = `audience == "intrusion"` 或 `"fire"` 的告警；误报 = 其中 `verdict == "false_alarm"`；机器人出问题 = `audience` 是 `help` / `offline` 的告警。
    - `days`: 每个站点时区的日子一条 `{"day": "YYYY-MM-DD", 四个数}`，没有事的日子也列（柱状图要连续）。
    - `events`: 给保安的告警（四类），`[{key, kind, level, robot, title, first_ms, verdict, resolved_by, audience}]`，按时间，最多 `ROWS_MAX`，截了 `truncated: true`。
  - `GET /api/reports/summary`（`view_audit`，跟现有日报同一个权限）。
  - `AuthzBook.end_now(id, *, by) -> bool`：此刻生效的那一段提前结束（`ended_until_ms` = 这一段的结束时刻），下一段照常。不在生效中回 `False`（接口 409「现在不在授权时间里」）。
  - `list(with_active=True)` 每条多 `on_today`：今天（站点时区）有没有这一条的时间段。

- [ ] **Step 1: 写测试**

```python
def test_汇总_四个数():                # 2 次 patrol、1 次入侵(误报)、1 次入侵(确认有人)、1 次 stuck、1 次 cert_expiring
                                      # → patrols 2, incidents 2, false_alarms 1, robot_problems 1
def test_汇总_没事的日子也列():        # 7 天 → days 长 7
def test_按天分_按站点时区():          # 吉隆坡 00:30 的告警归吉隆坡那一天
def test_汇总_最长31天():              # 32 天 → ReportError
def test_汇总_事件只列给保安的():
def test_授权提前结束():               # 09:00–18:00 的园丁,10:00 end_now → 10:01 不生效;明天 09:30 照常生效
def test_授权提前结束_不在时间里():    # 08:00 end_now → False,接口 409
def test_授权今天有没有():             # 只有周末的那条,星期一 on_today False
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: 跑 `test_site_reports.py`、`test_site_lists.py`，PASS**

- [ ] **Step 5: 提交** `git commit -m "站点:报表按天汇总、授权提前结束(W44 第二期)"`

---

### Task 6: 「管理」外框：三组导航、工程师角色（网页）

**Files:**
- Modify: `web/console/src/shell.tsx`（`AdminPage` 换成新的一组；`AdminNav` 分三组带小标题；`roleLabel` 认 `engineer`）
- Modify: `web/console/src/model.ts`（`isOwnerLike(role)` = admin 或 engineer；`canDispatch`、`canArm` 认 engineer）、`console.tsx`（`admin` 判断换 `isOwnerLike`；用户页只给 admin）、`recpage.tsx`、`alarmlog.tsx`（认 engineer）
- Modify: `web/console/src/i18n.ts`
- Test: `web/console/src/shell.test.tsx`（补）、`topbar.test.ts`（补）

**Interfaces:**
- Produces:
  - `AdminPage = "admin/home" | "admin/arming" | "admin/access" | "admin/users" | "admin/robots" | "admin/reports" | "admin/zones" | "admin/routes" | "admin/maps" | "admin/localization" | "admin/system"`。
  - 组：常用 = home、arming、access、users、robots、reports；布置 = zones、routes（第三期换成一个「布置」页）；高级 = maps、localization、system（默认收起来，点组名展开；第四期重做）。
  - 旧地址：`admin/notices` → `admin/home`，`admin/lists` → `admin/access`，`localization`、`reports` 照旧。
  - 「管理」入口指 `#/admin/home`；工程师看得到「管理」，看不到「用户」。
  - `isOwnerLike(role: string): boolean`。

- [ ] **Step 1: 写测试**

```tsx
it("旧的提醒、名单地址跳到新页")    // readRoute("#/admin/notices").page === "admin/home";"#/admin/lists" → "admin/access"
it("管理导航分三组,高级默认收着")   // 三个组标题;高级下的链接默认不在 DOM,点组名出来
it("工程师看得到管理、看不到用户")  // role engineer:有「管理」,AdminNav 里没有 admin/users
it("工程师的界面名字")              // roleLabel("engineer") → 「工程师(临时)」
it("家人看不到管理")
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**（新页面这一步先放占位组件，Task 7–11 换掉）

- [ ] **Step 4: `npx tsc --noEmit`、`npx vitest run`，PASS**

- [ ] **Step 5: 提交** `git commit -m "网页:管理分常用、布置、高级三组,认工程师角色(W44 第二期)"`

---

### Task 7: 布防时间页（网页）

**Files:**
- Create: `web/console/src/armingpage.tsx`、`web/console/src/armgrid.ts`（不碰 DOM 的段运算）
- Modify: `web/console/src/styles.css`、`i18n.ts`、`console.tsx`
- Test: `web/console/src/armgrid.test.ts`、`armingpage.test.tsx`（新）

**Interfaces:**
- Consumes: Task 1 `/api/duty-settings`；Task 2 `/api/arming/schedule`、`/api/arming/exceptions`。
- Produces:
  - `armgrid.ts`：`type Slot = { zone: string; day: number; start: number; end: number }`（分钟）；`addBlock(slots, zone, day, start, end): Slot[]`（跟同一防区同一天的段相交或相接就合并）；`removeBlock(slots, zone, day, at): Slot[]`（删掉含 `at` 的那一段）；`moveEdge(slots, zone, day, start, edge: "start"|"end", to): Slot[]`（拖边，吸到 30 分钟，不越过邻段，不短于 30 分钟）；`copyDay(slots, zone, from, toDays: number[]): Slot[]`。
  - 页面（设计 §5.1「布防时间」）：
    - 开头一句：「家里几点布防。业主手动切了模式，到下一个时间段之前以手动为准。」
    - 「全部防区一样 / 每个防区单独设」切换；单独设时上面一排防区按钮。
    - 七行（星期一到星期日）× 0–24 点，色块 = 布防；空白处按下拖 = 加一段，点色块 = 选中（出「删掉」），拖色块两边 = 改头尾；每行右边「复制到…」。
    - 下面「例外」：列表 + 「加一个例外」（从哪天到哪天、全天布防 / 全天撤防、备注）。
    - 再下面「交班时间」两个钟点、「多久没巡过算没巡」小时数。
    - 「时间表开着」开关；右上角「保存」（一次存整份）。改了没存离开页面要提醒（`beforeunload` + 页内提示条）。
    - 现在谁说了算：「现在按时间表：布防，22:00 撤防」或「业主手动切了在家，到 22:00 回时间表」（读 `/api/mode` 的 `source`、`next_change_ms`、`manual_until_ms`）。

- [ ] **Step 1: 写测试**

```ts
// armgrid.test.ts
addBlock 合并相接的段;moveEdge 吸到 30 分钟、不越过邻段、不短于 30 分钟;copyDay 盖掉目标那天原来的段;
removeBlock 只删含 at 的那一段
// armingpage.test.tsx
it("读出来画色块")                 // 星期一 22:00–24:00 → 星期一那行一个块,left/width 按 24 小时比例
it("拖出一段再保存,发整份")       // 模拟 pointerdown/move/up → POST /api/arming/schedule 带新段
it("单独设防区只改那个防区")
it("加例外")
it("改交班时间")                   // POST /api/duty-settings {shift_hours:[8,20], stale_hours:2}
it("没存离开有提示")
it("写明现在谁说了算")
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: tsc、vitest，PASS**

- [ ] **Step 5: 提交** `git commit -m "网页:布防时间页(一周七天色块、例外、交班时间)(W44 第二期)"`

---

### Task 8: 授权和访客页（网页）

**Files:**
- Create: `web/console/src/accesspage.tsx`
- Delete: `zonesadmin.tsx` 里的 `ListsPage`、`AddAuthz`（挪进新页，`daysText` 一并挪）
- Modify: `i18n.ts`、`styles.css`、`console.tsx`
- Test: `web/console/src/accesspage.test.tsx`（新）；`pages.test.tsx` 里名单页的测试删掉或挪过来

**Interfaces:**
- Consumes: `/api/lists/authorizations`（Task 5 的 `active_now`、`on_today`、`…/end`）、`/api/mode`、`POST /api/mode`（访客）。
- Produces（设计 §5.1「授权和访客」）：
  - 开头一句：「授权期间，机器人在这些防区看到人不报警，只记一笔。」
  - 上面「现在在家里的授权」：此刻生效的几条，谁、在哪、到几点，按钮「提前结束」（确认后 `…/end`）。没有就写「现在没有」。
  - 大按钮「加一个授权」：谁、哪些防区（或全部）、从几点到几点、「每周重复」开关 —— 开：选星期几、可选有效到哪天；关：只选一天（`days` = 那天的星期、`until_ms` = 那天 23:59:59）。
  - 「全部授权」表：谁、在哪、哪几天、几点到几点、有效到哪天、删。
  - 「访客模式」单独一块：开着时写「访客模式开到 HH:MM」+「现在结束」；关着时选防区和结束时间（1、2、4、8、24 小时），「开访客模式」。家人账号能开关（现有 `set_mode` 权限），保安看不到这一页。
  - 人脸、车牌名单那一块去掉（不在这一轮，设计 §2）。

- [ ] **Step 1: 写测试**

```tsx
it("现在生效的列在上面,能提前结束")  // active_now 的一条 → 上面一行;点「提前结束」→ 确认 → POST …/3/end
it("不重复的授权只占那一天")          // 关掉每周重复、选 2026-10-30(星期五)→ days=0b0010000,until_ms 是那天 23:59:59 本地
it("每周重复按选的星期")
it("开访客模式带防区和小时")          // 选后花园、2 小时 → POST /api/mode {mode:"visitor", zones:["后花园"], minutes:120}
it("访客开着写到几点、能现在结束")    // 现在结束 → POST /api/mode {mode: prev_mode || "armed"}
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: tsc、vitest，PASS**

- [ ] **Step 5: 提交** `git commit -m "网页:授权和访客页(W44 第二期)"`

---

### Task 9: 用户页（网页）

**Files:**
- Create: `web/console/src/userspage.tsx`
- Delete: `admin.tsx` 里的 `UsersPage`、`AddUser`、`PasswordDialog`（挪进新页）
- Create: `web/console/src/confirmpw.tsx`（「再输一次密码」对话框，Task 10 也用）
- Modify: `i18n.ts`、`styles.css`、`console.tsx`
- Test: `web/console/src/userspage.test.tsx`（新）；`pages.test.tsx` 里用户页的测试挪过来

**Interfaces:**
- Consumes: Task 3 的接口。
- Produces（设计 §5.1「用户」）：
  - `ConfirmPassword({ title, onConfirm: (pw: string) => Promise<void>, onClose })`：密码框 + 确定；站点回 403 写「密码不对」，429 写「错太多次，过几分钟再试」，不关对话框。
  - 表：名字（显示名，下面小字账号名）、角色（界面名字）、最后登录（「今天 08:12」「3 天前」「没登录过」）、状态、操作（改角色、停用 / 启用、重设密码、删除）。自己那一行不给改角色、停用、删除。工程师那一行只有「提前关」「删除」，写「还剩 2 小时 10 分」或「已过期」。
  - 「加一个用户」：显示名、账号名、角色（业主 / 保安 / 家人）、初始密码。
  - 「给工程师开临时账号」单独按钮：显示名、4 小时 / 24 小时 → 再输一次密码 → 弹出账号名和口令（只显示这一次，能复制，写「关掉以后就看不到了」）。
  - 点一个人 → 右边抽屉「他做过什么」：`/api/audit?actor=`，每行时间、做了什么（`action` 翻成人话，认不得的原样）、对象；工程师做的标「工程师」。
  - 删除要确认：「删掉以后他就登录不了，做过的事还留在记录里。」

- [ ] **Step 1: 写测试**

```tsx
it("角色写界面名字")                    // admin → 业主(最高权限),owner → 家人 / 只看
it("自己那一行不能删、不能停用")
it("开工程师账号要再输密码,口令只显示一次")
it("密码输错不关对话框")                // 403 → 写「密码不对」
it("工程师那一行写还剩多久、只能提前关和删")
it("点一个人看他做过什么")              // GET /api/audit?actor=guard1
it("删人要确认")
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: tsc、vitest，PASS**

- [ ] **Step 5: 提交** `git commit -m "网页:用户页(工程师临时账号、删人、看谁做过什么)(W44 第二期)"`

---

### Task 10: 机器人页 + 机器人名字用到保安页（网页）

**Files:**
- Create: `web/console/src/robotspage.tsx`
- Delete: `admin.tsx` 里的 `RobotsAdminPage`（`AddRobot`、`CodeDialog`、`deviceSummary`、`clientTooOld` 挪过来；摄像头那一块 `CamerasPanel` 暂挂到「高级 → 系统」页底下，第三期进「布置」）
- Modify: `web/console/src/data.ts`（`robotNames` 信号、`nameOf(id)`，从 `/api/robots` 的 `name` 来）
- Modify: `duty.tsx`、`livewall.tsx`、`slot.tsx`、`mappage.tsx`、`recpage.tsx`、`alarmlog.tsx`（给人看机器人的地方换成 `nameOf(id)`）
- Modify: `i18n.ts`、`styles.css`、`console.tsx`
- Test: `web/console/src/robotspage.test.tsx`（新）；`pages.test.tsx` 里机器人页的测试挪过来；保安页测试补一条名字

**Interfaces:**
- Consumes: Task 4 的 `/api/robots/overview`、`…/name`、`…/revoke`（带 `confirm_password`）；Task 9 的 `ConfirmPassword`。
- Produces（设计 §5.1「机器人」）：
  - `nameOf(id: string): string`（起了名用名字，没起用编号）。
  - 卡片：机器人图标位（照片这一期不做）、名字（下面小字编号）、在线 / 离线、电量、今天巡了几趟、毛病（没有写「正常」，有写几条，橙色）。
  - 点卡片 → 详情：改名字、软件版本（`agent` + 兼容结论说人话）、证书到期日、最近的毛病（时间、什么事）、「改成只能手动派 / 自动派」、「重出开通码」、「解绑」（确认 + 再输一次密码）。
  - 「添加一台机器人」：现有登记码流程（编号 → 开通码）；对话框上写「以后改成扫机器人身上的码」（设计 §11 第 6 条，W42 留位置）。
  - 电池健康这一期不显示（机器人还不报）。

- [ ] **Step 1: 写测试**

```tsx
it("卡片:名字、电量、今天几趟、毛病")
it("没毛病写正常")
it("改名字")                      // POST …/name {name:"前院"} → 卡片上「前院」
it("解绑要确认再输密码")          // Confirm → ConfirmPassword → POST …/revoke {confirm_password}
it("保安页用起的名字")            // duty 状态条、livewall 卡片标题显示「前院」不显示编号
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: tsc、vitest，PASS**

- [ ] **Step 5: 提交** `git commit -m "网页:机器人页(卡片、详情、起名、解绑再输密码),保安页用起的名字(W44 第二期)"`

---

### Task 11: 报表页（网页）

**Files:**
- Rewrite: `web/console/src/reportpage.tsx`（旧的值班日报去掉；`reports.ts` 里 CSV 那几样留着给「导出表格」用）
- Modify: `i18n.ts`、`styles.css`（打印样式 `@media print`）、`console.tsx`
- Test: `web/console/src/reportpage.test.tsx`（新）；`pages.test.tsx` 里报表的测试删掉或挪过来

**Interfaces:**
- Consumes: Task 5 `/api/reports/summary`；现有 `/api/reports/daily`（只给「导出表格」）。
- Produces（设计 §5.1「报表」）：
  - 时间段：这周 / 上周 / 这个月 / 自己选（最长 31 天，超了写明）。「这周」「这个月」从站点回的日子算，不在浏览器里算星期（站点回 `days`，网页只按回来的画）。
  - 四个大数字：巡了多少趟、出了几次事、误报几次、机器人出问题几次。
  - 按天柱状图（四样叠在一起，纯 CSS 柱子，不加图表库）。
  - 事件列表：时间、哪台机器人（`nameOf`）、什么事、结论；点一行跳 `#/recordings?robot=…&t=…`。
  - 「导出 PDF」= 浏览器打印（`window.print()`，打印样式只留标题、时间段、四个数、柱状图、事件表）；「导出表格」= 现有 CSV。

- [ ] **Step 1: 写测试**

```tsx
it("四个大数字")
it("柱子按天、高度按比例")
it("点事件跳到录像那一刻")
it("自己选超过31天写明不查")
it("导出 PDF 调打印")              // window.print 被调一次
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: tsc、vitest，PASS**

- [ ] **Step 5: 提交** `git commit -m "网页:报表页(四个数、按天柱状图、事件、打印成 PDF)(W44 第二期)"`

---

### Task 12: 家里的状况（网页）

**Files:**
- Create: `web/console/src/homepage.tsx`、`web/console/src/advice.ts`（技术提醒怎么说、该做什么、按钮去哪）
- Delete: `web/console/src/notices.tsx`
- Modify: `i18n.ts`、`styles.css`、`console.tsx`
- Test: `web/console/src/homepage.test.tsx`、`advice.test.ts`（新）

**Interfaces:**
- Consumes: 告警（`audience == "owner"` 的）、`/api/mode`（Task 2 的 `source`、`state`、`next_change_ms`）、`/api/arming/schedule` 的 `today`、`/api/lists/authorizations`（`on_today`、`active_now`）、`/api/robots/overview`、`/api/reports/summary`（这周、上周各一次）。
- Produces（设计 §5.0）：
  - `advice(kind: string): { say: Key; todo: Key; go?: AdminPage } | null`：至少写 `cert_expiring`（证书快到期，请联系工程师）、`site_disk_low` / `disk_80`（硬盘快满了）、`backup_stale` / `backup_verify_failed`（备份，去「系统」）、`evidence_*`（证据加密，请联系工程师）、`agent_too_old` / `site_too_old`（版本旧了）、`push_failed`（手机收不到推送）、`watchdog_died` / `schedule_died`（后台卡住了，重启站点主机或联系工程师）、`loc_degraded`（机器人认位置不太稳，常出现就去「定位」）、`finding`（巡检照片里有异常，去看照片）；没写的用告警标题 + 「请联系工程师」。
  - 页面四块，从上到下：
    1. **要你处理的事**：没有就整块不显示。每条：一句人话、该做什么、按钮（`go` 有就跳过去，否则「我知道了」= 确认）+「处理好了」。
    2. **今天的安排**：今天几点布防、几点撤防（按防区，全部一样就一行）；今天的授权（谁、哪、几点到几点，正在生效的标出来）；访客模式开到几点。右边切模式（布防 / 在家 / 访客，设计 §11 第 14 条），写现在谁说了算。
    3. **机器人**：每台一张小卡片（名字、电量、今天几趟、毛病），点了去机器人页。
    4. **这周**：出了几次事、几次误报，跟上周比（「比上周多 2 次」「跟上周一样」）。
  - 「管理」导航上「家里的状况」带数字角标 = 要你处理的事的条数（第一期提醒页的角标挪过来）。

- [ ] **Step 1: 写测试**

```ts
// advice.test.ts
cert_expiring 有 todo;backup_stale 的 go 是 admin/system;没写的 kind 回 null
// homepage.test.tsx
it("没有要处理的事,整块不显示")
it("证书提醒写人话和该做什么")
it("今天的安排:按防区的时间、今天的授权、访客到几点")
it("切模式要确认")                 // 点「在家」→ Confirm → POST /api/mode {mode:"home"}
it("机器人小卡片点了去机器人页")
it("这周比上周")                   // 这周 3 次、上周 1 次 → 「比上周多 2 次」
```

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 写**

- [ ] **Step 4: tsc、vitest，PASS**

- [ ] **Step 5: 提交** `git commit -m "网页:家里的状况(要你处理的事、今天的安排、机器人、这周)(W44 第二期)"`

---

### Task 13: 全量、截图、文档

**Files:**
- Modify: `packages/site-node/src/d1max_site/web/`（构建产物）
- Create: `docs/W44-第二期-完工报告.md`
- Modify: `docs/庄园场景待真机测试.md`（加 §3ab）、台账（W44 行）

- [ ] **Step 1: 构建网页、提交产物**（`npm run build`）

- [ ] **Step 2: 全量**：ruff；Python 四组（约 20 分钟，告诉用户；跑之前停掉演示站点）；vitest；`flutter test --concurrency=1`（约 2 分钟）、`flutter analyze`。
Expected: 全过。

- [ ] **Step 3: 演示站点截图对设计**：家里的状况、布防时间、授权和访客、用户、机器人、报表六页（`shot5.js`），发给用户。

- [ ] **Step 4: 写完工报告**（做了什么、测试、Rulings、没做的）、真机项 §3ab（时间表跨过边界时入侵派不派、工程师账号到点、手机上看到的「按时间表切的」、报表打印成 PDF 的样子）、台账。

- [ ] **Step 5: 提交、开 PR、九项检查过了合并**
