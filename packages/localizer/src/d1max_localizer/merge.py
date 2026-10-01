"""前后雷达合并(W09i 设计稿 §3):后雷达的帧按 ``T_front_rear`` 换进前雷达系、
跟时间最近的前雷达帧拼起来、
体素降采样。下游(MOLA、``frames.json``、定位器、建图预览、感知)看到的还是「前雷达系的一帧」,只是两头都有。

- 配对:后雷达帧跟前雷达帧的时间戳差 ≤ :data:`PAIR_S`;配不上的前雷达帧照样出(只有前雷达那份 —— 降级,
  不断)。
- 运动补偿(可选):给了 ``rel_pose(t_front, t_rear)``(那段时间里前雷达系的相对运动,4×4)就补;不给不补
  (在线 ≤ 50 ms × 0.6 m/s = 3 cm,写进已知限制)。
- 核心纯 numpy;ROS 节点在 :func:`main`。
"""

from __future__ import annotations

import argparse
import logging
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PAIR_S = 0.05
VOXEL_M = 0.05


def transform(T: Any, pts: Any) -> Any:
    import numpy as np
    T = np.asarray(T, dtype=float)
    p = np.asarray(pts, dtype=float)
    return p @ T[:3, :3].T + T[:3, 3]


def voxel_down(pts: Any, voxel: float = VOXEL_M) -> Any:
    """每个体素留一个点(先到的),顺序不保证。"""
    import numpy as np
    p = np.asarray(pts, dtype=float)
    if len(p) == 0 or voxel <= 0:
        return p
    keys = np.floor(p / voxel).astype(np.int64)
    _, idx = np.unique(keys, axis=0, return_index=True)
    return p[np.sort(idx)]


def merge_pair(front: Any, rear: Any, T_front_rear: Any, *, rel: Any = None,
               voxel: float = VOXEL_M) -> Any:
    """一帧前雷达 + 一帧后雷达 → 前雷达系里的一帧。``rel``:后雷达那一刻的前雷达系 →
    前雷达帧那一刻的前雷达系
    (4×4;不给 = 当没动)。"""
    import numpy as np
    r = transform(T_front_rear, rear)
    if rel is not None:
        r = transform(rel, r)
    return voxel_down(np.vstack([np.asarray(front, dtype=float), r]), voxel)


class Merger:
    """流式配对:前、后雷达的帧各自喂进来,前雷达帧一到就出(配得上就拼)。"""

    def __init__(self, T_front_rear: Any, *, pair_s: float = PAIR_S, voxel: float = VOXEL_M,
                 rel_pose: Callable[[float, float], Any] | None = None, keep: int = 5) -> None:
        self.T = T_front_rear
        self.pair_s, self.voxel, self.rel_pose = pair_s, voxel, rel_pose
        self._rear: deque = deque(maxlen=keep)       # (时刻, 点)
        self.merged = self.alone = 0

    def on_rear(self, t: float, pts: Any) -> None:
        self._rear.append((t, pts))

    def on_front(self, t: float, pts: Any) -> Any:
        best = None
        for tr, pr in self._rear:
            if abs(tr - t) <= self.pair_s and (best is None or abs(tr - t) < abs(best[0] - t)):
                best = (tr, pr)
        if best is None:
            self.alone += 1
            return voxel_down(pts, self.voxel)
        self.merged += 1
        rel = self.rel_pose(t, best[0]) if self.rel_pose is not None else None
        return merge_pair(pts, best[1], self.T, rel=rel, voxel=self.voxel)


def main(argv: Sequence[str] | None = None) -> int:
    """ROS 节点(系统 Python;``d1max-lidar-merge.service``):订前后雷达,发 ``/d1max/merged_lidar``
    (前雷达系、``frame_id`` 照前雷达的)。**后雷达外参没标过就只转发前雷达**(几何初值拼出来的会重影,
    定位器吃了反而坏事);后雷达没数据也只发前雷达那份(降级,不断)。"""
    ap = argparse.ArgumentParser(prog="d1max-lidar-merge")
    ap.add_argument("--front-topic", default="/front_lidar")
    ap.add_argument("--rear-topic", default="/rear_lidar")
    ap.add_argument("--out-topic", default="/d1max/merged_lidar")
    ap.add_argument("--lidars", type=Path, default=Path("/etc/d1max/lidars.json"),
                    help="后雷达外参(calibrate-rear 写的)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import json

    import numpy as np
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2, PointField
    from sensor_msgs_py import point_cloud2

    from d1max_localizer.build import cloud_xyz
    from d1max_localizer.lidars import Lidars, LidarsError

    T = None
    try:
        lid = Lidars.from_json(json.loads(a.lidars.read_text("utf-8")))
        if lid.calibrated:
            T = np.asarray(lid.T_front_rear)
        else:
            log.warning("%s 没标过:只转发前雷达", a.lidars)
    except (OSError, ValueError, LidarsError) as exc:
        log.warning("后雷达外参读不了(%s):只转发前雷达", exc)
    m = Merger(T if T is not None else np.eye(4))
    rclpy.init(args=None)
    node = rclpy.create_node("d1max_lidar_merge")
    qos = QoSProfile(depth=2, history=HistoryPolicy.KEEP_LAST,
                     reliability=ReliabilityPolicy.BEST_EFFORT)
    pub = node.create_publisher(PointCloud2, a.out_topic, qos)
    fields = [PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1)
              for i, n in enumerate("xyz")]

    def stamp(msg: Any) -> float:
        return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9

    def on_rear(raw: bytes) -> None:
        try:
            msg = deserialize_message(raw, PointCloud2)
            m.on_rear(stamp(msg), cloud_xyz(msg))
        except Exception:
            log.exception("后雷达这一帧处理不了")

    def on_front(raw: bytes) -> None:
        try:
            msg = deserialize_message(raw, PointCloud2)
            out = m.on_front(stamp(msg), cloud_xyz(msg))
            pub.publish(point_cloud2.create_cloud(msg.header, fields, out.astype(np.float32)))
        except Exception:
            log.exception("这一帧处理不了")

    if T is not None:
        node.create_subscription(PointCloud2, a.rear_topic, on_rear, qos, raw=True)
    node.create_subscription(PointCloud2, a.front_topic, on_front, qos, raw=True)
    log.info("合并:%s%s → %s", a.front_topic, f" + {a.rear_topic}" if T is not None else "(只转发)",
             a.out_topic)
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    try:
        while rclpy.ok():
            ex.spin_once(timeout_sec=0.5)
    finally:
        ex.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
