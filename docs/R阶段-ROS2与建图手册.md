# R 阶段 · ROS2 链路与自建建图（笔记本 = Ubuntu 22.04）

这一份是 [`真机联调手册.md`](真机联调手册.md) 的续篇，专门讲**绕开厂商 nav、自己跑 SLAM**
的现场步骤。背景和技术选型见 [`自建SLAM可行性.md`](自建SLAM可行性.md)。

**读者是笔记本上的那个 Claude。** 照着 R0 → R6 一步一步做，不要跳。

---

## 先说清楚：今天不一定能建出图，但一定要拿到 R2 的答案和 R4 的包

这一趟真正的**必成项**只有两个：

- **R2 的侦察答案** —— 它决定了自建 SLAM 到底是「明天就能建图」还是「要先干两周」。
  20 分钟就能问出来，不问出来后面全是瞎猜。
- **R4 的 bag** —— 五分钟、只读、零风险。有了它，所有算法工作都能在办公室做；
  没有它，回去一天工都开不了。

**建图本身（R5）是加分项，不是必成项。** 能成最好，不成就带着 bag 回来。
不要为了当场建出图去停厂商的节点、去改机器上的配置 —— 那是把「今天少一个成果」
换成「明天机器不能用了」。

---

## R0. 出发前在家装好（**现场不要装东西**）

现场的网可能只有机器的 AP 热点，`apt` 根本下不动。这一步必须在家做完。

```bash
sudo apt update
sudo apt install -y \
    ros-humble-desktop \
    ros-humble-rmw-zenoh-cpp \
    ros-humble-slam-toolbox \
    ros-humble-pointcloud-to-laserscan \
    ros-humble-nav2-map-server \
    ros-humble-tf2-tools \
    ros-humble-rosbag2-storage-mcap
```

验一下装好了：

```bash
source /opt/ros/humble/setup.bash
ros2 pkg list | grep -E 'slam_toolbox|pointcloud_to_laserscan|rmw_zenoh|map_server'
```

四个都要出现。少一个就现在补，别到现场才发现。

**再准备磁盘。** 96 线雷达 ×2 @10 Hz 的原始点云很大，`ros2 bag` 吃盘很凶。
出发前确认笔记本至少空出 **50 GB**：

```bash
df -h ~
```

---

## R1. 接上 zenoh 链路

前提：笔记本已经按 [`真机联调手册.md`](真机联调手册.md) C 阶段连上机器了（有线或 WiFi 都行）。

### R1.1 笔记本 IP 必须在 `192.168.168.0/24` 网段

厂商文档写死的要求：`192.168.168.xxx`，**xxx 不能是 100（Orin NX）、168（RK3588）、255**。

```bash
ip addr show                     # 先看当前 IP
ping -c2 192.168.168.100         # Orin NX，ROS2 都在这台上
```

ping 不通就先回 C 阶段，不要往下走。走 WiFi 的话别忘了那条路由（清单 #28）：

```bash
sudo ip route add 192.168.168.0/24 via 192.168.234.1
```

### R1.2 改 zenoh router 配置

```bash
sudo nano /opt/ros/humble/share/rmw_zenoh_cpp/config/DEFAULT_RMW_ZENOH_ROUTER_CONFIG.json5
```

找到 `connect` 下面的 `endpoints`，填进去：

```json5
endpoints: ["tcp/192.168.168.100:7447"],
```

### R1.3 起环境

**开一个终端，让它一直开着**：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=24
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
ros2 run rmw_zenoh_cpp rmw_zenohd
```

**另开一个终端**，以后所有命令都在这种终端里跑（每开一个新终端都要重新 source + export）：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=24
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
ros2 daemon stop && ros2 daemon start
ros2 topic list
```

**看到一串话题名 = 链路通了。** 这是今天第一个里程碑。

看不到就：① 确认 router 那个终端没报错 ② 确认三个环境变量在**当前**终端里都设了
③ `ros2 daemon stop && ros2 daemon start` 再试 ④ ping 一下 `192.168.168.100:7447`。
折腾超过 30 分钟就停手，直接跳到 R4 的备用方案（在机器上录包）。

---

## R2. 侦察四件事（**今天最重要的 20 分钟**）

把每一条的**原始输出**都存下来，回去要抄进清单。

```bash
mkdir -p ~/d1max-recon && cd ~/d1max-recon
```

### R2.1 话题到底叫什么（清单 #57）

```bash
ros2 topic list > topics.txt
cat topics.txt
grep -iE 'lidar|scan|point|imu|odom|tf|camera|uss|rtk' topics.txt
```

⚠️ 固件更新记录里明写过一条「修改前后激光雷达 IP 地址及 rostopic」。
**文档里的 `/front_lidar` 未必是现在的名字。以 `topic list` 的输出为准。**

### R2.2 厂商发不发 odom 和 TF（**这一条决定今天能不能建图**）

```bash
ros2 topic list | grep -i odom
ros2 topic echo /tf_static --once > tf_static.txt
ros2 run tf2_tools view_frames        # 生成 frames.pdf / frames.gv
```

