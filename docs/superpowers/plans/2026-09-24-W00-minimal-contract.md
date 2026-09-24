# W00 · 最小契约 + adapter-sim —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 三个新包（contract、adapter-sim、robot-agent）加一条端到端验收：站点派 `goto` → 代理校验 → sim 执行 → 回执/进度 → 断线 → 重复投递 → 重连对账 → `abort` → 停止确认。

**Architecture:** 契约包是唯一被两侧依赖的东西：报文数据类（真理源）、主题与 ACL、资源/断线策略表、`RobotHAL` 协议、`Transport` 接口 + 进程内 `MemoryBroker` + `PahoTransport`、派遣客户端。adapter-sim 用平面运动学实现 HAL 的 W00 子集。robot-agent 是契约运行时：校验链、幂等落盘、事件簿、资源仲裁、状态发布、`goto` 任务。设计：`docs/superpowers/specs/2026-09-24-W00-minimal-contract-design.md`。

**Tech Stack:** Python 3.10、asyncio、dataclasses、paho-mqtt 2.x（仅 `PahoTransport`）、pytest（`asyncio_mode=auto`）。

**测试命令：** `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest -p no:cacheprovider -q`（根 `pyproject` 的 `testpaths` 加了三个包的 `tests/`）。

---

## 目录

```
packages/contract/            pyproject.toml  src/d1max_contract/  tests/  fixtures/
packages/adapter-sim/         pyproject.toml  src/d1max_adapter_sim/  tests/
packages/robot-agent/         pyproject.toml  src/d1max_agent/  tests/
```

依赖方向：adapter-sim → contract；robot-agent → contract（+ adapter-sim 仅测试）。契ract 依赖根包 `d1max-patrol` 里的 `Pose`？**不**：契约自带 `MapPose`，不 import 根包。adapter-sim 复用 `d1max_sim.kinematics.wrap_angle`（根包已装在 venv 里），仅此一处。

## 关键类型（后面所有任务都按这里的名字）

