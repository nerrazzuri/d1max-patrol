# D1 Max 现场录包 —— 两个包(标定 + 覆盖)

目标两个 bag:
1. **标定包(calibration)** —— 解决 IMU-雷达外参弱可观。**我用 SDK 脚本控制**做多轴激励动作。
2. **整层室内地图(coverage)** —— 至少把室内扫清楚。**你用遥控器**慢速走遍全场。

两个包都用 `record_bag.sh` 录(**含 `/front_lidar/imu`** —— 代理自带的录包(站点下「开始录包」)漏了 IMU,标定包必须自己录)。

---

## 关键事实(已从代码/现场定死)
- **运动接口** = `walk(时长, forward, lateral, yaw)`,开环脉冲,控制量是**百分比**,**死区 ~0.3**(0.3~0.5 才真走,<0.3 静默不动)。yaw 死区 `MIN_YAW=0.30`。
- **没有身体 pitch/roll 命令**。只有 `stand()` / `lie()`(整体起立/趴下)和 `set_gimbal`(云台/头,不是身体)。
- **狗上老服务那套 HTTP 控制接口(`/api/control/*`、`/api/teleop`)W00c5e 退役了。** 本目录的 `calibrate_drive.py` 本来就不走它:直接连旁路进程(`SidecarDeviceBackend`,TCP 8090)。人手遥控改走站点(手机遥控页,决策 7:只有前后与转向)。
- **stand/lie 只有旁路进程这一条路**(`SidecarDeviceBackend` 的 `stand/lie`,`calibrate_drive.py --stand-lie` 用的就是它);站点遥控不发 stand/lie。
- 代理自带录包话题(`src/d1max_patrol/app/mapping.py` 的 `RECORD_TOPICS`)= `/front_lidar,/tf,/tf_static,/odom/*`,**无 IMU** → 用本目录 `record_bag.sh` 代替。

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

## SDK 标定驱动:`calibrate_drive.py`(我控制那半)
复用项目 `SidecarDeviceBackend` 的 `walk/stand/lie`,默认 dry-run,`--arm` 才动。
```bash
# 这台笔记本带去办公室。连上狗网络后(host 填 Orin IP):
python3 tools/field-record/calibrate_drive.py --host <ORIN_IP> --dry-run          # 看计划
python3 tools/field-record/calibrate_drive.py --host <ORIN_IP> --test             # 小步试(1拍前进+停)
python3 tools/field-record/calibrate_drive.py --host <ORIN_IP> --measure-yaw      # 转5s测角速率→改 YAW_DEG_PER_S
python3 tools/field-record/calibrate_drive.py --host <ORIN_IP> --arm --stand-lie  # 正式跑(另开终端先跑 record_bag.sh calib)
```
- **前提(关键)**:`acquire_control()` 只是**核对**旁路进程握着控制权;真正的 TakeControl 是**旁路进程在开机窗口抢的**。
  所以 SDK 驱动前必须:**重启狗 + 旁路进程抢赢握手**(否则上装占着,acquire 抛错,当天走不了 SDK 这条)。
- 安全:walk 有硬上限(≤10s,|amt|≤0.5),脚本再收紧到 ≤4s;全程 try/finally 兜底 halt;**你守硬急停**。

## 现场 preflight 清单(笔记本带到办公室、连上狗网络后逐项过)
1. [ ] 笔记本连上办公室网络 + 能到狗(`ping 192.168.168.100` / Orin;zenoh 路由通)
2. [ ] `ros2 topic echo /front_lidar --once` 收到点云;**`/front_lidar/imu` 也收得到**(标定命根子)
3. [ ] 磁盘 >5GB(笔记本 262G,够)
4. [ ] **【标定专属】重启狗 + 旁路进程抢赢握手**;`calibrate_drive.py --host <IP> --dry-run` 能连、acquire 不报错
5. [ ] `--test` 一小拍前进:确认真走、方向对、死区够(<0.3 不动)
6. [ ] `--measure-yaw` 转 5s:从 /odom/current_pose 或 IMU 读角度,改 `YAW_DEG_PER_S`
7. [ ] `stand`/`lie` 单独试一次能不能通(决定 `--stand-lie` 开不开;它是唯一 pitch/Z 激励)
8. [ ] **录标定包**:终端A `./record_bag.sh calib`;终端B `calibrate_drive.py --host <IP> --arm --stand-lie`;你守急停
9. [ ] **录覆盖包**:`./record_bag.sh coverage` + 你遥控走遍室内(慢、平顺、连续、回起点)
10. [ ] 两个包跑 MOLA:`tools/lio/mola/make_floor_map.sh <bag> mola_out /front_lidar rslidar_head`;标定包另做 IMU-雷达外参
