"""``d1max-loc``:开发机上的定位工具(W09b)。要 MOLA 的子命令先
``source /opt/ros/humble/setup.bash``。

- ``calibrate TRAJ.tum --out frames.json``:从建图轨迹标 ``frames.json``(放进先验目录,跟
  ``prior.mm`` 一起);
- ``replay --bag B --prior DIR --init x,y,yaw --out OUT``:只定位回放一个录包,出 ``metrics.json``、
  ``report.md``;``--reference`` 给参考轨迹(TUM)算误差,不在先验的系里就加
  ``--reference-other-frame``(按时间对齐);``--reuse`` 用 ``OUT`` 里已有的 MOLA 输出,只重算;
- ``sweep --bag B --prior DIR --init x,y,yaw --out OUT --only-first-n N``:初值加偏差各跑一遍,出
  「初值要多准」的表(手机「设位置」提示用)。
- ``calibrate-rear --bag B --traj TRAJ.tum --out lidars.json``(W09i):前雷达建图轨迹当参照,标后雷达
  相对前雷达的外参;过了守门才写(``calibrated: true``)。要 ROS 的系统 Python。
- ``merge-bag --bag B --out B2``(W09i):照抄录包,另加前后雷达合并的话题 ``/d1max/merged_lidar``
  (``build --lidar-topic /d1max/merged_lidar`` 拿它建图);``--traj`` 给了就补两台之间的运动。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Sequence
from pathlib import Path

from d1max_localizer import replay
from d1max_localizer.backend import FRAMES_FILE
from d1max_localizer.frames import Frames, calibrate

#: sweep 的偏差:(沿 x 的米, 沿 y 的米, 朝向的度)。
SWEEP = ((0.5, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (0.0, 2.0, 0.0),
         (0.0, 0.0, 15.0), (0.0, 0.0, 30.0), (0.0, 0.0, 45.0), (1.0, 0.0, 15.0))
CONVERGED_M = 0.3


def _floats(s: str, n: int) -> tuple[float, ...]:
    v = tuple(float(c) for c in s.split(","))
    if len(v) != n or not all(math.isfinite(c) for c in v):
        raise argparse.ArgumentTypeError(f"要 {n} 个有限数,逗号隔开:{s!r}")
    return v


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="d1max-loc", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate", help="从建图轨迹标 frames.json")
    c.add_argument("traj", type=Path)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--sensor-up", type=lambda s: _floats(s, 3), default=(-1.0, 0.0, 0.0),
                   help="雷达系里的「上」。C40011 的 RS-Airy 是 X **朝下**装的(W11 #64:录包里"
                        "地面在 +X 0.5 m;按 +X 标出来的地图是镜像的,跟雷达自带陀螺的转向反相关 "
                        "−0.98)。"
                        "建图流水线(build.orient)按点云判,不靠这个")
    c.add_argument("--forward-hint", type=lambda s: _floats(s, 3), default=(0.0, 0.0, 1.0),
                   help="装法上雷达系里的「朝前」(RS-Airy 是 Z),拿来核")
    c.add_argument("--sensor-in-base", type=lambda s: _floats(s, 2), default=(0.4043, 0.0),
                   help="雷达在狗身上的水平偏移(朝前,朝左,米;前雷达按 SDK 文档 2.10 节)")
    b = sub.add_parser("build", help="从录包建一个地图版本(MOLA 建图 + 打包)")
    b.add_argument("--bag", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True, help="版本文件放这儿")
    b.add_argument("--work", type=Path, default=None, help="中间文件(默认 <out>.work)")
    b.add_argument("--lidar-topic", default="/front_lidar")
    b.add_argument("--prior-pack", default="none", help="none 或 regroup:<体素米>:<范围倍数>")
    b.add_argument("--reuse", action="store_true", help="用 --work 里已有的 MOLA 输出,只打包")
    b.add_argument("--rtk-antenna", type=lambda s: _floats(s, 2), default=(0.0, 0.0),
                   help="RTK 天线在狗身上的水平位置(朝前,朝左,米;W09e 配经纬度用,真机量)")
    cr = sub.add_parser("calibrate-rear", help="标后雷达外参(W09i)")
    cr.add_argument("--bag", type=Path, required=True)
    cr.add_argument("--traj", type=Path, required=True,
                    help="前雷达建图轨迹(build 的 work/traj.tum)")
    cr.add_argument("--out", type=Path, required=True, help="lidars.json(狗上 /etc/d1max/)")
    cr.add_argument("--init", type=Path, default=None, help="初值的 lidars.json(默认几何初值)")
    cr.add_argument("--every", type=int, default=5, help="每几个前雷达帧取一个")
    for x in (cr,):
        x.add_argument("--sensor-up", type=lambda s: _floats(s, 3), default=(-1.0, 0.0, 0.0),
                       help="前雷达系里的「上」(几何初值用;C40011 是 X 朝下,#64)")
        x.add_argument("--forward-hint", type=lambda s: _floats(s, 3), default=(0.0, 0.0, 1.0))
        x.add_argument("--front-topic", default="/front_lidar")
        x.add_argument("--rear-topic", default="/rear_lidar")
    mb = sub.add_parser("merge-bag", help="录包加前后雷达合并的话题(W09i)")
    mb.add_argument("--bag", type=Path, required=True)
    mb.add_argument("--out", type=Path, required=True)
    mb.add_argument("--lidars", type=Path, default=None, help="lidars.json(没有 = 几何初值)")
    mb.add_argument("--traj", type=Path, default=None, help="前雷达建图轨迹:补两台之间的运动")
    mb.add_argument("--sensor-up", type=lambda s: _floats(s, 3), default=(-1.0, 0.0, 0.0))
    mb.add_argument("--forward-hint", type=lambda s: _floats(s, 3), default=(0.0, 0.0, 1.0))
    mb.add_argument("--front-topic", default="/front_lidar")
    mb.add_argument("--rear-topic", default="/rear_lidar")
    for name in ("replay", "sweep"):
        r = sub.add_parser(name)
        r.add_argument("--bag", type=Path, required=True)
        r.add_argument("--prior", type=Path, required=True, help="先验目录(prior.mm、frames.json)")
        r.add_argument("--init", type=lambda s: _floats(s, 3), required=True,
                       help="初值:地图平面上的 x,y,yaw(米、弧度)")
        r.add_argument("--out", type=Path, required=True)
        r.add_argument("--lidar-topic", default="/front_lidar")
        r.add_argument("--only-first-n", type=int, default=None)
        r.add_argument("--skip-first-n", type=int, default=None)
        if name == "replay":
            r.add_argument("--reference", type=Path, default=None, help="参考轨迹(TUM)")
            r.add_argument("--reference-other-frame", action="store_true",
                           help="参考轨迹不在先验的系里(独立建图):按时间配对后平面对齐")
        r.add_argument("--reuse", action="store_true", help="用 --out 里已有的 MOLA 输出,只重算")
    a = p.parse_args(argv)
    if a.cmd == "calibrate":
        return _calibrate(a)
    if a.cmd == "replay":
        return _replay(a)
    if a.cmd == "build":
        return _build(a)
    if a.cmd == "calibrate-rear":
        return _calibrate_rear(a)
    if a.cmd == "merge-bag":
        return _merge_bag(a)
    return _sweep(a)


def _calibrate_rear(a: argparse.Namespace) -> int:
    import time

    import numpy as np

    from d1max_localizer import calib, dualbag
    from d1max_localizer.lidars import Lidars, geometry_guess, load
    T0 = np.array(load(a.init, up=a.sensor_up, forward=a.forward_hint).T_front_rear if a.init
                  else geometry_guess(a.sensor_up, a.forward_hint))
    off = dualbag.stamp_offset(a.bag, front_topic=a.front_topic, rear_topic=a.rear_topic)
    print(f"两台雷达消息头时刻差(中位数):{off if off is None else round(off, 4)} s")
    if off is not None and abs(off) > 0.05:
        print("差得比半帧还多:有一台用了自己的钟?先核时间同步再标")
        return 1
    frames = dualbag.read_frames(a.bag, replay.read_tum(a.traj), front_topic=a.front_topic,
                                 rear_topic=a.rear_topic, every=a.every)
    print(f"{len(frames)} 帧配上了轨迹与后雷达")
    r = calib.calibrate(frames, T0)
    print(f"对上 {r.inlier:.0%}、残差中位数 {r.median_m * 100:.1f} cm、离初值 {r.shift_m:.3f} m / "
          f"{r.turn_deg:.2f}°:{r.why}")
    if not r.ok:
        print("没写文件")
        return 1
    Lidars(T_front_rear=[[float(v) for v in row] for row in r.T], calibrated=True,
           residual_m=round(r.median_m, 4), stamp_offset_s=off,
           calibrated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
           note=f"{a.bag.name}:{r.frames} 帧").save(a.out)
    print(f"写好了 {a.out}")
    return 0


def _merge_bag(a: argparse.Namespace) -> int:
    from d1max_localizer import dualbag
    from d1max_localizer.lidars import Lidars, geometry_guess, load
    lid = (load(a.lidars, up=a.sensor_up, forward=a.forward_hint) if a.lidars
           else Lidars(T_front_rear=geometry_guess(a.sensor_up, a.forward_hint)))
    if not lid.calibrated:
        print("后雷达外参没标定(几何初值):合并出来的图可能重影,先 calibrate-rear")
    n = dualbag.write_merged(a.bag, a.out, lid.T_front_rear,
                             traj=replay.read_tum(a.traj) if a.traj else None,
                             front_topic=a.front_topic, rear_topic=a.rear_topic)
    print(f"写好了 {a.out}:{n} 帧合并点云在 {dualbag.MERGED_TOPIC}")
    return 0


def _build(a: argparse.Namespace) -> int:
    from d1max_localizer import build
    work = a.work or a.out.with_name(a.out.name + ".work")
    timings = {} if a.reuse else build.run_mapping(a.bag, work, lidar_topic=a.lidar_topic)
    files = build.package(work, a.out, prior_pack=build.PriorPack.parse(a.prior_pack),
                          source=f"bag:{a.bag.name}", timings=timings, bag=a.bag,
                          lidar_topic=a.lidar_topic, rtk_antenna=tuple(a.rtk_antenna))
    print(json.dumps(json.loads((a.out / "build.json").read_text()), ensure_ascii=False,
                     indent=2))
    print("文件:", ", ".join(files))
    return 0


def _calibrate(a: argparse.Namespace) -> int:
    poses = [(pp, q) for _, pp, q in replay.read_tum(a.traj)]
    f, why = calibrate(poses, sensor_up=a.sensor_up, forward_hint=a.forward_hint, explain=True)
    f = f.with_sensor_in_base(*a.sensor_in_base)
    f.save(a.out)
    print(why)
    print(f"写好了 {a.out}")
    return 0


def _replay(a: argparse.Namespace) -> int:
    frames_cfg = Frames.load(a.prior / FRAMES_FILE)
    if not a.reuse:
        replay.run_mola(a.bag, a.prior, a.init, a.out, lidar_topic=a.lidar_topic,
                        only_first_n=a.only_first_n, skip_first_n=a.skip_first_n)
    frames = replay.read_run(a.out)
    msgs = replay.simulate(frames, frames_cfg, a.init)
    view = replay.agent_view(msgs)
    ref = replay.read_tum(a.reference) if a.reference else ()
    m = replay.metrics(frames, msgs, view, frames_cfg, reference=ref,
                       reference_same_frame=not a.reference_other_frame)
    replay.write(a.out, a.bag.name, m)
    print(replay.report_md(a.bag.name, m))
    return 0


def _sweep(a: argparse.Namespace) -> int:
    frames_cfg = Frames.load(a.prior / FRAMES_FILE)
    base_dir = a.out / "base"
    if not a.reuse:
        replay.run_mola(a.bag, a.prior, a.init, base_dir, lidar_topic=a.lidar_topic,
                        only_first_n=a.only_first_n, skip_first_n=a.skip_first_n)
    base = {round(f.stamp * 1000): frames_cfg.to_map2d(f.p, f.q)
            for f in replay.read_run(base_dir)}
    rows = []
    for dx, dy, dyaw in SWEEP:
        x, y, yaw = a.init
        init = (x + dx, y + dy, yaw + math.radians(dyaw))
        d = a.out / f"off_{dx:g}_{dy:g}_{dyaw:g}"
        if not a.reuse:
            replay.run_mola(a.bag, a.prior, init, d, lidar_topic=a.lidar_topic,
                            only_first_n=a.only_first_n, skip_first_n=a.skip_first_n)
        rows.append(((dx, dy, dyaw), sweep_row(base, replay.read_run(d), frames_cfg)))
    md = ["# 初值要多准(以初值不偏那一趟为参考)", "",
          "| 偏差(x m, y m, 朝向°) | 最后 10% 的误差(m) | 对上没有 | 第一次对上用了(s) "
          "| 期间最大误差(m) |",
          "|---|---|---|---|---|"]
    for off, r in rows:
        md.append(f"| {off} | {r['final_m']} | {'是' if r['converged'] else '**否**'} | "
                  f"{r['first_s']} | {r['max_m']} |")
    (a.out / "sweep.md").write_text("\n".join(md) + "\n")
    (a.out / "sweep.json").write_text(json.dumps([{"offset": o, **r} for o, r in rows],
                                                 ensure_ascii=False, indent=2) + "\n")
    print("\n".join(md))
    return 0


def sweep_row(base: dict[int, tuple[float, float, float]], run: Sequence[replay.Frame],
              frames_cfg: Frames) -> dict[str, object]:
    """一趟偏了初值的跟不偏的比:最后 10% 的误差、对上没有(最后 10% 在线内)、第一次对上用了多久、
    期间最大误差。不用「对上之后一直在线内」:参照那一趟自己也会跳(2026-09-27 回放见),会把所有偏差
    都拖成一样久。"""
    errs = []
    for f in run:
        b = base.get(round(f.stamp * 1000))
        if b is not None:
            x, y, _ = frames_cfg.to_map2d(f.p, f.q)
            errs.append((f.stamp, math.hypot(x - b[0], y - b[1])))
    if not errs:
        return {"final_m": None, "converged": False, "first_s": None, "max_m": None}
    tail = [e for _, e in errs[-max(1, len(errs) // 10):]]
    final = statistics.median(tail)
    t0 = errs[0][0]
    first = next((round(t - t0, 1) for t, e in errs if e < CONVERGED_M), None)
    return {"final_m": round(final, 3), "converged": final < CONVERGED_M, "first_s": first,
            "max_m": round(max(e for _, e in errs), 2)}


if __name__ == "__main__":
    raise SystemExit(main())
