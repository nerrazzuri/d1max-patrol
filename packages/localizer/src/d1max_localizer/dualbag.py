"""录包里的前后雷达(W09i 设计稿 §2–3):读成标定要的帧、写合并话题的录包。要 ROS 的系统 Python
(``rosbag2_py``)。

- :func:`read_frames`:前雷达建图轨迹(TUM,前雷达在建图系里的位姿)+ 录包 → 标定的帧
  ``(前雷达帧那一刻的位姿, 前雷达的点, 后雷达帧那一刻的位姿, 后雷达的点)``;位姿按时刻在轨迹里线性插
  (朝向球面插),轨迹外的帧不要。
- :func:`write_merged`:照抄录包,另加一个合并话题(前雷达系、``frame_id`` 照前雷达的);给了轨迹就按它
  补两台雷达之间的运动。
- :func:`stamp_offset`:两台雷达消息头时刻的差(同一台主机打的戳的话应该在一帧以内)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MERGED_TOPIC = "/d1max/merged_lidar"
REAR_TOPIC = "/rear_lidar"
FRONT_TOPIC = "/front_lidar"


def _reader(bag: Path, topics: Sequence[str]) -> Any:
    import rosbag2_py
    storage = "mcap" if any(Path(bag).glob("*.mcap")) else "sqlite3"
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=storage),
           rosbag2_py.ConverterOptions("", ""))
    if topics:
        r.set_filter(rosbag2_py.StorageFilter(topics=list(topics)))
    return r


def _stamp(msg: Any) -> float:
    return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9


class Trajectory:
    """TUM 轨迹 → 任意时刻的 4×4 位姿(线性插、球面插;轨迹外是 None)。"""

    def __init__(self, traj: Sequence[Any]) -> None:
        import numpy as np
        self.t = np.array([float(t) for t, _, _ in traj])
        self.p = np.array([list(p) for _, p, _ in traj], dtype=float)
        self.q = np.array([list(q) for _, _, q in traj], dtype=float)   # x, y, z, w

    def at(self, t: float) -> Any:
        import numpy as np

        from d1max_localizer.frames import quat_to_mat
        if len(self.t) == 0 or t < self.t[0] or t > self.t[-1]:
            return None
        k = int(min(max(np.searchsorted(self.t, t), 1), len(self.t) - 1))
        t0, t1 = self.t[k - 1], self.t[k]
        f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        q0, q1 = self.q[k - 1], self.q[k]
        if float(q0 @ q1) < 0:
            q1 = -q1
        q = (1 - f) * q0 + f * q1                    # 相邻两帧很近:归一化线性插就够
        q = q / np.linalg.norm(q)
        T = np.eye(4)
        T[:3, :3] = np.array(quat_to_mat(tuple(q)))
        T[:3, 3] = (1 - f) * self.p[k - 1] + f * self.p[k]
        return T


def read_frames(bag: Path, traj: Sequence[Any], *, front_topic: str = FRONT_TOPIC,
                rear_topic: str = REAR_TOPIC, every: int = 5, pair_s: float = 0.05
                ) -> list[tuple[Any, Any, Any, Any]]:
    """标定用的帧(每 ``every`` 个前雷达帧取一个,配时刻最近的后雷达帧)。"""
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2

    from d1max_localizer.build import cloud_xyz
    tr = Trajectory(traj)
    r = _reader(bag, (front_topic, rear_topic))
    fronts: list[tuple[float, Any]] = []
    rears: list[tuple[float, Any]] = []
    i = -1
    while r.has_next():
        topic, raw, _ = r.read_next()
        msg = deserialize_message(raw, PointCloud2)
        if topic == front_topic:
            i += 1
            if i % every == 0:
                fronts.append((_stamp(msg), cloud_xyz(msg)))
        else:
            rears.append((_stamp(msg), cloud_xyz(msg)))
    out = []
    rt = [t for t, _ in rears]
    for tf, pf in fronts:
        if not rt:
            break
        j = min(range(len(rt)), key=lambda k: abs(rt[k] - tf))
        if abs(rt[j] - tf) > pair_s:
            continue
        Tf, Tr = tr.at(tf), tr.at(rt[j])
        if Tf is None or Tr is None:
            continue
        out.append((Tf, pf, Tr, rears[j][1]))
    return out


def stamp_offset(bag: Path, *, front_topic: str = FRONT_TOPIC, rear_topic: str = REAR_TOPIC,
                 n: int = 400) -> float | None:
    """后雷达每一帧跟时刻最近的前雷达帧,消息头时刻差的中位数(秒)。两台都是同一台主机打的戳时,
    应该在半帧(≤ 50 ms)以内;差得多说明有一台用了自己的钟(``use_lidar_clock``),配对前要先挪。"""
    from d1max_localizer.livemap import cdr_stamp
    r = _reader(bag, (front_topic, rear_topic))
    f, b = [], []
    while r.has_next() and len(f) + len(b) < n:
        topic, raw, _ = r.read_next()
        (f if topic == front_topic else b).append(cdr_stamp(raw))
    if not f or not b:
        return None
    d = sorted(min((tb - tf for tf in f), key=abs) for tb in b)
    return d[len(d) // 2]


def merged_messages(bag: Path, T_front_rear: Any, *, traj: Sequence[Any] | None = None,
                    front_topic: str = FRONT_TOPIC, rear_topic: str = REAR_TOPIC,
                    voxel: float = 0.05) -> Iterator[tuple[int, Any]]:
    """按录包里的次序出 ``(录包时刻 ns, 合并后的 PointCloud2)``(每个前雷达帧一条)。"""
    import numpy as np
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2, PointField
    from sensor_msgs_py import point_cloud2

    from d1max_localizer.build import cloud_xyz
    from d1max_localizer.merge import Merger
    tr = Trajectory(traj) if traj else None

    def rel(tf: float, trr: float) -> Any:
        a, b = tr.at(tf), tr.at(trr)
        return None if a is None or b is None else np.linalg.inv(a) @ b

    m = Merger(np.asarray(T_front_rear, float), voxel=voxel,
               rel_pose=rel if tr is not None else None)
    fields = [PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1)
              for i, n in enumerate("xyz")]
    pending: list[tuple[int, Any]] = []                  # 前雷达帧等它之后 50 ms 内的后雷达帧
    r = _reader(bag, (front_topic, rear_topic))

    def flush(upto: float) -> Iterator[tuple[int, Any]]:
        while pending and _stamp(pending[0][1]) + m.pair_s < upto:
            t_ns, msg = pending.pop(0)
            out = m.on_front(_stamp(msg), cloud_xyz(msg))
            yield t_ns, point_cloud2.create_cloud(msg.header, fields, out.astype(np.float32))

    while r.has_next():
        topic, raw, t_ns = r.read_next()
        msg = deserialize_message(raw, PointCloud2)
        if topic == rear_topic:
            m.on_rear(_stamp(msg), cloud_xyz(msg))
        else:
            pending.append((t_ns, msg))
        yield from flush(_stamp(msg))
    yield from flush(math.inf)
    log.info("合并:%d 帧拼上了后雷达、%d 帧只有前雷达", m.merged, m.alone)


def write_merged(bag: Path, out: Path, T_front_rear: Any, *, traj: Sequence[Any] | None = None,
                 front_topic: str = FRONT_TOPIC, rear_topic: str = REAR_TOPIC,
                 out_topic: str = MERGED_TOPIC) -> int:
    """照抄 ``bag`` 到 ``out``(sqlite3),另加 ``out_topic``。回合并了几帧。"""
    import rosbag2_py
    from rclpy.serialization import serialize_message

    w = rosbag2_py.SequentialWriter()
    w.open(rosbag2_py.StorageOptions(uri=str(out), storage_id="sqlite3"),
           rosbag2_py.ConverterOptions("", ""))
    r = _reader(bag, ())
    for t in r.get_all_topics_and_types():
        w.create_topic(rosbag2_py.TopicMetadata(name=t.name, type=t.type,
                                                serialization_format="cdr"))
    w.create_topic(rosbag2_py.TopicMetadata(name=out_topic, type="sensor_msgs/msg/PointCloud2",
                                            serialization_format="cdr"))
    while r.has_next():
        topic, raw, t_ns = r.read_next()
        w.write(topic, raw, t_ns)
    n = 0
    for t_ns, msg in merged_messages(bag, T_front_rear, traj=traj, front_topic=front_topic,
                                     rear_topic=rear_topic):
        w.write(out_topic, serialize_message(msg), t_ns)
        n += 1
    del w                                                # 关掉才落盘
    return n
