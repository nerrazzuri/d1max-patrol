# W00 · 最小契约 + adapter-sim —— 设计稿（待用户拍板）

日期：2026-09-24 · 工单：`docs/庄园安防-差距核查与工单.md` W00（L）· 上游：`docs/superpowers/specs/2026-09-22-saas-platform-robot-decoupling-design.md`（下称「总设计」）§2–4、§6
状态：**已确认**（2026-09-24 用户拍板：四个决定全选 A）。

## 0. 范围（照总设计 §6，一字不加）

覆盖：设备认证与注册的数据结构、能力/状态两份报文、`goto`、`abort`、命令幂等与过期、`control_epoch`、事件序号、断线与重连对账、资源表与断线策略表的数据结构。**不做**：`patrol/standoff/deter/recharge/return_home/teleop`、证据回传、遥控通道、站点 UI、云端。

验收（端到端一条测试，必须过）：站点派 `goto` → 代理校验（认证、代次、过期、地图版本、前置条件）→ adapter-sim 执行 → 回执与进度 → 中途断线 → 命令重复投递 → 重连对账 → `abort` → 停止确认。

## 1. 要拍板的四个决定

### 决定 1：包怎么摆

| | 方案 | 代价 | 推荐 |
|---|---|---|---|
| A | 按总设计 §5：`packages/contract`、`packages/adapter-sim`、`packages/robot-agent`，每个有自己的 `pyproject.toml` + `src/` + `tests/`；根 `pyproject` 的 pytest `testpaths` 加上它们；`.venv` 里 `pip install -e` 三次 | 装机脚本、`release pack` 现在只打根 `src/`，W00b 把 robot-agent 搬进去时要一起改；测试命令不变 | **推荐**。现在就按最终形状放，W00b 不用再搬一次 |
| B | 先放在根 `src/` 下三个新顶级包 `d1max_contract`、`d1max_adapter_sim`、`d1max_agent`，W00b 再挪到 `packages/` | 现在零打包改动；W00b 多一次目录搬迁 + import 路径全改 | 不推荐：等于把同一批文件搬两次 |

### 决定 2：MQTT 库与测试用总线

| | 方案 | 说明 | 推荐 |
|---|---|---|---|
| A | 新依赖 **`paho-mqtt>=2.1`**（纯 Python，`py3-none-any` 一个轮子 66 KB，狗上离线可装）；代码只依赖我们自己的 `Transport` 接口，`PahoTransport` 是其一实现；测试用**进程内 `MemoryBroker`**（自己写，几百行：QoS 1 至少一次、retained、LWT、按主题订阅、断线/重复投递注入），验收流程在它上面跑；有 `D1MAX_MQTT_TEST_URL` 时另跑一条对真 broker 的冒烟测试，本机没 mosquitto 就跳过 | 验收流程确定性高、不装 broker 也能跑全量；paho 只在冒烟和真机用 | **推荐** |
| B | 测试也用真 broker（本机装 mosquitto，或 Python 的 `amqtt`） | 全量测试依赖外部进程/多一个大依赖；断线、重复投递靠真网络注入，不确定 | 不推荐 |

### 决定 3：契约的「真理源」放哪

| | 方案 | 说明 | 推荐 |
|---|---|---|---|
| A | **Python 数据类是真理源**（`to_wire/from_wire/validate`，每类带 `schema` 主版本），黄金夹具由 Python 生成到 `packages/contract/fixtures/*.json`，供 Dart（W00c 手机改连站点时）与站点复用——沿用现有「Python 夹具先红、Dart 跟着红」机制 | 不引入 `jsonschema`（它拖 `rpds-py` 这种 Rust 轮子，狗上离线麻烦）；JSON Schema 文件等第二个非 Python 消费者出现（W00c）再从数据类导出 | **推荐** |
| B | 先写 JSON Schema 文件 + 代码生成 Python/Dart 模型 | 总设计 §5 原话是「schema + 生成的模型」；但现在只有一个消费者，生成器本身是新工作量 | 可接受，但 W00 里是纯开销 |

### 决定 4：W00 的「认证与注册」做到哪一步

| | 方案 | 说明 | 推荐 |
|---|---|---|---|
| A | W00 只做**数据结构与代理侧执行**：`Registration`（站点签发 `robot_id`、`site_id`、凭证指纹、有效期）、`TopicAcl(robot_id)` 生成允许发/订的主题表，代理的 `Transport` 包装层**拒绝**发到自己命名空间之外、拒绝订别人的 `cmd`；broker 侧 mTLS 与 ACL 落地归 W00c（站点最小版，那时才有 broker 配置可写） | 与总设计 §3.6「站点最小规格的一部分」一致：规格在 W00 定，站点侧执行在 W00c | **推荐** |
| B | W00 里把 mosquitto 的 mTLS/ACL 配置也写出来 | 没有 site-node 包可放，写了也没处跑 | 不推荐 |

