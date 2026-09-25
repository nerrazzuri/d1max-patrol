# W00d · adapter-d1max（真狗的 RobotHAL）—— 设计稿

日期：2026-09-25 · 工单：W00d（M，依赖 W00b；工单原文「控制权、速度单位与死区、运动模式、停止语义；尽早」）
状态：**按推荐（用户 2026-09-25 授权连续推进：「把整个 W00 跑完再给外审，都按你推荐的来」）**。四个决定都取推荐项。**真机上的验证不在本次执行**：狗关着，动机器的测试要用户在场（用户规矩）。

## 0. 事实

- 真狗的运动走**常驻旁路进程** `motion/patrol_agent.cpp`（握着厂商 SDK 会话），Python 经 TCP 行协议（`protocol/agent_frames.py`，现为 v2）接上去。**SDK 控制权独占：一旦释放，上装立刻收回，只能重启 RK3588 再抢**（清单 #46/#47）。
- 旁路进程现有命令：`stand`、`lie`、`walk`（定时行走：做完才回执，先 `Gait(2000)` 起步 0.5 s，末尾连发零速）、`halt`、`estop`、`light`。**没有带有效期的持续速度** —— 而 HAL 的 `set_velocity(VelocityCommand(ttl))` 与代理的速度环（`HalNavBackend`，每拍一条）要的正是它。这也是工单 W05b「速度契约在 sidecar 上落地」。
- SDK 有 `Move(左右, 前后, 转向)`，参数是**比例值**（旧 W05 已记：单位不是 m/s）；`SetSpeed(1/2/3)` 选档（1/2/3 m/s 档，代码从没调过）。**比例值到 m/s 的系数、死区、停止时间都没实测过**。
- 厂商 SDK（含 x86_64 库）在 `refs/robot-sdk/` 里：开发机能**编译**旁路进程，跑不了（要真狗）。仿真旁路 `d1max_sim/agent_server.py` 实现同一个协议，测试都靠它。

## 1. 决定一：持续速度怎么走

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 旁路进程加 **`vel`** 命令（协议 v3）：`{"cmd":"vel","fwd","lat","yaw","ttl_ms"}`，**立刻回执**；一条速度线程在有效期内按 50 ms 一拍发 `Move`，到期连发零速自停；新的 `vel` 覆盖旧的并续期；`halt`/`estop` 插队作废；从静止开始时先 `Gait` 一次。代理的速度环（10 Hz、ttl 300 ms）每拍续一次 |
| B | 用 `walk(seconds=ttl)` 顶。它做完才回执、每次 0.5 s 起步，速度环跑不起来 |

## 2. 决定二：控制权

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 适配器能力里 `control_releasable = false`；`release_control()` 抛 `HalUnsupported`（**绝不真放**）；`acquire_control()` 只确认旁路进程握着（它开机时抢）。控制权丢了（旁路进程报 `control_lost`）→ HAL 的状态如实反映，代理按已有规则处理 |
| B | 照 HAL 接口真放真取。放一次就得重启整机 |

## 3. 决定三：速度单位与死区

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 配置参数 `mps_per_unit`（`Move` 比例 1.0 对应的 m/s，**默认 0.4**，取旧记录「低档有效约 0.42 m/s」的保守值）、`deadband_mps`（默认 0.05）、`max_fraction`（默认 0.5，跟 `walk` 的上限一致）。`set_velocity` 把 m/s 换成比例、夹上限、低于死区拒；`VelocityResult` 如实报夹没夹、拒没拒。**三个数都进真机待测**，实测后改配置 |
| B | 现在就写死一个系数。没实测过的数写死，比显式标「待测」更糟 |

## 4. 决定四：真机验证交付什么

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 两件工具，**本次都不执行**：`tools/w00d_probe.py`（**不动机器**：连旁路进程、握手版本、控制权、电量、急停、里程计是否在刷）；`tools/w00d_motion_check.py`（**要人在场**：必须带 `--i-am-present` 才跑；原地慢转、前进 0.3 m/s 两秒、停止用时、`ttl` 到期自停、低于死区不动；结果写进 `runs/field-logs/`）。两件都写进 `docs/庄园场景待真机测试.md` |
| B | 只写适配器，验证留到以后再想 |

## 5. 形状

- 协议：`agent_frames.PROTO_VERSION = 3`（加 `vel`）；`patrol_agent.cpp` 同步到 3，加 `DoVel` 与速度线程；`d1max_sim/agent_server.py` 实现 `vel`（仿真里按比例 × 系数积分里程）。
- 新包 `packages/adapter-d1max`（`d1max_adapter_d1max`）：`D1MaxHal` 实现 `RobotHAL`，经 TCP 接旁路进程；过渡性依赖根包的 `protocol.agent_frames`（同 robot-agent）。
- 代理入口 `d1max-agent --hal d1max --sidecar 127.0.0.1:8090 [--mps-per-unit … --deadband …]`。
- 测试：用仿真旁路跑 `D1MaxHal` 的全部原语；C++ 在开发机上**编译通过**作为门槛（`make patrol_agent`）。

## 6. 验收（开发机）

- 仿真旁路上：代理 `--hal d1max` 跑通一趟 `goto`；`ttl` 到期仿真狗自停；`halt` 插队；`release_control` 抛不支持；低于死区的速度被拒。
- `make patrol_agent` 编译通过；协议版本两边一致（有测试读 C++ 源码里的常量）。
- 真机项全部进待测文档，**本次不执行**。
