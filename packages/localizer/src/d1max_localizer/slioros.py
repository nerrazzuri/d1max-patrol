"""Super-LIO 的 ROS 适配(W68):只在机器人上(ROS 的系统 Python)与回放实跑时 import。

- 订阅 Super-LIO 发的位姿 :data:`~d1max_localizer.slioloc.POSE_TOPIC`(``nav_msgs/Odometry``,**IMU**
  在地图里的位姿,时间戳 = 扫描结束时刻)→ 乘外参换成雷达位姿,配上质量交给核心;
- 订阅前雷达点云(Super-LIO 只用它,外参也是它的):每帧告诉核心「点云在来」;每
  :data:`COUNT_EVERY` 帧反序列化一次,数有效点(雷达被挡,W34)、留给套图打分
  (:class:`~d1max_localizer.slioloc.ScanScorer`)。
- 没有重定位服务:重定位 = 后端换初值重启进程(:mod:`d1max_localizer.slioback`)。
- rclpy 在自己的线程里转,回调经 ``call_soon_threadsafe`` 交回 asyncio 那边。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import rclpy
from nav_msgs.msg import Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import PointCloud2

from d1max_localizer import slioloc as L
from d1max_localizer import superlio as S
from d1max_localizer.backend import cloud_xyz, valid_points
from d1max_localizer.core import Estimate

COUNT_EVERY = 5


class SlioRos:
    def __init__(self, loop: asyncio.AbstractEventLoop, *, lidar_topic: str,
                 on_scan: Callable[..., None], on_estimate: Callable[[Estimate], None],
                 ext: L.Extrinsic) -> None:
        del lidar_topic                                   # Super-LIO 只用前雷达:打分也只认它
        self._loop = loop
        self._ext = ext
        self.scorer = L.ScanScorer()
        from d1max_localizer import rosinit
        rosinit.init(rclpy)
        self.node = rclpy.create_node("d1max_localizer")
        best_effort = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
                                 history=HistoryPolicy.KEEP_LAST)
        self._on_scan, self._on_estimate, self._n = on_scan, on_estimate, 0
        self.node.create_subscription(Odometry, L.POSE_TOPIC, self._on_pose, 10)
        self.node.create_subscription(PointCloud2, S.LIDAR_TOPIC, self._scan, best_effort,
                                      raw=True)
        self._exec = SingleThreadedExecutor()
        self._exec.add_node(self.node)
        self._thread = threading.Thread(target=self._spin, name="rclpy", daemon=True)

    def set_fit(self, fit: L.MapFit) -> None:
        self.scorer.set_fit(fit)

    async def relocalize(self, *_: Any) -> bool:          # 后端不调(没有服务)
        return False

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        """先等 rclpy 线程退出再收(工单 1,同 :meth:`MolaRos.shutdown`)。"""
        self._exec.shutdown(timeout_sec=2.0)
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    # ------------------------------------------------------------ rclpy 线程里

    def _spin(self) -> None:
        try:
            self._exec.spin()
        except Exception:  # noqa: BLE001 —— 收尾时 executor 被关,照常退出
            pass

    def _scan(self, raw: bytes) -> None:
        self._n += 1
        valid = None
        if self._n % COUNT_EVERY == 0:
            try:
                msg = deserialize_message(raw, PointCloud2)
                xyz = cloud_xyz(msg)
                valid = valid_points(msg, xyz=xyz)
                st = msg.header.stamp
                self.scorer.scan(st.sec + st.nanosec * 1e-9, xyz)
            except Exception:  # noqa: BLE001 - 这一帧数不了:当没数,不挡「点云在来」
                valid = None
        self._loop.call_soon_threadsafe(self._on_scan, valid)

    def _on_pose(self, m: Odometry) -> None:
        s = m.header.stamp
        stamp = s.sec + s.nanosec * 1e-9
        pp, oo = m.pose.pose.position, m.pose.pose.orientation
        lp, lq = L.lidar_pose((pp.x, pp.y, pp.z), (oo.x, oo.y, oo.z, oo.w), self._ext)
        quality = self.scorer.pose(stamp, lp, lq)
        self._loop.call_soon_threadsafe(
            self._on_estimate, Estimate(stamp=stamp, p=lp, q=lq, quality=quality))
