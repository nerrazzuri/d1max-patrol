# W00b · robot-agent 骨架 —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `engine/` 整棵搬进 robot-agent（根包留别名壳），`goto` 经两个桥跑在 `MissionEngine` 上，新入口 `d1max-agent` 可选托管老 `AppServer`，装机能装出 robot-agent（单元装而不 enable），W00 验收四条原样通过且走引擎。

**Architecture:** 设计 `docs/superpowers/specs/2026-09-24-W00b-robot-agent-skeleton-design.md`（四个决定全选 A）。robot-agent 过渡性依赖根包 `d1max-patrol`（`protocol`、`backends.base`）。`HalNavBackend`/`HalDeviceBackend` 把 `RobotHAL` 包成引擎认识的后端，并按厂商导航状态机时序（`init 0.3 s`、终态驻留 `0.5 s`）发 `NavStatusEvent`。`EngineGotoTask` 替掉 W00 的 `GotoTask`。

**测试命令：** `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest -p no:cacheprovider -q`

---

### Task 1：`engine/` 物理搬迁 + 别名壳

**Files:** `git mv src/d1max_patrol/engine/*.py packages/robot-agent/src/d1max_agent/engine/`；新 `src/d1max_patrol/engine/__init__.py`（壳）；`packages/robot-agent/pyproject.toml` 依赖加 `d1max-patrol`；Test `tests/test_workspace.py`

- [ ] 测试：`import d1max_patrol.engine.machine as a; import d1max_agent.engine.machine as b; a is b`（24 个模块逐个）；`d1max_patrol.engine.__file__` 的目录下只有 `__init__.py`；moved 文件里没有 `d1max_patrol.engine` 字样；robot-agent `pyproject` 含 `d1max-patrol`。
- [ ] 实现：`git mv`；`sed 's/d1max_patrol\.engine/d1max_agent.engine/g'` 于 moved 文件；壳 `__init__.py`：文档 + `for name in _MODULES: sys.modules[f"d1max_patrol.engine.{name}"] = importlib.import_module(f"d1max_agent.engine.{name}")`，并把它们挂成本包属性。`tests/app/test_server.py::test_引擎和判读都不import_http` 改为扫 `d1max_agent.engine` 的目录。`tests/engine/test_selfcheck_post.py` 5 处 logger 名 `d1max_patrol.engine.selfcheck` → `d1max_agent.engine.selfcheck`。`d1max_agent/engine/schedule.py alerts.py baselines.py` 文件头加「借住，W00c 搬 site-node」。
- [ ] 全量（根 tests + packages）跑：失败集合 ⊆ 已知 5 条。Commit。

### Task 2：`HalNavBackend` / `HalDeviceBackend`

**Files:** Create `d1max_agent/bridges/__init__.py`、`bridges/hal_nav.py`、`bridges/hal_device.py`；Test `packages/robot-agent/tests/test_hal_bridges.py`

- [ ] 测试（SimRobot + 假钟，`tick(dt)` 驱动桥与 sim）：`connect()` 后 `nav_status() is STANDBY`、`loc_status() is CONTINUOUS_LOC`（HAL `loc_quality>0`）；`goto(pose)` → 立刻 `INITIALIZING`，`init_delay_s` 后 `ACTIVE` 并开始发速度，到点 → `SUCCEED` 事件，`terminal_hold_s` 后回 `STANDBY` 事件；`stop()` 在 ACTIVE 时 → `CANCELLED` → `STANDBY`；`goto` 非 STANDBY 抛 `NavRequestError`；HAL 拒速度（急停）→ `FAILED`；`loc_quality==0` → `loc_status() is LOC_LOST` 并停；`set_speed(x)` 夹到 HAL 上限并返回实际值、`get_speed`；`return_home()` 走到 `set_home(pose)` 登记的原点；`capabilities` 不含建图/路径（`list_maps` 等抛 `NavRequestError("not supported")`）。Device：`battery()` 百分数、`emergency()`、`has_control()`、`acquire/release_control` 透传；`set_light/set_gimbal` HAL 报 `HalUnsupported` → 记日志不抛；`take_photo` 抛 `MediaError`；`walk/stand/lie/halt` → `halt` = `hal.stop()`，其余抛 `DeviceBackendError("not supported")`。
- [ ] 实现 → 绿。Commit `agent: HAL → NavBackend/DeviceBackend bridges`.

### Task 3：`EngineGotoTask` + 装配

**Files:** Create `d1max_agent/tasks/engine_goto.py`、`d1max_agent/assembly.py`（`build_engine(hal, runs_root, clock, wall_ms, home)` 返回 `(engine, nav, device)`）；Modify `runtime.py`（`AgentRuntime(..., engine_parts)`；`_make_task` → `EngineGotoTask`；`step` 里 `nav.tick(dt)`）；Delete `tasks/goto.py` 与 `tests/test_goto.py`（其断言迁到新测试）；Test `test_engine_goto.py`

