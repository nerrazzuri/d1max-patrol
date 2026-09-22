# 平台与机器狗解耦：SaaS 化架构设计

日期：2026-09-22 · 基准：`master` @ `59a7620` · 状态：**已确认**（2026-09-22，用户确认第二稿；第一稿经外部审查后按其意见重写）

上游：`docs/庄园安防-差距核查与工单.md`（W01–W30）、《私人庄园智能机器狗巡检与安防解决方案 V1.0》。
下游：本设计确认后，工单表的归属与顺序按第七节调整。

## 0. 结论与前提

整套安防系统做成**类 SaaS 的平台**，机器狗是**按需集成的执行单元**。平台始终与狗一起交付，狗的品牌可换。
狗与平台之间只有**一条契约**（MQTT 报文，第三节），换品牌只写一个**适配器**（第二节）。
现有代码**拆开重组、优先保留模块**，不重写（第五节）。

已拍板、不再讨论的前提：

| 问题 | 决定 |
|---|---|
| 集成边界 | 始终带狗一起卖，品牌可换（不做"无狗也能卖"，不做无人机等其他执行设备） |
| 部署形态 | 现场节点 + 云端控制面 |
| 导航与安全行为放哪 | 狗上跑我们自己的代理；只通过硬件抽象层碰厂商 SDK |
| 驱离上装的归属 | 算本体能力，由适配器声明 |

契约形式：**消息总线 + 能力清单**（MQTT + JSON）。放弃 ROS2 直连（把平台绑死在 ROS、换品牌要重来）与 gRPC（点对点，扇出与断线重连要自己做）。

**"换品牌只写适配器"的准确含义**：满足第二节最低硬件与控制契约的品牌，业务层（代理、站点、手机）不需要改写；**仍需完成机型参数、标定、能力验证和安全验收**。解耦的是代码，不是验证。

## 1. 分层与边界

```
云端控制面（多租户）
  账号/租户/角色 · 站点注册 · 远程访问中转 · 订阅授权 · 版本分发 · 异地备份 · 多站点总览
        ▲  HTTPS/WSS，站点主动外连；断了站点照常运行
现场节点（每客户一台，"站点"）
  MQTT broker · 设备认证 · 事件引擎(CCTV/AI → 安防事件 → 派遣规则) · 派遣器 · 告警与通知
  · 证据库与录像 · 地图/路线/区域/拦截点（权威版本）· 排程执行器 · 站点 API（App 与大屏只连这里）
        ▲  MQTT：能力/状态 · 命令/回执 · 遥测/事件 —— 唯一的狗↔平台契约；证据走 HTTP
机器人代理（跑在狗的计算单元上，品牌无关）
  任务执行 · 资源仲裁 · 定位/规划/避障/保持距离 · 安全策略 · 断线策略 · 离线队列 · 租约边界执行
        ▲  Python 接口 RobotHAL（第二节）
品牌适配器：d1max（sidecar + 厂商 SDK）· sim（假狗）· 未来其他品牌
  设备连接 · 厂商控制权 · 运动模式 · 设备级安全互锁
```

每层只和相邻层说话。硬规则：

1. **手机与大屏永远不直连狗**，都连站点 API。"狗发热点、手机进热点"的产品形态取消。
2. **站点断外网照常安防**。狗断站点的行为**按任务类型定义**（第四节），不是笼统的"照常完成"。
3. **换品牌只写适配器**，含义见第 0 节。
4. **安全分三层，任何一层失效下一层兜底**：站点校验权限 → 代理执行策略与租约边界 → 适配器执行设备级互锁。上层不能绕过下层的限制。

已知前提：D1 Max 本体只能发热点、不能作为客户端连 Wi-Fi。狗上站点的网靠上装 4G/CPE 走网口，或站点侧一个无线客户端接狗的热点。**必须在真机接入站点之前定**（第七节风险）。

## 2. RobotHAL：适配器接口

### 2.1 职责边界

