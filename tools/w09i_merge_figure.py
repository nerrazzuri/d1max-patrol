#!/usr/bin/env python3
"""W09i 前后雷达合并,画出来看:一帧里两台雷达各看见什么、只用前雷达 vs 前后合并累积出来的点、两张图的
地面栅格。真机录包、仿真录包(``tools/w09i_sim_bag.py``)都能用。要 ROS 的系统 Python(读录包)和
matplotlib。

    source /opt/ros/humble/setup.bash
    export PYTHONPATH=packages/localizer/src:packages/contract/src:$PYTHONPATH
    python3 tools/w09i_merge_figure.py \\
        --bag runs/w09i-sim/bag --traj runs/w09i-sim/map_front.work/traj.tum \\
        --frames runs/w09i-sim/map_front/frames.json --lidars runs/w09i-sim/lidars.json \\
        --maps runs/w09i-sim/map_front runs/w09i-sim/map_merged --out runs/w09i-sim/merge.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from d1max_localizer import dualbag
from d1max_localizer.frames import Frames
from d1max_localizer.lidars import load
from d1max_localizer.merge import transform, voxel_down
from d1max_localizer.replay import read_tum


def _pgm(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    parts = raw.split(maxsplit=4)
    w, h = int(parts[1]), int(parts[2])
    return np.frombuffer(parts[4][-w * h:], dtype=np.uint8).reshape(h, w)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bag", type=Path, required=True)
    ap.add_argument("--traj", type=Path, required=True, help="前雷达建图轨迹(TUM)")
    ap.add_argument("--frames", type=Path, required=True, help="那张图的 frames.json")
    ap.add_argument("--lidars", type=Path, required=True)
    ap.add_argument("--maps", type=Path, nargs="*", default=[], help="版本目录(画 floor.pgm)")
    ap.add_argument("--every", type=int, default=3)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for fp in sorted(Path("/usr/share/fonts").rglob("*CJK*")):   # 中文字体按文件加(按名字常找不到)
        font_manager.fontManager.addfont(str(fp))
        plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(fp)).get_name()]
        break
    plt.rcParams["axes.unicode_minus"] = False

    f = Frames.load(a.frames)
    L = np.array(f.level_matrix())
    lid = load(a.lidars, up=f.sensor_up, forward=f.sensor_forward)
    T = np.asarray(lid.T_front_rear)
    frames = dualbag.read_frames(a.bag, read_tum(a.traj), every=a.every)
    print(f"{len(frames)} 帧")
    front = np.vstack([transform(Tf, voxel_down(pf, 0.1)) for Tf, pf, _, _ in frames]) @ L.T
    rear = np.vstack([transform(Tr @ T, voxel_down(pr, 0.1)) for _, _, Tr, pr in frames]) @ L.T
    ground = np.percentile(np.concatenate([front[:, 2], rear[:, 2]]), 5)

    def obstacles(p: np.ndarray) -> np.ndarray:
        h = p[:, 2] - ground
        return p[(h > 0.15) & (h < 2.0)]

    fo, ro = obstacles(front), obstacles(rear)
    n = 3 + len(a.maps)
    fig, ax = plt.subplots(1, n, figsize=(6 * n, 5.4))
    # 1:倒数某一帧(仿真里是倒着走那段),两台雷达各自看见的
    Tf, pf, Tr, pr = frames[-len(frames) // 10]
    one_f = obstacles(transform(Tf, pf) @ L.T)
    one_r = obstacles(transform(Tr @ T, pr) @ L.T)
    here = (L @ Tf[:3, 3])[:2]
    ax[0].scatter(one_f[:, 0], one_f[:, 1], s=1, c="#2b6cb0", label=f"前雷达 {len(one_f)} 点")
    ax[0].scatter(one_r[:, 0], one_r[:, 1], s=1, c="#dd6b20", label=f"后雷达 {len(one_r)} 点")
    ax[0].plot(*here, "k^", ms=9)
    ax[0].annotate("狗(前雷达)", here, textcoords="offset points", xytext=(6, 6), fontsize=8)
    ax[0].set_title("一帧:两台雷达各看一个半球")
    ax[0].legend(loc="upper right", markerscale=6, fontsize=8)
    path = np.array([L @ Tf_[:3, 3] for Tf_, _, _, _ in frames])
    for i, (pts, title) in enumerate(((fo, "只用前雷达,累积"),
                                      (np.vstack([fo, ro]), "前后雷达合并,累积"))):
        k = ax[1 + i]
        k.scatter(pts[:, 0], pts[:, 1], s=0.3, c="#2d3748")
        if i:
            k.scatter(ro[:, 0], ro[:, 1], s=0.3, c="#dd6b20", label="后雷达补上的")
            k.legend(loc="upper right", markerscale=12, fontsize=8)
        k.plot(path[:, 0], path[:, 1], "-", c="#38a169", lw=1)
        k.set_title(f"{title}({len(pts)} 点,离地 0.15–2 m)")
    for i, d in enumerate(a.maps):
        img = _pgm(d / "floor.pgm")
        ax[3 + i].imshow(img, cmap="gray", origin="upper")
        ax[3 + i].set_title(f"地面栅格:{d.name}")
        ax[3 + i].set_xticks([])
        ax[3 + i].set_yticks([])
    for k in ax[:3]:
        k.set_aspect("equal")
        k.grid(alpha=0.2)
    fig.tight_layout()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, dpi=110)
    print(f"画好了 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
