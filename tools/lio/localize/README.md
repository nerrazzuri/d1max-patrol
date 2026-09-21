# D1 Max 定位原型(在 MOLA 地图里定位机器狗)

现场验证(2026-09-21，智元展厅，玻璃幕墙+漏斗柱）得到的结论，供后续做产品级定位参考。

## 结论(重要)
- **纯单帧 LiDAR scan-to-map 匹配在这类玻璃+柱子+细长对称空间里不可靠**：激光穿透玻璃产生大量"看穿"点，匹配的"内点最多"位姿往往**不是真位姿**，会跳到假位置（位置错几米、朝向错几十度）。全局搜、局部搜都试过，都会跳。
- **能用的定位 = 里程计锚定 + 跟踪**：
  1. 给一个初始真值位姿（人指 / 或已知点），同时记下当时的 `/odom/current_pose`(nav_msgs/Odometry, odom→base_link) → 锁定 `T_map_odom`。
  2. 之后读 odom 套 `T_map_odom` 得到地图位姿。odom 平滑、短期不跳，能跟着狗走。
  3. **但四足腿式里程计会漂**：实测走 ~9m 累积 ~2m 误差。
- **scan 微修正(odom预测附近局部匹配)在这里也不稳**：预测已近，但局部匹配仍被玻璃/柱子拉偏，把 y 越修越错。所以本环境**不能只靠 LiDAR**。
- **产品级方案**：AMCL(粒子滤波，比手搓稳) **+ 二维码/反光标记在关键点纠偏**（玻璃场景基本必须）。或用 3D 匹配(漏斗柱/天花的3D形状更独特)而非压成2D。

## 脚本
- `anchor.py` — 读当前 odom，锁定到给定的地图真值位姿，存 `lock.json`。
- `track.py` — 读 odom+lock 算地图位姿，叠加当前 /front_lidar 2D scan 画图；含(不稳的)scan 微修正。
- `scan_match_localize.py` — 单帧全局/局部 scan-to-map 匹配（**已证明在玻璃环境不可靠**，仅留档）。

环境：`RMW_IMPLEMENTATION=rmw_zenoh_cpp ROS_DOMAIN_ID=24 ZENOH_ROUTER_CONFIG_URI=config/zenoh_router.json5`，笔记本需常驻 `rmw_zenohd`。
