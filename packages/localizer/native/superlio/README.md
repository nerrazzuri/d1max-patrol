# Super-LIO 建图前端的源码（工单 21）

- `Super-LIO-f89f48d.tar.gz`：上游 GitHub `Liansheng-Wang/Super-LIO`，`ros2` 分支，提交 `f89f48d`，原样打包，未改。
  **许可证 GPL-3.0**（包里 `Super-LIO/LICENSE`）。
- `livox_ros_driver2_stub/`：只有消息定义的空壳包（Super-LIO 编译时要 `livox_ros_driver2` 的消息；我们用的是 Airy，不用 Livox）。
- `kf2sm/`：我们自己写的，关键帧 + 原始点云 → MRPT simplemap（接原来的出图流程）。不链 Super-LIO 的代码。

编译：`deploy/nexwar_superlio_build`，在机器人上编，装到 `/opt/nexwar/superlio/<版本>/`。

**GPL 的边界**：Super-LIO 只作为单独的进程跑（`super_lio_node`），我们的程序只订它发的 ROS 话题，不链接、不改它的代码。
产品化（对外卖带这份源码的发行包）之前要定法律口径：随包附源码和许可证是否足够、要不要另行提供源码下载。