然后看 `frames.pdf`：

- **有 `odom → base_link` 这条边** → 走 **R5 甲路线**，今天很可能建得出图。
- **没有** → 走 **R5 乙路线**（用我们自己的 `patrol_agent` 补 odom），要多花一个钟头，
  而且依赖两个没验过的假设（清单 #51/#52）。

### R2.3 点云里有没有逐点时间戳（清单 #58）

```bash
ros2 topic echo /front_lidar --once --field fields > lidar_fields.txt
cat lidar_fields.txt
```

（`--field` 不支持的话就 `ros2 topic echo /front_lidar --once | head -60`。）

盯着 `fields` 里的 `name`：除了 `x y z intensity`，**有没有 `time` / `t` / `offset_time` /
`timestamp`**。有 → FAST-LIO2 能做运动去畸变，精度有保障。没有 → 回去要另想办法。

顺便记下 `point_step`、`width`、`height`，这决定带宽。

### R2.4 带宽和频率扛不扛得住

```bash
ros2 topic hz /front_lidar &
sleep 12 && kill %1
ros2 topic bw /front_lidar &
sleep 12 && kill %1
```

`hz` 应该在 10 左右。`bw` 是关键 —— 如果单个雷达就几十 MB/s，
**R4 录包时只录前雷达**，两个一起录会丢帧，丢帧的包回去是没法用的。

---

## R3. 用 rviz2 肉眼看一眼点云（10 分钟，第一个可视产物）

```bash
rviz2
```

在 rviz2 里：

1. 左下 **Add** → **By topic** → 选 `/front_lidar` 的 **PointCloud2**
2. 左上 **Global Options → Fixed Frame** 改成点云的 `frame_id`
   （不知道叫什么就看 `ros2 topic echo /front_lidar --once | head -8` 里的 `frame_id`）
3. PointCloud2 项下面把 **Size** 调到 0.02，**Color Transformer** 选 `Intensity` 或 `AxisColor`

**看到房间的形状 = 传感器是好的。** 这一步和 SLAM 无关，但它把「点云到底能不能拿到」
这个问题一次性关掉了。截个图带回来。

---

## R4. 录一包 bag（**必成项，五分钟**）

```bash
cd ~/d1max-recon
ros2 bag record -o d1max-walk-01 \
    /front_lidar /front_lidar/imu /imu_driver/imu_central \
    /tf /tf_static
```

（话题名以 R2.1 的实际输出为准。R2.4 显示带宽富余、并且要用后雷达时，再加上
`/rear_lidar /rear_lidar/imu`。）

录的时候**让机器走一圈**：遥控或 SDK 都行，慢一点，走一个能回到原点的闭环
（绕房间一周再走回起点最理想 —— 有闭环，回去才能验证回环检测）。
**60~90 秒就够**，别录十分钟，盘吃不消。

`Ctrl+C` 停。然后：

```bash
ros2 bag info d1max-walk-01
du -sh d1max-walk-01
```

`ros2 bag info` 里每个话题的消息条数要和「时长 × 频率」对得上。
**差很多就是丢帧了，删掉重录一次更短的。**

### R4 的备用方案：链路没通也要拿到包

R1 折腾不通的话，直接 SSH 上机器录，回来再 scp：

```bash
ssh robot@192.168.168.100          # 密码 1
df -h                              # 先看盘还有多少
source /opt/ros/humble/setup.bash
ros2 bag record -o /tmp/d1max-walk-01 /front_lidar /front_lidar/imu /tf /tf_static
# Ctrl+C 之后回到笔记本：
scp -r robot@192.168.168.100:/tmp/d1max-walk-01 .
```

⚠️ 机器上的盘不大，**录之前一定先 `df -h`**，录完记得把 `/tmp` 里的删掉。

---

## R5. 建图（加分项）

### 甲路线 · 厂商已经发了 `odom → base_link`

这条最省事，全是 apt 装好的现成东西，不用编译。

**终端 1** —— 把 3D 点云拍成一层 2D 扫描：

```bash
ros2 run pointcloud_to_laserscan pointcloud_to_laserscan_node \
  --ros-args \
  -r cloud_in:=/front_lidar \
  -p target_frame:=base_link \
  -p min_height:=-0.20 -p max_height:=0.60 \
  -p angle_min:=-3.14159 -p angle_max:=3.14159 \
  -p angle_increment:=0.0087 -p range_min:=0.3 -p range_max:=30.0
```

`min_height` / `max_height` 是**唯一要现场调的参数**。它是相对 `base_link`（机身原点）的
高度带。切太低会把地面当障碍（图上全是噪点），切太高会漏掉矮障碍。
先按上面的值跑，在 rviz2 里加一个 `/scan` 的 **LaserScan** 显示看效果，不对再调。

**终端 2** —— 建图：

```bash
ros2 launch slam_toolbox online_async_launch.py \
  use_sim_time:=false
```

**终端 3** —— 看：

```bash
rviz2
```

