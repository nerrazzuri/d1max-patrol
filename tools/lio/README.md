# D1 Max 3D LIO 建图 —— 台式机(4060 GPU)跑图指南

现场结论(2026-09-18）：机器狗自己的 **RS-Airy(360°×90° 3D 雷达 + 123Hz IMU）足够做 3D LIO 建图，不用加装雷达**。
2D LaserScan SLAM 在商场（开阔+玻璃）失败的真因是"把 3D 压成单层 2D 丢了 93% 几何"，不是传感器/算法问题。
上装（竞争对手）实测用的是 **FAST-LIO 系 LIO 前端 + GTSAM 位姿图回环后端**（arc_mapping 链接 libgtsam，用 PriorFactor/BetweenFactor/FactorGraph）。
→ 我们要复刻的就是这套；**GLIM 开箱即是**（GTSAM 因子图 + 回环 + 原生多雷达）。

## 关键传感器事实
- 前雷达点云 `/front_lidar`（frame `rslidar_head`，RSAIRY），字段 `x,y,z,intensity,ring(u16),timestamp(f64 绝对秒)`
- 前雷达 IMU `/front_lidar/imu` ~123Hz（Airy 内置，LIO 用这个）
- 后雷达 `/rear_lidar`（frame `rslidar_tail`，同款 RSAIRY）—— 先单雷达跑通再上双
- 网络：狗 Orin ws/zenoh；录包 topic 见下

## A. FAST-LIO2（CPU 即可，快速验证）
```bash
mkdir -p ~/lio_ws/src && cd ~/lio_ws/src
git clone https://github.com/Ericsii/FAST_LIO.git
cd FAST_LIO && git submodule update --init --recursive   # ★ 必须！ikd-Tree 是子模块，忘了会 "No SOURCES"
cd ~/lio_ws/src
# FAST_LIO 依赖 livox 的 CustomMsg 定义（即使不用 Livox）。全驱动要 Livox SDK，改成"只生成消息的最小包"：
git clone --depth 1 https://github.com/Livox-SDK/livox_ros_driver2.git
# 用本目录 minimal_livox/ 里的 CMakeLists.txt + package.xml 覆盖 livox_ros_driver2 的（只留 msg/）
cd ~/lio_ws
sudo apt install -y ros-humble-pcl-ros ros-humble-pcl-conversions
colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release
```
**RS-Airy 适配（★已验证的正解：原生 C++ handler）**：给 FAST_LIO 的 preprocess 加一个原生
RoboSense Airy handler，直接读 `/front_lidar`（per-point 绝对秒 timestamp → 相对帧头 ms），
**去掉 Python 中转**。打上本目录 `fastlio_robosense_airy.patch`：
```bash
cd ~/lio_ws/src/FAST_LIO && git apply /path/to/fastlio_robosense_airy.patch && cd ~/lio_ws && colcon build --packages-select fast_lio
```
配置用 `rsairy.yaml`（本目录，**lidar_type=5 ROBOSENSE_AIRY，lid_topic=/front_lidar**，blind=1.0 滤机身，extrinsic_est_en 在线估 lidar-imu 外参）。
> **实测（2026-09-18）**：原生 handler 后 FAST-LIO2 点云处理在**笔记本 CPU 实时 9.2Hz**（rs2velo Python 时仅 1.05Hz）——
> 瓶颈确认在 Python 中转，不在 FAST-LIO/GPU。rs2velo.py 只作临时方案，正式用原生 handler。
> **注意（诚实记录）**：点云处理实时了，但**里程计尚未正确跟踪** —— 见下面「RS-Airy IMU bring-up」：
> IMU 加速度是 g 且重力在 Y 轴，未标好 extrinsic_R 前 LIO 会冻住或发散，此时的"地图"不可信。
> **RS-Airy 是 3D 好数据（360°/46k点/扫到14m）这点是确证的；LIO 跑出正确轨迹还需完成 IMU-雷达标定。**
```bash
# 隔离域跑
export ROS_DOMAIN_ID=110 RMW_IMPLEMENTATION=rmw_fastrtps_cpp ROS_LOCALHOST_ONLY=1
source ~/lio_ws/install/setup.bash
python3 rs2velo.py &                                   # 转格式
ros2 run fast_lio fastlio_mapping --ros-args --params-file rsairy.yaml -p use_sim_time:=true &
ros2 bag play <bag> --clock --topics /front_lidar /front_lidar/imu
# RViz 看 /cloud_registered（3D图）、/Odometry、/path；退出时存 PCD
```
**已知瓶颈**：`rs2velo.py` 是 Python，46k点/帧转换只有 ~1.2Hz，拖慢实测。台式机上仍慢 →
提速办法：① 用 numpy 直接切 buffer（本脚本已尽量）② 或给 FAST_LIO 的 preprocess.cpp 加一个原生 RoboSense handler（读 timestamp 字段），彻底去掉 Python 中转。**GLIM 直接吃 PointCloud2，不需要 rs2velo。**

## B. GLIM（GPU，产品级 —— 推荐，等价于上装那套）
- 内置 GTSAM 全局优化 + 回环 + 原生多雷达（正好补 FAST-LIO2 缺的回环，且能双 RS-Airy 融合）
- 直接订 `/front_lidar`(+`/rear_lidar`) 的原始 PointCloud2 + `/front_lidar/imu`，**不用 rs2velo**
- 4060 GPU 跑 gtsam_points CUDA 后端很快
- 装：见 https://github.com/koide3/glim （GTSAM/Ceres/gtsam_points，建议按官方 docker 或 apt）
- config 要点：point cloud topic、imu topic、lidar-imu extrinsic（先 identity，Airy IMU 在雷达内）、多雷达时两颗 Airy 的外参+时间同步必须准（40ms@1rad/s=2.3°重影 → 先单雷达）

## C. 3D → Nav2 的 2D（导航用）
LIO 出的是 6DoF pose + 3D 点云。给 Nav2 用时：**去地面 → 高度过滤（厚带 0.15-2.0m，非薄片）→ voxel → XY 投影 → 2D occupancy**。
不要再走 "3D→单层 LaserScan→2D SLAM" 老路（那正是今天丢 93% 信息的坑）。

## 测试数据
今天录的验证包（前雷达+IMU+tf，83s）在笔记本 `runs/bags/d1max-lio-*`（1.8G，gitignore，本地拷走）。

## ★ RS-Airy IMU bring-up（外参已解，见最底部「★★★ 外参已解」；本节留档背景）
实测 Airy IMU 有两个必须处理的点，否则 LIO 不动或发散：
1. **加速度单位是 g（静止模长≈1.0），不是 m/s²** → 必须 ×9.81。已在 patch 的 imu_cbk 里做了。
2. **重力在 +Y 轴（静止 accel≈(0,1,0)），不是 Z** → **IMU 坐标系与雷达差约 90°**，必须给对
   `extrinsic_R`（IMU→LiDAR 旋转）。单位阵 + 在线估计会**发散**（实测轨迹飙到 11000m）。
   - 正解：查 RoboSense Airy 的 IMU-点云外参数据手册；或跑一次 **LI-Init** 自标定拿 extrinsic_T/R；
     然后 `extrinsic_est_en: false` 固定它。
   - 验证阶梯（评审建议）：静止（应几乎不漂）→ 慢直线5m（墙不弯）→ 慢原地转360°（闭合）→ 才谈整段。
3. 协方差（acc_cov/gyr_cov/b_*）按实测 IMU 噪声调。
**在 extrinsic_R 标对之前，不要相信 LIO 的轨迹/地图**（会像"冻住"或"发散"）。

## ★ 拿 extrinsic_R 真值的最快路径（等狗上线，优先做这个）
瞎猜 90° 旋转会发散（实测 rotX±90 都失败）。**IMU-雷达外参要真值。**
**最快：抄竞争对手的标定**（他们的 LIO 用的就是这两颗 Airy，早标好了）：
```bash
# 狗上线后
sshpass -p 1 ssh robot@192.168.168.100 'grep -niA3 -iE "extrinsic|T_imu|R_imu|lidar_to_imu|imu_to_lidar|il_|T_il|R_il|rot|trans" /opt/runtime/config/nx_zg.yaml'
# 找 IMU<->LiDAR 的 3x3 R 和 3x1 T，填进 rsairy.yaml 的 extrinsic_R / extrinsic_T，extrinsic_est_en:false
```
备选：跑 **LI-Init**（hku-mars/LiDAR_IMU_Init）自标定，需一段激励充分的运动录包（各轴都转一转）。
**验证阶梯**：静止(轨迹≈0漂但不冻)→慢直线5m(位移≈5m、墙直)→慢转360°(回到起点附近)→整段。
判据：轨迹**跨度合理(几米，非0.01也非上万)**、回到起点附近。

## ★★ 修正与确证（2026-09-18 夜，经第二轮评审）
- **acc 单位不用手动 ×9.81**：FAST-LIO(Ericsii fork)`IMU_Processing.hpp` 本就 `acc_avr = acc_avr*G_m_s2/mean_acc.norm()` 自动归一化（第260行）+ grav 归一（193行）。**已撤掉手加的 ×9.81。** 单位转换只能有一处。
- **"不缩放冻结、缩放后飞11km" 的差异不是单位** → 指向**外参/时戳**（评审判断）。
- **优先级**：① IMU-LiDAR 出厂外参 ② per-point 时戳 ③ 时间同步 ④ 单位 ⑤ 协方差。别先调协方差、别先 LI-Init。
- **确证：竞争对手用固定外参**（nx_zg.yaml `estimate_extrinsic_flag: false`，无在线估计），extrinsic 来自 **Airy DIFOP 的 IMU_CALIB_DATA**（每台出厂标定：四元数 qx,qy,qz,qw + 平移 x,y,z）。
- `/front_lidar/imu` 是**原始 IMU**（重力在 +Y、orientation=单位阵，未应用标定）→ 外参必须由我们提供。
- 现场事实：前雷达 IP `192.168.1.200`（Orin 网卡 enx…27b4=192.168.1.102）；imu_port 6688、difop_port 7788；`use_lidar_clock: true`。

### ★★★ 外参已解（2026-09-18 夜）—— 从 DIFOP 直接读出出厂标定（含一次 offset 修正）
用 Python AF_PACKET 原始套接字抓 7788 端口 DIFOP（Orin 上 tcpdump 没装），按 `RSAIRYDifopPkt`
结构（`decoder_RSAIRY.hpp` + 官方 Airy 手册确认：`status` 后 **offset 1092, length 28**，
接 BE float32 `qx,qy,qz,qw,x,y,z`）解出前后雷达出厂 IMU-LiDAR 外参（脚本：scratchpad/difop_raw.py）。

> **⚠️ 踩坑记录**：一开始读 offset **1084**（错 8 字节），把 status 尾部两个≈0 的垃圾值
> （`1fe21a45`≈5e-20、`00000362`≈1e-42，当 float 恰好≈0）当成了 qx,qy，凑出一个"假的干净"
> 四元数 (0,0,−0.7029,0.7112)。**真 offset 是 1092**，官方手册确认。别被"看起来干净"骗了。

**前雷达 DIFOP 原始 28 字节（offset 1092..1119，BE hex，存证——以后不用再开狗）：**
```
qx=bf33f22c qy=3f3612d0 qz=bb3927ef qw=bc02982b   x=3b8b4396 y=3b88f862 z=bb922531
```

| | qx | qy | qz | qw | x(m) | y(m) | z(m) | \|q\| |
|---|---|---|---|---|---|---|---|---|
| 前 rslidar_head | −0.702914 | 0.711225 | −0.002825 | −0.007971 | 0.004250 | 0.004180 | −0.004460 | 1.0000 |
| 后 rslidar_tail | −0.700577 | 0.713576 | −0.000341 | −0.001023 | 0.004250 | 0.004180 | −0.004460 | 1.0000 |

- 这是 **qw≈0 的 ~180° 旋转**（不是之前误判的绕 Z 89°）。DIFOP 定义 `p_imu = R·p_lidar + t`
  = `R_IL`（经 RoboSense support issue #172 确认），FAST-LIO 的 `extrinsic_R/T` 正是 LiDAR 在 IMU 系
  = `R_IL` → **直接填，不求逆**。
- 前雷达 rsairy.yaml：`extrinsic_R=[-0.011698,-0.999905,-0.007367, -0.999815,0.011808,-0.015224, 0.015310,0.007187,-0.999858]`，`extrinsic_T=[0.004250,0.004180,-0.004460]`
- **物理验证（地面法向 vs 重力，离线在包上做，scratchpad/ground_test.py）**：
  点云地面法向 `n_L=[-0.9995,-0.003,-0.033]`（"上"在点云 **X 轴** → 是 Airy 原始帧，未被转到 base_link 的 Z-up），
  IMU 重力 `g_I=[0.015,0.9999,0.005]`（"上"在 IMU **Y 轴**）。off1092 **direct 得分 0.9999(最高)**
  → 竖直对上、不求逆两点都确证。**但静态只锁竖直 2 DOF，yaw 需运动段重跑 FAST-LIO 才能定**。
- **反驳评审的隐藏变换担忧**：若点云已转 base_link，法向会是 Z=[0,0,1]；实测是 X → **无隐藏安装变换**，
  DIFOP 外参是唯一需要的变换。`frame_id=rslidar_head`（原始帧）也印证。
- `extrinsic_est_en: false` 固定用它。若运动段发散，试 transpose（另一个 yaw 候选，见 yaml 注释）。

### 验证阶梯（务必逐级，判据）
静止30s(位移≈0不冻) → 慢直线3m(位移≈3m、墙直) → 原地转90°(xyz≈不变、yaw≈90°) → 才谈整段。
**标好前 LIO 轨迹/图不可信**（实测：外参错→冻死0.01m 或 发散11000m）。

## ★★★★ 最终结论（2026-09-18 深夜，务必读这段——上面的 off1092 direct 已被证伪）
**DIFOP off1092 字面四元数(-0.7029,0.7112,-0.0028,-0.0080)是 qw≈0 的 ~180° 解；固定填入 FAST-LIO 直接发散(飞 1e11m)。**
地面法向-重力静态测试只锁竖直 2DOF(4 个候选都≈0.9999)，锁不住 yaw，所以别用它判 yaw。
真正能跑的外参 = **FAST-LIO 在线估计收敛值**(给 laserMapping.cpp 加了 `[EXT]` 打印读出来的)：
- `euler(RPY)≈(-0.62°, +1.54°, +89.05°)`，`T≈(1.5,1.3,-5.8)cm`，末端 30 样本 std 极小(yaw 0.008°)。
- `extrinsic_R=[0.016772,-0.999805,-0.010401, 0.999496,0.016485,0.027128, -0.026951,-0.010850,0.999578]`
- `extrinsic_T=[0.015183,0.013217,-0.057787]`（已填 rsairy.yaml，extrinsic_est_en:false）
- 这是 ~90°yaw(Z 保持)解，**不是** DIFOP 的 180° 解 → RoboSense DIFOP 四元数的 frame 约定没对上，存疑，先不纠结。

**观测性(评审要求的扰动初值测试)**：89°初值→收敛 89.05；但 **80°初值在本段运动 67s 只从 80° 慢爬到 84°、仍在移动没会合** →
**本 bag 旋转激励不足、yaw 弱可观**。此值够建可用图（89°初值那次干净跟踪 35m；固定值那次 Y 竖直只飘 ±0.07m，远好于粗糙 off1084 的 ±1.1m），
但**非标定级精确**。要标定级精度：**补录一段激进多轴激励运动(pitch/roll/yaw 都甩、走 8 字)跑 LI-Init** —— 这是唯一值得再为外参开狗做的事(~5 分钟)。

**性能**：笔记本 FAST-LIO 只 ~0.3-0.5Hz 跟不上，整层 858s 出图/GLIM **必须上台式机 4060**。
**进程坑**：同 ROS_DOMAIN 别重复起 bag play（重复帧→`lidar loop back, clear buffer`→发散）；停进程用精确 PID，别用 `pkill -f 模式`（自匹配 → exit 144 中断脚本）。
