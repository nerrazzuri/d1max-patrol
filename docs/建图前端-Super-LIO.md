# 建图前端换成 Super-LIO（工单 21）

**结论**：建图默认用 Super-LIO（只用前雷达和它的 IMU，跟竞品同一套做法）。MOLA 留着：没编 Super-LIO 的机器人自动用 MOLA，
也可以在 `/etc/d1max/env` 里写 `D1MAX_MAP_FRONT=mola` 切回去。**巡逻定位不变，仍是 MOLA**，用的还是同一种 `prior.mm`。

为什么换：2026-11-02 商场两版图，MOLA 都跟丢了（机器人上首尾差 56 m 和 12.5 m）。同一个商场，Super-LIO 在机器人上实时跑，首尾差 5 cm；
台式机离线重算 v2 包，首尾差 0.14 m（`docs/Super-LIO现场验证-2026-11-02.md`）。

## 一趟建图怎么走

1. 站点上「开始建图」（边走边建）：
   - 先经特权执行器**暂停**合并雷达（`d1max-lidar-merge`）和定位器（`d1max-localizer`），给 Super-LIO 让出 CPU。避障不停。
   - 录包：原来那几个话题，**加上** Super-LIO 的位姿 `/d1max_mapping/lio/odom`。
   - 起 Super-LIO（`deploy/d1max-superlio-live`，单独一个进程）。它发的话题全部改名到 `/d1max_mapping/` 下面，
     跟定位器、竞品的 `/lio/*` 不会混。
   - 建图预览订这个位姿，手机上照常能看到图在长。
2. 「结束建图」：停录包、停 Super-LIO，**恢复**那两个服务。代理中途重启了也会恢复（`.paused` 标记）。
3. 打包（`d1max-map-build --front superlio --reuse`）：
   - 从录包里读 Super-LIO 的位姿，换成雷达位姿，写出 `traj.tum`；
   - 每走 0.3 m 或转 10° 取一个关键帧，用录包里的**原始整帧点云**（不是 Super-LIO 降采样后的点），由 `kf2sm` 写成 `map.simplemap`；
   - `sm2mm` 从 simplemap 出 `raw_prior.mm`；
   - 之后完全是原来的流程：回环（装了回环工具就做，工单 20）、标「上」、画 `floor.pgm`、打 `prior.mm`、坐标、覆盖、`build.json`。
4. **建完检查**：相邻两个位姿跳超过 1 m（跟丢了），这一趟不出图，提示慢一点重走。首尾相距、路程写进 `build.json`
   （`mola.quality`），回到起点的话首尾相距就是误差。

从录包重建（老包里没有 Super-LIO 位姿）：在隔离域里以 0.5 倍速回放前雷达和 IMU，交给 Super-LIO 重算（录包 5 分钟约算 10 分钟）。

## 两个容易弄错的地方（对着源码核过）

- `/lio/odom` 发的是 **IMU 的位姿**（`ROSWrapper::pub_odom`，用 `state.p` 和 `state.R`），不是雷达的。
  出图前要乘「雷达 → IMU」外参（参数文件里的 `lio.extrinsic.lidar_imu`）换成雷达位姿，否则图会偏几厘米到十几厘米，转弯时更明显。
- 位姿的时刻是这一帧**最后一个点**（比点云报文头晚约 0.1 s）。关键帧取在一帧的中间时刻插值；预览配对的窗口也按这个设。

## 装

源码随发行包带（`packages/localizer/native/superlio/`，GPL-3.0，见那里的 README）。在机器人上编一次（Orin 上约 7 分钟）：

```bash
sudo /opt/d1max/current/deploy/d1max-superlio-build
```

装到 `/opt/d1max/superlio/<版本>/`，`current` 指向它。同一份源码不会重复编。没编之前，建图自动用 MOLA，装机脚本会提示。

## 参数

默认参数：`deploy/superlio/d1max.yaml`（前雷达 Airy 的外参，就是 GLIM 标定的 `T_lidar_imu` 取逆）。
现场要调参的话，放一份 `/etc/d1max/superlio.yaml`，有就优先用它，不用改代码、也不进仓库。

## 还没做的

- **巡逻定位改用 Super-LIO 的重定位模式**（`relocation_node`：载入地图，NDT 粗配加 ICP 精配，之后在已有地图上跟踪）。已经编进去了，还没测；
  先在办公室玻璃那段单独验证，过了再另开工单接到代理的定位接口上。
- 后雷达不用。要不要加，以后再评估。
- RK3588（V2 算力板）上要重新量 CPU。在 Orin 上约 0.35 核、每帧 6–7.6 ms。
- GPL-3.0：产品化前定法律口径。
