# D1 Max RS-Airy 整层建图 —— GLIM(台式机 4060,产品级,带回环)

FAST-LIO2 已证实：管线跑通、3x 实时、89° mapping-grade 外参可跟踪，**但整层商场只靠 FAST-LIO 局部里程计会漂移/错层**
（实测 858s 全程出现约 9m 竖直漂移面 + 拖影，见 tools/lio/README.md「★★★★」段）。
→ 需要 **回环 + 位姿图**。GLIM(koide3）开箱即带：GTSAM 因子图 + 全局回环 + 原生多雷达 + GPU 后端。
这正是竞争对手上装的架构（FAST-LIO 前端 + GTSAM 后端）。**GPU 后端就是台式机 4060 真正有用的地方**
（FAST-LIO2 本身吃 CPU，与 4060 无关）。

官方文档：https://koide3.github.io/glim/ ・ 仓库：https://github.com/koide3/glim

---

## 1. 安装（Ubuntu 22.04 + ROS2 Humble + CUDA，PPA 法最快）

```bash
sudo apt install curl gpg
curl -s https://koide3.github.io/ppa/setup_ppa.sh | sudo bash
sudo apt update
sudo apt install -y libiridescence-dev libboost-all-dev libglfw3-dev libmetis-dev
# 按你 4060 的 CUDA 版本选一个（nvcc --version 查）：
sudo apt install -y libgtsam-points-cuda12.6-dev      # 或 cuda12.2 / cuda13.1
sudo apt install -y ros-humble-glim-ros-cuda12.6      # 与上面 CUDA 版本一致
sudo ldconfig
```

源码编译兜底（PPA 装不上时）：
```bash
sudo apt install libomp-dev libboost-all-dev libmetis-dev libfmt-dev libspdlog-dev libglm-dev libglfw3-dev libpng-dev libjpeg-dev
# GTSAM 4.3a0
git clone https://github.com/borglab/gtsam && cd gtsam && git checkout 4.3a0
mkdir build && cd build && cmake .. -DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF -DGTSAM_BUILD_TESTS=OFF -DGTSAM_WITH_TBB=OFF -DGTSAM_USE_SYSTEM_EIGEN=ON -DGTSAM_BUILD_WITH_MARCH_NATIVE=OFF && make -j$(nproc) && sudo make install
# Iridescence(可视化)
git clone https://github.com/koide3/iridescence --recursive && mkdir iridescence/build && cd iridescence/build && cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc) && sudo make install
# gtsam_points(CUDA)
git clone https://github.com/koide3/gtsam_points && mkdir gtsam_points/build && cd gtsam_points/build && cmake .. -DBUILD_WITH_CUDA=ON && make -j$(nproc) && sudo make install
# GLIM
cd ~/ros2_ws/src && git clone https://github.com/koide3/glim && git clone https://github.com/koide3/glim_ros2
cd ~/ros2_ws && colcon build --cmake-args -DBUILD_WITH_CUDA=ON -DBUILD_WITH_VIEWER=ON && sudo ldconfig
```

## 2. 准备我们的 config

GLIM 的 config 是**完整 JSON**，别用残缺文件覆盖。做法：拷官方默认 config 目录，再把本目录两个文件里的**关键字段**填进去。

```bash
# 找到默认 config 目录(PPA 装的一般在这)：
GLIM_CFG=$(ros2 pkg prefix glim)/share/glim/config     # 或源码里的 glim/config
cp -r "$GLIM_CFG" ~/d1max_glim_config
```

然后按本目录 `config_ros.json` / `config_sensors.json` 的值，改 `~/d1max_glim_config/` 下同名文件的这些字段：

**config_ros.json**（话题）：
```json
"imu_topic": "/front_lidar/imu",
"points_topic": "/front_lidar",
"acc_scale": 0.0,          // 0.0=自动检测 → 自动搞定 RS-Airy IMU 的 g→m/s²(不用手动 ×9.81)
"gyro_scale": 1.0
```

**config_sensors.json**（外参，格式 `[x,y,z, qx,qy,qz,qw]`，约定=把点从 imu 系变到 lidar 系）：
```json
"T_lidar_imu": [ -0.015022, 0.014335, 0.057562, 0.013318, -0.005804, -0.701128, 0.712888 ]
```
> 这是我们 FAST-LIO 收敛的 R_IL(euler RPY -0.62/1.54/89.05°)求逆到 GLIM 约定后的值。
> **mapping-grade candidate，非标定级**。若 GLIM 也漂，先试它自带的在线外参/时偏优化，或跑 LI-Init 拿标定级值。

## 3. 跑我们的包

把笔记本上的包拷到台式机：`runs/bags/d1max-lio-20260918T163107/`（1.8G，前雷达+IMU+tf）。

```bash
source ~/ros2_ws/install/setup.bash   # 或 source /opt/ros/humble/setup.bash(PPA)
# 在线可视化跑 rosbag：
ros2 run glim_ros glim_rosbag ~/path/to/d1max-lio-20260918T163107 --ros-args --params-file <(echo "") \
  -p config_path:=$HOME/d1max_glim_config
# 或用节点 + 单独播包：
ros2 run glim_ros glim_rosnode --ros-args -p config_path:=$HOME/d1max_glim_config &
ros2 bag play runs/bags/d1max-lio-20260918T163107 --topics /front_lidar /front_lidar/imu
```
> 具体子命令名以 `ros2 pkg executables glim_ros` 和官方 docs 为准（版本间略有差异）。
> 建图结束在 GLIM 界面点 “save” 导出点云/位姿图；离线复看用 `offline_viewer`。

## 4. 验收（和 FAST-LIO 同一套判据，重点看回环有没有救回来）
1. 同一堵墙**无重影**（回环闭合后应该单层）
2. 走廊前后**不弯**
3. **回到旧区域无错层**（← 最关键，这是 FAST-LIO 挂掉的地方，GLIM 的回环就是治这个）
4. Z/楼层高度长距离**无持续漂移**（FAST-LIO 那条低 9m 的漂移面应消失）

## 5. RS-Airy 专属注意
- **IMU 单位 g**：GLIM `acc_scale:0.0` 自动检测处理，无需改数据。
- **重力在 IMU +Y、点云原始帧"上"是 X 轴**（不是 Z-up）；GLIM 用 IMU 初始化重力方向，配上上面的 T_lidar_imu 即可。
- **per-point 时戳**：RS-Airy 点云带绝对秒 timestamp 字段，GLIM 一般自动识别用于去畸变；若报时戳异常，查 config_preprocess.json 的时间字段设置。
- **双雷达**（后续）：GLIM 原生支持多雷达，可再加 `/rear_lidar`(+其 T_lidar_imu，见 tools/lio/rsairy.yaml 后雷达注释)融合，但先单前雷达跑通。
- **别用 use_sim_time 节流**：离线直接墙钟跑，GLIM/gtsam_points GPU 后端在 4060 上很快。
