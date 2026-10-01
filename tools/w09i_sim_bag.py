#!/usr/bin/env python3
"""W09i 开发机仿真录包:前后两台半球雷达(:mod:`d1max_localizer.lidarsim`)在一个院子里,狗先狗头为前
绕一圈、再倒着(狗尾在前)走回去。出 ``/front_lidar``、``/rear_lidar``(``PointCloud2``,10 Hz)的录包、
真值轨迹(前雷达在世界系的位姿,TUM)、两台雷达的真值外参(``lidars_truth.json``)。

    source /opt/ros/humble/setup.bash
    PYTHONPATH=packages/localizer/src:packages/contract/src:$PYTHONPATH \\
        python3 tools/w09i_sim_bag.py --out runs/w09i-sim

没有狗:合并、标定、建图在这上面跑通(``docs/W09i-完工报告.md``);真机录包是真机项。
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from d1max_localizer import lidarsim as ls
from d1max_localizer.frames import mat_to_quat
from d1max_localizer.lidars import Lidars

HZ = 10.0
SPEED = 0.5                                      # m/s
TURN = 0.6                                       # rad/s


def 院子() -> list[ls.Box]:
    """20 × 14 m 的院子:四面墙、一栋小屋、几根柱子、箱子、一段矮墙。"""
    return ls.room(w=20.0, h=14.0, height=2.5, extra=(
        ls.Box((6.0, 9.0, 0.0), (10.0, 13.8, 3.0)),          # 小屋
        ls.Box((14.0, 3.0, 0.0), (14.4, 3.4, 2.2)),          # 柱子
        ls.Box((14.0, 9.0, 0.0), (14.4, 9.4, 2.2)),
        ls.Box((3.0, 3.0, 0.0), (4.0, 3.6, 0.8)),            # 箱子
        ls.Box((10.5, 5.0, 0.0), (11.3, 5.6, 1.0)),
        ls.Box((16.5, 6.0, 0.0), (19.8, 6.3, 1.1)),          # 矮墙
    ))


def 路线() -> list[tuple[float, float, float]]:
    """机身位姿 (x, y, yaw),一帧一个:绕一圈(狗头为前),再沿底边倒着走回去(狗尾为前)。"""
    pts = [(2.5, 2.0), (17.5, 2.0), (17.5, 4.8), (12.5, 7.5), (2.5, 7.5), (2.5, 2.0)]
    out: list[tuple[float, float, float]] = []
    yaw = 0.0
    x, y = pts[0]
    step = SPEED / HZ
    for nx, ny in pts[1:]:
        want = math.atan2(ny - y, nx - x)
        while abs(d := math.remainder(want - yaw, 2 * math.pi)) > 1e-6:   # 原地转
            yaw += math.copysign(min(abs(d), TURN / HZ), d)
            out.append((x, y, yaw))
        n = max(1, int(math.hypot(nx - x, ny - y) / step))
        for i in range(1, n + 1):
            out.append((x + (nx - x) * i / n, y + (ny - y) * i / n, yaw))
        x, y = nx, ny
    # 转到朝 −x(狗头朝西),然后狗尾在前往 +x 倒着走 8 m
    while abs(d := math.remainder(math.pi - yaw, 2 * math.pi)) > 1e-6:
        yaw += math.copysign(min(abs(d), TURN / HZ), d)
        out.append((x, y, yaw))
    for i in range(1, int(8.0 / step) + 1):
        out.append((x + step * i, y, yaw))
    return out


def _cloud(frame: str, t: float, pts: np.ndarray):
    from builtin_interfaces.msg import Time
    from sensor_msgs.msg import PointField
    from sensor_msgs_py import point_cloud2
    from std_msgs.msg import Header
    sec = int(t)
    h = Header(frame_id=frame, stamp=Time(sec=sec, nanosec=int(round((t - sec) * 1e9))))
    fields = [PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1)
              for i, n in enumerate("xyz")]
    return point_cloud2.create_cloud(h, fields, pts.astype(np.float32))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rays", default="48x240", help="每台雷达一帧的射线:天顶 × 方位")
    ap.add_argument("--rear-lag", type=float, default=0.02, help="后雷达比前雷达晚多少秒扫")
    a = ap.parse_args(argv)
    import rosbag2_py
    from rclpy.serialization import serialize_message

    nt, nphi = (int(v) for v in a.rays.split("x"))
    a.out.mkdir(parents=True, exist_ok=True)
    bag = a.out / "bag"
    if bag.exists():
        print(f"{bag} 已经有了", file=sys.stderr)
        return 1
    world, poses = 院子(), 路线()
    w = rosbag2_py.SequentialWriter()
    w.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="sqlite3"),
           rosbag2_py.ConverterOptions("", ""))
    for topic in ("/front_lidar", "/rear_lidar"):
        w.create_topic(rosbag2_py.TopicMetadata(name=topic, type="sensor_msgs/msg/PointCloud2",
                                                serialization_format="cdr"))
    rng = np.random.default_rng(0)
    t0 = 1_760_000_000.0
    tum = []
    for i, (x, y, yaw) in enumerate(poses):
        t = t0 + i / HZ
        Tw = ls.body_to_world(x, y, yaw)
        Twf = Tw @ ls.T_BODY_FRONT
        f = ls.scan(Twf, world, n_theta=nt, n_phi=nphi, rng=rng)
        # 后雷达晚 rear_lag 扫:那时狗又往前走了一点(按下一帧插)
        nx, ny, nyaw = poses[min(i + 1, len(poses) - 1)]
        k = a.rear_lag * HZ
        Twr = ls.body_to_world(x + (nx - x) * k, y + (ny - y) * k,
                               yaw + math.remainder(nyaw - yaw, 2 * math.pi) * k) @ ls.T_BODY_REAR
        r = ls.scan(Twr, world, n_theta=nt, n_phi=nphi, rng=rng)
        w.write("/front_lidar", serialize_message(_cloud("rslidar_head", t, f)), int(t * 1e9))
        tr = t + a.rear_lag
        w.write("/rear_lidar", serialize_message(_cloud("rslidar_tail", tr, r)), int(tr * 1e9))
        q = mat_to_quat(Twf[:3, :3].tolist())
        tum.append(f"{t:.6f} {Twf[0, 3]:.6f} {Twf[1, 3]:.6f} {Twf[2, 3]:.6f} "
                   f"{q[0]:.9f} {q[1]:.9f} {q[2]:.9f} {q[3]:.9f}")
        if i % 100 == 0:
            print(f"{i}/{len(poses)}", flush=True)
    del w
    (a.out / "traj_truth.tum").write_text("\n".join(tum) + "\n")
    Lidars(T_front_rear=ls.T_FRONT_REAR.tolist(), calibrated=True,
           note="仿真真值").save(a.out / "lidars_truth.json")
    print(f"写好了 {bag}:{len(poses)} 帧 × 2 台、{len(poses) / HZ:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
