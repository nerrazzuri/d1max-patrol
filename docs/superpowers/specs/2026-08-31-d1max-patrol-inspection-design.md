# D1 Max 巡检系统设计

日期:2026-08-31
状态:已评审,待转实施计划

---

## 1. 背景与目标

### 1.1 要做什么

为智元四足机器人 D1 Max 开发一套**自主巡检系统**:机器狗按预设路线巡航,在每个巡检点执行定点拍照,产出结构化的巡检归档与报告。

第一版范围**不含上装**,只使用机器狗自带能力:前/后相机、补光灯、云台、本体遥测、厂商自主导航。

### 1.2 现状与约束

**手上的资料**(位于 `D:\Projects\D1 Max`,均为压缩包):

| 资料 | 内容 |
|---|---|
| `3.自主导航二开资料.zip` | RobotSDK-0.2.0、**自主导航 WEBSOCKET API 文档**、URDF、SDK 指南(20260729)、产品说明书/规格书 |
| `AgibotSdk-Max-v0.1.1.zip` | RobotSDK-0.1.1(旧版) |
| `智元四足机器人D1 Max Urdf文件.zip` | URDF + meshes |
| `智元四足机器人D1 Max SDK开发指南V0.1.0_20260721.pdf` | SDK 开发指南(旧版) |
| `D1-Pro-Edu外观模型_v2.0_202509.stp` | 三维模型 |

**硬约束**:

1. **没有真机**。开发期无法联机验证任何接口。
2. SDK 只有 C++ 头文件 + 预编译 `.so`(x86_64 / aarch64),无官方 Python 绑定。
3. 导航能力由厂商 WebSocket JSON 接口提供,不开放内部算法。
4. 开发在 Windows PC 上进行,目标运行环境是 Ubuntu 22.04(先 PC,后续搬板载 arm64)。

### 1.3 已确定的路线选择