> 适配器**不得持有**巡逻、派遣、驱离等业务任务状态；**允许并必须维护**设备连接、厂商控制权、硬件操作状态及底层安全互锁（SDK 断连即停止输出、速度命令超时即停、硬件急停状态、运动模式切换互锁）。任务策略属于代理；设备安全限制不可被上层绕过。

理由：代理进程卡死时，最靠近执行器的一层必须还能保护设备。

### 2.2 方法范围

具体命名在 W00 定；范围如下，**不得缺项**：

| 类别 | 原语 | 语义要求 |
|---|---|---|
| 生命周期 | `connect()`、`close()`、`health()` | 谁连 SDK、何时释放；`health()` 报 SDK 链路、控制权、急停、故障 |
| 设备控制权 | `acquire_control()`、`release_control()`、`control_status()` | 站点租约 ≠ 厂商运动控制权（D1 Max 有 `TakeControl/ReleaseControl` 与 `ControlLost` 事件）。不支持释放的机型在 HAL 能力里明示 |
| 运动准备 | `motion_status()`、可选 `set_motion_mode()` | 站立/趴下/轮/足/能否接收速度的统一入口；任务不得绕过 HAL 调厂商接口 |
| 速度命令 | `set_velocity(cmd)` → `VelocityResult` | `cmd` 含 `seq`、`ttl_ms`、`frame`、`vx vy wz`（m/s、rad/s）。返回**真实应用值**与 `clamped/rejected` 原因。做不到的速度**明确拒绝或返回实际值**，禁止静默改写（D4 的教训：上层看到零速、底层仍在动）。`ttl` 到期无新命令即停 |
| 停止 | `stop()` → 停止请求；`stopped()`/状态流中的停止确认 | 请求与确认分开；停止时间是**测出来的参数**，不是承诺 |
| 急停 | `emergency_stop(on)`、`estop_status()`、`estop_reset()` | 复位**不自动恢复任务**；硬件急停状态只读 |
| 感知流 | `odometry()`、`imu()`、`lidar()`、`ultrasonic()`、`joints()`（位置/速度/力矩）、`contacts()`（可选） | 统一封装：`stamp`、`frame_id`、`valid`、`age_ms`；提供机体尺寸、传感器标定与外参的静态描述 |
| 电池与故障 | `battery()`、`faults()` | 电池含充电状态；故障带厂商码与是否致命 |
| 执行器（有类型） | `light(channel, on)`、`strobe(channel, pattern, max_s)`、`sound(clip_id | tts, max_s)`、`spotlight(on, max_s)`、`head(pan, tilt)` | 简单开关可通用；语音、音频流、云台用有类型的操作并返回结果。声光类**必带最大持续时间** |
| 媒体 | `camera_sources()`、`snapshot(source)`、`stream_url(source)`、可选 `depth()`/`thermal()`、可选 `audio_session()` | 声明了 depth/thermal/intercom 就必须有对应读取入口；D1 Max 无音频 API，则不声明 |
| 回充 | 可选 `recharge_start()`、`recharge_stop()`、`undock()`、`recharge_status()` | 状态枚举：`idle / aligning / contacted / charging / failed(code) / undocking / undocked`。厂商不报成功的（D1 Max），适配器用电池充电状态合成 `charging` |

### 2.3 回充的两种实现边界

- **厂商提供完整自动对接**：HAL 暴露上表的可选回充操作；代理负责导航到停靠位、触发、监视状态、重试与放弃。
- **我们自己用标记做对接**：寻桩、对齐、重试属于代理；HAL 只提供运动、充电触点状态与电池反馈。

两种可并存，但**不得在同一个方法名后含糊处理**：HAL 能力里明示 `recharge.mode ∈ {vendor_dock, none}`。D1 Max 是 `vendor_dock`（无图回充，需正对桩约 1.5 m）。

### 2.4 HAL 能力（硬件层）

`hal_capabilities()` 只回答"设备能提供什么"：传感器、执行器、控制接口及其限制（最大速度、档位、死区、是否可释放控制权、回充模式）。**它不是站点看到的清单**，见 3.1。

## 3. 契约：狗 ↔ 站点

