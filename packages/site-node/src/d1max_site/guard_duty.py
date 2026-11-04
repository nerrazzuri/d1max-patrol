"""布防值守(W73 第二期,设计 §5):布防着的时候,机器人自己一圈圈跑防区路线;没有外部设备也能用。

**值守方案**(``guard_plan``,管理员在「布防时间」页配):哪条防区路线给哪台机器人(或任意一台)、
每圈之间隔多久(0 = 跑完马上下一圈)、要不要打乱起点和方向。布防时间表决定什么时候布防,值守方案
决定布防时谁跑哪条。方案挂在全站,不按时间段各挂一份:时间表本来就按防区、按星期分,一条方案在它
经过的任何一个防区布防着时就跑。

站点每拍对账(:meth:`GuardDuty.tick`,事件循环里的一条道):

- **起**:方案里的路线经过的防区有布防着的 → 挑机器人(在线、新鲜、自己能走、不是只许手动派、
  没在驱离、没在回充、加载着路线那张图、电量过 :data:`MIN_PCT`、手上没有要等的任务)→ 派一圈
  ``patrol``(``guard-<方案>-…``,优先级 :data:`~d1max_site.priorities.GUARD_LAP`,排程巡逻让给它)。
  撤防的防区里的点不去(留守、临时撤防);一圈从**最久没看的那个防区**的第一个点开始(§5.3)。
- **一圈跑完**:离下一圈还有 :data:`WATCH_MIN_S` 秒以上 → 去这一圈最早看的那个防区的**值守位**
  站着盯(``guard-watch-…``,优先级低一点,到点下一圈直接抢下来);没空档直接下一圈。
  站在值守位上算看着那个防区。
- **被打断**(事件派遣、人手动派的抢走了,§5.3):机器人空了以后接着跑。这一圈已经看过的防区
  不重复,从剩下的里面最久没看的开始;都看过了就是新的一圈。人中止的:歇 :data:`PAUSE_MS`。
  派了、狗上没在跑、也没终态事件,过了 :data:`LOST_S` 当被打断。连着 :data:`FAIL_ALERT` 圈
  跑不成报 P2。
- **收队**:这条路线经过的防区全撤防了(或方案删了、换了机器人)→ 手上这一趟停下,回待命点
  交给待命点管理器(值守中的任务 :meth:`holds`,不让它一圈一回;收队了放开)。
- **多久没看**(§5.5):每个布防着、有方案看着的防区,最后看过(走过它的点、站在它的值守位上)
  离现在超过超时线 → P2 ``zone_unwatched``(记在 ``zone:<防区>`` 名下,写明原因);看到了、
  撤防了自动解决。
- **值守记录**(§5.6):每一段布防(``guard_duties``)里的每一件事记进 ``guard_log``:一圈开始 /
  跑完、到了哪个点、没到、被打断、接着跑、去值守位、等机器人(为什么)、收队。

电量闭环(几点补能、换电、补能时报警怎么判)是第四期;这一期只守一条底线:电量不过
:data:`MIN_PCT` 不开新的一圈(有桩的让回充接手 —— 站在值守位上、空着的机器人回充照常起)。
"""
from __future__ import annotations

import json
import logging
import math
import random
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from d1max_contract.messages import MapPose
from d1max_site import energy
from d1max_site.db import SiteDB
from d1max_site.priorities import GUARD_LAP, GUARD_PREFIX, GUARD_WATCH, STANDBY_PREFIX
from d1max_site.routes import NAME_RE

log = logging.getLogger(__name__)

#: 派了之后狗上既没在跑、也没终态事件,等这么久(秒)当被打断。
LOST_S = 90
#: 人中止之后歇多久(毫秒)。
PAUSE_MS = 10 * 60_000
#: 派不出去之后隔多久(毫秒)再派。
RETRY_MS = 30_000
#: 跑不成(``task_failed``)之后隔多久(毫秒)再派。
FAIL_RETRY_MS = 60_000
#: 两圈之间空档不到这么多秒,不去值守位,直接下一圈。
WATCH_MIN_S = 30
#: 离值守位这么近(米)才算站在值守位上。
POST_NEAR_M = 1.5
#: 看见人(P1 ``dog_sees_person``)以后原地盯着等保安下结论,最多这么久(设计 §5.4:10 分钟推给经理),
#: 之后接着值守(外审 r1 #5)。
PERSON_HOLD_MS = 10 * 60_000
PERSON_KIND = "dog_sees_person"
#: 撤防时停下这一圈以后,过这么久按还布防着的接着跑(等狗上停稳)。
SETTLE_MS = 3_000
#: 要停这一圈(看见人、撤防)发了中止,这么久还没停下再发一次;发了 :data:`STOP_ALERT` 次还没停
#: 报 P2 ``guard_stuck``。确认停下之前不盯人、不派新的一圈(外审 r2 #5:中止没发成曾当已停)。
STOP_RETRY_MS = 5_000
STOP_ALERT = 3
#: 连着这么多圈跑不成报 P2。
FAIL_ALERT = 3
#: 电量不过这条线(%)不开新的一圈。第四期换成按任务算的出站线。
#: 「不够跑一圈」判过以后这么久内算数(回充 / 换电按它起)。
LOW_FRESH_MS = 120_000
#: 布防开始前这么多分钟查电量(§7.1)。
PREP_MIN = 30
MIN_PCT = 25.0                                   # W73 第四期:兜底;实际按 energy.lap_line
MAX_ENTRIES = 50
MAX_GAP_MIN = 240
MAX_LIMIT_MIN = 24 * 60
_KEY = "guard_plan"
_TERMINAL = ("task_done", "task_failed", "task_aborted", "task_preempted")
ALERT_KIND = "zone_unwatched"
ZONE_PREFIX = "zone:"


class PlanError(ValueError):
    """值守方案写得不对(接口回 400)。"""


class PlanConflict(PlanError):
    """别人刚改过(接口回 409)。"""


def _accepted(r: Any) -> bool:
    ack = r.get("ack", {}) if isinstance(r, dict) else {}
    return ack.get("result") in ("accepted", "duplicate")