| 决策项 | 选择 | 理由 |
|---|---|---|
| 导航方案 | **混合**:先用厂商自主导航跑通,预留 Nav2 替换 | 厂商已实现四足 SLAM/定位/避障/回充,自建 Nav2 工作量数倍且需先验证能否与狗上原生导航共存 |
| 部署位置 | 先 PC 开发,代码按可搬到 arm64 板载的方式写 | 开发调试效率优先,不牺牲最终部署目标 |
| 巡检内容 | 定点拍照 + 结构化归档 | 闭环完整、不依赖算法效果,后续加识别容易 |
| 技术栈 | Python 主体 + C++ SDK 旁路进程 | 业务逻辑迭代快、易测;SDK 隔离在薄进程内 |
| 开发方式 | 契约先行 + 全仿真开发 | 无真机时唯一能保证质量的做法;模拟器同时是接口边界的可执行定义 |
| 路线执行 | **全逐点 `start_nav`** | 多点导航接口无到点事件,做不了定点动作(详见 §4.2) |
| 仓库布局 | `refs\`(只读资料)+ `d1max-patrol\`(git 仓库) | 250MB 资料不进 git |

---

## 2. 关键事实(来自官方资料)

### 2.1 自主导航 WebSocket 接口

地址 `192.168.144.100:10010`。消息结构:

```json
{
  "head": {"type": "app_req", "time_stamp": 1234567890123, "source": "app", "frame_count": 1},
  "data": {"req_func": {"<function_name>": <args>}}
}
```

响应 `data.req_result` 含 `req_func` / `status`(ok|error)/ `msg` / `data`。

**能力清单**:

- **建图**:`start_mapping` / `stop_mapping` / `get_mapping_status`;`get_pgm_map` / `get_all_pgm_map` / 删除 / 重命名
- **路径管理**:`get_all_paths_by_mapid` / `add_nav_path` / 修改 / 删除。点位格式为 `[point_name, {position:{x,y,z}, orientation:{x,y,z,w}}]`
- **导航控制**:`start_nav`(单点)/ `start_multi_nav`(按 path_id)/ `start_multi_nav_by_points` / `start_nav_return_home` / `stop_nav` / `pause_nav` / `continue_nav` / `set_navigation_speed`
- **定位**:加载定位地图 / `reset_loc` / `get_loc_status`
- **回充 ARC**:`stop_arc` / `start_arc_align_coarse` / `get_arc_alg_status` / 请求出桩(文档注明中狗仅支持 7.3、7.4、7.12、7.14)
- **故障推送**:`alg_error_code_notify`,含 `code` / `description` / `severity`,已知 13330 navigation blocked、13331 lidar disconnected

**状态枚举**:

- 导航:`StandBy`(仅此状态可启动导航)/ `Initializing` / `Active` / `Pause` / `Cancelled` / `Succeed` / `Failed`
- 定位:`Init` / `MapLoading` / `InitLocalization` / `ContinuousLoc` / `Error` / `DynamicInitLoc` / `LocLost`

**文档注意事项**:`frame_count` 用于匹配请求响应;消息可能不按顺序到达;连接断开时进行中的操作可能失败。

### 2.2 RobotSDK 0.2.0

地址 `192.168.234.1:8081`,默认传输层 UDP。

**控制接口**(节选):`StandUp` / `LieDown` / `BalanceStandUp` / `Move(left_right, forward_back, yaw)` / `PosMove` / `ControlHead` / `HighLowStance` / `FrontLight` / `BackLight` / `SetSpeed` / `TakePhoto` / `TakeControl` / `ReleaseControl` / `SoftEmergencyStop` / `StartRechargeTask` / `StopRechargeTask` / `StartUnDockTask` / `SetPeriphPower`。

**数据回调** `IDataCallback`:`OnImuData` / `OnMcData`(50Hz)/ `OnSpeedData` / `OnJointStateData` / `OnRobotStateData`(1Hz)/ `OnFaultData` / `OnLuxData` / `OnTaskStateData` / **`OnControlLost`** / **`OnControlAvailable`**。

头文件明确要求:**回调必须轻量,不得在回调内做文件 I/O、网络传输等耗时操作**。

**对本设计重要的类型**:

- `RobotState.machine_status`:`IDLE / REMOTE / OTA / RECHARGE / MAPPING / NAVIGATION / SAFETY / SELFTEST / SOFT_SHUTDOWN / SILENCE / FOLLOW / TRACK / UNDOCK` —— SDK 链路可独立观察导航状态
- `RobotState.control_source`:`APP / SDK / OTHER` —— 控制权归属可观测
- `RobotState.battery`:两块电池的百分比、电压、温度、电流、充放电状态
- `TaskStateInfo`:`task_type`(含 `NAV` / `MAPPING` / `RECHARGING` / `UNDOCK`)+ `task_status`(`STARTING / RUNNING / SUCCESS / FAILURE / STOPPED`)
- `MotionData`:四元数、**世界系位置**、世界系/本体系速度与角速度、纳秒时间戳
- `FaultData`:`FaultCode` + `FaultLevel`(`FatalError / Error / Warn`)
- `TakePhotoCmd{task_id, device_id}` → `TakePhotoAck{task_id, device_id, error_code, reason}`

**`Move()` 行为**(据 SDK 指南):单次命令约持续 1 秒,需重复发送,推荐 50Hz。速度映射:前后 `[-1,1] → [-1.0,1.0] m/s`,左右 `[-1,1] → [-0.5,0.5] m/s`,偏航 `[-1,1] → [-1.5,1.5] rad/s`。

### 2.3 待真机验证的未知点

以下事项文档未说明或存在疑点,**设计已针对每一项给出不依赖其答案的方案**,由一致性验证套件(§7)在真机到货后确认:

| # | 未知点 | 设计上的规避 |
|---|---|---|
| U1 | 导航 WS 在 `192.168.144.100`、SDK 在 `192.168.234.1`,两网段实际如何互通、PC 能否同时连接 | 两个端点均由配置文件驱动,不硬编码;任一链路不可用时降级运行并明确报错 |
| U2 | `TakePhotoAck` 只返回 `error_code`,未说明照片存储位置与取回方式 | 取图主路径走 RTSP 抽帧,`TakePhoto` 作为备选实现 |
| U3 | 厂商导航运行时,SDK 侧是否需要 / 能否 `TakeControl`,取控是否会打断导航 | 默认不主动取控;取控行为由配置开关控制,默认关闭 |
| U4 | `get_nav_status` 是否有推送,还是只能轮询 | 按轮询实现(3Hz),推送若存在则作为加速手段接入,不改变正确性 |
| U5 | `frame_count` 在真机响应中是否可靠回填 | 两级匹配降级策略(§4.1),两种情况都能工作 |
| U6 | SLAM 地图坐标系与 SDK `MotionData` 世界坐标系之间的变换 | 设计不依赖该变换(§4.2) |

---

## 3. 整体架构

### 3.1 进程视图

| 进程 | 语言 | 职责 | 存在周期 |
|---|---|---|---|
| `patrol-core` | Python / asyncio | 任务引擎、导航客户端、巡检动作、归档、CLI | 永久 |
| `d1max-sdk-bridge` | C++ | 链接 `librobot_sdk`,将 SDK 转换为本地 JSON-line IPC | 永久 |
| `d1max-sim` | Python | 假导航 WS 服务 + 假 SDK 桥,含运动学仿真与故障注入 | 仅开发/测试 |

`patrol-core` 不直接接触 C++;`d1max-sdk-bridge` 不包含业务逻辑。SDK 升级只影响 bridge;导航替换为 Nav2 只影响导航后端实现。

### 3.2 可替换接口(Port)

```
NavBackend       导航能力       → VendorNavBackend | Nav2Backend(将来) | SimNavBackend(测试)
DeviceBackend    本体动作与遥测  → SdkBridgeBackend | SimDeviceBackend
MediaSource      取图           → RtspFrameGrabber(主) | SdkPhotoSource(备) | FakeImageSource(测试)
```

这三个接口是"混合路线"的落点。任务引擎只依赖接口,不依赖实现。针对接口编写的**契约测试**在引入 `Nav2Backend` 时可原封不动复用。

### 3.3 数据流

```
mission.yaml
    │
    ▼