MQTT over TLS。主题 `site/<site_id>/robot/<robot_id>/<kind>`。每条报文带 `schema`（主版本不同即拒连）。

### 3.1 能力与状态：两份独立信息

**代理能力** `…/capabilities`（retained，变化时重发，**掉线不清空**）：
由代理**合成**——HAL 能力 × 当前软件 × 已加载地图 × 标定 × 已通过的能力验证。有深度相机不等于支持 `standoff`；有速度接口不等于支持安全导航。

```json
{"schema":"1.0","robot_id":"D1MAX-C40011","agent":"1.4.0","adapter":"d1max/0.2.0",
 "tasks":{"goto":{"max_speed_mps":3.0},"patrol":{},"standoff":{"min_m":2.0,"max_m":8.0},
          "deter":{"levels":["L0","L1"]},"recharge":{"mode":"vendor_dock"},"return_home":{}},
 "actuators":{"light":["front","back"],"siren":false,"speaker":false,"spotlight":false},
 "sensing":{"lidar":true,"depth":false,"thermal":false,"imu_hz":100,"joint_effort":true,"foot_force":false}}
```

**可用性** `…/status`（retained + LWT 改写为 `offline`）：`online`、`boot_id`（启动实例）、`ready`（控制权、运动模式、急停、定位质量）、`control_epoch`、`last_seen`、当前任务摘要。

**派遣条件**：已认证 ∧ 在线且 `last_seen` 新鲜 ∧ `ready` ∧ 所需任务能力存在 ∧ 所需资源空闲。**不是"有 retained 能力清单就能派"。**

### 3.2 命令 `…/cmd`（站点→狗，QoS 1，**不 retained**）与回执 `…/cmd/ack`

| 字段 | 作用 |
|---|---|
| `command_id` | 每条命令唯一；与 `task_id` 分开——同一任务的开始、暂停、取消是不同命令 |
| `task_id` | 命令作用的任务 |
| `issued_at`、`expires_at` | 过期命令代理**拒绝**并回 `expired`；防重连后执行十分钟前的出动/恢复 |
| `control_epoch` | 站点会话代次；旧代次命令拒绝 |
| `precondition` | 可选：`expect_task_state`，如 `resume` 必须指向 `paused` 的那个任务 |
| `priority`、`offline_policy` | 见第四节 |
| `payload` | 任务参数 |

**幂等**：代理持久化 `command_id → 结果`，重复投递返回原结果、不再执行；代理重启后仍成立。
**位姿**必带 `map_id`、`map_version`、`frame_id`；与代理已加载地图不一致即拒绝。

任务集：`goto`、`patrol`、`standoff`、`deter`、`recharge`、`return_home`、`pause/resume/abort`、`teleop_lease(grant/revoke)`。
回执：`accepted | rejected(reason) | expired | duplicate(原结果)`；随后 `progress | done | failed | preempted`。

**离线排队按命令类型定义**（第四节表），不是所有命令都排队。

### 3.3 遥测 `…/telemetry` 与事件 `…/event`

- 遥测：1 Hz，QoS 0。位姿（带 `map_id/frame_id`）、电池、任务态、定位质量、网络质量。
- 事件：QoS 1，带 `event_id`、`seq`、`boot_id`、`stamp`；站点按 `(boot_id, seq)` 去重与排序。事件类型：`person_detected`、`tamper`、`fallen`、`loc_lost`、`low_battery`、`stuck`、`task_*`、`evidence_ready`。狗只报**事实**，判定与升级在站点。
- **重连对账**：重连后代理先发 `…/reconcile`——当前任务、其状态、`control_epoch`、未确认事件区间；站点以此为准更新，**不凭历史事件猜狗现在在做什么**。

### 3.4 证据回传

HTTP，站点分配的对象存储凭证，收方重算哈希；完成后发 `evidence_ready`。狗侧只做**断网缓冲、上传确认后回收**；正式归档、保留、导出、审计在站点。

### 3.5 遥控

路径：手机 → 站点 → 代理，独立低延迟 WebSocket，不经 cmd 主题。