class GuardDuty:
    def __init__(self, db: SiteDB, dispatcher: Any, *, routes: Any, areas: Any, arming: Any,
                 posts: Callable[[str, str], dict[str, dict[str, Any]]],
                 now_ms: Callable[[], int], rng: random.Random | None = None,
                 min_pct: float = MIN_PCT) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self.routes = routes
        self.areas = areas
        self.arming = arming
        self.posts = posts
        self._now = now_ms
        self.rng = rng or random.Random()
        self.min_pct = min_pct
        #: 告警台(站点接上;事件循环里用)。
        self.alerts: Any = None
        #: 正在驱离的狗(``DeterrenceDesk.busy``)。
        self.busy: Callable[[], set[str]] | None = None
        #: 这台在不在回充(``ChargeDesk``,站点接上)。
        self.charging: Callable[[str], bool] | None = None
        #: W73 第四期:没桩的换电进度(``battery_swap.SwapDesk``;``status`` 里带出去)。
        self.swap: Any = None
        self._prep_told: dict[str, int] = {}
        #: W73 第四期:电量过不了一圈出站线的 ``{机器人: (那条线, 一圈秒数)}``(补能按它起)。
        self.low: dict[str, tuple[float, float, int]] = {}
        #: 收队时站在值守位上(手上没任务)的狗回待命点用(``StandbyManager``)。
        self.standby: Any = None
        #: 估一圈多久用的可达(``InterceptReach``;没有按直线估)。
        self.reach: Any = None
        self._retry_at: dict[int, int] = {}
        self._reserved: set[str] = set()
        self._waiting: dict[int, str] = {}
        self._route_cache: dict[tuple, dict[str, Any]] = {}
        self._lap_cache: dict[tuple, float | None] = {}
        #: 看见人盯满 :data:`PERSON_HOLD_MS` 还没人管、接着值守了的:这一条告警不再拦它。
        self._person_done: set[str] = set()
        #: 在停这一圈的方案:停下以后进哪一步(``held`` / ``resume``)、中止发了几次。只在内存:
        #: 站点重启后停下了按 ``resume`` 接着办(还看见人的话下一拍照样再盯)。
        self._stop_then: dict[int, str] = {}
        self._stop_tries: dict[int, int] = {}
        dispatcher.on_event(self._on_event)

    # ------------------------------------------------------------ 方案

    def plan(self) -> dict[str, Any]:
        from d1max_site.site_settings import SiteSettings
        meta = SiteSettings(self.db).get(_KEY, {}) or {}
        entries = [{"id": r["id"], "route": r["route"], "robot": r["robot"],
                    "gap_min": r["gap_min"], "shuffle": bool(r["shuffle"])}
                   for r in self.db.query("SELECT * FROM guard_plan ORDER BY id")]
        limits = {r["zone"]: r["max_gap_min"]
                  for r in self.db.query("SELECT * FROM guard_zone_limits ORDER BY zone")}
        return {"revision": int(meta.get("revision", 0)), "entries": entries, "limits": limits,
                "updated_by": meta.get("by", ""), "updated_ms": meta.get("at", 0)}

    def put_plan(self, d: Any, *, by: str) -> dict[str, Any]:
        """整份换:``{"entries": [{route, robot?, gap_min?, shuffle?}], "limits"?: {防区: 分钟},
        "base_revision"}``。"""
        if not isinstance(d, dict):
            raise PlanError("要一个对象")
        raw = d.get("entries")
        if not isinstance(raw, list):
            raise PlanError("entries 要是一个列表")
        if len(raw) > MAX_ENTRIES:
            raise PlanError(f"值守方案最多 {MAX_ENTRIES} 条")
        rows: list[tuple[int, str, str, int, int]] = []
        fixed: dict[str, str] = {}
        for i, e in enumerate(raw, start=1):
            if not isinstance(e, dict):
                raise PlanError("每一条要是一个对象")
            route = e.get("route")
            if not isinstance(route, str) or not route:
                raise PlanError("每一条要写 route(哪条防区路线)")
            got = self.db.query("SELECT kind FROM routes WHERE route_id=?", (route,))
            if not got:
                raise PlanError(f"没有路线 {route!r}")
            if got[0]["kind"] != "guard":
                raise PlanError(f"{route} 不是防区路线(巡逻路线不能拿来值守)")
            robot = e.get("robot", "")
            if not isinstance(robot, str) or (robot and not NAME_RE.fullmatch(robot)):
                raise PlanError(f"robot 要是机器人编号或空(任意一台):{robot!r}")
            gap = e.get("gap_min", 0)
            if not isinstance(gap, int) or isinstance(gap, bool) or not 0 <= gap <= MAX_GAP_MIN:
                raise PlanError(f"gap_min 要是 0–{MAX_GAP_MIN} 的整数(分钟;0 = 跑完马上下一圈)")
            shuffle = e.get("shuffle", False)
            if not isinstance(shuffle, bool):
                raise PlanError("shuffle 要是 true/false")
            if robot:
                if robot in fixed:
                    raise PlanError(f"同一台机器人 {robot} 不能同时跑两条({fixed[robot]}、{route})")
                fixed[robot] = route
            rows.append((i, route, robot, gap, int(shuffle)))
        lim = d.get("limits", {}) or {}
        if not isinstance(lim, dict):
            raise PlanError("limits 要是 {防区: 分钟}")
        limits: list[tuple[str, int]] = []
        for z, m in lim.items():
            if not isinstance(z, str) or not z or not isinstance(m, int) or isinstance(m, bool) \
                    or not 1 <= m <= MAX_LIMIT_MIN:
                raise PlanError(f"limits 里每个防区写 1–{MAX_LIMIT_MIN} 分钟:{z!r}={m!r}")
            limits.append((z, m))
        clash = getattr(self, "clash", None)            # W73 第三期:跟巡逻排程撞了不让存
        if clash is not None:
            got = clash.new(plan={"entries": [{"id": i, "route": r, "robot": rb}
                                              for i, r, rb, _, _ in rows]})
            if got:
                raise PlanError("跟巡逻排程撞了:" + ";".join(got))
        from d1max_site.site_settings import SiteSettings
        now = self._now()
        with self.db.tx() as c:
            row = c.execute("SELECT value FROM site_settings WHERE key=?", (_KEY,)).fetchone()
            cur = int(json.loads(row["value"]).get("revision", 0)) if row else 0
            if d.get("base_revision") != cur:
                raise PlanConflict(f"值守方案别人刚改过(现在第 {cur} 版,你看到的是第 "
                                   f"{d.get('base_revision')} 版):刷新后再改")
            c.execute("DELETE FROM guard_plan")
            c.executemany("INSERT INTO guard_plan(id, route, robot, gap_min, shuffle) "
                          "VALUES (?,?,?,?,?)", rows)
            c.execute("DELETE FROM guard_zone_limits")
            c.executemany("INSERT INTO guard_zone_limits(zone, max_gap_min) VALUES (?,?)", limits)
            SiteSettings.put_in(c, _KEY, {"revision": cur + 1, "by": by, "at": now})
        self._waiting.clear()
        return self.plan()

    # ------------------------------------------------------------ 路线

    def _route(self, route_id: str) -> dict[str, Any] | None:
        """方案里的一条防区路线:任务定义、每个点落在哪个防区(不在防区里的是 ``''``,路过的点)。"""
        rows = self.db.query("SELECT * FROM routes WHERE route_id=?", (route_id,))
        if not rows or rows[0]["kind"] != "guard" or not rows[0]["map_version"]:
            return None
        r = rows[0]
        stamp = self.db.query("SELECT MAX(updated_ms) AS t, COUNT(*) AS n FROM zone_areas "
                              "WHERE map_id=? AND map_version=?", (r["map_id"], r["map_version"]))
        key = (route_id, r["revision"], stamp[0]["t"], stamp[0]["n"])
        hit = self._route_cache.get(key)
        if hit is not None:
            return hit
        wire = json.loads(r["definition"])
        points = []
        for w in wire.get("waypoints", []):
            pos = (w.get("pose") or {}).get("position") or {}
            x, y = float(pos.get("x", 0.0)), float(pos.get("y", 0.0))
            zone = self.areas.zone_at(r["map_id"], r["map_version"], x, y) or ""
            points.append({"name": w["name"], "zone": zone, "x": x, "y": y})
        zones: list[str] = []
        for p in points:
            if p["zone"] and p["zone"] not in zones:
                zones.append(p["zone"])
        info = {"id": route_id, "map_id": r["map_id"], "map_version": r["map_version"],
                "revision": r["revision"], "wire": wire, "points": points, "zones": zones}
        self._route_cache = {k: v for k, v in self._route_cache.items() if k[0] != route_id}
        self._route_cache[key] = info
        return info

    def _armed(self, zone: str) -> bool:
        try:
            return bool(self.arming.armed(zone)[0])
        except Exception:  # 说不清按布防:宁可去看
            log.exception("%s 布不布防查不出来", zone)
            return True

    # ------------------------------------------------------------ 进度

    def _runs(self) -> dict[int, dict[str, Any]]:
        return {r["entry"]: dict(r) for r in self.db.query("SELECT * FROM guard_runs")}

    def _set_run(self, entry: int, **f: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in f)
        with self.db.tx() as c:
            c.execute(f"UPDATE guard_runs SET {cols} WHERE entry=?", (*f.values(), entry))

    def _drop_run(self, entry: int) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM guard_runs WHERE entry=?", (entry,))

    def _duty(self) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM guard_duties WHERE ended_ms IS NULL "
                             "ORDER BY id DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def _log(self, kind: str, *, robot: str = "", entry: int | None = None, zone: str = "",
             point: str = "", detail: str = "", duty: int | None = None) -> None:
        if duty is None:
            d = self._duty()
            if d is None:
                return
            duty = d["id"]
        with self.db.tx() as c:
            c.execute("INSERT INTO guard_log(duty_id, at_ms, robot_id, entry, kind, zone, point, "
                      "detail) VALUES (?,?,?,?,?,?,?,?)",
                      (duty, self._now(), robot, entry, kind, zone, point, detail[:500]))

    def _seen(self, map_id: str, version: str) -> dict[str, int]:
        return {r["zone"]: r["last_ms"] for r in self.db.query(
            "SELECT zone, last_ms FROM zone_seen WHERE map_id=? AND map_version=?",
            (map_id, version))}

    def _mark_seen(self, map_id: str, version: str, zone: str, robot: str, how: str) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO zone_seen(map_id, map_version, zone, last_ms, robot_id, how) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT(map_id, map_version, zone) DO UPDATE SET "
                      "last_ms=excluded.last_ms, robot_id=excluded.robot_id, how=excluded.how",
                      (map_id, version, zone, self._now(), robot, how))

    # ------------------------------------------------------------ 给别人问的

    def holds(self, robot_id: str, task_id: str) -> bool:
        """待命点管理器问:这一趟跑完先别回。值守中的任务(一圈、去值守位)是;收队了就不是。"""
        if not task_id.startswith(GUARD_PREFIX):
            return False
        return bool(self.db.query("SELECT 1 FROM guard_runs WHERE robot_id=? LIMIT 1",
                                  (robot_id,)))

    def watching(self, robot_id: str) -> str | None:
        """这台此刻站在哪个防区的值守位上盯着(看见人按这个防区判,设计 §5.4);不在值守位上 ``None``。
        """
        rows = self.db.query("SELECT watch_zone FROM guard_runs WHERE robot_id=? "
                             "AND state IN ('post', 'held')",
                             (robot_id,))
        return rows[0]["watch_zone"] or None if rows else None

    # ------------------------------------------------------------ 机器人

    def _client(self, rid: str) -> Any:
        return self.dispatcher.clients.get(rid)

    def _running(self, rid: str) -> str:
        c = self._client(rid)
        t = c.status.task if c is not None and c.status is not None else None
        return t.task_id if t is not None else ""

    def _blocking(self, rid: str) -> str:
        """机器人手上那一趟要不要等它跑完:值守自己的、回待命点的不用等;优先级比值守一圈低的(排程巡逻)
        也不用等 —— 一圈派下去狗自己抢下来(设计 §5.2 的优先级)。要等 → 那一趟的任务号,不用等 →
        空串。"""
        running = self._running(rid)
        if not running or running.startswith((GUARD_PREFIX, STANDBY_PREFIX)):
            return ""
        rows = self.db.query("SELECT priority FROM commands WHERE robot_id=? AND task_id=? "
                             "AND kind IN ('patrol', 'goto') ORDER BY rowid DESC LIMIT 1",
                             (rid, running))
        if rows and rows[0]["priority"] is not None and int(rows[0]["priority"]) < GUARD_LAP:
            return ""
        return running

    def _prep_alerts(self, plan: dict[str, Any]) -> None:
        """W73 §7.1:布防开始前 :data:`PREP_MIN` 分钟,值守方案里指定的机器人电量过不了一圈的出站线
        → P2 ``guard_prep``「布防前请先充电 / 换电池」(一段布防一次)。"""
        sched = getattr(self.arming, "schedule", None)
        if self.alerts is None or sched is None or not sched.enabled():
            return
        now = self._now()
        nb = sched.next_boundary(now)
        if nb is None or nb - now > PREP_MIN * 60_000 or nb <= now:
            return
        at = datetime.fromtimestamp(nb / 1000, self._tz()).strftime("%H:%M")
        for e in plan["entries"]:
            rid, info = e["robot"], self._route(e["route"])
            if not rid or info is None or self._prep_told.get(rid) == nb:
                continue
            pct = self._battery(rid)
            line = max(self.min_pct, energy.lap_line(self._lap_s(info) or 0.0))
            if pct is not None and pct < line:
                self._prep_told[rid] = nb
                self.alerts.raise_alert(
                    kind="guard_prep", robot=rid, title=f"{rid} 电量不够开始值守",
                    detail=f"{at} 开始布防值守,{rid} 现在 {pct:.0f}%,跑一圈要 ≥ {line:.0f}%:"
                           f"布防前请先充电 / 换电池", now_ms=now)

    def _tz(self) -> Any:
        sched = getattr(self.arming, "schedule", None)
        try:
            return sched._tz() if sched is not None else None
        except Exception:  # noqa: BLE001 - 时区读不出:按本机
            return None

    def topup_line(self, rid: str) -> float | None:
        """W73 第四期:这台值守着、电量过不了一圈的出站线 → 那条线(回充 / 换电按它起);不然 None。"""
        got = self._low_fresh(rid)
        return got[0] if got is not None else None

    def _low_fresh(self, rid: str) -> tuple[float, float, int] | None:
        """值守刚判过「不够」的(值守每秒判一次;两分钟没再判 = 不在值守了)。"""
        got = self.low.get(rid)
        if got is None or self._now() - got[2] > LOW_FRESH_MS:
            return None
        return got

    def topup_eta_min(self, rid: str) -> float | None:
        """值守着的这台大约再过几分钟要补能(电量掉到一圈的出站线);没在值守 → None。"""
        run = next((r for r in self._runs().values() if r["robot_id"] == rid), None)
        if run is None:
            return None
        e = next((x for x in self.plan()["entries"] if x["id"] == run["entry"]), None)
        info = self._route(e["route"]) if e is not None else None
        pct = self._battery(rid)
        if info is None or pct is None:
            return None
        line = max(self.min_pct, energy.lap_line(self._lap_s(info) or 0.0))
        return max(0.0, (pct - line) / (energy.DRAIN_PCT_PER_H / 60.0))

    def resume_pct(self, rid: str) -> float | None:
        """补能补到多少回来:够撑到这段布防结束,最多 95%(§7.3)。"""
        got = self._low_fresh(rid)
        if got is None:
            return None
        now = self._now()
        nb = None
        sched = getattr(self.arming, "schedule", None)
        if sched is not None:
            try:
                nb = sched.next_boundary(now)
            except Exception:  # noqa: BLE001 - 算不出:按最多充
                nb = None
        remaining = (nb - now) / 1000.0 if nb is not None and nb > now else 12 * 3600.0
        return energy.topup_target(remaining, got[1])

    def robots(self) -> set[str]:
        """值守中的机器人(排程别往它们身上派)。"""
        return {r["robot_id"] for r in self.db.query("SELECT robot_id FROM guard_runs")}

    def _battery(self, rid: str) -> float | None:
        c = self._client(rid)
        t = c.telemetry if c is not None else None
        return float(t.battery_pct) if t is not None and t.battery_pct is not None else None

    def _why_not(self, rid: str, info: dict[str, Any]) -> str:
        """这台现在能不能派去跑一圈;能 → 空串,不能 → 为什么。"""
        c = self._client(rid)
        if c is None:
            return f"{rid} 没登记"
        if c.status is None or not c.status.online:
            return f"{rid} 不在线"
        fresh = getattr(self.dispatcher, "_fresh", None)
        if callable(fresh) and not fresh(c):
            return f"{rid} 的状态不新鲜"
        m = getattr(getattr(self.dispatcher, "registry", None), "manual_only", None)
        if callable(m) and m(rid) is not None:
            return f"{rid} 设成只许手动派"
        auto = getattr(self.dispatcher, "autonomy", None)
        if callable(auto) and auto(rid) != "autonomous":
            return f"{rid} 要人在旁边监护,不自己出去"
        if self.busy is not None and rid in self.busy():
            return f"{rid} 在驱离"
        if self.charging is not None and self.charging(rid):
            return f"{rid} 在回充"
        caps = c.capabilities.tasks.get("patrol", {}) if c.capabilities is not None else {}
        if (caps.get("map_id"), caps.get("map_version")) != (info["map_id"], info["map_version"]):
            return (f"{rid} 加载的地图不是 {info['map_id']} v{info['map_version']}"
                    f"(防区路线 {info['id']} 画在这一版上)")
        pct = self._battery(rid)
        if pct is None:
            return f"{rid} 还没报电量"
        lap_s = self._lap_s(info) or 0.0
        line = max(self.min_pct, energy.lap_line(lap_s))
        if pct < line:                                   # W73 第四期:出站线,不够就先补能
            self.low[rid] = (line, lap_s, self._now())
            return f"{rid} 电量 {pct:.0f}%,不够跑一圈(要 ≥ {line:.0f}%),先补能"
        self.low.pop(rid, None)
        if self.swap is not None:                        # 外审 r1 #1:有一块拔着、在等换电池不派
            w = self.swap.busy(rid)
            if w:
                return w
        running = self._blocking(rid)
        if running:
            return f"{rid} 在跑别的任务 {running}"
        return ""

    def _pick(self, e: dict[str, Any], info: dict[str, Any], taken: set[str]) -> tuple[str, str]:
        """→ (挑中的机器人, 为什么一台都挑不出)。指定了机器人的就是它(能不能派由派之前查)。"""
        if e["robot"]:
            if e["robot"] in taken:                      # 外审 r1 #6:别的方案占着
                return "", f"{e['robot']} 在跑另一条值守方案"
            return e["robot"], ""
        why = []
        cands = []
        for rid in sorted(self.dispatcher.clients):
            if rid in taken or rid in self._reserved:
                continue
            w = self._why_not(rid, info)
            if w:
                why.append(w)
            else:
                cands.append(rid)
        if not cands:
            return "", ";".join(why) or "没有登记的机器人"
        cands.sort(key=lambda r: -(self._battery(r) or 0.0))
        return cands[0], ""

    def _wait(self, e: dict[str, Any], rid: str, why: str) -> None:
        """记一笔「等机器人」(原因变了才记)。"""
        if self._waiting.get(e["id"]) != why:
            self._waiting[e["id"]] = why
            self._log("waiting", robot=rid, entry=e["id"], detail=why)

    # ------------------------------------------------------------ 每拍

    async def tick(self) -> None:
        plan = self.plan()
        entries = {e["id"]: e for e in plan["entries"]}
        runs = self._runs()
        for eid, r in runs.items():
            e = entries.get(eid)
            if e is None or (e["robot"] and e["robot"] != r["robot_id"]):
                await self._stand_down(r, "值守方案改了")
        runs = self._runs()
        active: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
        for e in plan["entries"]:
            info = self._route(e["route"])
            armed = [z for z in info["zones"] if self._armed(z)] if info else []
            if armed:
                active.append((e, info, armed))  # type: ignore[arg-type]
            elif e["id"] in runs:
                await self._stand_down(runs[e["id"]], "这条路线经过的防区都撤防了")
        duty = self._duty()
        if active and duty is None:
            with self.db.tx() as c:
                c.execute("INSERT INTO guard_duties(started_ms) VALUES (?)", (self._now(),))
            duty = self._duty()
            self._log("duty_start", detail="布防开始值守")
        # 外审 r1 #6:布防着的固定方案先把自己的机器人留住;「任意一台」占着它的,跑完这一圈让出来
        self._reserved = {e["robot"] for e, _, _ in active if e["robot"]}
        for e, _, _ in active:
            r = runs.get(e["id"])
            if not e["robot"] and r is not None and r["robot_id"] in self._reserved \
                    and r["state"] not in ("lap", "held", "stopping"):
                await self._stand_down(r, f"{r['robot_id']} 让给指定它的值守方案")
        runs = self._runs()
        for e, info, armed in active:
            try:
                await self._drive(e, info, armed, runs.get(e["id"]),
                                  {r["robot_id"] for k, r in runs.items() if k != e["id"]})
            except Exception:
                log.exception("值守方案 %s 这一拍没办成", e["id"])
            runs = self._runs()
        self._post_seen(active)
        if not active and duty is not None:
            self._log("duty_end", detail="布防结束(有方案的防区都撤防了)")
            with self.db.tx() as c:
                c.execute("UPDATE guard_duties SET ended_ms=? WHERE id=?",
                          (self._now(), duty["id"]))
            duty = None
        try:
            self._zone_alerts(plan, active, duty)
        except Exception:
            log.exception("防区多久没看这一拍没对上账")
        if duty is None:
            try:
                self._prep_alerts(plan)
            except Exception:
                log.exception("布防前电量检查没办成")

    async def _drive(self, e: dict[str, Any], info: dict[str, Any], armed: list[str],
                     run: dict[str, Any] | None, taken: set[str]) -> None:
        now = self._now()
        if run is None:
            rid, why = self._pick(e, info, taken)
            if not rid:
                self._wait(e, "", why)
                return
            await self._start_lap(e, info, armed, rid, None)
            return
        rid, state = run["robot_id"], run["state"]
        running = self._running(rid)
        if state == "stopping":
            await self._stopping(e, run, running)
            return
        if await self._person_hold(e, run, running):
            return
        foreign = bool(self._blocking(rid))
        if state in ("post", "wait") and foreign:
            self._log("interrupted", robot=rid, entry=e["id"], zone=run["watch_zone"],
                      detail=f"在值守位上被 {running} 叫走")
            self._set_run(e["id"], state="resume", until_ms=now)
            return
        if state == "lap":
            await self._lap_state(e, info, armed, run, running)
        elif state == "watch":
            term = self._terminal(rid, run["task_id"])
            if now >= run["next_lap_ms"]:
                await self._start_lap(e, info, armed, rid, run)
            elif term is None:
                if running != run["task_id"] and now - run["sent_ms"] >= LOST_S * 1000:
                    self._set_run(e["id"], state="wait")
            elif term[0] == "task_done":
                self._set_run(e["id"], state="post")
                self._log("watch_arrive", robot=rid, entry=e["id"], zone=run["watch_zone"])
            elif term[0] == "task_preempted":
                self._log("interrupted", robot=rid, entry=e["id"], zone=run["watch_zone"],
                          detail="去值守位的路上被叫走")
                self._set_run(e["id"], state="resume", until_ms=now)
            else:
                self._log("watch_failed", robot=rid, entry=e["id"], zone=run["watch_zone"],
                          detail=str(term[1].get("reason") or term[0])[:200])
                self._set_run(e["id"], state="wait")
        elif state in ("post", "wait"):
            if now >= run["next_lap_ms"]:
                await self._start_lap(e, info, armed, rid, run)
        elif state == "resume":
            if now >= run["until_ms"] and not foreign:
                await self._start_lap(e, info, armed, rid, run, resume=True)
        elif state == "paused":
            if now >= run["until_ms"]:
                self._set_run(e["id"], state="resume", until_ms=now)

    def _person_open(self, rid: str) -> bool:
        if self.alerts is None:
            return False
        if not self.alerts.has_open(rid, PERSON_KIND):
            self._person_done.discard(rid)               # 那一条处理掉了:下一回看见人照样盯
            return False
        return rid not in self._person_done

    async def _person_hold(self, e: dict[str, Any], run: dict[str, Any], running: str) -> bool:
        """看见人(设计 §5.4):停在原地盯着,等保安下结论;处理了、或盯满 :data:`PERSON_HOLD_MS`
        没人管 → 接着值守。在盯 → ``True``(这一拍别的不办)。"""
        eid, rid, now = e["id"], run["robot_id"], self._now()
        if run["state"] == "held":
            if self._person_open(rid) and now < run["until_ms"]:
                return True
            if now >= run["until_ms"]:
                self._person_done.add(rid)
            self._log("person_resume", robot=rid, entry=eid,
                      detail="保安处理了,接着值守" if now < run["until_ms"]
                      else f"盯了 {PERSON_HOLD_MS // 60_000} 分钟没人管,接着值守")
            self._set_run(eid, state="resume", until_ms=now)
            return True
        if not self._person_open(rid):
            return False
        self._log("person_hold", robot=rid, entry=eid, zone=run["watch_zone"],
                  detail="看见人:原地停下盯着,等保安处理")
        if running and running == run["task_id"]:        # 一圈 / 去值守位的路上:停在原地
            await self._stop(eid, run, "held")
        else:
            self._set_run(eid, state="held", until_ms=now + PERSON_HOLD_MS)
        return True

    async def _stop(self, eid: int, run: dict[str, Any], then: str) -> None:
        """停手上这一趟:记成 ``stopping``、发中止;确认停下(:meth:`_stopping`)才进 ``then``。"""
        self._stop_then[eid] = then
        self._stop_tries[eid] = 0
        self._set_run(eid, state="stopping")
        await self._send_abort(eid, run)

    async def _send_abort(self, eid: int, run: dict[str, Any]) -> None:
        rid = run["robot_id"]
        self._stop_tries[eid] = self._stop_tries.get(eid, 0) + 1
        self._set_run(eid, until_ms=self._now() + STOP_RETRY_MS)
        try:
            r = await self.dispatcher.abort(rid, run["task_id"], issued_by="guard")
            if not _accepted(r):
                log.warning("%s 停这一圈没被接:%s", rid, r)
        except Exception as exc:  # noqa: BLE001 - 没发成:过几秒再发
            log.warning("%s 停这一圈没发成:%s", rid, exc)

    async def _stopping(self, e: dict[str, Any], run: dict[str, Any], running: str) -> None:
        """在等这一趟停下:旧任务有结果了、或手上已经不是它 = 停了。没停到点再发中止,
        发够 :data:`STOP_ALERT` 次报警;停下之前什么都不派。"""
        eid, rid, now = e["id"], run["robot_id"], self._now()
        stopped = running != run["task_id"] or self._terminal(rid, run["task_id"]) is not None
        if stopped:
            then = self._stop_then.pop(eid, "resume")
            self._stop_tries.pop(eid, None)
            if then == "held":
                self._set_run(eid, state="held", until_ms=now + PERSON_HOLD_MS)
            else:
                self._set_run(eid, state="resume", until_ms=now + SETTLE_MS)
            return
        if now < run["until_ms"]:
            return
        tries = self._stop_tries.get(eid, 0)
        if tries >= STOP_ALERT and self.alerts is not None \
                and not self.alerts.has_open(rid, "guard_stuck"):
            self._log("stop_failed", robot=rid, entry=eid,
                      detail=f"发了 {tries} 次中止,这一趟还没停下")
            self.alerts.raise_alert(
                kind="guard_stuck", robot=rid, title=f"{rid} 停不下这一圈",
                detail=f"值守要停下这一圈(看见人或撤防),发了 {tries} 次中止还在跑;"
                       "站点接着发,请看机器人是否在线、必要时到现场或用遥控器停下")
        await self._send_abort(eid, run)

    def _off_zones(self, info: dict[str, Any], armed: list[str], run: dict[str, Any]) -> list[str]:
        """这一圈还没走到的点里,所在防区已经撤防的(外审 r1 #4)。"""
        seen = self._seen(info["map_id"], info["map_version"])
        done = {z for z, t in seen.items() if t >= run["lap_started_ms"]}
        plan = json.loads(run["plan"] or "[]")
        return list(dict.fromkeys(p["zone"] for p in plan if p.get("zone")
                                  and p["zone"] not in armed and p["zone"] not in done))

    async def _lap_state(self, e: dict[str, Any], info: dict[str, Any], armed: list[str],
                         run: dict[str, Any], running: str) -> None:
        now, rid, eid = self._now(), run["robot_id"], e["id"]
        term = self._terminal(rid, run["task_id"])
        if term is None and running == run["task_id"]:
            off = self._off_zones(info, armed, run)
            if off:                                      # 外审 r1 #4:别把这一圈跑进撤防的防区
                self._log("zone_disarmed", robot=rid, entry=eid,
                          detail=f"{'、'.join(off)} 撤防了:这一圈停下,按还布防着的接着跑")
                await self._stop(eid, run, "resume")
                return
        if term is None:
            if running != run["task_id"] and now - run["sent_ms"] >= LOST_S * 1000:
                self._log("interrupted", robot=rid, entry=eid,
                          detail="派了没在跑,也没结果(当被打断)")
                self._set_run(eid, state="resume", until_ms=now)
            return
        kind, data = term
        if kind == "task_done":
            self._log("lap_done", robot=rid, entry=eid,
                      detail=f"第 {run['lap']} 圈跑完,用时 "
                             f"{(now - run['lap_started_ms']) // 1000} 秒")
            if run["fails"] and self.alerts is not None:
                self.alerts.resolve_all(rid, "guard_stuck", who="site:guard_lap_done")
            gap = int(e["gap_min"]) * 60_000
            nxt = max(run["lap_started_ms"] + gap, now) if gap else now
            # 先记成「等下一圈」:下一圈这一拍派不出去(机器人忙、电量低…)就在 wait 里接着等,
            # 不会每拍把这一圈的「跑完」再记一遍
            self._set_run(eid, state="wait", fails=0, next_lap_ms=nxt)
            if nxt - now >= WATCH_MIN_S * 1000:
                await self._go_watch(e, info, {**run, "fails": 0}, nxt)
            else:
                await self._start_lap(e, info, armed, rid, {**run, "fails": 0})
        elif kind == "task_preempted":
            self._log("interrupted", robot=rid, entry=eid,
                      detail=f"第 {run['lap']} 圈被 {running or '别的任务'} 打断")
            self._set_run(eid, state="resume", until_ms=now)
        elif kind == "task_aborted":
            self._log("aborted", robot=rid, entry=eid,
                      detail=f"第 {run['lap']} 圈被人中止:歇 {PAUSE_MS // 60_000} 分钟再接着跑")
            self._set_run(eid, state="paused", until_ms=now + PAUSE_MS)
        else:
            fails = run["fails"] + 1
            why = str(data.get("reason") or data.get("error") or "")[:200]
            self._log("lap_failed", robot=rid, entry=eid, detail=f"第 {run['lap']} 圈跑不成:{why}")
            self._set_run(eid, state="resume", until_ms=now + FAIL_RETRY_MS, fails=fails)
            if fails >= FAIL_ALERT and self.alerts is not None \
                    and not self.alerts.has_open(rid, "guard_stuck"):
                self.alerts.raise_alert(
                    kind="guard_stuck", robot=rid,
                    title=f"防区路线 {info['id']} 连着 {fails} 圈跑不成",
                    detail=(why or "看机器人的日志") + ";站点一分钟后再试,路被堵了请人去看",
                    context={"route": info["id"]})

    def _terminal(self, rid: str, task_id: str) -> tuple[str, dict[str, Any]] | None:
        if not task_id:
            return None
        rows = self.db.query(
            "SELECT kind, data FROM events WHERE robot_id=? AND kind IN "
            "('task_done','task_failed','task_aborted','task_preempted') "
            "AND json_extract(data, '$.task_id')=? ORDER BY rowid DESC LIMIT 1", (rid, task_id))
        if not rows:
            return None
        try:
            data = json.loads(rows[0]["data"])
        except ValueError:
            data = {}
        return rows[0]["kind"], data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 派

    def _lap_points(self, info: dict[str, Any], armed: list[str], run: dict[str, Any] | None,
                    resume: bool, shuffle: bool) -> list[dict[str, Any]]:
        keep = [p for p in info["points"] if not p["zone"] or p["zone"] in armed]
        seen = self._seen(info["map_id"], info["map_version"])
        if resume and run is not None:
            done = {z for z, t in seen.items() if t >= run["lap_started_ms"]}
            rest = [p for p in keep if not p["zone"] or p["zone"] not in done]
            if any(p["zone"] for p in rest):
                keep = rest
        zones = [p["zone"] for p in keep if p["zone"]]
        if not zones:
            return []
        if shuffle:
            start = self.rng.randrange(len(keep))
            out = keep[start:] + keep[:start]
            return out[::-1] if self.rng.random() < 0.5 else out
        order = list(dict.fromkeys(zones))
        oldest = min(order, key=lambda z: (seen.get(z, 0), order.index(z)))
        start = next(i for i, p in enumerate(keep) if p["zone"] == oldest)
        return keep[start:] + keep[:start]

    async def _start_lap(self, e: dict[str, Any], info: dict[str, Any], armed: list[str],
                         rid: str, run: dict[str, Any] | None, *, resume: bool = False) -> None:
        eid, now = e["id"], self._now()
        if now < self._retry_at.get(eid, 0):
            return
        why = self._why_not(rid, info)
        if why:
            self._wait(e, rid, why)
            return
        pts = self._lap_points(info, armed, run, resume, bool(e["shuffle"]))
        if not pts:
            self._wait(e, rid, f"防区路线 {info['id']} 上布防着的防区里没有点")
            return
        self._waiting.pop(eid, None)
        self._retry_at[eid] = now + RETRY_MS              # 派成了再清
        names = [p["name"] for p in pts]
        by_name = {w["name"]: w for w in info["wire"]["waypoints"]}
        mission = {**info["wire"], "waypoints": [by_name[n] for n in names]}
        tid = f"{GUARD_PREFIX}{eid}-{uuid.uuid4().hex[:10]}"
        lap = (run["lap"] if run else 0) + (0 if resume else 1)
        started = run["lap_started_ms"] if (resume and run) else now
        plan = json.dumps([{"name": p["name"], "zone": p["zone"]} for p in pts], ensure_ascii=False)
        with self.db.tx() as c:                          # 先落库:写不进去就不派
            c.execute("INSERT OR REPLACE INTO guard_runs(entry, robot_id, state, task_id, sent_ms, "
                      "lap, lap_started_ms, next_lap_ms, until_ms, plan, watch_zone, fails) "
                      "VALUES (?,?,?,?,?,?,?,?,0,?,'',?)",
                      (eid, rid, "lap", tid, now, lap, started,
                       started + int(e["gap_min"]) * 60_000, plan, run["fails"] if run else 0))
        self._log("resume" if resume else "lap_start", robot=rid, entry=eid,
                  detail=(f"第 {lap} 圈接着跑" if resume else f"第 {lap} 圈") + ":"
                  + "→".join(names))
        from d1max_site.dispatcher import DispatchRefused
        try:
            r = await self.dispatcher.patrol(rid, mission, issued_by="guard", priority=GUARD_LAP,
                                             task_id=tid)
        except DispatchRefused as exc:
            self._refused(e, rid, run, str(exc), resume)
            return
        except Exception as exc:  # noqa: BLE001 - 说不清狗收没收:留着,按终态 / 丢了对账
            log.warning("%s 值守一圈没派成(回执没到?):%s", rid, exc)
            return
        if not _accepted(r):
            ack = (r or {}).get("ack", {}) if isinstance(r, dict) else {}
            self._refused(e, rid, run, str(ack.get("reason") or ack.get("result") or "被拒"),
                          resume)
            return
        self._retry_at.pop(eid, None)

    def _refused(self, e: dict[str, Any], rid: str, run: dict[str, Any] | None, why: str,
                 resume: bool) -> None:
        """派不出去:退回上一步(刚起的作废;接着跑的、下一圈的过一阵再试)。"""
        self._wait(e, rid, f"{rid} 没接:{why}")
        if run is None:
            self._drop_run(e["id"])
            return
        back = {"task_id": run["task_id"], "lap": run["lap"],
                "lap_started_ms": run["lap_started_ms"], "plan": run["plan"]}
        if resume:
            self._set_run(e["id"], state="resume", until_ms=self._now() + RETRY_MS, **back)
        else:
            self._set_run(e["id"], state="wait", next_lap_ms=self._now() + RETRY_MS, **back)

    async def _go_watch(self, e: dict[str, Any], info: dict[str, Any], run: dict[str, Any],
                        next_ms: int) -> None:
        """一圈跑完、离下一圈还有空档:去这一圈最早看的那个防区的值守位站着盯。"""
        eid, rid, now = e["id"], run["robot_id"], self._now()
        plan = json.loads(run["plan"] or "[]")
        zone = next((p["zone"] for p in plan if p.get("zone")), "")
        try:
            post = (self.posts(info["map_id"], info["map_version"]).get(zone) or {}).get("watch")
        except Exception:  # 选不出值守位:原地等
            log.exception("%s 的值守位拿不到", zone)
            post = None
        if not post:
            self._set_run(eid, state="wait", next_lap_ms=next_ms, watch_zone=zone)
            self._log("watch_none", robot=rid, entry=eid, zone=zone,
                      detail="这个防区没有值守位:原地等下一圈")
            return
        tid = f"{GUARD_PREFIX}watch-{eid}-{uuid.uuid4().hex[:10]}"
        self._set_run(eid, state="watch", task_id=tid, sent_ms=now, next_lap_ms=next_ms,
                      watch_zone=zone)
        self._log("watch_go", robot=rid, entry=eid, zone=zone,
                  detail=f"离下一圈还有 {(next_ms - now) // 1000} 秒,去值守位盯着")
        target = MapPose(map_id=info["map_id"], map_version=info["map_version"], frame_id="map",
                         x=float(post["x"]), y=float(post["y"]),
                         yaw=float(post.get("yaw", 0.0))).to_wire()
        try:
            r = await self.dispatcher.goto(rid, target, None, issued_by="guard",
                                           priority=GUARD_WATCH, task_id=tid)
        except Exception as exc:  # noqa: BLE001 - 去不了值守位:原地等下一圈
            log.warning("%s 去值守位没派成:%s", rid, exc)
            self._set_run(eid, state="wait")
            return
        if not _accepted(r):
            self._set_run(eid, state="wait")

    async def _stand_down(self, run: dict[str, Any], why: str) -> None:
        """收队:手上这一趟停下,回待命点交给待命点管理器(先删进度,:meth:`holds` 就放开了)。"""
        rid, tid = run["robot_id"], run["task_id"]
        self._drop_run(run["entry"])
        self._retry_at.pop(run["entry"], None)
        self._log("stand_down", robot=rid, entry=run["entry"], detail=why)
        running = self._running(rid)
        try:
            if tid and running == tid:
                await self.dispatcher.abort(rid, tid, issued_by="guard")
            elif not running and self.standby is not None and run["state"] in ("post", "wait"):
                await self.standby.return_to(rid, issued_by="guard")
        except Exception as exc:  # noqa: BLE001 - 收不了队:记下,机器人原地;人看值守记录
            log.warning("%s 收队没办成:%s", rid, exc)
            self._log("stand_down_failed", robot=rid, entry=run["entry"], detail=str(exc)[:200])

    # ------------------------------------------------------------ 看着

    def _on_event(self, robot_id: str, e: Any) -> None:
        if e.kind != "patrol_waypoint" or not isinstance(e.data, dict):
            return
        tid = str(e.data.get("task_id") or "")
        if not tid.startswith(GUARD_PREFIX):
            return
        try:
            rows = self.db.query("SELECT r.entry, r.plan, p.route FROM guard_runs r "
                                 "JOIN guard_plan p ON p.id=r.entry WHERE r.task_id=?", (tid,))
            if not rows:
                return
            name = str(e.data.get("name", ""))
            zone = next((p["zone"] for p in json.loads(rows[0]["plan"] or "[]")
                         if p.get("name") == name), "")
            info = self._route(rows[0]["route"])
            if e.data.get("ok"):
                if zone and info is not None:
                    self._mark_seen(info["map_id"], info["map_version"], zone, robot_id, "point")
                self._log("point", robot=robot_id, entry=rows[0]["entry"], zone=zone, point=name)
            else:
                self._log("missed", robot=robot_id, entry=rows[0]["entry"], zone=zone, point=name,
                          detail=str(e.data.get("note") or "")[:200])
        except Exception:
            log.exception("%s 值守的到点事件记不下来", robot_id)

    def _post_seen(self, active: list[tuple[dict[str, Any], dict[str, Any], list[str]]]) -> None:
        """站在值守位上(离得够近)就算看着那个防区。"""
        runs = self._runs()
        for e, info, _ in active:
            r = runs.get(e["id"])
            if r is None or r["state"] != "post" or not r["watch_zone"]:
                continue
            c = self._client(r["robot_id"])
            fresh = getattr(self.dispatcher, "_fresh", None)
            if c is None or c.status is None or not c.status.online \
                    or (callable(fresh) and not fresh(c)):
                continue                                 # 外审 r1 #7:掉线、状态旧了不算看着
            pose = c.telemetry.pose if c.telemetry is not None else None
            if pose is not None and (getattr(pose, "map_id", info["map_id"]) != info["map_id"]
                                     or str(getattr(pose, "map_version", info["map_version"]))
                                     != str(info["map_version"])):
                continue
            try:
                book = self.posts(info["map_id"], info["map_version"])
                post = (book.get(r["watch_zone"]) or {}).get("watch")
            except Exception:  # noqa: BLE001 - 值守位拿不到:这一拍不算
                post = None
            if pose is None or not post:
                continue
            if math.hypot(pose.x - float(post["x"]), pose.y - float(post["y"])) <= POST_NEAR_M:
                self._mark_seen(info["map_id"], info["map_version"], r["watch_zone"],
                                r["robot_id"], "post")

    # ------------------------------------------------------------ 预报、超时

    def _lap_s(self, info: dict[str, Any]) -> float | None:
        key = (info["id"], info["revision"])
        if key not in self._lap_cache:
            from d1max_site.route_eta import estimate
            flat = {"id": info["id"], "map_id": info["map_id"], "map_version": info["map_version"],
                    "waypoints": [{"name": p["name"], "x": p["x"], "y": p["y"],
                                   "yaw": float((w.get("pose") or {}).get("yaw", 0.0) or 0.0),
                                   **({"actions": w["actions"]} if w.get("actions") else {})}
                                  for p, w in zip(info["points"], info["wire"]["waypoints"],
                                                  strict=True)]}
            try:
                got = estimate(self.db, self.reach, flat)
                total = float(got["total_s"])
                if len(flat["waypoints"]) > 1:   # 一圈跑完回到第一个点那一段
                    a, b = flat["waypoints"][-1], flat["waypoints"][0]
                    m = self.reach.path_m(info["map_id"], info["map_version"], (a["x"], a["y"]),
                                          (b["x"], b["y"])) if self.reach is not None else None
                    if m is None:
                        m = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                    from d1max_site.route_eta import NOMINAL_SPEED_MPS
                    total += m / NOMINAL_SPEED_MPS
                self._lap_cache[key] = round(total, 1) if total > 0 else None
            except Exception:  # 估不出:预报里说
                log.exception("%s 一圈多久估不出来", info["id"])
                self._lap_cache[key] = None
        return self._lap_cache[key]

    def forecast(self, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        from d1max_site.guard_forecast import forecast
        plan = plan or self.plan()
        lap_s: dict[str, float | None] = {}
        zones_of: dict[str, list[str]] = {}
        maps: set[tuple[str, str]] = set()
        for e in plan["entries"]:
            info = self._route(e["route"])
            if info is None:
                continue
            lap_s[e["route"]] = self._lap_s(info)
            zones_of[e["route"]] = info["zones"]
            maps.add((info["map_id"], info["map_version"]))
        armable: list[str] = []
        for m, v in sorted(maps):
            for a in self.areas.list(m, v):
                if a["zone"] not in armable:
                    armable.append(a["zone"])
        sched = getattr(self.arming, "schedule", None)
        view = sched.view() if sched is not None else {"enabled": False, "slots": []}
        return forecast(plan["entries"], lap_s=lap_s, zones_of=zones_of, schedule=view,
                        limits=plan["limits"], armable=armable)

    def _zone_alerts(self, plan: dict[str, Any], active: list, duty: dict[str, Any] | None) -> None:
        if self.alerts is None:
            return
        keep: set[str] = set()
        if duty is not None and active:
            limits = {z["zone"]: z["limit_min"] for z in self.forecast(plan)["zones"]}
            runs = self._runs()
            now = self._now()
            watched: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
            for e, info, armed in active:
                for z in armed:
                    watched.setdefault(z, []).append((e, info))
            for z, who in watched.items():
                keep.add(z)
                info = who[0][1]
                last = self._seen(info["map_id"], info["map_version"]).get(z, 0)
                base = max(last, duty["started_ms"])
                limit = int(limits.get(z, 15)) * 60_000
                key = f"{ZONE_PREFIX}{z}"
                if now - base > limit:
                    if not self.alerts.has_open(key, ALERT_KIND):
                        mins = (now - base) // 60_000
                        reasons = [self._reason(e, info, runs.get(e["id"])) for e, info in who]
                        self.alerts.raise_alert(
                            kind=ALERT_KIND, robot=key, title=f"{z} 已 {mins} 分钟没巡到",
                            detail="原因:" + ";".join(r for r in reasons if r)
                            + ";先看这个防区的摄像头",
                            context={"zone": z, "last_seen_ms": last or None})
                elif self.alerts.has_open(key, ALERT_KIND):
                    self.alerts.resolve_all(key, ALERT_KIND, who="site:zone_seen")
        self.alerts.resolve_except(ALERT_KIND, ZONE_PREFIX, keep, who="site:zone_not_watched")

    def _reason(self, e: dict[str, Any], info: dict[str, Any], run: dict[str, Any] | None) -> str:
        if run is None:
            return self._waiting.get(e["id"]) or (f"{e['robot']} 还没出发" if e["robot"]
                                                  else "还没挑到机器人")
        rid, st = run["robot_id"], run["state"]
        why = self._why_not(rid, info)
        if st == "lap":
            return why or f"{rid} 在跑第 {run['lap']} 圈(可能被挡住了、走得慢)"
        if st == "paused":
            return f"{rid} 的值守被人中止,歇着"
        if st == "resume":
            return why or f"{rid} 被叫走了,等它回来接着跑"
        return why or f"{rid} 在值守位上盯着别的防区"

    # ------------------------------------------------------------ 看

    def status(self) -> dict[str, Any]:
        duty = self._duty()
        plan = self.plan()
        runs = list(self._runs().values())
        fc = {z["zone"]: z for z in self.forecast(plan)["zones"]} if plan["entries"] else {}
        now = self._now()
        zones = []
        seen_maps: set[tuple[str, str]] = set()
        for e in plan["entries"]:
            info = self._route(e["route"])
            if info is None or (info["map_id"], info["map_version"]) in seen_maps:
                continue
            seen_maps.add((info["map_id"], info["map_version"]))
            rows = {r["zone"]: dict(r) for r in self.db.query(
                "SELECT * FROM zone_seen WHERE map_id=? AND map_version=?",
                (info["map_id"], info["map_version"]))}
            for z in fc:
                if z not in info["zones"] or any(x["zone"] == z for x in zones):
                    continue
                r = rows.get(z)
                last = r["last_ms"] if r else None
                limit = fc[z]["limit_min"]
                zones.append({"zone": z, "map_id": info["map_id"],
                              "map_version": info["map_version"],
                              "last_ms": last, "robot_id": r["robot_id"] if r else "",
                              "how": r["how"] if r else "", "limit_min": limit,
                              "armed": self._armed(z),
                              "overdue": bool(duty is not None and self._armed(z) and now - max(
                                  last or 0, duty["started_ms"]) > limit * 60_000)})
        return {"duty": {"id": duty["id"], "started_ms": duty["started_ms"]} if duty else None,
                "runs": [{k: r[k] for k in ("entry", "robot_id", "state", "lap", "task_id",
                                            "next_lap_ms", "watch_zone")} for r in runs],
                "waiting": {str(k): v for k, v in self._waiting.items()},
                "swaps": self.swap.view()["robots"] if self.swap is not None else [],
                "zones": zones, "now_ms": now}

    def _summary(self, d: dict[str, Any]) -> dict[str, Any]:
        rows = [dict(r) for r in self.db.query(
            "SELECT kind, zone, at_ms FROM guard_log WHERE duty_id=? ORDER BY id", (d["id"],))]
        end = d["ended_ms"] or self._now()
        zones: dict[str, dict[str, Any]] = {}
        last: dict[str, int] = {}
        for r in rows:
            if r["kind"] in ("point", "watch_arrive") and r["zone"]:
                z = zones.setdefault(r["zone"], {"zone": r["zone"], "looks": 0, "max_gap_s": 0})
                z["looks"] += 1
                prev = last.get(r["zone"], d["started_ms"])
                z["max_gap_s"] = max(z["max_gap_s"], (r["at_ms"] - prev) // 1000)
                last[r["zone"]] = r["at_ms"]
        for z, t in last.items():
            zones[z]["max_gap_s"] = max(zones[z]["max_gap_s"], (end - t) // 1000)
        count = lambda k: sum(1 for r in rows if r["kind"] == k)  # noqa: E731
        return {"id": d["id"], "started_ms": d["started_ms"], "ended_ms": d["ended_ms"],
                "laps": count("lap_done"), "interruptions": count("interrupted"),
                "missed": count("missed"), "failed": count("lap_failed"),
                "zones": sorted(zones.values(), key=lambda z: z["zone"])}

    def duties(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近几段布防的值守记录摘要(新的在前)。"""
        return [self._summary(dict(r)) for r in self.db.query(
            "SELECT * FROM guard_duties ORDER BY id DESC LIMIT ?", (max(1, min(200, limit)),))]

    def duty(self, duty_id: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM guard_duties WHERE id=?", (duty_id,))
        if not rows:
            return None
        out = self._summary(dict(rows[0]))
        out["log"] = [dict(r) for r in self.db.query(
            "SELECT at_ms, robot_id, entry, kind, zone, point, detail FROM guard_log "
            "WHERE duty_id=? ORDER BY id LIMIT 5000", (duty_id,))]
        return out
