# W00c2a 巡检任务 + 站点排程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 站点导入任务包 → 排程到点 → 派 `patrol` → 代理在 `MissionEngine` 上跑完整趟 → 站点记下结果；站点重启不重复起；狗离线到点按 `on_missed` 记账。

**Architecture:** 设计 `docs/superpowers/specs/2026-09-25-W00c2-dispatcher-design.md`（五个决定全 A）。纯逻辑下沉到契约包：几何类型（`Pose` 等）、任务模型与解析、排程模型与 `decide/pick`、任务包的格式与校验（清单、纯数据闸、整包指纹）。原模块改为**同一对象**的转手（`sys.modules` 指向契约里的模块，或 re-export 同一个类），老用户与 monkeypatch 不受影响。狗端的包落盘、槽位、回退留在 robot-agent。

**Tech Stack:** 同 W00c1；契约包新增依赖 `PyYAML`（已在离线轮子里）。

**实现口径（设计的延伸，报告里写明）：**
- 决定五写的是「任务与排程的解析进契约」。任务点位用的是根包的 `Pose`（四元数朝向），引擎与各导航后端都在用它；只有**把这个类原样搬进契约、根包转手同一个类**，狗端才不会出现两种 `Pose`。所以搬的是原类，不是另写一个精简版。
- 决定四「复用任务包的格式与校验」要求站点能校验任务包：清单解析、纯数据闸、整包指纹（`tree_sha256`）一起进契约 `d1max_contract.bundle_format`。

---

### Task 1: 几何类型进契约
- [ ] `d1max_contract/geometry.py`：`Position`、`Orientation`、`Pose`、`yaw_to_orientation`、`orientation_to_yaw`（原样搬）。
- [ ] `d1max_patrol/protocol/nav_types.py` 改为从契约 import 并 re-export；测试：`nav_types.Pose is d1max_contract.geometry.Pose`；契约包不 import 根包（工作区测试已有）。

### Task 2: 任务与排程模型进契约
- [ ] `d1max_contract/mission.py`、`d1max_contract/schedule.py`：原样搬（`mission` 的 `Pose` 改从 `geometry` 来）。契约 `pyproject` 加 `PyYAML`。
- [ ] `d1max_agent/engine/mission.py`、`schedule.py` 改为 `sys.modules[__name__] = 契约模块`：`d1max_patrol.engine.mission is d1max_agent.engine.mission is d1max_contract.mission`。
- [ ] 别名壳名单测试与「搬走的 engine 里没有旧包名」测试跟上。

### Task 3: 任务包格式与校验进契约
- [ ] `d1max_contract/bundle_format.py`：`BundleError`、`BundleManifest`、`parse/read/write/dump_manifest`、纯数据闸（`scan/verify_pure_data`）、`tree_sha256`（连同 `sha256_file`）、`bundle_sha256`、`verify_bundle`、`read_bundle_schedule`、常量。
- [ ] `engine/bundle.py`、`engine/release.py` 从契约 import 这些名字（模块命名空间里仍有，老调用不变）；落盘、槽位、回退留在 `bundle.py`。
- [ ] 全仓跑 `tests/engine/test_bundle*.py`、`tests/test_cli_bundle.py`、`tests/app/test_api_bundle.py`、`test_release_pack.py`。

### Task 4: 契约加 `patrol`
- [ ] 资源表 `patrol: {motion, camera, light, head}`；断线策略 `continue_if_safe`，排队 `abort`；黄金夹具加一条 `patrol` 命令。
- [ ] 能力报文：有地图时 `tasks.patrol = {"map_id", "map_version"}`（站点据此填 `map_version`）。

### Task 5: 代理 `PatrolTask`
- [ ] 命令校验：`payload = {"mission": <任务定义>, "map_version": str}`；`parse_mission` 过不了 → `rejected(payload…)`；`(mission.map_id, map_version) != loaded_map` → `map_mismatch`。
- [ ] `tasks/patrol.py`：`EngineGotoTask` 的推广（抽公共基类）：`engine.start(mission, home)`；每个航点结果发 `patrol_waypoint` 事件；终态：全部航点 ok → `done`，有跳过 → `failed`（理由列出失败的航点）；abort/preempted 同 goto。
- [ ] 测试：整趟三点走完、中途 abort、一点不可达被跳过 → failed、地图不符、payload 坏。

### Task 6: 站点：任务、排程、执行器
- [ ] 表：`bundles`（id、version、sha、导入时刻、导入人）、`missions`（bundle、mission_id、定义 JSON）、`schedules`（bundle、条目 JSON、时区）、`schedule_runs`（entry_id、计划时刻、决定、task_id、robot_id、结果）。
- [ ] `import-bundle <目录>` 与 `POST /api/bundles`（服务端路径导入，c1 不做上传）：`verify_bundle` → 解析任务与排程 → 事务落库；同一 `bundle_id` 版本不升不收。
- [ ] 执行器（`serve` 里每 30 s 一拍）：对每条排程 `decide(now, last_started)`；`pick`；到点 → 选狗（c2a：排程条目可写 `robot`，没写就用唯一一台在线的狗；不止一台且没写 → 记 `alarm`）→ 派 `patrol`；派不出去（离线、没就绪）→ 按 `on_missed` 记账；`last_started` 与每次决定落 `schedule_runs`，站点重启从库里恢复。钟偏：沿用 `clock_skew`，偏太多不起并记账。
- [ ] 任务结果：事件里 `task_done/failed/aborted` 回写 `schedule_runs.result`。
- [ ] API：`POST /api/robots/<id>/patrol`（`{"mission_id"}`，手动起一趟）、`GET /api/schedule`（每条排程下次何时、最近一次决定与结果）。

### Task 7: 验收、突变、评审、报告
- [ ] 端到端（MemoryBroker + 真 AgentRuntime，注入钟）：导入包 → 快进到排程时刻 → 派 `patrol` → 走完 → `schedule_runs` 有 `done`；「站点重启」（新 Dispatcher/执行器、同一个库）同一时刻不再起；狗离线到点 → `skip` 记账。
- [ ] 真进程验收在 W00c1 的端到端上加一段：`import-bundle` + 手动 `patrol`。
- [ ] 突变检查、内部只读评审、`docs/W00c2a-完工报告.md`、工单表、全量、推送。
