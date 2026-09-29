#!/usr/bin/env python3
"""W09f 真实链路检查(开发机,**不碰真狗**):真的建图编排 + 代理的 ``MappingService``。

拿一个录包在隔离的 ROS 域里放、当实时雷达 —— 边走边建(``mola-cli``)+ 建图预览(``mapview``)+ 录包
一起起,边放边像手机那样读预览,放完停录、打包,看停录、出图要多久,预览在各个阶段回什么。

要本机有 ROS 2 Humble、MOLA、``rmw_zenoh_cpp``。自己起一个 zenoh 路由,跑完按进程号收掉。

用法(仓库根目录,venv 的 Python,**别带 ROS 的 PYTHONPATH**)::

    env -u PYTHONPATH .venv/bin/python tools/w09f_live_preview_check.py \\
        --bag runs/bags/newdog2-20260921T164640 --out /tmp/w09f-check [--seconds 60]

输出:每 10 s 一行(seq、累加帧数、画幅、PNG 大小……),预览图存成 ``<out>/pv_<seq>.png``,
最后一行是 JSON 小结。2026-09-30 newdog2 整包:90 张、每秒累加约 1.5 帧、最大一张 PNG 12 KB;
停录 4.3 s,停下到出图 100 s(``mode: live``)。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROS = "set +u; . /opt/ros/humble/setup.bash; exec "


def log(*a: object) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _spawn(cmd: str, out: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(["bash", "-c", ROS + cmd], stdout=open(out, "w"),
                            stderr=subprocess.STDOUT, env=env, start_new_session=True)


def _kill(p: subprocess.Popen, sig: int = signal.SIGINT) -> None:
    if p.poll() is None:
        os.killpg(p.pid, sig)
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)


async def main(a: argparse.Namespace) -> dict:
    from d1max_agent.mapping import MappingService
    from d1max_patrol.app.mapping import MappingConfig, MappingOrchestrator
    from d1max_patrol.app.procs import ProcManager

    out: Path = a.out
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ROS_DOMAIN_ID=a.domain, RMW_IMPLEMENTATION="rmw_zenoh_cpp")
    router = _spawn("ros2 run rmw_zenoh_cpp rmw_zenohd", out / "zenohd.log", env)
    await asyncio.sleep(2)
    orch = MappingOrchestrator(ProcManager(out / "logs"), MappingConfig(
        bags_dir=out / "outbox" / "bags", maps_dir=out / "maps", live_domain=a.domain,
        ros_wrapper=REPO / "deploy/d1max-ros", live_preview=REPO / "deploy/d1max-live-preview",
        map_builder=REPO / "deploy/d1max-map-build"))
    svc = MappingService(orch, bags_root=out / "outbox" / "bags", maps_out=out / "maps-out")
    summary: dict = {}
    play = None
    try:
        await svc.start("check", ("check", "1"), task_id="t-check")
        log("开录;预览起没起来:", repr(orch.preview_error) or "起来了", orch._procs.running())
        play = _spawn(f"ros2 bag play '{a.bag.resolve()}' --topics {a.lidar_topic}",
                      out / "play.log", env)
        t0, seq, biggest = time.time(), 0, 0
        while play.poll() is None and (not a.seconds or time.time() - t0 < a.seconds):
            await asyncio.sleep(10)
            alive = svc.preview_alive()
            d = await asyncio.to_thread(svc.preview, seq)
            png = d.get("png")
            if png:
                biggest = max(biggest, len(png))
                (out / f"pv_{d['seq']:03d}.png").write_bytes(base64.b64decode(png))
            seq = d.get("seq", seq)
            log(f"t={time.time() - t0:4.0f}s seq={d.get('seq')} 帧={d.get('frames')} "
                f"限速丢={d.get('dropped')} 多久前={d.get('age_s')} res={d.get('res')} "
                f"{d.get('width')}x{d.get('height')} 轨迹点={len(d.get('trail') or [])} "
                f"png_b64={len(png) if png else '-'} 预览进程在={alive}")
        _kill(play)
        await asyncio.sleep(2)
        t1 = time.time()
        await svc.stop()
        stop_s = time.time() - t1
        after_stop = svc.preview(0)
        log(f"停录 {stop_s:.1f} s;停后预览 live={after_stop.get('live')} "
            f"recording={after_stop.get('recording')} seq={after_stop.get('seq')} "
            f"带图={'png' in after_stop}")
        ref, mode = await svc.finish()
        log(f"出图 mode={mode},停下到出图 {time.time() - t1:.1f} s,{ref.map_id}:{ref.version}")
        summary = {"snapshots": seq, "biggest_png_b64": biggest, "stop_s": round(stop_s, 1),
                   "to_map_s": round(time.time() - t1, 1), "mode": mode,
                   "after_stop_has_png": "png" in after_stop,
                   "after_finish": svc.preview(0), "rays": svc.last_rays}
    finally:
        if play is not None:
            _kill(play)
        if svc.recording:
            await svc.stop()
        await orch._procs.stop_all()
        _kill(router)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bag", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--domain", default="7", help="隔离的 ROS 域(别用实时域)")
    ap.add_argument("--lidar-topic", default="/front_lidar")
    ap.add_argument("--seconds", type=float, default=0, help="只放这么多秒(0 = 整包)")
    s = asyncio.run(main(ap.parse_args()))
    print(json.dumps(s, ensure_ascii=False))
    sys.exit(0 if s.get("mode") else 1)
