# W00c5a · 告警与值守搬到站点 —— 设计稿

日期：2026-09-25 · 工单：W00c5a（L，依赖 W00c4）
状态：**按推荐（用户 2026-09-25 授权连续推进：「从W00c5a到W00c5e直接做完」）**。四个决定都取推荐项。受决策 8 约束（「所有信息都在服务器上，不能在狗上」）。

## 0. 事实

- 告警簿 `AlertBook`（`d1max_agent/engine/alerts.py`，纯状态机：三级、按 `robot/kind` 聚合 15 分钟、确认与解决分开、P1 未确认 2/5 分钟升级换通道）标着「借住」，总设计 §5 归站点。告警来源 `app/alert_sources.py` 与值守汇总 `app/watch.py` 住在狗上的老服务里，手机的值守屏（`watch_page.dart`）直连狗读 `/api/alerts*`、`/api/watch/summary`。
- 站点现在只有事件账（`incidents`），没有告警。
- 狗经 MQTT 给站点的事实：状态（在线、就绪四项：控制权/姿态/急停/定位、当前任务）、事件（`task_progress/done/failed/aborted/preempted`、`patrol_waypoint`）、遥测（位姿、电量、定位质量、时间戳）、断线遗言。**没有故障码**：HAL 的 `faults()` 从没上过线。
- 总设计 §3.3：「狗只报事实，判定在站点」。决策 8：信息不留在狗上。

## 1. 决定一：告警簿放哪、狗上那份怎么办

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | **整个搬**：`alerts.py` 移到 `d1max_site/alerts.py`，判级表、聚合、升级规则原样；持久化从 jsonl 换成站点库（`alerts` 表，每次变化写穿，启动时读回）。狗上的告警来源、值守汇总、`/api/alerts*`、`/api/watch/summary` 与老服务里所有 `raise_alert` **删掉**（原处的处置动作——停车、挂起——保留，只是不再记告警；租约到期、挂起超时这两条随 W00c5c 在站点重建）。手机直连模式的「值守」入口删掉 |
| B | 先复制一份到站点，狗上那份留到 W00c5e。两份判据并存，正是这个模块开头警告的「两处说法打架」 |

## 2. 决定二：告警的事实从哪来

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 站点从 MQTT 已有的事实里**自己判**（`d1max_site/alert_sources.py`），契约只加一种事件 **`robot_fault`**：代理每拍看 HAL 的 `faults()`，故障集合**变了**才发一条（`{"faults":[{code,fatal,text}]}`，空列表 = 都消了）。事件走事件簿，断线期间留在狗上、重连补投、站点确认即清——正好是决策 8 允许的断网暂存 |
| B | 代理自己算告警、把告警发给站点。判定又回到了狗上 |

站点的判定（都只在**变了**的那一刻报一次，沿用原来那套「上次是什么」的记忆，按狗分开记）：

| kind | 级别 | 站点怎么认 |
|---|---|---|
| `estop_pressed` | P1 | 状态的 `ready.estop_clear` 由真变假 |
| `fallen` | P1 | `robot_fault` 的文字命中跌倒词表（原词表照搬；**待真机**：按故障码认） |
| `loc_lost_paused` | P1 | 有任务在跑时 `ready.loc_ok` 由真变假（代理的引擎丢定位就暂停） |
| `stuck` | P1 | `patrol_waypoint` 报某点位没到 |
| `run_abort` / `battery_abort` | P1 | `task_failed`，按理由里有没有「电量/电池」分（人点的中止是 `task_aborted`，不报） |
| `robot_offline` **（新）** | P1 | 狗掉线（遗言 `online=false` 或状态过期）且掉线前有任务在跑 |
| `robot_offline_idle` **（新）** | P2 | 狗掉线，掉线前空闲 |
| `clock_skew` | P2 | 遥测的 `stamp` 跟站点钟比，判据用契约里现成的 `clock_skew` |
| `run_start` | P3 | 状态里出现一个新的在跑任务 |
| `run_done` | P3 | `task_done` |
| `schedule_died` | P1 | 站点自己的排程协程死了（`robot` 记 `site`） |

`disk_80`、`upload_backlog`、`bundle_lag`、`finding` 是狗上存储与证据的事，归 **W00c5d**；`lease_expired`、`suspend_stale` 归 **W00c5c**。判级表里的登记保留，站点暂时没有来源。

## 3. 决定三：谁能确认、怎么送到人

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 新权限 `handle_alerts`（admin、guard 有；业主只能看）。确认人**取登录账号**，不信请求体（W00c3 的规矩）。站点每 5 s 跑一次升级；每次告警变化经 SSE 推 `kind: alert`。手机值守屏挂在站点模式下，读 `/api/alerts`、`/api/watch/summary`，订 SSE；升到「声音」档时在 App 里响铃。**App 没开时的系统推送不在这一张**（要接 FCM 与外网，列进待办） |
| B | 现在就接系统推送。要谷歌服务和站点外网，范围翻倍 |

## 4. 决定四：值守汇总在站点上长什么样

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | `GET /api/watch/summary`：每台狗一行（在线、最后一次见到、电量与取值时刻、站点量的钟偏、未解决告警按级别计数），加站点自身一行（排程协程活着没有）。狗上存储那几项（盘水位、证据积压、包落差、备份盘）先报 `null`，附一句人话「狗上的存储情况还没接到站点，这一档是『不知道』」，W00c5d 接上。**缺的一律 `null` 不写 0** 的纪律照旧 |
| B | 等 W00c5d 一起做汇总 |

## 5. 形状

- 契约：`EVENT_KINDS` 加 `robot_fault`；黄金夹具。
- 代理：`runtime` 每拍比较 HAL 故障集合，变了发 `robot_fault`。
- 站点：`alerts.py`（搬来的 `AlertBook` + 库持久化）、`alert_sources.py`（按狗分开记忆的判定）、`watch.py`（汇总）；派遣器加状态回调（`on_status`）；站点主循环每 5 s 升级一次、排程协程死了报 `schedule_died`；API：`GET /api/alerts?open=1`、`POST /api/alerts/<key>/ack`、`POST /api/alerts/<key>/resolve`、`GET /api/watch/summary`；SSE `kind: alert`；权限 `handle_alerts`；审计记确认与解决。
- 狗上老服务：删 `alert_sources.py`、`watch.py`、告警与值守路由、`AlertBook` 的使用与相关测试；原来靠告警通知的处置照做、改记日志。
- 手机：值守屏改用站点（`SiteApi` 加告警与汇总），站点狗列表页加「值守」入口；直连模式删「值守」入口。夹具由 Python 从真站点 API 生成。

## 6. 验收

- 仿真：站点 + 代理（sim HAL）跑一趟巡检，点位卡住 → 站点出 P1 `stuck`；急停 → `estop_pressed`；代理断线（有任务）→ `robot_offline`；`robot_fault` 带跌倒字样 → `fallen`；同一件事不重复报。
- P1 两分钟没人确认 → 升到推送档、五分钟 → 声音档；guard 确认后不再升；业主确认 → 403；确认人是登录账号。
- 站点重启后未解决的告警还在。
- 狗上老服务不再有 `/api/alerts`、`/api/watch/summary`；仓库里不再有 `d1max_patrol.app.alert_sources`、`d1max_patrol.app.watch`。
- 手机（Flutter 测试）：站点模式值守屏看到告警、确认后 SSE 更新、业主看不到确认按钮。
