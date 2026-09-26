"""录包回放出指标(W09b 决定 4),开发机上跑、不要狗。

1. :func:`run_mola` 用 MOLA 命令行在录包上只定位跑一遍(先验、初值),得到每帧的位姿(``traj.tum``)与
   ICP 质量、处理耗时(调试输出 ``traces.csv``);
2. :func:`simulate` 把每帧按「时间戳 + 处理耗时」当成到达时刻,喂给**狗上同一份定位核心**,得到给
   代理的报文流;
3. :func:`agent_view` 再把报文流喂给**代理同一份** ``BridgeLocalizer``(W09a),看代理每一刻信不信;
   录包里没有腿式里程,可以拿录包里的 ``odom → base_link`` 当替身(没有就不做交叉校验);
4. :func:`metrics` 出指标,:func:`report_md` 写一页报告。有参考轨迹(同一个 MOLA 系里的建图轨迹,或者
   另一个系里的独立建图轨迹 —— 按时间配对、平面刚体对齐)就算位置误差。

MOLA 命令行在开发机上;``run_mola`` 之外都是纯 Python,测试用合成数据。
"""

from __future__ import annotations

import asyncio
import csv
import glob
import json
import math
import os
import statistics
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract.locbridge import Pose, Reply, State
from d1max_localizer.backend import FRAMES_FILE, PRIOR_FILE
from d1max_localizer.core import Estimate, LocalizerCore
from d1max_localizer.frames import Frames, quat_to_mat

MOLA_SHARE = Path("/opt/ros/humble/share/mola_lidar_odometry")
MAP_REF = ("replay", "1")


# ------------------------------------------------------------ 跑 MOLA


def mola_env_and_cmd(bag: Path, prior_dir: Path, init: tuple[float, float, float], out: Path, *,
                     lidar_topic: str = "/front_lidar", only_first_n: int | None = None,
                     skip_first_n: int | None = None) -> tuple[dict[str, str], list[str]]:
    """MOLA 命令行只定位的环境变量与命令(探路时用过的配方,见 W09b 设计稿)。"""
    frames = Frames.load(prior_dir / FRAMES_FILE)
    p, q = frames.to_mola(*init)
    yaw, pitch, roll = _ypr_deg(q)
    env = {"MOLA_LIDAR_TOPIC": lidar_topic, "MOLA_TF_BASE_LINK": _frame_of(lidar_topic),
           "MOLA_MAPPING_ENABLED": "false", "MOLA_LOAD_MM": str(prior_dir / PRIOR_FILE),
           "MOLA_SAVE_DEBUG_TRACES": "true", "MOLA_DEBUG_TRACES_FILE": str(out / "traces.csv"),
           "MOLA_INITIAL_X": f"{p[0]:.4f}", "MOLA_INITIAL_Y": f"{p[1]:.4f}",
           "MOLA_INITIAL_Z": f"{p[2]:.4f}", "MOLA_INITIAL_YAW": f"{yaw:.3f}",
           "MOLA_INITIAL_PITCH": f"{pitch:.3f}", "MOLA_INITIAL_ROLL": f"{roll:.3f}"}
    plugins = sorted(glob.glob("/opt/ros/humble/lib/**/libmola_metric_maps.so", recursive=True))
    cmd = ["mola-lidar-odometry-cli"]
    if plugins:
        cmd += ["-l", plugins[0]]                     # 载 prior.mm 要先有这个插件(探路踩过)
    cmd += ["-c", str(MOLA_SHARE / "pipelines/lidar3d-default.yaml"),
            "--state-estimator-param-file",
            str(MOLA_SHARE / "state-estimator-params/state-estimation-simple.yaml"),
            "--input-rosbag2", str(bag), "--lidar-sensor-label", lidar_topic,
            "--output-tum-path", str(out / "traj.tum"), "--progress-bar-period", "0"]
    if only_first_n:
        cmd += ["--only-first-n", str(only_first_n)]
    if skip_first_n:
        cmd += ["--skip-first-n", str(skip_first_n)]
    return env, cmd


def run_mola(bag: Path, prior_dir: Path, init: tuple[float, float, float], out: Path,
             **kw: Any) -> None:
    """跑一遍(要先 ``source /opt/ros/humble/setup.bash``)。输出在 ``out``:traj.tum、traces.csv、
    mola.log。"""
    out.mkdir(parents=True, exist_ok=True)
    env, cmd = mola_env_and_cmd(bag, prior_dir, init, out, **kw)
    with open(out / "mola.log", "w") as log:
        subprocess.run(cmd, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT,
                       check=True)