```python
# d1max_contract/messages.py
SCHEMA = "1.0"
@dataclass(frozen=True) class MapPose: map_id: str; map_version: str; frame_id: str; x: float; y: float; yaw: float
class AckResult(str, Enum): ACCEPTED="accepted"; REJECTED="rejected"; EXPIRED="expired"; DUPLICATE="duplicate"
class TaskState(str, Enum): PENDING="pending"; RUNNING="running"; DONE="done"; FAILED="failed"; ABORTED="aborted"; PREEMPTED="preempted"
@dataclass(frozen=True) class Precondition: expect_task_state: TaskState | None = None
@dataclass(frozen=True) class Command: command_id: str; task_id: str; kind: str; issued_at: int; expires_at: int; control_epoch: int; payload: dict; priority: int = 0; offline_policy: str = "default"; precondition: Precondition | None = None
@dataclass(frozen=True) class Ack: command_id: str; task_id: str; result: AckResult; reason: str = ""; original: dict | None = None
@dataclass(frozen=True) class Event: event_id: str; seq: int; boot_id: str; stamp: int; kind: str; data: dict
@dataclass(frozen=True) class Ready: control: bool; motion: bool; estop_clear: bool; loc_ok: bool   (+ property ok)
@dataclass(frozen=True) class TaskSummary: task_id: str; kind: str; state: TaskState
@dataclass(frozen=True) class Status: online: bool; boot_id: str; ready: Ready; control_epoch: int; last_seen: int; task: TaskSummary | None
@dataclass(frozen=True) class Capabilities: robot_id: str; agent: str; adapter: str; tasks: dict[str, dict]; actuators: dict; sensing: dict
@dataclass(frozen=True) class Reconcile: boot_id: str; control_epoch: int; task: TaskSummary | None; unacked_from_seq: int; unacked_to_seq: int
@dataclass(frozen=True) class Telemetry: stamp: int; pose: MapPose | None; battery_pct: float; task_state: TaskState | None; loc_quality: float; net: dict
每类: to_wire() -> dict(含 "schema"); @classmethod from_wire(d) -> 校验字段类型,主版本不同抛 SchemaMismatch,缺字段/类型错抛 ContractError

# d1max_contract/topics.py
@dataclass(frozen=True) class Topics: site_id; robot_id; .capabilities .status .cmd .ack .event .reconcile .telemetry ; @classmethod parse(topic) -> (site_id, robot_id, kind)
class TopicAcl: __init__(topics); may_publish(topic) -> bool (只许自己的 capabilities/status/ack/event/reconcile/telemetry); may_subscribe(filter) -> bool (只许自己的 cmd)

# d1max_contract/hal.py
class MotionStatus(str, Enum): UNKNOWN LYING STANDING READY   # READY = 能收速度
@dataclass(frozen=True) class VelocityCommand: seq: int; ttl_ms: int; frame: str; vx: float; vy: float; wz: float
@dataclass(frozen=True) class VelocityResult: applied_vx: float; applied_wz: float; clamped: bool; rejected: bool; reason: str = ""
@dataclass(frozen=True) class Health: link_ok: bool; control: bool; estop: bool; faults: tuple[str, ...]; loc_quality: float
@dataclass(frozen=True) class Odometry: stamp_ms: int; frame_id: str; x: float; y: float; yaw: float; vx: float; wz: float; valid: bool
@dataclass(frozen=True) class Battery: percent: float; charging: bool
@dataclass(frozen=True) class Fault: code: str; fatal: bool; text: str
@dataclass(frozen=True) class HalCapabilities: max_vx: float; max_wz: float; deadband_vx: float; lateral: bool; control_releasable: bool; recharge_mode: str; sensing: dict[str,bool]; actuators: dict[str,bool]
class HalUnsupported(Exception)
class RobotHAL(Protocol): 十一类原语全部列出(见设计 §4,方法名照总设计 §2.2 表)

# d1max_contract/transport.py
@dataclass(frozen=True) class Message: topic: str; payload: bytes; qos: int; retain: bool
Handler = Callable[[Message], Awaitable[None]]
class Transport(Protocol):
    connected: bool
    async def connect(self) -> None; async def close(self) -> None
    def set_will(self, topic, payload: bytes, qos=1, retain=True) -> None
    async def publish(self, topic, payload: bytes, *, qos=1, retain=False) -> None
    async def subscribe(self, topic_filter, handler: Handler, *, qos=1) -> None
    def on_connection(self, cb: Callable[[bool], None]) -> None    # True=连上 False=断了

# d1max_contract/memory_broker.py
class MemoryBroker: retained: dict; clients; publish(topic, payload, qos, retain, from_client); subscribe(client, filter, qos); disconnect(client) → 触发 LWT、清订阅但保留 client 的离线队列; duplicate_next(n) 让接下来 n 条 QoS1 投递重复一次; async drain()
class MemoryTransport(Transport): __init__(broker, client_id); 断线时 publish 入本地队列,重连后按序补发(模仿 paho QoS1); connected 属性
```

---

### Task 1：三个包的骨架与工作区接线

**Files:** Create `packages/contract/pyproject.toml`、`packages/adapter-sim/pyproject.toml`、`packages/robot-agent/pyproject.toml`、各 `src/<pkg>/__init__.py`、各 `tests/__init__.py`（空）；Modify 根 `pyproject.toml`（`testpaths` 加三处；`[tool.ruff]` 默认全仓扫描，不用改）；Test `tests/test_workspace.py`（新）