MissionRunner (显式状态机, 状态持久化)
    ├──▶ NavBackend      逐点导航 / 暂停 / 取消 / 状态轮询
    ├──▶ DeviceBackend   姿态、灯、云台、拍照指令 + 遥测流(电量/故障/控制权)
    └──▶ MediaSource     到点取图
              │
        RunRecorder ──▶ runs/<mission>/<ts>/{manifest.json, events.jsonl, telemetry.jsonl, photos/}
              │
        ReportBuilder ──▶ report.md
```

### 3.4 真理源规则

导航进展**以导航 WebSocket 为主真理源**。SDK 侧的 `machine_status` / `TaskStateInfo` / `FaultDatas` / `OnControlLost` 作为**交叉校验与安全兜底**。

两边状态不一致时,任务引擎**停车并记录异常**,不做猜测性推断。此规则防止双真理源退化为不可预测行为。

---

## 4. 导航链路设计

### 4.1 `VendorNavBackend`

asyncio WebSocket 客户端。

**请求响应匹配**:维护 `frame_count → Future` 表,带超时。匹配采用两级降级:

1. 优先按响应中的 `frame_count` 匹配
2. 匹配失败时,退回按 `data.req_result.req_func` 名匹配**最早的未完成同名请求**,并记录 warning

降级策略应对 U5。真机行为经一致性验证确认后,可将实际生效的一级固化为默认,另一级保留为兜底。

**推送处理**:无对应请求的消息(`notify_stop_mapping_status`、`alg_error_code_notify`)直接投递到事件总线。

**重连**:指数退避重连。**重连后不盲目续跑**——先查询 `get_nav_status` 与 `get_loc_status` 对齐真实状态,再由状态机决定继续或中止。

**轮询频率**:`get_nav_status` 默认 3Hz,`get_loc_status` 默认 1Hz,均可配置。

### 4.2 关键决策:全逐点 `start_nav`

**决策**:路线执行由 `MissionRunner` 自行逐点驱动,每次只发一个 `start_nav`。

**理由一:多点导航接口没有到点事件。** `start_multi_nav` 启动后,`get_nav_status` 只反映整条任务的总体状态(`Active` / `Succeed` / `Failed`),协议未提供"已到达第 N 点"的通知。因此无法在指定点位执行拍照等动作。

**理由二:无法通过位姿自行判断到点。** 判断需要**地图坐标系**下的当前位姿,但:

- SDK 的 `MotionData.position` 是本体里程计世界系,不是 SLAM 地图系
- 导航 WS 协议未暴露"当前地图位姿"接口
- 两坐标系之间的变换未知(U6)

**执行模型**:

```
for wp in route:
    start_nav(wp.pose)
    等待 nav_status: Active → Succeed        # 带 per-waypoint 超时
    执行 wp.actions