- [ ] 测试：`goto` → `engine.state` 经 `PREFLIGHT → LOCALIZING → RUNNING → DONE`；`task.state` 映射（`RUNNING/…→RUNNING`，`DONE→DONE`，`ABORTED→ABORTED`（abort 请求）/`PREEMPTED`（preempted 请求），`ABORTED` 且原因是 preflight → `FAILED(preflight: …)`）；进度事件含 `distance_m`；`abort` → `engine.abort` → 等 `hal.stopped()` 才终态；归档目录里有 `manifest`；预飞没过（没原点）→ `FAILED` 且理由带 `home`。
- [ ] 实现：`EngineGotoTask.start()` → `Mission(mission=task_id, map_id, waypoints=(MissionWaypoint(name="target", pose=Pose.from_xy_yaw(...)),), policy=Policy(waypoint_timeout_s=…))` → `nav.set_speed(max_speed)` → `engine.start(mission, home=home)`（`EngineBusy` → `FAILED(busy)`）；`step` 读 `engine.snapshot()`；`abort(reason)` → `engine.abort(reason)`。runtime 每拍 `nav.tick(dt)` + `hal.tick` 由外部（sim）驱动不变。
- [ ] W00 验收四条原样通过 + 新断言（引擎状态序列、manifest）。Commit `agent: goto runs on MissionEngine`.

### Task 4：`d1max-agent` 入口 + 可选 legacy HTTP

**Files:** Create `d1max_agent/main.py`；robot-agent `pyproject` `[project.scripts] d1max-agent = "d1max_agent.main:main"`；Test `test_agent_main.py`

- [ ] 测试：`build(args)` 用 `--transport memory://`、`--hal sim`、`--registration <file>`、`--store-dir`、`--runs-root`、`--map m:1`、`--home 0,0,0` 装出 `AgentRuntime` + `LoopBridge`；在 bridge 里 `start`、跑 N 拍、用 `DispatchClient` 派 `goto` 到 `done`；`--legacy-http 127.0.0.1:0` 起 `AppServer`（`--pin`），`GET /api/state` 的 `mission == task_id`（同一台引擎）；`--transport` 非法 → `SystemExit`；`d1max-agent --help` 子进程退 0。
- [ ] 实现：`main()`：`LoopBridge().start()`；在 bridge 里 `build_engine` + `AgentRuntime`；`bridge.call(runtime.start)`；`bridge.spawn(_drive(runtime, sim?, period))`（sim 的 `tick` 也在这里，真机 HAL 无 tick）；可选 `AppContext(...)` + `AppServer(...).start(postcheck=False)`；主线程 `Event.wait` 直到 Ctrl+C；`shutdown`。
- [ ] Commit `agent: d1max-agent entry, optional legacy HTTP facade sharing the engine`.

### Task 5：打包与装机

**Files:** `engine/release.py`（`_PACK_INCLUDE` 加 `packages`；`_PACK_SKIP_DIRS` 加 `tests`）；`deploy/install.sh` 4/7（三包 `pip install`）；`deploy/d1max-agent.service`（新，`ExecStart=/opt/d1max/current/venv/bin/d1max-agent …`，`User=robot`）；`install.sh` 5/7 `install -m 0644` 该单元，**不 enable**，声明块；`uninstall.sh` 删；`d1max-privileged` 白名单不变（agent 单元不经 OTA）；Tests `tests/engine/test_release_pack.py`、`tests/test_deploy_files.py`

- [ ] 测试：包里有 `packages/robot-agent/pyproject.toml`、没有 `packages/*/tests`；`install.sh` 含三次 `pip install … packages/…`（顺序 contract → adapter-sim → robot-agent）与 `d1max-agent.service` 的 `install -m 0644`，且**没有** `systemctl enable d1max-agent`；单元 `User=robot`、`ExecStart` 指 `current/venv/bin/d1max-agent`；`uninstall.sh` `rm_sys "$UNIT_DIR/d1max-agent.service"` + 声明。
- [ ] 实现 → 绿；`bash -n`。Commit `deploy: pack packages/, install robot-agent into the slot venv, ship d1max-agent.service disabled`.

### Task 6：文档、报告、工单

- [ ] `docs/W00b-完工报告.md`；工单表 W00b 打勾；`分卷与进度.md` 口径段一句；`庄园场景待真机测试.md` 加 W00b 的一条（装机后 `ls /opt/d1max/current/venv/bin/d1max-agent`、`systemctl is-enabled d1max-agent` = disabled）。全量、突变检查、内部评审、push。
