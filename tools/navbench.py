"""W47 导航仿真基准:同一批场景(`tools/navbench_scenarios.json`)每个随机扰动跑 N 次,出成功率、用时、
停车次数、撞没撞,给导航后端打分(先给现在的 ``planned`` 量基线,Nav2 后端做好了跑同一批比)。

仿真用的是 pytest 里那套闭环台子(`packages/robot-agent/tests/obs_world.py`、
`test_obstacle_avoid.台子`):
真值障碍、照 C40221 实测的雷达视野(两侧看不见)、感知晚 0.15 s 送到、链路延迟、起步切步态、刹车上限、
旁路的净空许可;每拍查机身矩形压没压上障碍。

用法(台式机,仓库根目录;跟 `pt.sh` 一样的 PYTHONPATH)::

    python tools/navbench.py                       # 全部场景,每个 10 次
    python tools/navbench.py -n 20 --only c1_box,door_085 --json runs/navbench/planned.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "tools" / "navbench_scenarios.json"
#: 仿真狗的参数(照真机量过的、保守的):链路延迟、起步切步态、刹车减速度上限。
ROBOT = {"rear": True, "cone": True, "latency": 0.1, "gait": 0.3, "decel": 1.5, "clearance": True,
         "max_vx": 1.0}
#: 导航限速(米/秒;``--vmax`` 改):低速档满量程约 1.0(用户 2026-10-29:遥控器低速档 0.6–1.0)。
VMAX = {"v": 1.0}
#: ``--trace``:每秒记一笔(时刻、位置、实际速度、Nav2 给的速度),查慢在哪。
TRACE = {"on": False}


@dataclass
class Result:
    scenario: str
    seed: int
    expect: str
    outcome: str = "timeout"          # reach / fail / refused / timeout
    ok: bool = False
    crashed: bool = False
    time_s: float = 0.0
    stops: int = 0
    blocked: int = 0
    replans: int = 0
    goal_dist_m: float = math.nan
    #: 场景要求多少秒内到(``deadline_s``);到了而且没超时才算「限时内」。
    deadline_s: float | None = None
    in_time: bool | None = None
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def load(path: Path = SCENARIOS) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d["defaults"], d["scenarios"]


def _jit(rng: random.Random, m: float) -> float:
    return rng.uniform(-m, m) if m else 0.0


def _shift(rect: list[float], dx: float, dy: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = rect
    return (x0 + dx, y0 + dy, x1 + dx, y1 + dy)


#: Nav2 后端:整批共用的转接服务端、Nav2 进程、单调往前的仿真钟(跨场景不许往回跳,TF 会乱)。
_NAV2: dict[str, Any] = {}


async def _nav2_up() -> Any:
    """起 Nav2(``deploy/nav2/d1max_nav2.launch.py`` + 我们的参数)和转接节点,等它们连上代理这头的服务端。"""
    if "link" in _NAV2:
        return _NAV2["link"]
    import os
    import subprocess

    from d1max_agent.navbridge import NavBridgeServer
    d = Path(tempfile.mkdtemp(prefix="navbench-"))
    sock = d / "nav2.sock"
    link = NavBridgeServer(sock)
    await link.start()
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "VIRTUAL_ENV", "AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH")}
    env |= {"ROS_DOMAIN_ID": os.environ.get("NAVBENCH_DOMAIN", "77"), "ROS_LOCALHOST_ONLY": "1",
            "PATH": "/usr/bin:/bin"}
    src = "source /opt/ros/humble/setup.bash && exec "
    params = ROOT / "deploy" / "nav2" / "params.yaml"
    logs = ROOT / "runs" / "navbench"
    logs.mkdir(parents=True, exist_ok=True)
    # 跟狗上同一个启动文件(Nav2 + 转接节点 + 行为树换成绝对路径)
    pypath = f"{ROOT}/packages/localizer/src:{ROOT}/packages/contract/src"
    nav2 = subprocess.Popen(
        ["bash", "-c", src + f"ros2 launch {ROOT}/deploy/nav2/d1max_nav2.launch.py "
                             f"params_file:={params} socket:={sock} pythonpath:={pypath}"],
        env=env, stdout=open(logs / "nav2.log", "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    _NAV2.update(link=link, procs=[nav2], t_ms=1_700_000_000_000)
    for _ in range(300):
        if link.connected:
            break
        await asyncio.sleep(0.1)
    if not link.connected:
        raise RuntimeError("Nav2 转接节点 30 s 没连上(看 runs/navbench/nav2.log)")
    return link


def _nav2_down() -> None:
    import os
    import signal
    for p in _NAV2.get("procs", []):
        try:
            os.killpg(p.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    for p in _NAV2.get("procs", []):
        try:
            p.wait(15)
        except Exception:                             # noqa: BLE001
            os.killpg(p.pid, signal.SIGKILL)


async def run_planned(sc: dict[str, Any], defaults: dict[str, Any], seed: int,
                      backend: str = "planned") -> Result:
    """跑一次(``planned`` 或 ``nav2`` 后端;仿真台子同一个)。"""
    from test_obstacle_avoid import 台子

    from d1max_patrol.backends.base import NavRequestError
    from d1max_patrol.protocol.nav_types import NavStatus, Pose

    rng = random.Random(seed)
    jit = {**defaults["jitter"], **sc.get("jitter", {})}
    sx, sy, syaw = sc["start"]
    sx, sy = sx + _jit(rng, jit["start_m"]), sy + _jit(rng, jit["start_m"])
    syaw = math.radians(syaw + _jit(rng, jit["yaw_deg"]))
    boxes = [(_shift(b["rect"], _jit(rng, jit["box_m"]), _jit(rng, jit["box_m"])),
              float(b.get("appear_s", 0.0))) for b in sc.get("boxes", [])]
    movers = sc.get("movers", [])
    res = Result(sc["name"], seed, sc.get("expect", "reach"))
    with tempfile.TemporaryDirectory() as d:
        t = 台子(Path(d), walls=[tuple(w) for w in sc.get("walls", [])], **ROBOT)
        t.world.__init__(*sc.get("world", defaults["world"]),
                         walls=[tuple(w) for w in sc.get("walls", [])])
        t.world.地图(Path(d))
        link = None
        if backend == "nav2":
            link = await _nav2_up()
            from d1max_agent.bridges.nav2_nav import Nav2NavBackend
            from d1max_agent.planning.planner import Planner
            old = t.nav
            nav = Nav2NavBackend(t.r, now_ms=t.c, map_id="m", planner=Planner(in_process=True),
                                 link=link)
            nav.obstacles, nav.guard, nav.on_event = old.obstacles, old.guard, old.on_event
            nav.emit = old.emit
            t.nav = nav
            t.c.ms = _NAV2["t_ms"] = _NAV2["t_ms"] + 60_000      # 仿真钟跨场景只往前
        t.nav.load_grid(Path(d))
        await t.start(sx, sy, syaw)
        await t.nav.set_speed(VMAX["v"])
        if link is not None:
            for _ in range(200):                              # 等 Nav2 收下这张图、时钟走起来
                await t.一拍()
                if link.ready:
                    break
                await asyncio.sleep(0.05)
        for i, (r, at) in enumerate(boxes):
            if at <= 0:
                t.world.dyn[f"box{i}"] = r
        gx, gy, gyaw = sc["goal"]
        try:
            await t.nav.goto(Pose.from_xy_yaw(gx, gy, math.radians(gyaw)))
        except NavRequestError as exc:
            res.outcome, res.reason = "refused", str(exc)[:160]
            res.ok = res.expect == "fail"
            return res
        max_s = float(sc.get("max_s", defaults["max_s"]))
        tick, moving = 0.1, False
        steps = int(max_s / tick)
        for k in range(steps):
            now = k * tick
            for i, (r, at) in enumerate(boxes):
                if 0 < at <= now:
                    t.world.dyn[f"box{i}"] = r
            for i, m in enumerate(movers):
                name = f"mover{i}"
                if m["t0"] <= now <= m["t1"]:
                    dt = now - m["t0"]
                    t.world.dyn[name] = _shift(m["rect"], m["v"][0] * dt, m["v"][1] * dt)
                else:
                    t.world.dyn.pop(name, None)
            await t.一拍(tick)
            if link is not None:
                _NAV2["t_ms"] = t.c.ms
                if t.status and t.status[-1] is NavStatus.ACTIVE:
                    want = t.nav._t_ms
                    # 等 Nav2 对这一拍出了速度(墙钟 ≤ 0.3 s)
                    for _ in range(60):
                        c = link.cmd
                        if c is not None and c[1] >= want:
                            break
                        await asyncio.sleep(0.005)
            if t.crashed and "crash_at" not in res.extra:
                c = getattr(getattr(t.nav, "link", None), "cmd", None)
                res.extra["crash_at"] = {
                    "t": round(now, 1), "pose": [round(t.r.x, 3), round(t.r.y, 3),
                                                 round(math.degrees(t.r.yaw), 1)],
                    "speed": [round(float(x), 3) for x in t.r.speed],
                    "nav2_cmd": None if c is None else [round(float(x), 3) for x in c[2:]],
                    "status": str(t.status[-1]) if t.status else "",
                    "path": "nav2" if getattr(t.nav, "_nav2_goal", None) else "planned"}
            if TRACE["on"] and k % 10 == 0:
                c = getattr(getattr(t.nav, "link", None), "cmd", None)
                res.extra.setdefault("trace", []).append(
                    [round(now), round(t.r.x, 1), round(t.r.y, 1), round(float(t.r.speed[0]), 2),
                     None if c is None else round(float(c[2]), 2)])
            v = abs(t.r.speed[0]) > 0.02 or abs(t.r.speed[1]) > 0.05
            if moving and not v:
                res.stops += 1
            moving = v
            last = t.status[-1] if t.status else None
            if last in (NavStatus.SUCCEED, NavStatus.FAILED, NavStatus.CANCELLED) or (
                    last is NavStatus.STANDBY and len(t.status) > 1):
                res.time_s = round((k + 1) * tick, 1)
                break
        else:
            res.time_s = max_s
        st = t.status
        if backend == "nav2":
            res.extra["fallback"] = getattr(t.nav, "fallback_reason", "")
            if t.status and t.status[-1] not in (NavStatus.STANDBY,):
                await t.nav.stop()
        if NavStatus.SUCCEED in st:
            res.outcome = "reach"
        elif NavStatus.FAILED in st:
            res.outcome = "fail"
            res.reason = (getattr(t.nav, "fail_reason", "") or "")[:160]
        res.crashed = t.crashed
        kinds = [k for k, _ in t.events]
        res.blocked = kinds.count("nav_blocked")
        res.replans = kinds.count("nav_replan")
        res.goal_dist_m = round(math.hypot(t.r.x - gx, t.r.y - gy), 2)
    if res.expect == "reach":
        res.ok = res.outcome == "reach" and not res.crashed
    else:
        res.ok = res.outcome in ("fail", "refused") and not res.crashed
    if sc.get("deadline_s") is not None:
        res.deadline_s = float(sc["deadline_s"])
        res.in_time = res.ok and res.time_s <= res.deadline_s
    return res


BACKENDS = {"planned": run_planned,
            "nav2": lambda sc, d, seed: run_planned(sc, d, seed, backend="nav2")}


def summarize(results: list[Result]) -> list[dict[str, Any]]:
    by: dict[str, list[Result]] = {}
    for r in results:
        by.setdefault(r.scenario, []).append(r)
    rows = []
    for name, rs in by.items():
        times = [r.time_s for r in rs if r.ok]
        reasons: dict[str, int] = {}
        for r in rs:
            if not r.ok:
                key = r.outcome + ("+撞" if r.crashed else "") + (f":{r.reason[:40]}" if r.reason
                                                                  else "")
                reasons[key] = reasons.get(key, 0) + 1
        rows.append({
            "scenario": name, "n": len(rs), "ok": sum(r.ok for r in rs),
            "crash": sum(r.crashed for r in rs),
            "time_med": round(statistics.median(times), 1) if times else None,
            "stops_mean": round(statistics.mean(r.stops for r in rs), 1),
            "blocked_mean": round(statistics.mean(r.blocked for r in rs), 1),
            "fails": reasons,
            "in_time": (sum(bool(r.in_time) for r in rs)
                        if any(r.deadline_s is not None for r in rs) else None)})
    return rows


def table(rows: list[dict[str, Any]], backend: str) -> str:
    out = [f"## 后端 {backend}", "",
           "| 场景 | 成功 | 撞 | 用时中位(s) | 限时内 | 平均停车 | 平均被挡 | 没成的原因 |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        why = ";".join(f"{k} ×{v}" for k, v in sorted(r["fails"].items(), key=lambda x: -x[1]))
        med = r["time_med"] if r["time_med"] is not None else "—"
        intime = "—" if r["in_time"] is None else f"{r['in_time']}/{r['n']}"
        out.append(f"| {r['scenario']} | {r['ok']}/{r['n']} | {r['crash']} | {med} | {intime} | "
                   f"{r['stops_mean']} | {r['blocked_mean']} | {why[:160] or '—'} |")
    tot_ok = sum(r["ok"] for r in rows)
    tot = sum(r["n"] for r in rows)
    out += ["", f"合计 {tot_ok}/{tot}({100 * tot_ok / max(tot, 1):.0f}%),"
                f"撞 {sum(r['crash'] for r in rows)} 次"]
    return "\n".join(out)


async def bench(backend: str, n: int, only: set[str] | None, seed0: int,
                progress: bool = True) -> list[Result]:
    defaults, scs = load()
    run = BACKENDS[backend]
    out = []
    for sc in scs:
        if only and sc["name"] not in only:
            continue
        for i in range(n):
            t0 = time.monotonic()
            r = await run(sc, defaults, seed0 + i)
            out.append(r)
            if progress:
                print(f"{sc['name']:<16} #{i:<2} {r.outcome:<8} ok={int(r.ok)} "
                      f"crash={int(r.crashed)} t={r.time_s:5.1f}s stops={r.stops} "
                      f"({time.monotonic() - t0:.1f}s 实)", file=sys.stderr, flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--backend", choices=sorted(BACKENDS), default="planned")
    p.add_argument("-n", type=int, default=10, help="每个场景跑几次(随机扰动)")
    p.add_argument("--only", default="", help="只跑这几个场景(逗号分开)")
    p.add_argument("--seed", type=int, default=1000)
    p.add_argument("--json", type=Path, default=None, help="每一次的结果存成 JSON")
    p.add_argument("--vmax", type=float, default=1.0, help="导航限速(米/秒,缺省 1.0)")
    p.add_argument("--trace", action="store_true", help="每秒记一笔速度,存进 JSON")
    a = p.parse_args(argv)
    sys.path.insert(0, str(ROOT / "packages" / "robot-agent" / "tests"))
    import logging
    logging.disable(logging.WARNING)
    only = {s for s in a.only.split(",") if s} or None
    VMAX["v"] = a.vmax
    TRACE["on"] = a.trace
    try:
        results = asyncio.run(bench(a.backend, a.n, only, a.seed))
    finally:
        _nav2_down()
    rows = summarize(results)
    print(table(rows, a.backend))
    if a.json is not None:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"backend": a.backend, "robot": ROBOT, "vmax": a.vmax,
                                      "rows": rows,
                                      "runs": [asdict(r) for r in results]},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
