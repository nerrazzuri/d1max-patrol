# W00b · robot-agent 骨架 —— 设计稿（待用户拍板）

日期：2026-09-24 · 工单：`docs/庄园安防-差距核查与工单.md` W00b（L，依赖 W00）· 上游：总设计 §1、§5、§7；W00 设计与报告
状态：**已确认**（2026-09-24 用户拍板：四个决定全选 A）。

## 0. 工单原文与范围

> `engine/` 搬入 `packages/robot-agent`，接 cmd/event 主题，用 adapter-sim 跑通 W00 验收流程；`server.py` 先包成可替换适配层不删。

事实（决定的依据）：

- `engine/` 24 个模块、约 12 000 行；内部依赖成一张网：`release → bundle → {mission, release, schedule}`，`datadir → {release, upload_queue}`，`machine → {archive, form, homing, mission, preflight, removable, safety}`。总设计 §5 说 `schedule/alerts/baselines` 归站点，但 `bundle`（狗要读任务包）依赖 `schedule` —— **只搬「狗侧那一半」会把依赖图切断**。
- 六个模块（`machine safety preflight selfcheck mission homing`）依赖根包的 `backends.base`（`NavBackend/DeviceBackend`）与 `protocol.nav_types`（`Pose/Waypoint/NavStatus/LocStatus`）。`backends`/`protocol`/`config` **不**反向依赖 `engine`/`app`，没有环。
- 81 个测试文件、`app/` 8 个模块、`cli.py`、`inspect/`、`d1max_console/store.py` 直接 `import d1max_patrol.engine.*`。
- `MissionEngine(nav: NavBackend, device: DeviceBackend, media, runs_root, …)`；引擎调的是 `nav.goto/stop/nav_status/loc_status/return_home/reset_localization` 与 `device.battery/emergency/has_control/set_light/set_gimbal`，并靠 `NavStatusEvent` 流等终态（StandBy → Active → Succeed → StandBy）。adapter-sim 实现的是 `RobotHAL`（速度环），不是 `NavBackend`。
- W00 的 `GotoTask` 直接在 HAL 上走速度环；契约、校验链、事件、状态都不依赖它的实现。

## 1. 要拍板的四个决定

### 决定 1：「`engine/` 搬入」搬什么、怎么搬

| | 方案 | 代价 / 风险 | 推荐 |
|---|---|---|---|
| A | **整棵 `engine/` 物理搬**到 `packages/robot-agent/src/d1max_agent/engine/`（24 个模块一起，含站点将来要拿走的 `schedule/alerts/baselines`——在文件头标「借住，W00c 搬走」）；根包 `d1max_patrol/engine/` 留一层**别名壳**：`__init__.py` 用 `sys.modules` 把 `d1max_patrol.engine.<m>` 指到同一个模块对象，81 个测试、`app/`、`cli.py` **一字不改**照旧工作（私有名也在，因为是同一个模块对象）；robot-agent 的 `pyproject` 声明**过渡性**依赖 `d1max-patrol`（要它的 `protocol`、`backends.base`），W00d 出 adapter-d1max 时切掉 | 搬迁本身是 `git mv` + 改 24 个文件的内部 import（`from d1max_patrol.engine.x` → `from d1max_agent.engine.x`）；别名壳是十几行；测试零改动。风险：两个包名指向同一批模块，谁不知道别名壳会困惑——壳里写清楚、W00c 之后删壳 | **推荐** |
| B | 只搬狗侧那一半，`schedule/alerts/baselines` 留根包 | `bundle → schedule` 断掉，要先给 `bundle` 抽接口；W00c 还得再搬一次 | 不推荐 |
| C | 物理搬 + 全仓把 `d1max_patrol.engine` 的 import 都改掉，不留壳 | 81 个测试文件 + 10 个源文件改 import，一次几百行纯机械改动，与真实改动混在一个工单里，审核成本高 | 不推荐（W00c 删壳时再做） |

### 决定 2：`goto` 怎么接到 `MissionEngine` 上（「接 cmd/event 主题」的实质）

| | 方案 | 说明 | 推荐 |
|---|---|---|---|
| A | 写两个**桥**：`HalNavBackend(RobotHAL) → NavBackend`（`goto` = 用 W00 的速度环控制律向目标走，并按厂商状态机发 `NavStatusEvent`：StandBy → Active → Succeed/Failed → StandBy；`stop/nav_status/loc_status` 从 HAL 派生；`return_home` 走到登记的原点）与 `HalDeviceBackend(RobotHAL) → DeviceBackend`（`battery/emergency/has_control`；`set_light/set_gimbal` 在 HAL 报没有时变成 no-op 并记日志）。`goto` 命令 → 一个只有一个航点的 `Mission` → `MissionEngine.start()`；`abort` → `engine.abort()`；`RunSnapshot` 事件流 → `task_progress/done/failed/aborted`。W00 的 `GotoTask` 删除，契约不变 | 引擎真正进了链路：预飞检查、返航兜底、暂停/恢复、归档目录这些存量能力都跟着进来；验收流程跑在引擎上。桥是 W00d 里 adapter-d1max 也要用的（那时 HAL 后面是真狗），现在就在 sim 上练 | **推荐** |
| B | `GotoTask` 保持在 HAL 上；引擎只是搬了文件、不接 | 便宜，但「接 cmd/event 主题」名存实亡，验收不经过引擎，W00c 前引擎一行没被新链路跑过 | 不推荐 |

