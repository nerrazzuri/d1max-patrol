"""每个点的预计时间(W73 第一期,设计 §6.2;用户 11-03:「每个点位的预计时间需要精确到秒」)。

每一段(上一个点 → 这个点)的用时 = 走路 + 原地转向 + 这个点上的动作:

- **没跑过**:走路 = 规划路径长度(跟机器人同一份代价图上的 A*,不是直线;规划不了才按直线,``basis``
  写 ``straight``)÷ :data:`NOMINAL_SPEED_MPS`;转向 = (上一段的朝向 → 这一段的方向 → 这个点要的朝向)
  转过的角度 ÷ :data:`TURN_RAD_S`;动作 = 停留秒数 + 每张照片 :data:`PHOTO_S` + 其他动作各
  :data:`OTHER_ACTION_S`。第一个点没有「上一个点」:只算动作(机器人从哪儿出发不知道)。
- **跑过**:用最近 :data:`HISTORY_SAMPLES` 次实际用时的中位数(这条路线派出去的巡逻任务的
  ``patrol_waypoint`` 事件,相邻两个都到了的点之间的时间差;按点名对,打乱顺序跑的也认)。

这是估算:「看得清就快」(决策 76)、绕障、等人让路都会让实际时间浮动;实际用时出来以后以实际为准。
速度按站点常数算(站点不知道每台机器人此刻的速度档),所以没跑过的估计偏保守。
"""

from __future__ import annotations

import json
import math
import statistics
from typing import Any

#: 没跑过时按这个速度估(米/秒)。机器人的速度档是 1.0 / 0.6 / 0.3(看得清就快),绕障、减速区会慢:
#: 取 0.5 偏保守(我定的,W73 设计 §6.2)。
NOMINAL_SPEED_MPS = 0.5
#: 原地转向(弧度/秒):SDK 低速档 1.5 rad/s,实际转身要加减速,取 0.8。
TURN_RAD_S = 0.8
#: 拍一张照片(秒,含对焦、落盘);其他动作(开灯、转头)各算 1 秒。
PHOTO_S = 2.0
OTHER_ACTION_S = 1.0
#: 实际用时:看最近多少趟、每段取最近几次的中位数。
HISTORY_TASKS = 20
HISTORY_SAMPLES = 5
#: 相邻两个事件间隔短于这个不算(事件一批报上来的,时间戳挨着,不是真用时)。
MIN_SAMPLE_MS = 500


def action_s(actions: list[dict[str, Any]] | None) -> float:
    t = 0.0
    for a in actions or []:
        if a.get("type") == "dwell":
            t += float(a.get("seconds") or 0)
        elif a.get("type") == "photo":
            t += PHOTO_S
        else:
            t += OTHER_ACTION_S
    return t


def _turn(a: float, b: float) -> float:
    return abs(math.remainder(b - a, math.tau))


def actual_segments(db: Any, route_id: str) -> dict[tuple[str, str], list[float]]:
    """这条路线最近派出去的巡逻里,(上一个点, 这个点) → 实际用时(秒,新的在后)。"""
    tasks = [r["task_id"] for r in db.query(
        "SELECT task_id FROM commands WHERE kind='patrol' "
        "AND json_extract(payload, '$.mission.mission')=? "
        "AND (ack_result IS NULL OR ack_result='accepted') "
        "ORDER BY issued_at DESC LIMIT ?", (route_id, HISTORY_TASKS))]
    if not tasks:
        return {}
    marks = ",".join("?" * len(tasks))
    rows = db.query(
        "SELECT data, stamp FROM events WHERE kind='patrol_waypoint' "
        # 按机器人上的时刻排:一趟巡逻中途机器人重启(换了 boot)也排得对
        f"AND json_extract(data, '$.task_id') IN ({marks}) ORDER BY stamp, seq",
        tuple(tasks))
    by_task: dict[str, list[tuple[str, bool, int]]] = {}
    for r in rows:
        d = json.loads(r["data"])
        by_task.setdefault(str(d.get("task_id")), []).append(
            (str(d.get("name", "")), bool(d.get("ok")), int(r["stamp"])))
    out: dict[tuple[str, str], list[float]] = {}
    for tid in reversed(tasks):                       # 老的先,新的后
        evs = by_task.get(tid, [])
        for (n0, ok0, t0), (n1, ok1, t1) in zip(evs, evs[1:], strict=False):
            if ok0 and ok1 and t1 - t0 >= MIN_SAMPLE_MS:
                out.setdefault((n0, n1), []).append((t1 - t0) / 1000.0)
    return out


def estimate(db: Any, reach: Any, route: dict[str, Any]) -> dict[str, Any]:
    """一条路线(``RouteStore.get`` 的平格式)每个点的预计时间。

    回 ``{"waypoints": [{name, travel_m, planned_s, actual_s, samples, use_s, arrive_s}],
    "total_s", "speed_mps", "basis"}``:``planned_s`` 是估的这一段,``actual_s`` 是实际用时的中位数
    (没跑过是 None),``use_s`` 取实际(有的话)否则估的,``arrive_s`` 是从第一个点算起到这个点
    做完动作的累计秒数。``basis``:``planned`` 全按规划路径估、``straight`` 有的段规划不了按直线。"""
    wps = route.get("waypoints") or []
    history = actual_segments(db, str(route.get("id", ""))) if route.get("id") else {}
    out: list[dict[str, Any]] = []
    total = 0.0
    basis = "planned"
    heading = None
    for i, w in enumerate(wps):
        acts = action_s(w.get("actions"))
        travel = 0.0
        turn = 0.0
        if i > 0:
            a = wps[i - 1]
            got = reach.path_m(route["map_id"], route["map_version"], (a["x"], a["y"]),
                               (w["x"], w["y"])) if reach is not None else None
            if got is None:
                got = math.hypot(w["x"] - a["x"], w["y"] - a["y"])
                basis = "straight"
            travel = float(got)
            if travel > 0.05:
                d = math.atan2(w["y"] - a["y"], w["x"] - a["x"])
                turn = _turn(heading if heading is not None else float(a.get("yaw", 0.0)), d)
                heading = d
        turn += _turn(heading if heading is not None else float(w.get("yaw", 0.0)),
                      float(w.get("yaw", 0.0))) if i > 0 else 0.0
        heading = float(w.get("yaw", 0.0))
        planned = travel / NOMINAL_SPEED_MPS + turn / TURN_RAD_S + acts
        samples = history.get((wps[i - 1]["name"], w["name"]), []) if i > 0 else []
        recent = samples[-HISTORY_SAMPLES:]
        actual = round(statistics.median(recent), 1) if recent else None
        use = actual if actual is not None else planned
        total += use
        out.append({"name": w["name"], "travel_m": round(travel, 2),
                    "planned_s": round(planned, 1), "actual_s": actual, "samples": len(recent),
                    "use_s": round(use, 1), "arrive_s": round(total, 1)})
    return {"waypoints": out, "total_s": round(total, 1), "speed_mps": NOMINAL_SPEED_MPS,
            "basis": basis}
