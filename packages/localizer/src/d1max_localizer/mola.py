"""ROS 适配(W09b 决定 2):只在狗上(ROS 的系统 Python)与回放实跑时 import,别的模块不依赖它。

- 订阅 MOLA 的位姿 :data:`POSE_TOPIC`(``nav_msgs/Odometry``,时间戳 = 那一帧点云的时间)与每帧 ICP
  质量 :data:`QUALITY_TOPIC`(``Float32``,没有消息头),经 :class:`~d1max_localizer.backend.Pairer`
  配成一帧;
- 订阅点云只为了知道「点云在来」:用原始订阅(``raw=True``),不反序列化 2.7 MB 的点云;
- 重定位调 :data:`RELOC_SERVICE`;
- rclpy 在自己的线程里转,回调经 ``call_soon_threadsafe`` 交回 asyncio 那边(核心、客户端都在那边)。

话题、服务名与启动参数 2026-09-27 在录包上用 ROS 跑通核过(见 W09b 设计稿)。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from mola_msgs.srv import RelocalizeNearPose
from nav_msgs.msg import Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import PointCloud2
from std_msgs.msg import Float32

from d1max_localizer.backend import Pairer
from d1max_localizer.core import Estimate

POSE_TOPIC = "/lidar_odometry/pose"
QUALITY_TOPIC = "/lidar_odometry/pose_quality"
RELOC_SERVICE = "/relocalize_near_pose"
RELOC_TIMEOUT_S = 0.7
#: 重定位初值的姿态不确定度(弧度)。
RELOC_SIGMA_RAD = 0.3


class MolaRos:
    def __init__(self, loop: asyncio.AbstractEventLoop, *, lidar_topic: str,
                 on_scan: Callable[[], None], on_estimate: Callable[[Estimate], None]) -> None:
        self._loop = loop
        if not rclpy.ok():
            rclpy.init(args=None)
        self.node = rclpy.create_node("d1max_localizer")
        self._pairer = Pairer(lambda e: loop.call_soon_threadsafe(on_estimate, e))
        best_effort = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
                                 history=HistoryPolicy.KEEP_LAST)
        self.node.create_subscription(Odometry, POSE_TOPIC, self._on_pose, 10)
        self.node.create_subscription(Float32, QUALITY_TOPIC, self._on_quality, 10)
        self.node.create_subscription(PointCloud2, lidar_topic,
                                      lambda _raw: loop.call_soon_threadsafe(on_scan),
                                      best_effort, raw=True)
        self._reloc = self.node.create_client(RelocalizeNearPose, RELOC_SERVICE)
        self._exec = SingleThreadedExecutor()
        self._exec.add_node(self.node)
        self._thread = threading.Thread(target=self._spin, name="rclpy", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._exec.shutdown()
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    async def relocalize(self, p: tuple[float, float, float],
                         q: tuple[float, float, float, float], sigma: float) -> bool:
        """请 MOLA 在 ``(p, q)``(MOLA 系里雷达的位姿)附近重定位;收下回真。"""
        if not self._reloc.service_is_ready():
            return False
        req = RelocalizeNearPose.Request()
        msg: PoseWithCovarianceStamped = req.pose
        msg.header.frame_id = "map"
        msg.header.stamp = self.node.get_clock().now().to_msg()
        pos, ori = msg.pose.pose.position, msg.pose.pose.orientation
        pos.x, pos.y, pos.z = p
        ori.x, ori.y, ori.z, ori.w = q
        cov = [0.0] * 36
        for i in range(3):
            cov[i * 7] = sigma * sigma
            cov[(i + 3) * 7] = RELOC_SIGMA_RAD * RELOC_SIGMA_RAD
        msg.pose.covariance = cov
        done: asyncio.Future = self._loop.create_future()

        def _back(f: Any) -> None:
            self._loop.call_soon_threadsafe(_settle, done, f)
        self._reloc.call_async(req).add_done_callback(_back)
        res = await asyncio.wait_for(done, RELOC_TIMEOUT_S)
        return bool(res is not None and res.accepted)

    # ------------------------------------------------------------ rclpy 线程里

    def _spin(self) -> None:
        try:
            self._exec.spin()
        except Exception:  # noqa: BLE001 —— 收尾时 executor 被关,照常退出
            pass

    def _on_pose(self, m: Odometry) -> None:
        s = m.header.stamp
        pp, oo = m.pose.pose.position, m.pose.pose.orientation
        self._pairer.pose(s.sec + s.nanosec * 1e-9, (pp.x, pp.y, pp.z), (oo.x, oo.y, oo.z, oo.w),
                          at=time.monotonic())

    def _on_quality(self, m: Float32) -> None:
        self._pairer.quality(m.data, at=time.monotonic())


def _settle(fut: asyncio.Future, ros_fut: Any) -> None:
    if fut.done():
        return
    try:
        fut.set_result(ros_fut.result())
    except Exception as exc:  # noqa: BLE001 —— 服务调用失败当没收下
        fut.set_exception(exc)