A 的已知硬点（要在计划里逐个钉）：引擎预飞检查里的**原点**（`HomePoint`，按地图登记）、`removable` 探针（默认 `DEFAULT_PROBE` 会去扫盘，测试注入空探针）、`media`（空字典）、`runs_root`（数据根）。`goto` 的 `Mission.policy` 用 W00 的 `max_speed_mps` 换算不了（引擎不管速度）——速度上限由 `HalNavBackend.set_speed` 承接，命令里的 `max_speed_mps` 在下发前调一次。

### 决定 3：进程入口与部署到哪一步

| | 方案 | 说明 | 推荐 |
|---|---|---|---|
| A | 新入口 `d1max-agent`（`d1max_agent.main`）：读注册文件、按参数选 Transport（`mqtt://` 走 Paho，`memory://` 走进程内 broker——给演示与联调）、选 HAL（`--hal sim` 现在；`d1max` 归 W00d）、`asyncio` 循环每 100 ms `step`；`release pack` 白名单加 `packages/`；`install.sh` 4/7 在槽 venv 里把三个包装进去（`pip install <slot>/packages/{contract,adapter-sim,robot-agent}`，离线轮子多一个 paho）；交付一份 `deploy/d1max-agent.service`，`install.sh` **装但不 enable**（站点 broker 在 W00c 才有；单元里 `ExecStart` 指 `current/venv/bin/d1max-agent`），文档写明「W00c 起用」 | 狗上从此能装出 robot-agent，但不启动；W00c 一到就是 `systemctl enable`。装机脚本的对账声明块、卸载、足迹都要跟上（W01b 那套守卫会逼着做对） | **推荐** |
| B | W00b 只做代码与测试，打包与单元全部推到 W00c | 省事，但 W00c 会同时改站点、装机、单元三样，违反总设计 §5「不要拆包、改协议、改鉴权、换客户端同时进行」 | 不推荐 |

### 决定 4：「`server.py` 先包成可替换适配层不删」做到哪一步

| | 方案 | 说明 | 推荐 |
|---|---|---|---|
| A | `d1max-agent` 进程**可选**地在同一进程里起老的 `AppServer`（`--legacy-http :8095`），两者**共用同一个** `MissionEngine`、nav、device：手机 app 与值守屏照旧连 8095，站点经 MQTT 派的任务和手机发起的任务走同一台引擎、同一套资源仲裁（引擎的 `EngineBusy` 就是仲裁）。`server.py` 本身不改（`AppContext` 已经是注入式的），只是不再自己 `main()` 组装——组装权移到 `d1max_agent.main`。旧的 `d1max-app` 入口与 `d1max-patrol.service` 原样保留，等 W00c 手机改连站点后再退役 | 「可替换适配层」= 旧 HTTP 面变成 agent 进程里的一个可选组件；两条链路同时能跑，迁移期间没有功能空窗。风险：两个进程都想拿 sidecar 控制权——所以必须**同一进程**，这也是为什么不能两套服务并跑 | **推荐** |
| B | 不碰 `server.py`，两套服务各自独立 | 两个进程抢 SDK 控制权（§7.1），真机上一定出事 | 不推荐 |

## 2. 验收（照工单，必须端到端过）

W00 的 `test_acceptance_w00.py` 四条**原样通过**，但 `goto` 走的是 `MissionEngine`（断言里加一条：引擎 `RunState` 序列 `PENDING → RUNNING → DONE/ABORTED`，归档目录里有这趟的 `manifest`）。另加：`d1max-agent --transport memory:// --hal sim` 起一个进程、用 `DispatchClient` 从测试里派一条 `goto` 到 `done`（进程级冒烟，验证入口装配）；`--legacy-http` 起来后 `GET /api/state` 能看到 MQTT 派的那趟。

## 3. 不做 / 留给后面

- 不改契约、不加任务类型、不动 broker 侧 ACL（W00c）。
- 不切掉 robot-agent 对 `d1max-patrol` 的过渡性依赖；不删别名壳（W00c 手机改连站点后一起）。
- 不在狗上 enable `d1max-agent.service`（W00c）。
- `schedule/alerts/baselines` 只是借住在 `d1max_agent.engine` 下，W00c 搬去 site-node。

## 4. 风险

- 别名壳让 `d1max_patrol.engine` 与 `d1max_agent.engine` 是同一批对象：monkeypatch 一处两处都变——这正是要的，但要写进壳的 docstring。
- `HalNavBackend` 要模拟厂商导航状态机的时序（终态保持 ~0.5 s 回落 StandBy 等，`真机待验证清单` #1 那一条），引擎压在这上面；sim 上按现有仿真器的时序参数走。
- `release pack` 加 `packages/` 后包体变大、树哈希覆盖面变大；装机 4/7 多三次 `pip install`，离线轮子目录要多放 paho。