- [ ] 测试：三个包能 import（`d1max_contract.SCHEMA == "1.0"`、`d1max_adapter_sim`、`d1max_agent`）；三个 `pyproject.toml` 各自 `name` 为 `d1max-contract`/`d1max-adapter-sim`/`d1max-robot-agent`，依赖方向正确（adapter-sim 依赖 contract、robot-agent 依赖 contract、contract 不依赖 d1max-patrol）；`paho-mqtt` 只在 contract 的 `[project.optional-dependencies] mqtt`；根 `pyproject` 的 `testpaths` 含三处。
- [ ] 跑：红。
- [ ] 实现三个 pyproject（`setuptools`，`where=["src"]`，`requires-python >=3.10`），`__init__.py`；根 `pyproject` `testpaths = ["tests", "packages/contract/tests", "packages/adapter-sim/tests", "packages/robot-agent/tests"]`；`.venv/bin/python -m pip install -e packages/contract -e packages/adapter-sim -e packages/robot-agent --no-deps`；`pip install "$CLAUDE_JOB_DIR/tmp/wheels/paho_mqtt-2.1.0-py3-none-any.whl"`。
- [ ] 跑：绿。Commit `W00: workspace — contract / adapter-sim / robot-agent packages`.

### Task 2：报文数据类（真理源）+ 主题与 ACL

**Files:** Create `d1max_contract/{errors,wire,messages,topics}.py`；Test `packages/contract/tests/test_messages.py`、`test_topics.py`

- [ ] 测试（每类）：`from_wire(x.to_wire()) == x`；缺字段 → `ContractError` 且文本含字段名；`schema="2.0"` → `SchemaMismatch`；`schema="1.3"` 接受；`AckResult`/`TaskState` 非法值拒；`Command.expires_at < issued_at` 拒；`MapPose` 字段类型错拒。Topics：七个主题字符串精确；`parse` 往返；`TopicAcl`：自己的六个发布主题放行、别人的 robot_id 拒、发到 `cmd` 拒、订 `site/+/robot/+/cmd` 拒、订自己的 `cmd` 放行。
- [ ] 跑：红。实现。跑：绿。Commit `contract: messages, topics, ACL`.

### Task 3：资源表、断线策略表、注册数据结构

**Files:** Create `d1max_contract/{resources,policy,registration}.py`；Test `test_tables.py`、`test_registration.py`

- [ ] 测试：`RESOURCES` 六个；`TASK_RESOURCES["goto"] == {"motion"}`、`["abort"] == set()`；每个任务的资源都在 `RESOURCES` 里；`OFFLINE_POLICY` 对 `goto`/`abort` 的三个字段与设计 §3 一致；`conflicts("goto", "goto") is True`、`conflicts("goto","abort") is False`。`Registration` 往返 JSON 文件、`valid_at(now)` 过期判定、`fingerprint` 非空。
- [ ] 跑：红 → 实现 → 绿。Commit `contract: resource table, offline policy, registration`.

### Task 4：RobotHAL 协议与数据类

**Files:** Create `d1max_contract/hal.py`；Test `test_hal_protocol.py`

- [ ] 测试：`RobotHAL` 有总设计 §2.2 表列出的每个方法名（用一个显式名单断言 `hasattr`，名单写进测试）；`VelocityResult` 不允许 `rejected and clamped` 同时为真（构造即抛）；`HalCapabilities.to_wire()` 往返。
- [ ] 实现 → 绿。Commit `contract: RobotHAL protocol`.

### Task 5：Transport 接口 + MemoryBroker

**Files:** Create `d1max_contract/{transport,memory_broker}.py`；Test `test_memory_broker.py`

- [ ] 测试：pub/sub 精确主题；`+`/`#` 过滤；retained 对后订阅者投递；LWT 在 `disconnect(client)` 时投给订阅者且 retained 覆盖；断线期间发布入队、重连后按序补发；`duplicate_next(1)` 让下一条 QoS1 投递两次，QoS0 不重复；`drain()` 之后没有挂起投递；一个 handler 抛异常不影响其他订阅者。
- [ ] 实现 → 绿。Commit `contract: Transport + MemoryBroker`.

### Task 6：PahoTransport（冒烟，默认跳过）

**Files:** Create `d1max_contract/paho_transport.py`；Test `test_paho_smoke.py`（`D1MAX_MQTT_TEST_URL` 没设 → `pytest.skip`）