rviz2 里 Add → By topic → `/map` 的 **Map**，Fixed Frame 设成 `map`。
再 Add 一个 `/scan` 的 LaserScan 和 `/front_lidar` 的 PointCloud2。

**然后牵着机器慢慢走一圈。** 你会看到栅格图一格一格铺开。这就是肉眼验收。

### 乙路线 · 厂商不发 odom，用我们自己的

先按 [`真机联调手册.md`](真机联调手册.md) **S1** 把 `patrol_agent` 跑起来（它得抢开机窗口，
所以这条路线要在机器刚开机时做）。然后：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=24 RMW_IMPLEMENTATION=rmw_zenoh_cpp
/usr/bin/python3 tools/ros2_odom_bridge.py --agent 127.0.0.1:8090 --static
```

（注意是**系统 Python**，不是本仓库的 venv —— venv 里没有 rclpy。）

看到 `odom #1: x=... y=... yaw=...` 就通了。然后：

```bash
ros2 topic echo /odom --once
ros2 run tf2_tools view_frames     # 现在应该能看到 odom -> base_link 了
```

**先验一下这份里程是不是真的能用（清单 #51/#52）**，这比直接建图重要：

```bash
ros2 topic echo /odom --field pose.pose.position    # 开着不动
# 让机器往前走 2 米，再看一次
```

- `x` 一直是 0 → `MotionData.position` 在真机上没数据，**乙路线到此为止**，
  带 bag 回家跑 FAST-LIO2。
- `x` 涨了但明显不是 2 米 → 记下比例，先照旧建图，回去查。
- 原地转 90°，`yaw` 应该变约 1.57。**变成别的值 = 四元数顺序猜错了（#52）**，
  这时候建出来的图会歪得很好看但完全没用。

里程验过了，再照**甲路线**的三个终端建图。

### 丙路线 · 前两条都不成

**不要在现场硬啃。** 确认 R2 的答案抄全了、R4 的包录好了，就收工。
FAST-LIO2 那条路本来就是设计成在办公室离线做的（见 `自建SLAM可行性.md` 阶段 1）。

---

## R6. 存图 + 肉眼验收标准

```bash
ros2 run nav2_map_server map_saver_cli -f ~/d1max-recon/map-01
```

生成 `map-01.pgm` + `map-01.yaml`。

> 顺带一提：**这和厂商 nav 的地图是同一种格式**（他们的 `get_pgm_map` 就是发 PGM）。
> 所以我们的图和他们的图可以直接摆在一起对比。

直接看图：

```bash
eog ~/d1max-recon/map-01.pgm     # 或者 xdg-open
```

**怎么算「建得好」：**

| 看什么 | 好的样子 | 坏了说明什么 |
|--------|---------|-------------|
| 墙 | 一条干净的细线 | 变成两条平行的墙 = 回环没闭上，或者里程比例不对 |
| 走廊 | 直的 | 弯的、越走越歪 = IMU / 里程有问题 |
| 回到起点 | 起点那块和第一次扫的对得上 | 对不上、错开一截 = 回环检测没触发 |
| 空地 | 大片白色 | 满地黑点 = `min_height` 切到地面了，调高 |
| 边角 | 有明确的黑色边界 | 大片灰色 = 走得太快，或者点云丢帧 |

**双墙是最典型也最容易看出来的失败**，一眼就认得出。看到双墙就把 `min_height` /
`max_height` 和走的速度记下来，重走一次。

---

## R7. 要带回来的东西

放进 `~/d1max-recon/`，整个目录带回来：

- [ ] `topics.txt` —— 完整的 `ros2 topic list`
- [ ] `frames.pdf` —— TF 树（**最关键的一份**）
- [ ] `tf_static.txt`
- [ ] `lidar_fields.txt` —— 点云的 `fields` 定义
- [ ] `hz` / `bw` 的输出（贴进 R2.4 的记录里）
- [ ] `d1max-walk-01/` —— **bag，最关键的一份**
- [ ] `map-01.pgm` + `map-01.yaml`（如果建出来了）
- [ ] rviz2 的截图（点云一张、地图一张）
- [ ] 一句话：R2.2 的答案是甲还是乙

---

## 绝对不要做的事

- ❌ **不要 `robot-launch stop` 厂商的节点。** 停错了可能连遥控都不能用，
  而现场没人能帮你恢复。今天只订阅、不发布、不停任何东西。
- ❌ **不要同时跑我们的 odom 桥和厂商的 TF。** 两个源发同一条 `odom → base_link`，
  tf2 会打架，而且打得很隐蔽 —— 地图会莫名其妙地抖。R2.2 查出厂商有，就别跑桥。
- ❌ **不要为了建图去动机器上的配置文件。**
- ❌ **不要在电量低于 30% 时开始建图。** 清单 #43：一天从 71% 掉到 17%，
  而低电量很可能就是厂商建图被拒（#42）的原因。四足断电会当场瘫倒。
- ❌ **不要录十分钟的包。** 盘会满，而且回去也用不上那么长。
