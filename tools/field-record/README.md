# D1 Max 现场录包 —— 两个包(标定 + 覆盖)

目标两个 bag:
1. **标定包(calibration)** —— 解决 IMU-雷达外参弱可观。**我用 SDK 脚本控制**做多轴激励动作。
2. **整层室内地图(coverage)** —— 至少把室内扫清楚。**你用遥控器**慢速走遍全场。

两个包都用 `record_bag.sh` 录(**含 `/front_lidar/imu`** —— app 自带录包漏了 IMU,标定包必须自己录)。

---

## 关键事实(已从代码/现场定死)
- **运动接口** = `walk(时长, forward, lateral, yaw)`,开环脉冲,控制量是**百分比**,**死区 ~0.3**(0.3~0.5 才真走,<0.3 静默不动)。yaw 死区 `MIN_YAW=0.30`。
- **没有身体 pitch/roll 命令**。只有 `stand()` / `lie()`(整体起立/趴下)和 `set_gimbal`(云台/头,不是身体)。
- **控制走 app HTTP API**:`/api/control/acquire`(抢租约)→ `/api/teleop`(发脉冲,每拍要 `/api/teleop/heartbeat`,>0.6s 不发就停车)→ `/api/control/release`。
- **stand/lie 没有 HTTP 路由** → 要用它做 pitch 激励,得现场直连 backend 或加一条路由(现场定)。
- app 自带录包话题 = `/front_lidar,/tf,/tf_static,/odom/*`,**无 IMU** → 用本目录 `record_bag.sh` 代替。

---

## 标定包:SDK 动作序列(我控制)
做不出 pitch/roll 身姿,用 **stand↔lie 循环**补 pitch+竖直 Z;roll 最弱(靠侧移+步态),接受。
序列(总 ~5 分钟,原地/小范围,你在旁边守急停):
1. 静止 10s
2. yaw 左/右各 90°,×3
3. yaw 左/右各 180°,×2
4. 缓慢 360° 正反各一次
5. 前后走 3–5m,明显加速/减速
6. 走 8 字 ×2–3
7. **stand→lie→stand ×3–4**(补 pitch + Z 激励;若 stand/lie 打通)
8. 左右侧移(lateral)来回 ×几次(补一点 roll + Y)
9. 再走 8 字 ×1
10. 静止 10s

> 关键:**别只做 yaw 转圈**——之前弱可观就是因为运动几乎全在水平面。多轴角运动 + 线加速度一起才把各自由度激活。

## 覆盖包:操作卡(你用遥控器)
- 目标:**走遍整个室内**每条走廊/每个区域,不漏。
- **慢、平顺、连续**(LiDAR 里程计喜欢帧间大重叠);别快走、别急转、别原地猛转。
- **回到起点**(方便看漂移/以后回环)。
- 玻璃/大开阔区:贴着有结构的一侧走(让墙/柜/柱在 ~10m 内),别走大空场正中间。
- 时长看楼层大小,一般 5–15 分钟;电量不够就分段录多个包。

---

## 现场 preflight 清单(到办公室、连上狗后逐项过)
1. [ ] 狗开机;`record_bag.sh` 的机器能 `ros2 topic echo /front_lidar --once` 收到点云
2. [ ] **`/front_lidar/imu` 也收得到**(标定包命根子)
3. [ ] 磁盘 >5GB(笔记本 262G,够)
4. [ ] app 在跑、能登录、`POST /api/control/acquire` 抢到租约(标定用)
5. [ ] `walk` 控制量/死区实测:先 `forward=0.35` 一小拍,确认真走且方向对
6. [ ] `stand`/`lie` 能不能通过(决定第 7 步 pitch 激励做不做)
7. [ ] **先小幅试跑一遍标定动作**确认安全,你守硬急停,再正式录
8. [ ] 录标定包:`./record_bag.sh calib` + 我跑 SDK 序列
9. [ ] 录覆盖包:`./record_bag.sh coverage` + 你遥控走遍室内
10. [ ] 两个包跑 MOLA 出图:`tools/lio/mola/make_floor_map.sh <bag> ...`;标定包另做 IMU-雷达外参
