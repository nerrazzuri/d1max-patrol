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
    from d1max_localizer.obstacles import Config, Mount, Perception, permit_vmax
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
    vmaxs: list[float] = []
    firsts: dict[str, int] = {}
    pic = ""
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
        vmaxs.append(permit_vmax(per.last_clear, cfg))
        # 11-04:许可走廊(机身宽 + 两边余量)里第一格挡 / 看不见的是哪一格
        n_, front_, hw = cfg.size, cfg.body_len / 2, cfg.body_wid / 2 + cfg.margin
        why, rows = "走廊全空", []
        for rr in range(n_ // 2, n_):
            x0 = (rr - n_ / 2) * cfg.res
            if x0 < front_ - 1e-9:
                continue
            line = ""
            for c in range(n_):
                y0 = (c - n_ / 2) * cfg.res
                if y0 + cfg.res <= -hw or y0 >= hw:
                    continue
                i = rr * n_ + c
                line += "#" if occ[i] else ("?" if not known[i] else ".")
                if why == "走廊全空" and (occ[i] or not known[i]):
                    why = f"{'挡' if occ[i] else '看不见'} 前 {x0:.1f} m"
            if len(rows) < 12:
                rows.append(f"  {x0:4.1f} m |{line}|")
        firsts[why] = firsts.get(why, 0) + 1
        pic = "\n".join(reversed(rows))
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
        print(f"分档许可 vmax(m/s):中位 {statistics.median(vmaxs):.2f},最大 {max(vmaxs):.2f};"
              f"< 0.05 不发的帧 {sum(1 for v in vmaxs if v < 0.05)} / {done}")
        print("走廊里第一格挡 / 看不见:" + ",".join(
            f"{k} ×{v}" for k, v in sorted(firsts.items(), key=lambda kv: -kv[1])[:6]))
        print("最后一帧的许可走廊(# 挡、? 看不见、. 空;上 = 远,下 = 紧贴机身前沿):\n" + pic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