- 手机身份与权限由站点验证；**代理仍独立验证**租约、`control_epoch`、帧 `seq` 与新鲜度。站点授予权限，代理执行权限边界。
- 站点**不得**在手机断开后用自己的心跳替手机维持运动。
- 旧杆量**不排队补发**；只处理有效期内的最新一帧。
- 代理心跳失效 → 本地停车、旧租约作废；代理与适配器之间链路失效 → 适配器 `ttl` 到期自停（2.2）。
- 现有 0.6 s 只是**超时参数**；实际停止 = 检测 + 传输 + 机械制动，须真机测量。

### 3.6 设备认证（站点最小规格的一部分，不推给云端）

设备注册与凭证签发、MQTT mTLS、按 `robot_id` 的主题 ACL（一台狗只能发自己的主题、只能订自己的 cmd）、禁止冒充。证据上传凭证按狗、按时限签发。

## 4. 资源、抢占与断线策略

### 4.1 资源模型

代理内资源：`motion`、`light`、`sound`、`spotlight`、`head`、`camera`。任务声明占用哪些。
`standoff`（占 `motion`）与 `deter`（占 `light/sound`）**并行**；两个占 `motion` 的任务互斥。W00 定资源表；**不许所有命令共用一个任务槽**。

### 4.2 抢占

高优先级命令到达：**先取消旧动作并确认其资源已安全释放（停止确认），再让新任务拿 `motion`**；旧任务回 `preempted`。
**安全互锁不受任务优先级覆盖**：急停、电量底线、定位丢失策略高于一切命令。

### 4.3 断线策略（按当前任务）

| 当前任务 | 断站点后的默认行为 | 命令离线排队 |
|---|---|---|
| 普通巡逻 | 定位、电量、安全条件满足 → 按**已获批的离线策略**继续；否则停并等待 | `abort` 排队；`patrol` 新任务过期即弃 |
| 遥控 | 心跳失效即本地停车，租约作废 | 不排队 |
| 保持距离 / 跟随 | 目标可信且撤离条件成立才继续；否则停止或撤离到安全点 | 不排队 |
| 灯光 / 警笛 / 喊话 | 到最大持续时间自停；断线不会无限响 | 不排队 |
| 返航 / 回充 | 本地自主执行，受本地安全状态约束 | `abort` 排队 |

## 5. 存量模块去向

单仓多包：

```
packages/contract        schema + 生成的 Python/Dart 模型；唯一被两侧同时依赖
packages/robot-agent     跑在狗上
packages/adapter-d1max   RobotHAL 的 D1 Max 实现（sidecar + SDK）
packages/adapter-sim     RobotHAL 的假实现；代理与站点的测试全靠它
packages/site-node       现场节点
mobile/                  改连站点
```

云端控制面另起仓，不在本设计展开。

| 现有模块 | 去向 | 改动 |
|---|---|---|
| `engine/{mission,machine,safety,homing,preflight,form}` | robot-agent | **优先保留，经契约测试确认后调整**：抢占、并行资源、断线策略、重启恢复可能要改状态机 |
| `engine/{upload_queue,uploader,http_sink}` | robot-agent | 上传目标改站点对象存储；W01–W03 在此修 |
| `engine/{archive,retention,export}` | **拆责任** | 狗侧：断网缓冲、上传确认后回收。站点：正式归档、保留、导出、审计 |
| `engine/baselines` | **站点权威** | 判读已移到站点，历史基线不能只在狗上 |
| `engine/release.py`、`deploy/` | robot-agent | **要改**：阶段 0 的持久数据目录（W01）与拆包后的部署入口都涉及它们 |
| `backends/{base,local_nav,vendor_nav,sidecar_device}`、`motion/` | adapter-d1max + robot-agent | `base.py` 协议按第二节重做为 `RobotHAL`；`local_nav` 的规划/避障上移到代理，适配器只留速度与里程 |
| `engine/schedule.py` + 缺的执行器 | site-node | W06 在站点做 |
| `engine/alerts.py`、`app/alert_sources.py`、`app/watch.py` | site-node | 判定、聚合、升级链上提；数据源改为遥测/事件主题 |
| `engine/lease.py` | **两处** | 站点：授予与审计。代理：租约边界执行（3.5）。不是"全搬站点、狗不管租约" |
| `app/{auth,control,identity}` | site-node | PIN 换站点账号；**已认证用户必须绑定到命令、租约、审计事件**——不再信任请求体里的 `user`；`operator_verified` 由此解决，不是"自然消失" |
| `app/teleop.py`、`app/video.py` | 拆两半 | 杆量端点→代理；视频扇出与录像→站点 |
| `src/d1max_console/store.py` | site-node | 证据库起点 |
| `app/server.py`（约 4500 行） | **逐步退役** | 先包成可替换的适配层跑通新链路，再删。**不要拆包、改协议、改鉴权、换客户端同时进行** |
| `mobile/` | 改连站点 | 四屏保留；客户端换 base URL 与鉴权；名册从站点拉 |
| `d1max_sim/` | adapter-sim | 实现 `RobotHAL` |
| `tools/lio/` | **拆** | 离线建图与地图编辑→站点工具；运行时定位与传感器接入→代理 |
| `inspect/judge.py` | site-node | 判读在站点或云端，狗只拍 |