def _frame_of(topic: str) -> str:
    return "rslidar_head" if topic == "/front_lidar" else "base_link"


def _ypr_deg(q: Sequence[float]) -> tuple[float, float, float]:
    """四元数 → MOLA 初值用的 yaw、pitch、roll(度,ZYX)。"""
    m = quat_to_mat(q)
    pitch = math.asin(max(-1.0, min(1.0, -m[2][0])))
    yaw = math.atan2(m[1][0], m[0][0])
    roll = math.atan2(m[2][1], m[2][2])
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


# ------------------------------------------------------------ 读


@dataclass(frozen=True)
class Frame:
    stamp: float
    p: tuple[float, float, float]
    q: tuple[float, float, float, float]
    quality: float
    proc_s: float                                     # MOLA 处理这一帧用了多久


def read_tum(path: Path) -> list[tuple[float, tuple[float, float, float],
                                       tuple[float, float, float, float]]]:
    out = []
    for line in Path(path).read_text().splitlines():
        v = line.split()
        if len(v) != 8 or line.startswith("#"):
            continue
        f = [float(c) for c in v]
        out.append((f[0], (f[1], f[2], f[3]), (f[4], f[5], f[6], f[7])))
    return out


def read_run(out: Path) -> list[Frame]:
    """MOLA 一次运行的输出:位姿按时间戳配上质量、处理耗时(配不上的帧质量按 0、耗时按 0)。"""
    traces: dict[int, tuple[float, float]] = {}
    with open(out / "traces.csv", newline="") as f:
        for r in csv.DictReader(f):
            try:
                traces[round(float(r["timestamp"]) * 1000)] = (float(r["icp_quality"]),
                                                               float(r["time_onLidar"]))
            except (KeyError, ValueError):
                continue
    frames = []
    for stamp, p, q in read_tum(out / "traj.tum"):
        quality, proc = traces.get(round(stamp * 1000), (0.0, 0.0))
        frames.append(Frame(stamp=stamp, p=p, q=q, quality=quality, proc_s=proc))
    return frames


# ------------------------------------------------------------ 模拟


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def simulate(frames: Sequence[Frame], frames_cfg: Frames, init: tuple[float, float, float], *,
             sigma0: float = 0.5, latency_s: float = 0.0) -> list[tuple[float, Any]]:
    """按到达时刻(时间戳 + 处理耗时 + ``latency_s``)把每帧喂给定位核心;回 ``[(到达时刻, 报文)]``。
    开头当人在这里给了初值(``init``,σ ``sigma0``)。"""
    if not frames:
        return []
    clock = _Clock()
    core = LocalizerCore(monotonic=clock)
    t0 = frames[0].stamp
    clock.t = t0
    core.prior_loaded(MAP_REF, frames_cfg)
    core.backend_started()
    core.relocalized(req=1, x=init[0], y=init[1], yaw=init[2], sigma=sigma0, human=True)
    out: list[tuple[float, Any]] = []
    events = sorted(((f.stamp + f.proc_s + latency_s, f) for f in frames), key=lambda e: e[0])
    for at, f in events:
        clock.t = at
        core.on_scan()
        core.on_estimate(Estimate(stamp=f.stamp, p=f.p, q=f.q, quality=f.quality))
        core.tick()
        out.extend((at, m) for m in core.drain())
    return out


View = list[tuple[float, bool, str, int | None]]


def agent_view(msgs: Sequence[tuple[float, Any]], *,
               odom: Sequence[tuple[float, tuple[float, float, float]]] = ()) -> View:
    """把报文流喂给代理的 ``BridgeLocalizer``(W09a 同一份代码),每条报文之后记一行:
    ``(时刻, 信不信, 为什么, 这条是位姿的话它的 stamp_ns)``。
    ``odom`` 是 ``[(时刻, (x, y, yaw))]``(录包里的里程替身);空的话不做交叉校验、不推算。"""
    from d1max_agent.bridge_localizer import BridgeLocalizer

    return asyncio.run(_agent_view(BridgeLocalizer, msgs, odom))