- [ ] 实现 `PahoTransport(url, client_id, will)`：paho 2.x `CallbackAPIVersion.VERSION2`，`loop_start`，`on_message` 把消息投进 asyncio loop（`call_soon_threadsafe`），`publish` 用 `wait_for_publish` 于线程池；`on_connection` 回调。冒烟：pub/sub、retained、LWT（第二个客户端断开）、QoS1。paho 未安装 → `import` 时给清楚的 `ImportError` 文本。
- [ ] Commit `contract: PahoTransport (smoke test opt-in)`.

### Task 7：DispatchClient（站点侧最小派遣客户端）

**Files:** Create `d1max_contract/dispatch.py`；Test `test_dispatch.py`（用 MemoryBroker + 一个假代理 handler）

- [ ] 测试：`send(cmd)` 返回对应 `command_id` 的 `Ack`（其他 ack 不串）；超时抛 `DispatchTimeout`；事件按 `(boot_id, seq)` 去重（重复投递只回调一次）、按 `seq` 排序；`status`/`capabilities`/`reconcile` 最新值可读；`new_command(kind, payload, ttl_ms, epoch)` 生成唯一 `command_id`/`task_id`、`expires_at = issued_at + ttl`。
- [ ] 实现 → 绿。Commit `contract: DispatchClient`.

### Task 8：黄金夹具

**Files:** Create `d1max_contract/fixtures.py`（`generate() -> dict[name, dict]`）、`packages/contract/fixtures/*.json`；Test `test_fixtures.py`

- [ ] 测试：`generate()` 的每个样本 `from_wire` 成功；盘上文件与 `generate()` 逐字节一致（改了报文夹具先红，提示跑 `python -m d1max_contract.fixtures --write`）。
- [ ] 实现 → 生成 → 绿。Commit `contract: golden fixtures`.

### Task 9：adapter-sim

**Files:** Create `d1max_adapter_sim/robot.py`（`SimRobot`）、`d1max_adapter_sim/faults.py`；Test `packages/adapter-sim/tests/test_sim_hal.py`

- [ ] 测试：`hal_capabilities()` 声明的能力与入口一致（声明 `false` 的方法抛 `HalUnsupported`，声明 `true` 的不抛）；未 `acquire_control` → `set_velocity` `rejected(no_control)`；`vy≠0` → `rejected(no_lateral)`；`vx` 超上限 → `clamped` 且 `applied_vx == max`；`0<|vx|<deadband` → `rejected(deadband)`；`ttl` 到期后 `tick` 速度归零、`odometry().vx == 0`；`stop()` 后 `stopped()` 在 `stop_latency` 内变真；`emergency_stop(True)` 后 `set_velocity` 拒且 `stopped()`；`estop_reset()` 不恢复速度；`inject_control_lost()` 后 `control_status().held is False` 且拒速度；`inject_loc_lost(True)` → `health().loc_quality == 0`；`tick(dt)` 按 `vx,wz` 积分位姿（数值断言）；`battery()` 随时间线性降。
- [ ] 实现（时钟注入 `now_ms: Callable[[], int]`，`tick(dt_s)`）→ 绿。Commit `adapter-sim: SimRobot implements RobotHAL W00 subset`.

### Task 10：代理核 — identity、idempotency、events

**Files:** Create `d1max_agent/{identity,idempotency,events}.py`；Test `packages/robot-agent/tests/test_idempotency.py`、`test_events.py`

- [ ] 测试：`IdempotencyStore(path)`：`remember(command_id, ack)`/`lookup`；重开同一路径仍能 `lookup`；文件坏一行只丢那一行、其余可读。`EventBook(path, boot_id)`：`emit(kind, data) -> Event` `seq` 从 1 单调；重启新 `boot_id` `seq` 归 1；`unacked_range()` 给 `(from, to)`；`mark_acked(seq)`；outbox 落盘重放。
- [ ] 实现 → 绿。Commit `agent: identity, idempotency store, event book`.

### Task 11：代理核 — resources、policy、commands 校验链

**Files:** Create `d1max_agent/{resources,policy,commands}.py`；Test `test_commands.py`、`test_resources.py`