```

**`start_multi_nav` 的保留用途**:通过 `get_all_paths_by_mapid` **读取现场人员在厂商 App 中绘制的路径**,转换为本系统的 waypoint 列表后逐点执行。现场沿用熟悉的 App 画线流程,本系统负责精细的定点动作,两者不冲突。

**未来优化(不在第一版范围)**:路线中**没有动作的点**天然是通过点,可将连续的通过点合并为一次 `start_multi_nav` 以保留厂商的连续通行平滑。此优化不需要修改路线数据格式,待真机上确认逐点起停确实影响效率后再实施。

---

## 5. SDK 链路设计

### 5.1 `d1max-sdk-bridge`(C++)

单一职责进程:协议转换 + 抽样 + 看门狗。**不包含任何业务判断。**

**IPC**:localhost TCP(默认 `127.0.0.1:8790`),换行分隔 JSON。选择 TCP 而非 Unix domain socket,以便模拟器在 Windows 开发机上同样可运行。

```
上行 bridge→core:
  {"t":"telemetry","kind":"robot_state|imu|mc|speed|joint|fault|task_state|control|lux","ts":…,"data":{…}}
下行 core→bridge:
  {"t":"cmd","id":7,"name":"stand_up","args":{}}
  {"t":"ack","id":7,"ok":true,"error":null}
```

### 5.2 三条设计规则

**规则 1:50Hz `Move()` 重发在 bridge 内实现。**

`patrol-core` 只发送一次 `set_velocity{left_right, forward_back, yaw, ttl_ms}`;bridge 内部以 50Hz 持续调用 `Move()`,并在下列任一条件下**立即将速度归零**:

- `ttl_ms` 到期
- IPC 连接断开
- 进程收到终止信号

速度看门狗必须位于离机器最近的一层。Python 侧的 GC 停顿或事件循环阻塞不应导致机器继续运动。

**规则 2:遥测在 bridge 侧抽样。**

`OnMcData` 为 50Hz,全量转发会淹没 IPC 与归档,默认降采样至 10Hz(可配)。但**故障(`OnFaultData`)、控制权变更(`OnControlLost`/`OnControlAvailable`)、任务状态(`OnTaskStateData`)三类事件不抽样,立即转发**——它们是安全事件。

**规则 3:回调内只做拷贝入队。**

SDK 头文件明确要求回调轻量。回调仅将数据拷贝进队列,由独立线程执行 IPC 写出。

### 5.2.1 bridge 的可测性安排

bridge 对 `librobot_sdk` 的依赖收敛到一个薄接口层。测试时链接一个**桩实现**(stub)替代真实 `.so`:桩可按脚本产生回调数据、记录收到的 `Move()` 调用序列与时间戳。

因此以下逻辑在无真机时**可以**被测试:速度看门狗的三种归零条件、遥测抽样比例、事件类不被抽样、IPC 协议编解码、回调不阻塞。

无法被测试的仅剩:真实 SDK 的调用姿势是否正确(连接参数、配置开关的启用顺序、回调注册时机)。这是 §10.1 所指的残余风险。

### 5.3 控制权策略

bridge 提供 `take_control` / `release_control`,但**默认不主动调用**。是否在任务开始时取控由配置决定,默认关闭(U3)。

`OnControlLost` 立即上报,任务引擎按 policy 处置(默认 PAUSED)。**不做自动重试抢控**——控制权丢失通常意味着现场有人正在用 App 操作机器,抢控会造成危险的争用。

---

## 6. 任务引擎与数据模型

### 6.1 任务定义格式

```yaml
mission: substation_night_patrol
map_id: map_20260901_1

route:
  source: vendor_path        # vendor_path | inline
  path_id: path_a

waypoints:                   # 从 vendor_path 拉取后固化的快照
  - name: P1_transformer
    pose:
      position: {x: 1.2, y: 3.4, z: 0.0}
      orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}
    actions:
      - {type: dwell, seconds: 2}
      - {type: photo, camera: front}
      - {type: photo, camera: back}

policy:
  waypoint_timeout_s: 120
  on_waypoint_failed: retry_then_skip   # abort | skip | retry_then_skip
  waypoint_retry: 1
  battery_return_pct: 25
  battery_abort_pct: 15
  on_loc_lost: pause_then_abort
  on_control_lost: pause
  loops: 1