测试：`engine/` 测试跟着走；`server.py` 路由测试按新落点改写（主要损耗）；现有"Python 夹具先红、Dart 跟着红"机制搬到 `contract` 包。

## 6. W00 最小契约的范围

**不一次冻结全部未来能力。** W00 只覆盖：设备认证与注册、能力/状态两份报文、一个移动任务（`goto`）、`abort`、命令幂等与过期、`control_epoch`、事件序号、断线与重连对账、资源表与断线策略表的数据结构。其余任务类型在各自工单里加。

**验收流程（必须端到端通过才算 W00 完成）**：站点派 `goto` → 代理校验（认证、代次、过期、地图版本、前置条件）→ adapter-sim 执行 → 回执与进度 → 中途断线 → 命令重复投递 → 重连对账 → `abort` → 停止确认。

## 7. 对工单的影响与顺序

| 变化 | 说明 |
|---|---|
| W08 导航架构定案 | 已定：自建，在 robot-agent。销账 |
| W15 中央服务器补完 | 变为"建 site-node 包" |
| W06、W14、W16 | 合并为站点"派遣器"一个工单；抢占按 4.2 |
| W17、W20 | 站点账号体系是前置。§5.5"服务器不许有移动能力"改为"站点只发任务级指令、不发速度"（决策 5 由此有答案） |
| W21 上装 HAL | 变为"HAL 有类型执行器 + 代理能力合成" |
| W28 多机调度 | 派遣器的延伸 |
| 新增 W00 | 第六节 |
| 新增 W05b | 速度契约（2.2 的 `VelocityResult`）在 sidecar 上落地，和 W05 一起做 |

**顺序**：

1. **补本规格的契约决策**（即本稿第 2–4、6 节）。用户确认后改状态。
2. **阶段 0 止血**（W01–W05、W07）。特别是持久数据与版本目录分离。不依赖新平台。
3. **W00 最小契约 + adapter-sim**（第六节范围）。
4. **打通一条完整最小流程**（第六节验收）。
5. **尽早用 D1 Max 验证 HAL**：控制权、速度单位、死区、运动模式、停止语义。不等站点与手机全改完。
6. 再逐步迁移：派遣器、告警、录像、手机、其余功能。

**风险**：

1. 第 3–6 步期间无新功能可演示。工期**只有粗估**（数周量级），不作承诺；先用第四步证明分层成立，再估。
2. `server.py` 路由测试改写，测试总数先掉再涨。
3. 狗上网方案须在第 5 步前定。
4. D1 Max 在 SDK 0.2.0 下状态回调不触发（现场记录），第 5 步可能先要修它。

## 8. 不在本设计内

- 云端控制面内部设计。
- 各任务的算法（导航、保持距离、受力检测）——在代理内，接口由第 2–4 节固定。
- 上装硬件选型。