async def _agent_view(cls: Any, msgs: Sequence[tuple[float, Any]],
                      odom: Sequence[tuple[float, tuple[float, float, float]]]) -> View:
    clock = _Clock()
    clock.t = msgs[0][0] if msgs else 0.0
    agent = cls(monotonic=clock)

    class _Link:
        async def request(self, make: Any, timeout_s: float) -> Reply:
            m = make(1)
            return Reply(req=m.req, ok=True)
    agent.link = _Link()
    agent.on_map(MAP_REF, "")
    agent.on_connect()
    await agent.prior_task
    out: View = []
    oi = 0
    for at, m in msgs:
        clock.t = at
        while oi < len(odom) and odom[oi][0] <= at:
            agent.update(odom[oi][1], True)
            oi += 1
        if isinstance(m, Pose):
            agent.on_pose(m)
        elif isinstance(m, State):
            agent.on_state(m)
        why = agent.why_not(bool(odom))
        out.append((at, not why, why, m.stamp_ns if isinstance(m, Pose) else None))
    return out


# ------------------------------------------------------------ 指标


def align2d(a: Sequence[tuple[float, float]], b: Sequence[tuple[float, float]]
            ) -> tuple[float, float, float]:
    """平面刚体对齐(闭式):求 ``(th, tx, ty)`` 使 R(th)·a + t ≈ b(一一对应)。"""
    n = len(a)
    ax, ay = sum(p[0] for p in a) / n, sum(p[1] for p in a) / n
    bx, by = sum(p[0] for p in b) / n, sum(p[1] for p in b) / n
    pq = list(zip(a, b, strict=True))
    sxx = sum((p[0] - ax) * (q[0] - bx) + (p[1] - ay) * (q[1] - by) for p, q in pq)
    sxy = sum((p[0] - ax) * (q[1] - by) - (p[1] - ay) * (q[0] - bx) for p, q in pq)
    th = math.atan2(sxy, sxx)
    c, s = math.cos(th), math.sin(th)
    return th, bx - (c * ax - s * ay), by - (s * ax + c * ay)


def _pct(v: Sequence[float], p: float) -> float:
    if not v:
        return float("nan")
    s = sorted(v)
    return s[min(len(s) - 1, int(p / 100 * (len(s) - 1) + 0.5))]


def _intervals(view: View) -> list[dict[str, Any]]:
    """代理不信的时段:[{from, to, why}](why 取这一段里最常见的原因)。"""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for t, ok, why, _ in view:
        if not ok:
            if cur is None:
                cur = {"from": t, "to": t, "whys": {}}
            cur["to"] = t
            key = why.split("(")[0].split(":")[-1][:40]
            cur["whys"][key] = cur["whys"].get(key, 0) + 1
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return [{"from": i["from"], "to": i["to"], "why": max(i["whys"], key=i["whys"].get)}
            for i in out]


def metrics(frames: Sequence[Frame], msgs: Sequence[tuple[float, Any]],
            view: View, frames_cfg: Frames, *,
            reference: Sequence[tuple[float, tuple[float, float, float],
                                      tuple[float, float, float, float]]] = (),
            reference_same_frame: bool = True) -> dict[str, Any]:
    """指标。``reference``:参考轨迹(TUM 的行);同一个 MOLA 系(建图轨迹、先验就是它建的)直接比,
    不在同一个系(另一份独立建图)就按时间配对后平面刚体对齐再比。"""
    poses = [m for _, m in msgs if isinstance(m, Pose)]
    dur = frames[-1].stamp - frames[0].stamp if len(frames) > 1 else 0.0
    t_ok = _time_share(view)
    out: dict[str, Any] = {
        "frames": len(frames),
        "duration_s": round(dur, 1),
        "rate_hz": round((len(frames) - 1) / dur, 2) if dur > 0 else 0.0,
        "proc_ms": {k: round(_pct([f.proc_s * 1000 for f in frames], p), 1)
                    for k, p in (("p50", 50), ("p95", 95), ("max", 100))},
        "quality": {k: round(_pct([f.quality for f in frames], p), 3)
                    for k, p in (("p5", 5), ("p50", 50))},
        "sigma_m": {k: round(_pct([m.sigma_xy for m in poses], p), 2)
                    for k, p in (("p50", 50), ("p95", 95))},
        "poses_sent": len(poses),
        "jumps": sum(1 for m in poses if m.jump and m.reloc_id is None),
        "localizer_states": _state_share(msgs),
        "agent_trusted_share": round(t_ok, 3),
        "agent_untrusted": [{"from_s": round(i["from"] - frames[0].stamp, 1),
                             "to_s": round(i["to"] - frames[0].stamp, 1), "why": i["why"]}
                            for i in _intervals(view)],
    }
    if reference:
        out["error_m"] = _errors(poses, view, frames_cfg, reference, reference_same_frame)
    return out


def _time_share(view: View) -> float:
    if len(view) < 2:
        return 0.0
    ok = sum(b[0] - a[0] for a, b in zip(view, view[1:], strict=False) if a[1])
    return ok / (view[-1][0] - view[0][0]) if view[-1][0] > view[0][0] else 0.0