- [ ] 测试（校验顺序逐条，用一个假 HAL/假任务）：schema 主版本 → `rejected(schema)`；`robot_id/site_id` 不符 → `rejected(auth)`；`epoch` 小 → `rejected(stale_epoch)`；`epoch` 大 → 采纳并让旧代次后续命令 `stale_epoch`；`expires_at` 过 → `expired`；重复 `command_id` → `duplicate` 且 `original` 是原 ack、任务不重起；未知 `kind` → `rejected(unsupported)`；`goto` 的 `map_id/map_version` 不符 → `rejected(map_mismatch)`；`expect_task_state` 不符 → `rejected(precondition)`；`motion` 被占且 `priority` 不高 → `rejected(busy)`；`priority` 高 → 旧任务 `preempted`，且**先等 `stopped()` 再**给新任务 motion（用可控 `stopped` 的假任务断言顺序）。
- [ ] 实现 → 绿。Commit `agent: command validation chain, resources, offline policy`.

### Task 12：代理核 — status/capabilities/telemetry、goto 任务、runtime

**Files:** Create `d1max_agent/{status,tasks/goto,runtime}.py`；Test `test_goto.py`、`test_runtime.py`

- [ ] 测试 goto（SimRobot + 假钟）：向 `(2,0,0)` 走，`step` 若干次后 `odometry` 到目标容差内、任务 `done`、途中有 `task_progress`（含距离）、速度不超 `min(max_speed_mps, HAL max)`；`abort()` → 先 `stop()`、等 `stopped()` 再发 `task_aborted`；估停期间 `set_velocity` 被拒 → 任务 `failed(estop)`。
- [ ] 测试 runtime（MemoryBroker）：`start()` 后 retained `capabilities` 与 `status(online=true, boot_id)` 已发；`tasks` 只含 `goto`；断线 → 订阅者收到 LWT `online=false`；重连 → 第一条是 `reconcile`；`telemetry` 每 `step` 满 1 s 发一条。
- [ ] 实现 → 绿。Commit `agent: status/telemetry, goto task, runtime`.

### Task 13：验收

**Files:** Test `packages/robot-agent/tests/test_acceptance_w00.py`

- [ ] 一条测试按总设计 §6 顺序，每步断言：`goto` `accepted` → `task_progress` → `broker.disconnect(agent)` → 期间狗继续走（sim 位姿前进）→ 站点重发同一 `goto`（重复投递）与一条 `abort`（离线排队）→ `broker.reconnect(agent)` → 第一条 `reconcile`（`task.state == running`、`unacked` 区间含断线期间的进度事件）→ 重复 `goto` 回 `duplicate` 且未重起 → `abort` `accepted` → `stopped()` 真 → `task_aborted` → `status.task is None`。再一条：代理**重启**（新 `AgentRuntime` 同一路径、新 `boot_id`）后重发旧 `command_id` 仍 `duplicate`。
- [ ] 绿。Commit `W00: end-to-end acceptance on MemoryBroker`.

### Task 14：文档、报告、工单

- [ ] `docs/W00-完工报告.md`；工单表 W00 打勾；`docs/分卷与进度.md` 口径段加一句「packages/ 三包已建」；`docs/庄园场景待真机测试.md` 加 W00d 的预留一节（真机验 HAL 归 W00d，不在本单）。全量测试；突变检查；内部评审；push。

## 自查

- 设计 §0 验收 → Task 13；§2 报文 → Task 2；校验顺序 → Task 11；§3 表 → Task 3、11；§4 HAL → Task 4、9；§5 代理核模块 → Task 10–12；派遣客户端 → Task 7；夹具 → Task 8；冒烟 → Task 6；决定 1 → Task 1；决定 4 → Task 2（ACL）+ Task 12（runtime 用 `TopicAcl` 包 transport）。
- 名字一致：`Command/Ack/Event/Status/Capabilities/Reconcile/Telemetry/MapPose`、`Topics/TopicAcl`、`RobotHAL/HalCapabilities/VelocityCommand/VelocityResult`、`Transport/Message/MemoryBroker/MemoryTransport`、`DispatchClient`、`SimRobot`、`IdempotencyStore/EventBook`、`AgentRuntime`。