## 2. 契约细则（按总设计 §3 展开，W00 只实现表中打 ✔ 的）

主题：`site/<site_id>/robot/<robot_id>/<kind>`，`kind ∈ {capabilities, status, cmd, cmd/ack, telemetry, event, reconcile}`。每条报文顶层 `{"schema":"1.0", ...}`，主版本不同即拒（代理拒连、站点拒派）。

| 报文 | 方向 / QoS / retained | W00 字段 | ✔ |
|---|---|---|---|
| `capabilities` | 狗→站，QoS 1，retained，掉线不清 | `robot_id agent adapter tasks{goto{max_speed_mps}} actuators sensing` —— 由代理**合成**（HAL 能力 × 软件 × 已加载地图 × 标定），W00 里 `tasks` 只有 `goto`（后来加的：`goto.path` 导航走哪种路 `straight`/`planned`，W00c6b；`goto.autonomy`/`patrol.autonomy` 自主级别，W00c6i；`patrol`、`teleop` 等见各单） | ✔ |
| `status` | 狗→站，QoS 1，retained，LWT 改写为 `online:false` | `online boot_id ready{control motion estop_clear loc_ok} control_epoch last_seen task{task_id kind state}` | ✔ |
| `cmd` | 站→狗，QoS 1，**不 retained** | `command_id task_id kind issued_at expires_at control_epoch precondition{expect_task_state} priority offline_policy payload` | ✔ |
| `cmd/ack` | 狗→站，QoS 1 | `command_id task_id result ∈ accepted/rejected/expired/duplicate reason original(duplicate 时原结果)` | ✔ |
| `event` | 狗→站，QoS 1 | `event_id seq boot_id stamp kind data`；W00 只有 `task_progress task_done task_failed task_preempted task_aborted` | ✔ |
| `reconcile` | 狗→站，QoS 1，重连后第一条 | `boot_id control_epoch task{…} unacked_from_seq unacked_to_seq`（0/0 = 没有） | ✔ |
| `telemetry` | 狗→站，QoS 0，1 Hz | `stamp pose{map_id map_version frame_id x y yaw} battery_pct task_state loc_quality net` | ✔（最小字段） |

`goto` 的 `payload`：`{"target":{"map_id","map_version","frame_id","x","y","yaw"},"max_speed_mps"}`；`map_id/map_version` 与代理已加载地图不一致 → `rejected(map_mismatch)`。`abort` 的 `payload`：`{"reason"}`；前置 `expect_task_state` 可选。

**代理侧校验顺序**（固定，测试逐条钉）：schema 主版本 → 认证（`robot_id/site_id` 与自己的注册一致）→ `control_epoch` ≥ 当前代次（更小拒 `stale_epoch`；更大则**采纳新代次**并让旧代次命令失效）→ `expires_at` 未过 → `command_id` 是否已见过（见过 → `duplicate` + 原结果，**不执行**）→ 能力（`tasks` 里有这个 `kind`）→ 前置条件 → 资源（§3）→ `accepted`。

**幂等存储**：`command_id → 结果` 落盘（JSON 行文件，追加写；启动时重放），代理重启后重复投递仍回 `duplicate`。**事件序号**：`(boot_id, seq)`，`seq` 从 1 单调递增，落盘 outbox；站点 ack 到哪（W00 用「已发布即视为投递」+ 重连时 `unacked_events` 报最近窗口，站点侧去重）。

## 3. 资源表与断线策略表（数据，不是代码分支）

```python
RESOURCES = ("motion", "light", "sound", "spotlight", "head", "camera")
TASK_RESOURCES = {"goto": {"motion"}, "abort": set()}          # 其余任务各自工单再加
# 两个占 motion 的任务互斥;高优先级命令到达 → 先取消旧动作、等到停止确认、再让新任务拿 motion
OFFLINE_POLICY = {                                              # 总设计 §4.3
  "goto":  Policy(on_disconnect="continue_if_safe", queue_cmds={"abort"}, new_cmd_expires=True),
  "abort": Policy(on_disconnect="execute_locally", queue_cmds=set(), new_cmd_expires=False),
}
```

W00 里 `goto` 的断线行为：定位、电量、急停都正常 → 继续走到点；任一不满足 → 停并等待。`abort` 断线时排队（重连后仍执行）。

## 4. RobotHAL（总设计 §2.2 全表定名字；adapter-sim 只实现 W00 需要的那些，其余用能力声明「没有」）

`packages/contract/src/d1max_contract/hal.py`：一个 `Protocol` 把 §2.2 十一类原语全部列出（方法名在此定死）；`HalCapabilities` 数据类声明 `motion.max_vx/max_wz/deadband`、`control.releasable`、`recharge.mode`、传感器/执行器有无。适配器对没有的能力抛 `HalUnsupported`，且必须在 `hal_capabilities()` 里报 `false`——测试钉「声明了就得有入口，没声明就得抛」。