def _state_share(msgs: Sequence[tuple[float, Any]]) -> dict[str, float]:
    states = [(t, m.state) for t, m in msgs if isinstance(m, State)]
    if not msgs or not states:
        return {}
    end = msgs[-1][0]
    total: dict[str, float] = {}
    for (t, s), nxt in zip(states, [*states[1:], (end, "")], strict=True):
        total[s] = total.get(s, 0.0) + max(0.0, nxt[0] - t)
    span = sum(total.values()) or 1.0
    return {k: round(v / span, 3) for k, v in total.items()}


def _errors(poses: Sequence[Pose], view: View, frames_cfg: Frames,
            reference: Sequence[Any], same_frame: bool) -> dict[str, Any]:
    ref = {round(t * 1000): frames_cfg.to_map2d(p, q) for t, p, q in reference}
    pairs = []
    for m in poses:
        r = ref.get(round(m.stamp_ns / 1e6))
        if r is not None:
            pairs.append((m, r))
    if len(pairs) < 3:
        return {"matched": len(pairs)}
    if not same_frame:
        th, tx, ty = align2d([(r[0], r[1]) for _, r in pairs], [(m.x, m.y) for m, _ in pairs])
        c, s = math.cos(th), math.sin(th)
        pairs = [(m, (c * r[0] - s * r[1] + tx, s * r[0] + c * r[1] + ty, r[2] + th))
                 for m, r in pairs]
    trusted_at = {st for _, ok, _, st in view if ok and st is not None}
    all_e = [math.hypot(m.x - r[0], m.y - r[1]) for m, r in pairs]
    ok_e = [math.hypot(m.x - r[0], m.y - r[1]) for m, r in pairs if m.stamp_ns in trusted_at]
    yaw_e = [abs(math.degrees(math.remainder(m.yaw - r[2], 2 * math.pi))) for m, r in pairs
             if m.stamp_ns in trusted_at]
    return {"matched": len(pairs), "aligned": not same_frame,
            "all": {"p50": round(statistics.median(all_e), 3), "p95": round(_pct(all_e, 95), 3),
                    "max": round(max(all_e), 3)},
            "trusted": {"n": len(ok_e),
                        "p50": round(statistics.median(ok_e), 3) if ok_e else None,
                        "p95": round(_pct(ok_e, 95), 3) if ok_e else None,
                        "max": round(max(ok_e), 3) if ok_e else None,
                        "yaw_p95_deg": round(_pct(yaw_e, 95), 1) if yaw_e else None}}


def report_md(name: str, m: dict[str, Any]) -> str:
    lines = [f"# 定位回放:{name}", "",
             f"- 帧数 {m['frames']},时长 {m['duration_s']} s,帧率 {m['rate_hz']} Hz",
             f"- MOLA 每帧耗时 p50 {m['proc_ms']['p50']} ms、p95 {m['proc_ms']['p95']} ms、"
             f"最大 {m['proc_ms']['max']} ms(10 Hz 的预算是 100 ms)",
             f"- ICP 质量 p5 {m['quality']['p5']}、中位 {m['quality']['p50']};σ 中位 "
             f"{m['sigma_m']['p50']} m、p95 {m['sigma_m']['p95']} m",
             f"- 定位器报的跳变 {m['jumps']} 次;定位器状态占比 {m['localizer_states']}",
             f"- **代理判可信的时间占比 {m['agent_trusted_share']:.0%}**"]
    if "error_m" in m and "all" in m["error_m"]:
        e = m["error_m"]
        lines.append(f"- 位置误差(对{'齐后的独立建图' if e['aligned'] else '同一系的建图轨迹'},"
                     f"{e['matched']} 帧):全部 p50 {e['all']['p50']} / p95 {e['all']['p95']} / "
                     f"最大 {e['all']['max']} m;**代理判可信的** p50 {e['trusted']['p50']} / "
                     f"p95 {e['trusted']['p95']} / 最大 {e['trusted']['max']} m,朝向 p95 "
                     f"{e['trusted']['yaw_p95_deg']}°")
    if m["agent_untrusted"]:
        lines += ["", "代理不信的时段(录包里的秒数):", ""]
        lines += [f"- {i['from_s']}–{i['to_s']} s:{i['why']}" for i in m["agent_untrusted"][:20]]
    return "\n".join(lines) + "\n"


def write(out: Path, name: str, m: dict[str, Any]) -> None:
    (out / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
    (out / "report.md").write_text(report_md(name, m))
