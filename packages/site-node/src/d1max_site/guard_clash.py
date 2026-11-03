"""巡逻排程跟布防值守撞没撞(W73 第三期,设计 §6.3)。

- **按机器人算,不按全站**:布防只占值守方案里分到的机器人;「任意一台」的值守占一台(哪台到点再挑)。
- 巡逻占用 = 开始 … 开始 + 开始窗口 + 整条路线的**保守用时** + 2 分钟回待命点余量。保守用时 =
  每点预计时间合起来 × 1.3(预计本身跑过以后按实际的中位数算,:mod:`d1max_site.route_eta`;设计写的
  「90 分位」没单做)。重复的排程每一轮各算各的。
- 布防占用 = 布防时间表里这条防区路线经过的防区布防着的那几段(跨午夜、星期日连星期一算成一段),
  加上全天布防的例外日期那几天。时间表没开(手动布防)算不出占用:保存时不拦,运行时布防优先(§6.4)。
- **对称拦**:改巡逻排程、改布防时间表 / 例外、改值守方案都查;只拦这一次改动**新造出来**的冲突
  (老数据里本来就撞的不挡别的改动,列在提示里)。

一周按分钟算:星期一 0 点 = 0,一周 = 10080;区间可以伸过一周末尾(星期日连到星期一)。
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

WEEK = 7 * 1440
#: 没跑够次数时保守用时 = 预计 × 这个数。
SAFETY = 1.3
#: 跑完回待命点的余量(分钟)。
RETURN_MIN = 2
#: 实际用时够这么多次才按 90 分位算。
MIN_SAMPLES = 3
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_ZH_DAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(frozen=True)
class Patrol:
    id: str
    route: str
    robot: str                    # '' = 站点挑一台
    at_min: int
    days: frozenset[str]
    window_min: int
    every_min: int
    until_min: int
    duration_s: float             # 预计用时(measured 时就是 90 分位)
    measured: bool = False


def _hm(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _when(a: int, b: int) -> str:
    return f"{_ZH_DAYS[(a // 1440) % 7]} {_hm(a)}–{_hm(b)}"


def _guard_label(w: tuple[int, int]) -> str:
    return f"{_ZH_DAYS[(w[0] // 1440) % 7]} {_hm(w[0])} 开始的布防值守"


def patrol_busy(p: Patrol) -> list[tuple[int, int]]:
    """这条巡逻排程一周里占机器人的几段。"""
    dur = p.duration_s if p.measured else p.duration_s * SAFETY
    span = p.window_min + math.ceil(dur / 60) + RETURN_MIN
    offs = [p.at_min]
    if p.every_min > 0:
        end = p.until_min if p.until_min >= p.at_min else p.until_min + 1440
        offs = list(range(p.at_min, end + 1, p.every_min))
    out = []
    for d, name in enumerate(_DAYS):
        if name in p.days:
            out += [(d * 1440 + o, d * 1440 + o + span) for o in offs]
    return sorted(out)


def guard_busy(g: dict[str, Any]) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in g.get("windows", [])]


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return any(a[0] < b[1] + s and b[0] + s < a[1] for s in (-WEEK, 0, WEEK))


def clashes(patrols: Iterable[Patrol], guards: list[dict[str, Any]], *,
            robots: list[str]) -> list[str]:
    """撞上的每一处一句话(给人看的)。``guards``:``{id, route, robot('' = 任意一台), windows,
    note?}``;``robots``:站点登记着的机器人。"""
    out: list[str] = []
    allr = set(robots)
    for p in patrols:
        for iv in patrol_busy(p):
            hit = [(g, w) for g in guards for w in guard_busy(g) if _overlap(iv, w)]
            if not hit:
                continue
            fixed = {g["robot"] for g, _ in hit if g["robot"]}
            n_any = len({g["id"] for g, _ in hit if not g["robot"]})
            first_g, first_w = hit[0]
            label = first_g.get("note") or _guard_label(first_w)
            if p.robot:
                own = [(g, w) for g, w in hit if g["robot"] == p.robot]
                if own:
                    g, w = own[0]
                    lab = g.get("note") or _guard_label(w)
                    out.append(f"{p.robot} 在{_when(*iv)}有巡逻(排程 {p.id}),跟{lab}"
                               f"(防区路线 {g['route']})撞了")
                    break
                spare = allr - fixed - {p.robot}
                if n_any and len(spare) < n_any:
                    out.append(f"{p.robot} 在{_when(*iv)}有巡逻(排程 {p.id}),{label}要「任意一台」"
                               f"机器人值守,剩下的不够(还差 {n_any - len(spare)} 台)")
                    break
            else:
                free = len(allr - fixed) - n_any
                if free < 1:
                    out.append(f"排程 {p.id} 写的任意一台,{_when(*iv)}所有机器人都在布防值守"
                               f"({label})")
                    break
    return out


def new_clashes(before: list[str], after: list[str]) -> list[str]:
    """这一次改动新造出来的冲突(老数据里本来就撞的不算)。"""
    old = set(before)
    return [m for m in after if m not in old]


# ------------------------------------------------------------ 从库里读出来查

class ClashCheck:
    """站点上的冲突检查:读排程、值守方案、布防时间表,算「这一次改动新造出来的冲突」。

    ``schedule``:布防时间表(``ArmingSchedule``,``view()``);``guard``:``GuardDuty``(值守方案、
    路线经过哪些防区);``routes``:``RouteStore``(巡逻路线的预计用时);``reach``:可达(估用时)。"""

    def __init__(self, db: Any, *, schedule: Any, guard: Any, routes: Any,
                 now_ms: Any) -> None:
        self.db = db
        self.schedule = schedule
        self.guard = guard
        self.routes = routes
        self._now = now_ms

    def robots(self) -> list[str]:
        return [r["robot_id"] for r in self.db.query(
            "SELECT robot_id FROM robots WHERE revoked=0 ORDER BY robot_id")]

    def _duration(self, route_id: str) -> float:
        from d1max_site.route_eta import estimate
        try:
            view = self.routes.get(route_id)
            return float(estimate(self.db, getattr(self.routes, "reach", None), view)["total_s"])
        except Exception:  # noqa: BLE001 - 估不出:按 0,只算窗口和余量
            return 0.0

    def patrols(self, override: dict[str, dict[str, Any] | None] | None = None) -> list[Patrol]:
        rows = {r["entry_id"]: json.loads(r["definition"])
                for r in self.db.query("SELECT entry_id, definition FROM route_schedules")}
        for k, v in (override or {}).items():
            if v is None:
                rows.pop(k, None)
            else:
                rows[k] = v
        kinds = {r["route_id"]: r["kind"]
                 for r in self.db.query("SELECT route_id, kind FROM routes")}
        out = []
        for eid, w in sorted(rows.items()):
            route = w.get("mission", "")
            if kinds.get(route, "patrol") != "patrol":
                continue
            at = _min(w.get("at", "00:00"))
            out.append(Patrol(id=eid, route=route, robot=w.get("robot", "") or "", at_min=at,
                              days=frozenset(w.get("days", [])),
                              window_min=int(w.get("window_min", 0)),
                              every_min=int(w.get("every_min", 0) or 0),
                              until_min=_min(w["until"]) if w.get("until") else at,
                              duration_s=self._duration(route)))
        return out

    def guards(self, *, plan: dict[str, Any] | None = None,
               arming: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from datetime import date, timedelta

        from d1max_site.guard_forecast import weekly_windows
        plan = plan if plan is not None else self.guard.plan()
        view = arming if arming is not None else self.schedule.view()
        if not view.get("enabled"):
            return []                                    # 手动布防:运行时布防优先(§6.4)
        today = date.fromtimestamp(self._now() / 1000)
        armed_days: list[tuple[int, str]] = []
        for x in view.get("exceptions") or []:
            if x.get("state") != "armed":
                continue
            d0, d1 = date.fromisoformat(x["first_day"]), date.fromisoformat(x["last_day"])
            d = max(d0, today)
            n = 0
            while d <= d1 and n < 31:
                armed_days.append((d.weekday(), d.isoformat()))
                d += timedelta(days=1)
                n += 1
        out = []
        for e in plan.get("entries", []):
            info = self.guard._route(e["route"])
            if info is None:
                continue
            wins = weekly_windows(view.get("slots") or [], info["zones"], enabled=True)
            out.append({"id": e.get("id", e["route"]), "route": e["route"],
                        "robot": e.get("robot", ""), "windows": wins})
            for wd, iso in armed_days:
                out.append({"id": e.get("id", e["route"]), "route": e["route"],
                            "robot": e.get("robot", ""), "windows": [(wd * 1440, wd * 1440 + 1440)],
                            "note": f"{iso} 全天布防(例外)"})
        return out

    def all(self, **over: Any) -> list[str]:
        return clashes(self.patrols(over.get("schedules")),
                       self.guards(plan=over.get("plan"), arming=over.get("arming")),
                       robots=self.robots())

    def snapshot(self) -> list[str]:
        """改之前的冲突(配 :meth:`since`:改路线、导任务包这种直接写库的用)。算不出按没有。"""
        try:
            return self.all()
        except Exception:
            log.exception("冲突检查:改之前的算不出来,按没有")
            return []

    def since(self, before: list[str], *, routes: Iterable[str] = ()) -> list[str]:
        """库里已经写成新的(同一个事务、还没提交)以后:比 ``before`` 新多出来的冲突。
        外审 r1 #11:改路线(加点、加停留、改防区路线经过的防区)也会造出冲突。
        ``routes`` 的缓存用完清掉 —— 冲突了要回滚,值守那边别留着按回滚掉的版本算的东西。
        算不出不拦(写不进去比漏查一次更糟)。"""
        try:
            return new_clashes(before, self.all())
        except Exception:
            log.exception("冲突检查:改之后的算不出来,这一次不拦")
            return []
        finally:
            self.forget(routes)

    def forget(self, routes: Iterable[str]) -> None:
        ids = set(routes)
        for attr in ("_route_cache", "_lap_cache"):
            cache = getattr(self.guard, attr, None)
            if isinstance(cache, dict):
                for k in [k for k in cache if isinstance(k, tuple) and k and k[0] in ids]:
                    cache.pop(k, None)

    def new(self, **over: Any) -> list[str]:
        """这次改动(``schedules`` / ``plan`` / ``arming`` 换成给的)新造出来的冲突。"""
        try:
            before = self.all()
        except Exception:  # noqa: BLE001 - 老的算不出:当没有,新的照查
            before = []
        return new_clashes(before, self.all(**over))


def _min(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)
