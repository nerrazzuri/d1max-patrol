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
> **实测（2026-09-18）**：原生 handler 后 FAST-LIO2 在**笔记本 CPU 实时 9.2Hz**（rs2velo Python 时只有 1.05Hz）。
> 商场走廊段建出**两条清晰长直平行墙 + 房间结构，876k点/14m高/65万墙点**，对比 2D 图（~200墙点）质变。**方向验证通过。**
> rs2velo.py 仅作快速验证的临时方案，正式用原生 handler。
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