```

**路线固化为快照而非每次现拉**:路线是巡检结果的组成部分。若他人在 App 中修改了路径,历史报告不应随之改变。

**支持的动作类型(第一版)**:`dwell`(停留)、`photo`(拍照)、`light`(补光灯)、`head`(云台)。

### 6.2 状态机

```
IDLE → PREFLIGHT → LOCALIZING → RUNNING ──▶ RETURNING → DONE
                                   │ ↑
                     NAV_TO_WP ────┘ └──── AT_WP_ACTION
                                   │
                         PAUSED ───┴──▶ ABORTING → ABORTED
```

**PREFLIGHT 检查项**(不通过不启动):

1. 导航 WS 连通,且 `nav_status == StandBy`
2. SDK bridge 连通,无 `FatalError` 级故障,软/硬急停均未触发
3. 定位地图已加载,`loc_status == ContinuousLoc`
4. 电量高于 `battery_return_pct` 加余量
5. 归档目录可写且剩余空间充足

### 6.3 事件驱动与持久化

单一 asyncio 事件循环。所有输入(导航轮询结果、导航推送、SDK 遥测、用户命令)进入**同一个队列**,状态机是唯一消费者。状态不被并发修改,因此行为完全可重放——测试中注入事件序列即可断言状态迁移。

每次状态迁移写入 `events.jsonl`,并原子更新 `state.json`。崩溃或断电重启后可读出"停在哪个点、因何停止"。

**第一版只支持人工 resume,不做自动续跑。** 无真机时"自动恢复后继续行走"的失败模式无法验证,而该类缺陷的代价是物理损坏。

### 6.4 安全与降级规则表

| 事件 | 默认处置 |
|---|---|
| `loc_status → LocLost` | 立即 `stop_nav` → PAUSED → 尝试 `reset_loc`,N 次失败 → ABORT |
| 故障码 13330 navigation blocked | 记录并等待 T 秒;持续则当前点判失败,按 `on_waypoint_failed` 处置 |
| 故障码 13331 lidar disconnected | 立即 ABORT |
| SDK `FaultLevel::FatalError` | 立即 `stop_nav` + ABORT |
| `OnControlLost` | 按 policy,默认 PAUSED |
| 电量 < `battery_return_pct` | 完成当前点动作后转 RETURNING |
| 电量 < `battery_abort_pct` | 立即 `stop_nav`,原地 ABORT 并告警 |
| WS 断连超过阈值 | PAUSED;重连后先对齐状态再决定 |
| 导航状态与 SDK `machine_status` 矛盾 | 停车,记录异常(§3.4) |

**ABORT 的动作是"停止导航 + 记录 + 告警",不包含任何自动位移。** 不自动返航、不自动趴下。理由同 §6.3。返航仅在电量策略触发且系统状态健康时,通过 `start_nav_return_home` 执行。

### 6.5 归档结构

```
runs/<mission>/<UTC-timestamp>/
  manifest.json    任务定义快照 + 环境指纹(SDK 版本 / 协议版本 / 地图 ID) + 结果汇总
  events.jsonl     状态迁移、导航请求响应、故障、决策依据
  telemetry.jsonl  抽样遥测(位姿、速度、电量、关节温度)
  state.json       当前状态(崩溃恢复用)
  photos/P1_transformer__front__20260901T101500Z.jpg
  report.md
