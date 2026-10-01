"""W11 感知回放(设计稿 §8):拿录包的前雷达跑感知核心,看自检过不过、雷达离地多高、前方净空距离、
挡的格子有多少 —— 核 #64(外参按 ``frames.json``)、看开阔处误报多不多。

用法(开发机,ROS 的系统 Python)::

    PYTHONPATH=packages/localizer/src:packages/contract/src:$PYTHONPATH \\
        python3 tools/w11_obstacle_replay.py runs/bags/coverage2-20260919T155837 \\
        --frames runs/w09b/coverage2/frames.json [--every 5]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("bag", type=Path)
    p.add_argument("--frames", required=True, type=Path)
    p.add_argument("--topic", default="/front_lidar")
    p.add_argument("--every", type=int, default=5, help="每几帧跑一帧")
    p.add_argument("--max-frames", type=int, default=0)
    a = p.parse_args(argv)

    import rosbag2_py
    from d1max_localizer.build import cloud_xyz
    from d1max_localizer.frames import Frames
    from d1max_localizer.obstacles import Config, Mount, Perception
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2

    cfg = Config()
    per = Perception(Mount.from_frames(Frames.load(a.frames)), cfg=cfg)
    storage = "mcap" if any(a.bag.glob("*.mcap")) else "sqlite3"
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(a.bag), storage_id=storage),
           rosbag2_py.ConverterOptions("", ""))
    r.set_filter(rosbag2_py.StorageFilter(topics=[a.topic]))
    n = done = 0
    clears: list[float] = []
    occs: list[int] = []
    knowns: list[float] = []
    while r.has_next():
        _, raw, t_ns = r.read_next()
        n += 1
        if n % a.every:
            continue
        g = per.on_front(cloud_xyz(deserialize_message(raw, PointCloud2)), t_ns)
        if g is None:
            continue
        done += 1
        from d1max_contract.obsbridge import unpack_bits
        occ = unpack_bits(g["occ"], cfg.size)
        known = unpack_bits(g["known"], cfg.size)
        clears.append(per.last_clear)
        occs.append(sum(occ))
        # 前方 0.5–2.5 m、左右 1 m 的扇面里看见了多少
        half = cfg.size // 2
        fan = [known[(half + i) * cfg.size + half + j]
               for i in range(5, 25) for j in range(-10, 10)]
        knowns.append(sum(fan) / len(fan))
        if a.max_frames and done >= a.max_frames:
            break
    ck = per.check
    print(f"录包 {a.bag.name}:{n} 帧,跑了 {done} 帧")
    print(f"自检:{ck.check}{('(' + ck.reason + ')') if ck.reason else ''},雷达离地 "
          f"{ck.height if ck.height is None else round(ck.height, 3)} m")
    if done:
        q = statistics.quantiles(clears, n=10) if len(clears) >= 10 else clears
        print(f"前方净空(米):中位 {statistics.median(clears):.2f},最小 {min(clears):.2f},"
              f"十分位 {[round(x, 2) for x in q]}")
        print(f"挡的格子:中位 {statistics.median(occs):.0f},最多 {max(occs)}")
        print(f"前方 0.5–2.5 m 扇面看见的比例:中位 {statistics.median(knowns):.0%},"
              f"最低 {min(knowns):.0%}")
        print(f"给许可的帧:{sum(1 for c in clears if c >= cfg.stop_dist(cfg.max_v) + 0.3)}"
              f" / {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