W00 需要并由 adapter-sim 实现：`connect/close/health`、`acquire_control/release_control/control_status`、`motion_status`、`set_velocity(VelocityCommand{seq ttl_ms frame vx vy wz}) → VelocityResult{applied clamped rejected reason}`（`ttl` 到期自停）、`stop()/stopped()`、`emergency_stop/estop_status/estop_reset`、`odometry()`、`battery()`、`faults()`、`hal_capabilities()`。adapter-sim 用现有 `d1max_sim/kinematics.py` 的 `Planar2DModel` 推进位姿，可注入的钟，可注入故障（`estop`、`loc_lost`、控制权被夺）。

## 5. 代理核（`packages/robot-agent`，W00 只放契约运行时；`engine/` 在 W00b 搬）

```
d1max_agent/
  transport.py     GuardedTransport:代理侧命名空间守卫(决定 4);Transport 接口本身在契约包
  commands.py      CommandProcessor: §2 校验顺序 → ack;调度到任务;抢占(先停旧、等停止确认、再给新)
  idempotency.py   命令结果落盘/重放
  events.py        (boot_id, seq) 事件簿 + outbox;reconcile 报文由 runtime 组
  resources.py     资源账本(表本身在契约包 resources.py)
  status.py        capabilities 合成、status(含 LWT 那份)、telemetry
  tasks/base.py    步进式任务的最小形状
  tasks/goto.py    最小 goto:按里程计向目标点走(P 控制,受 max_speed 与 HAL 上限夹),progress 事件,abort → stop → 等 stopped → task_aborted
  runtime.py       把以上装起来的一个类:start()/step(dt)/close();断线策略表(契约包 policy.py)在这里被用
```

实施时的偏离:``Registration`` 装载与断线策略表都放在契约包(两侧都要认),代理里没有单独的 ``identity.py``/``policy.py``。``OfflinePolicy.queue_cmds``/``new_cmd_expires`` 与 ``Command.offline_policy`` 在 W00 里**只登记数据**:排队实际靠 broker 的 ``clean_session=False`` 离线收件箱,过期靠 ``expires_at``;这两个字段的消费者是 W00c 的派遣器(决定给哪类命令多长的 ttl、断线时要不要撤回)。

站点侧 W00 只需要一个**派遣客户端库**（`d1max_contract.dispatch.DispatchClient`：发命令、等回执/进度、处理 reconcile、按 `(boot_id, seq)` 去重），验收测试当「站点」用；W00c 的派遣器在它上面长。

## 6. 测试

- `packages/contract/tests`：每类报文 `to_wire/from_wire` 往返、坏字段拒绝、主版本不同拒绝、黄金夹具与 `fixtures/` 一致（夹具由 Python 生成，改了报文夹具先红）。
- `packages/adapter-sim/tests`：HAL 契约测试（能力声明与入口一致、`set_velocity` 夹与拒、`ttl` 自停、`stop → stopped` 顺序、估停被夺控制权后 `set_velocity` 拒）。
- `packages/robot-agent/tests`：校验顺序逐条；幂等落盘重启仍 `duplicate`；代次升降；过期；`map_mismatch`；资源互斥与抢占（先停止确认再给 motion）；事件 `seq` 单调、`boot_id` 变化；reconcile 内容；断线策略表。
- **验收**：`packages/robot-agent/tests/test_acceptance_w00.py` 在 `MemoryBroker` 上跑总设计 §6 的整条流程，含：断线期间 `abort` 排队、重连后先发 `reconcile`、重复投递回 `duplicate` 且不重走、代理**重启**（新 `boot_id`）后旧 `command_id` 仍 `duplicate`。
- 冒烟：`D1MAX_MQTT_TEST_URL` 设了才跑 `PahoTransport` 对真 broker 的 pub/sub/retained/LWT 四件事。

## 7. 不做 / 留给后面

- 不搬 `engine/`（W00b）；`goto` 在 W00 里走的是 HAL 速度环，不是 `MissionEngine`。W00b 把 `MissionEngine` 接进来时 `goto` 任务换实现、契约不变。
- 不改 `server.py`、手机、装机脚本；狗上不部署任何 W00 产物。
- broker 的 mTLS/ACL 配置（W00c）。JSON Schema 导出（W00c）。

## 8. 风险

- 抢占语义「先停止确认再交资源」依赖 `stopped()` 真能报——sim 上是模型量，真机上是 W00d 要测的参数。
- `MemoryBroker` 是我们自己的 QoS 1 语义模型；与 mosquitto 的差异只能靠冒烟测试与真机暴露。