```

照片文件名包含**点位名**,因此"同一点位跨日期的照片"可天然聚合比对。`manifest.json` 保存任务定义快照与环境指纹,确保历史报告可追溯到当时的路线版本与软件版本。

### 6.6 报告

- 点位级表格:到达时间、耗时、动作结果、缩略图
- 任务级汇总:总时长、成功/失败点数、电量曲线、异常时间线
- 输出 Markdown,以及一份图片内嵌的自包含 HTML

---

## 7. 模拟器与测试策略

### 7.1 `d1max-sim`

**`sim-nav`(假导航服务)**:实现与文档完全一致的 WebSocket JSON 协议。内部含 2D 运动学模型——收到 `start_nav` 后按配置速度朝目标插值移动,进入到达阈值后 `nav_status` 转 `Succeed`。完整实现导航状态机、定位状态机、建图状态机,以及地图与路径的增删改查(PGM 使用合成占用栅格)。

**`sim-bridge`(假 SDK 桥)**:说**与真 bridge 完全相同的 IPC 协议**。这是硬性约束——`patrol-core` 必须无法区分真假,否则模拟器失去意义。发送遥测(电量按配置速率下降、位姿、故障),`take_photo` 生成合成图。

**故障注入通道**:模拟器专有的旁路控制口,不属于机器人协议。支持注入:

```
loc_lost            alg_error <code>      control_lost
fault fatal         battery <pct>         disconnect <seconds>
frame_count_zero    reorder               nav_fail <waypoint>
slow <factor>       stuck
```

其中 `frame_count_zero` 与 `reorder` 专门用于验证 §4.1 的两级匹配降级策略——**无真机也能验证"文档不可信"这一情形**。

### 7.2 测试五层

| 层 | 测什么 | 依赖 |
|---|---|---|
| 1. 协议编解码 | 请求构造、响应解析、推送识别 | 无 |
| 2. 后端契约测试 | 同一套测试跑在多个 `NavBackend` 实现上 | sim |
| 3. 状态机 | 事件序列 → 状态迁移与产生的命令 | 假时钟 |
| 4. 端到端 | core + sim-nav + sim-bridge 跑完整任务,断言归档产物 | sim |
| 5. 真机一致性 | 文档描述 vs 真机实际 | **真机** |

**第 1 层的测试数据白捡**:WebSocket API 文档为每个接口给出了完整的请求/响应 JSON 样例,直接作为 golden fixture。

**第 2 层是混合路线的保险**:契约测试针对 `NavBackend` 接口编写,不针对实现。引入 `Nav2Backend` 时同一套测试直接套用——通过则任务引擎无需改动。**此测试套件必须在第一天建立**,不是事后补充。

**第 3 层**:§6.4 安全规则表**每一行对应一条测试**。

### 7.3 真机一致性验证套件 `conformance`

分阶段推进的可执行清单,真机到货当天执行:

| 阶段 | 内容 | 风险 |
|---|---|---|
| 0 | 网络:两网段可达性、端口、能否同时连接 | 无 |
| 1 | 导航只读接口全量调用,**录制原始报文** | 无 |
| 2 | SDK 只读:订阅全部遥测,记录字段实际取值与频率 | 无 |
| 3 | 低风险写操作:灯、姿态、`TakePhoto`、控制权切换并观察 `CtrlSource` | 低(机器架起或空旷场地) |
| 4 | 小范围建图,取回 PGM | 低 |
| 5 | 单点导航短距离:`nav_status` 时序、到点精度、有无推送 | 中 |
| 6 | 交叉校验:导航运行时 SDK 的 `machine_status` / `TaskStateInfo` 取值 | 中 |

**产物**:

1. `conformance-report.md` —— 文档 schema 与真机报文的逐字段 diff
2. 原始报文录像 `.jsonl`

录像是核心产出而非副产品:**将真机录像回灌进 sim,使模拟器行为向真机对齐**。真机联机时间稀缺,录制一次可离线复用长期;固件升级后重跑套件即可发现厂商的改动。

阶段 1、2 完成即可回答 U1、U2、U4 中的大部分,阶段 3 回答 U3,阶段 5 回答 U5。

---

## 8. 目录结构

```
D:\Projects\D1 Max\
├── refs\                        # 官方资料解压后的只读参考(不进 git)
│   ├── RobotSDK-0.2.0\
│   ├── nav_ws_api.md
│   ├── max_description\
│   └── pdf\
│
└── d1max-patrol\                # git 仓库根
    ├── pyproject.toml
    ├── src\d1max_patrol\
    │   ├── config\              # 端点、策略默认值、加载与校验
    │   ├── protocol\            # nav WS 报文 + bridge IPC 报文 编解码
    │   ├── backends\
    │   │   ├── base.py          # NavBackend / DeviceBackend / MediaSource
    │   │   ├── vendor_nav.py
    │   │   ├── sdk_bridge.py
    │   │   └── media_rtsp.py
    │   ├── mission\
    │   │   ├── model.py
    │   │   ├── runner.py
    │   │   ├── policy.py
    │   │   └── actions.py
    │   ├── record\
    │   │   ├── recorder.py
    │   │   └── report.py
    │   └── cli.py
    ├── src\d1max_sim\
    │   ├── nav_server.py
    │   ├── bridge_server.py
    │   ├── kinematics.py
    │   └── inject.py
    ├── bridge\                  # C++ 旁路进程(CMake,链接 refs 中的 SDK)
    ├── tests\
    │   ├── protocol\  contract\  mission\  e2e\  sim\
    ├── conformance\
    ├── missions\
    ├── runs\                    # gitignore
    └── docs\superpowers\specs\
