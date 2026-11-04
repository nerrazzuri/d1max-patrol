"""值守预报(W73 第二期,设计 §5.1、§7.1)。

用户 11-04:第二期先显示「这段布防大约跑几圈、每个防区多久被看一次」;几点补能、补能时哪些防区没人看
(要按电量算)放第四期。这里只做纯计算,不碰库、不碰机器人:

- 每条值守方案一圈的用时(:mod:`nexwar_site.route_eta` 的整条用时)和每圈间隔 → 一个周期
  ``cycle_s = max(一圈, 间隔)``;
- 布防时间表 → 这条路线经过的防区一周里布防的几段(跨午夜、星期日连星期一的算成一段);
  每段约几圈 = 段长 ÷ 周期(往下取整,最后跑不完的那圈不算);
- 每个防区多久被看一次 ≈ 看它的几条方案的周期「并联」(1 / Σ 1/周期);
- 超时线(§5.5):管理员设了按设的,不然 = 间隔 × 1.5,最少 15 分钟;
- 提示(只提示,不拦):一个防区被两条路线都看、布防的防区没有机器人去看、算不出一圈多久。
"""

from __future__ import annotations

import math
from typing import Any

from nexwar_site.energy import SWAP_GAP_MIN, laps_per_charge

WEEK_MIN = 7 * 1440
#: 超时线最少这么多分钟(设计 §5.5,「我定的」)。
MIN_LIMIT_MIN = 15
#: 超时线 = 预报的间隔 × 这个数。
LIMIT_FACTOR = 1.5


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _zone_slots(slots: list[dict[str, Any]], zone: str) -> list[tuple[int, int]] | None:
    """这个防区一周里布防的段(星期内的分钟);``None`` = 整周布防。规矩同
    :meth:`nexwar_site.arming_schedule.ArmingSchedule.scheduled`:有自己的用自己的,没有用 ``''``
    那份,
    都没有一律布防;一段都没画(全天撤防)在调用方处理。"""
    mine = [s for s in slots if zone and s.get("zone") == zone]
    if not mine:
        mine = [s for s in slots if s.get("zone", "") == ""]
        if not mine:
            return None
    return [(int(s["day"]) * 1440 + _min(s["start"]), int(s["day"]) * 1440 + _min(s["end"]))
            for s in mine]


def weekly_windows(slots: list[dict[str, Any]], zones: list[str], *,
                   enabled: bool) -> list[tuple[int, int]]:
    """这几个防区里**任何一个**布防着的段(星期一 0 点起的分钟,``[开始, 结束)``,合并好了)。

    跨午夜的两段连成一段;星期日连到星期一的那一段结束写成大于一周(``7*1440 + …``)。时间表没开回空
    (按手动布防,没有段可算)。"""
    if not enabled or not slots:
        return []
    raw: list[tuple[int, int]] = []
    for z in zones or [""]:
        got = _zone_slots(slots, z)
        if got is None:
            return [(0, WEEK_MIN)]
        raw.extend(got)
    raw.sort()
    merged: list[list[int]] = []
    for a, b in raw:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if len(merged) > 1 and merged[0][0] == 0 and merged[-1][1] == WEEK_MIN:
        first = merged.pop(0)
        merged[-1][1] = WEEK_MIN + first[1]
    return [(a, b) for a, b in merged]


def forecast(entries: list[dict[str, Any]], *, lap_s: dict[str, float | None],
             zones_of: dict[str, list[str]], schedule: dict[str, Any],
             limits: dict[str, int], armable: list[str]) -> dict[str, Any]:
    """→ ``{"entries": [...], "zones": [...], "warnings": [...]}``。

    ``entries``:值守方案(``{id, route, robot, gap_min, shuffle}``);``lap_s``:每条路线一圈多少秒
    (算不出的没有这一项或 ``None``);``zones_of``:每条路线经过哪些防区;``schedule``:布防时间表的
    ``view()``(``enabled``、``slots``);``limits``:管理员设的超时线(分钟);``armable``:这几张图上画了的
    防区(提示「布防了没人看」用)。"""
    enabled = bool(schedule.get("enabled"))
    slots = list(schedule.get("slots") or [])
    out_entries: list[dict[str, Any]] = []
    cycles: dict[str, list[float | None]] = {}
    covered_by: dict[str, list[str]] = {}
    warnings: list[str] = []
    for e in entries:
        route = e["route"]
        lap = lap_s.get(route)
        gap = int(e.get("gap_min") or 0) * 60
        cycle = max(float(lap), float(gap)) if lap else None
        zones = list(zones_of.get(route) or [])
        wins = []
        # W73 第四期:满电(按最多充到的 95%)出发,补一次能之前能开几圈;每段要补几次
        per = laps_per_charge(float(lap), cycle) if lap and cycle else None
        for a, b in weekly_windows(slots, zones, enabled=enabled):
            minutes = b - a
            laps = int(minutes * 60 // cycle) if cycle else None
            wins.append({"day": (a // 1440) % 7, "start": _hhmm(a), "end": _hhmm(b) if b % 1440
                         else "24:00", "minutes": minutes, "laps": laps,
                         "topups": (laps - 1) // per if laps and per else None})
        out_entries.append({"id": e["id"], "route": route, "robot": e.get("robot", ""),
                            "gap_min": int(e.get("gap_min") or 0),
                            "lap_s": float(lap) if lap else None, "cycle_s": cycle,
                            "mode": "schedule" if enabled else "manual", "windows": wins,
                            "laps_per_charge": per})
        if per is not None and any((w["topups"] or 0) > 0 for w in wins):
            n = max(w["topups"] or 0 for w in wins)
            warnings.append(f"{route}:约每 {per} 圈要补一次能(最长那段要补 {n} 次)。有桩自己去充;"
                            f"没桩要夜间换电 {n} 次,每次约 {SWAP_GAP_MIN} 分钟这条路线的防区"
                            f"没人看、"
                            f"只靠摄像头 —— 要不间断就两台轮换或配桩")
        if not lap:
            warnings.append(f"{route} 算不出一圈多久(路线有问题或地图没有规划栅格),圈数没法估")
        for z in zones:
            if route not in covered_by.setdefault(z, []):
                covered_by[z].append(route)
                cycles.setdefault(z, []).append(cycle)
    out_zones = []
    for z, routes in covered_by.items():
        known = [c for c in cycles[z] if c]
        interval = 1.0 / sum(1.0 / c for c in known) if known else None
        if z in limits:
            limit, is_set = int(limits[z]), True
        else:
            limit = MIN_LIMIT_MIN if interval is None else max(
                MIN_LIMIT_MIN, math.ceil(interval * LIMIT_FACTOR / 60))
            is_set = False
        out_zones.append({"zone": z, "routes": routes, "interval_s": interval,
                          "limit_min": limit, "limit_set": is_set})
        if len(routes) > 1:
            n = "两条" if len(routes) == 2 else f"{len(routes)} 条"
            warnings.append(f"{z} 被{n}路线都看着({'、'.join(routes)}):间隔按并起来算")
    for z in armable:
        if z not in covered_by:
            warnings.append(f"{z} 布防了但没有机器人去看(只靠摄像头)")
    return {"entries": out_entries, "zones": out_zones, "warnings": warnings}