```

---

## 9. 交付节奏与验收标准

| 阶段 | 内容 | 验收标准 | 需真机 |
|---|---|---|---|
| **0 地基** | 解压归档官方资料、建仓、pytest、配置模型 | 测试套件可运行;配置加载与错误校验有测试覆盖 | 否 |
| **1 导航协议 + sim-nav** | 全部 `req_func` 编解码;sim-nav 四套状态机 + 故障注入 | 协议单测全绿(文档样例作 fixture);CLI 可对 sim 建图、存路径、单点导航、查状态 | 否 |
| **2 NavBackend + 契约测试** | 三个 Port 定义;`VendorNavBackend`;契约测试套件 | 契约测试对 sim 全绿;断连 / 乱序 / `frame_count=0` 三个降级场景通过 | 否 |
| **3 SDK 桥 + sim-bridge** | IPC 协议;C++ bridge;sim-bridge;`SdkBridgeBackend` | 遥测流与命令 ack 有测试;**IPC 断开后 bridge 立即速度归零**有测试 | 否 |
| **4 任务引擎** | 数据模型、状态机、安全规则表、动作 | 安全规则表每行一条测试全绿;端到端跑通 5 点路线 | 否 |
| **5 归档与报告** | recorder、report、MediaSource | 端到端产出完整 run 目录与报告;RTSP 抽帧对本地 ffmpeg 推流实测通过一次 | 否 |
| **6 一致性套件** | 分阶段脚本、报文录制、schema diff | 对 sim 可跑完整流程并生成报告(证明套件自身无缺陷) | 否 |
| **7 真机 bring-up** | 执行 conformance → 修正差异 → 录像回灌对齐 sim → 首次真实巡检 | conformance 报告无阻断项;完成一次完整巡检并产出报告 | **是** |

阶段 0–6 全部不需要真机;阶段 7 在真机到货后立即可开始,前六阶段的产物即其作业指导书。

---

## 10. 已知风险

### 10.1 C++ bridge 无法在无真机时验证

`librobot_sdk` 是二进制库,无真机即无对端。这是全项目**唯一**"写完只能等真机验证"的部分。

**缓解**:两层。其一,刻意将 bridge 做薄——只做协议转换、抽样、看门狗,不含业务判断;业务逻辑全部位于 Python 侧,而 Python 侧对 sim-bridge 完全可测。其二,bridge 自身通过链接 SDK 桩实现进行单元测试(§5.2.1)。

经这两层缓解后,残余风险仅为"真实 SDK 的调用姿势不对"——连接参数、配置开关启用顺序、回调注册时机之类。该类问题在真机上定位迅速。

### 10.2 厂商导航行为与文档存在差异

**缓解**:一致性验证套件(§7.3)的全部意义即在于此;`frame_count` 两级降级(§4.1)是对文档不确定性的具体防御。

### 10.3 逐点导航的起停顿挫

全逐点执行会在每个点完全停稳,可能造成顿挫并延长总时长。

**缓解**:已在 §4.2 给出优化路径(合并连续通过点),不需要改变数据格式,待真机验证后按需实施。

### 10.4 两网段互通性

若 PC 无法同时连接 `192.168.144.100` 与 `192.168.234.1`,系统需分链路降级运行。

**缓解**:两端点均配置驱动;任一链路不可用时,PREFLIGHT 明确报错并说明缺失能力,而非静默地半能力运行。

---

## 11. 第一版明确不做

- 视觉识别(仪表读数、指示灯判读、异常检测)
- 上装载荷及其传感器
- 自建 Nav2 / SLAM Toolbox / robot_localization
- 崩溃后自动续跑
- ABORT 时的自动位移(自动返航、自动趴下)
- Web 界面(第一版 CLI 即可)
- 多机调度
