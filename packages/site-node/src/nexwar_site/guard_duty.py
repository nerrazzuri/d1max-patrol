"""布防值守(W73 第二期,设计 §5):布防着的时候,机器人自己一圈圈跑防区路线;没有外部设备也能用。

**值守方案**(``guard_plan``,管理员在「布防时间」页配):哪条防区路线给哪台机器人(或任意一台)、
每圈之间隔多久(0 = 跑完马上下一圈)、要不要打乱起点和方向。布防时间表决定什么时候布防,值守方案
决定布防时谁跑哪条。方案挂在全站,不按时间段各挂一份:时间表本来就按防区、按星期分,一条方案在它
经过的任何一个防区布防着时就跑。

站点每拍对账(:meth:`GuardDuty.tick`,事件循环里的一条道):

- **起**:方案里的路线经过的防区有布防着的 → 挑机器人(在线、新鲜、自己能走、不是只许手动派、
  没在驱离、没在回充、加载着路线那张图、电量过 :data:`MIN_PCT`、手上没有要等的任务)→ 派一圈
  ``patrol``(``guard-<方案>-…``,优先级 :data:`~nexwar_site.priorities.GUARD_LAP`,排程巡逻让给它)。
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

from nexwar_contract.geometry import Pose
from nexwar_contract.messages import MapPose
from nexwar_site import energy
from nexwar_site.db import SiteDB
from nexwar_site.priorities import GUARD_LAP, GUARD_PREFIX, GUARD_WATCH, STANDBY_PREFIX
from nexwar_site.routes import NAME_RE

log = logging.getLogger(__name__)

#: 派了之后狗上既没在跑、也没终态事件,等这么久(秒)当被打断。从「最后一次看见它在跑这一趟、
#: 或者联系不上它」算起(11-04 现场:走远断网,站点收到离线状态手上没任务;断网期间不算,
#: 连回来以后再给这么久让它把离线时的事件补上来,别把还在跑的那一圈当打断重派)。
LOST_S = 90
#: 每圈结束回待命点时接在路线最后的那个点叫什么(跟路线里的点重名就加 *)。
STANDBY_WP = "回待命点"
#: 连回来以后要连着听得见这么久(毫秒),才信它报的「手上没任务」(11-04 G2:断线时离线状态里手上没任务,
#: 连回来那一下站点当它空了,接着跑这一圈,把人派的 goto 顶掉)。
SETTLE_HEARD_MS = 5_000
#: 站在值守位上算「看着」,位置要是这么久以内收到的(外审 r2 #7)。
POSE_FRESH_MS = 10_000
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
#: 看见人(P1 ``dog_sees_person``)以后原地盯着等保安下结论,最多这么久,之后接着值守(外审 r1 #5)。
#: W74 §九:30 分钟(10 分钟没人到场推给经理,在 :mod:`nexwar_site.sightings`);告警照样开着。
PERSON_HOLD_MS = 30 * 60_000
#: W74 §九:看见人以后接着值守的头这么久**加密巡查** —— 一圈只跑最后看见人那个防区和相邻的
#: (:meth:`~nexwar_site.areas.AreaBook.neighbours`),从那个防区先跑,每圈之间不歇(不去值守位)。
INTENSE_MS = 60 * 60_000
#: W74 三期:跟丢以后看易藏点、加密巡查先看易藏点时每个点停几秒(设计 §六)。
SWEEP_DWELL_S = 10
PERSON_KIND = "dog_sees_person"
#: 撤防时停下这一圈以后,过这么久按还布防着的接着跑(等狗上停稳)。
SETTLE_MS = 3_000
#: 要停这一圈(看见人、撤防)发了中止,这么久还没停下再发一次;发了 :data:`STOP_ALERT` 次还没停
#: 报 P2 ``guard_stuck``。确认停下之前不盯人、不派新的一圈(外审 r2 #5:中止没发成曾当已停)。
STOP_RETRY_MS = 5_000
STOP_ALERT = 3
#: W74 外审 r1 #7:派跟人被拒,过这么久再试;回执没到、机器人手上也一直不是它,过这么久当没派成、再派。
FOLLOW_RETRY_MS = 30_000
#: 刚派出去的跟人类任务,这么久以内机器人状态里没有它也不算停了(状态可能还没跟上;外审 r3 #1)。
FOLLOW_SEEN_MS = 5_000
#: 跟人和它派生的那几趟(回线内、看易藏点)的任务号开头(:data:`nexwar_contract.follow.TASK_PREFIX`)。
FOLLOW_PREFIX = "follow-"
#: 连着这么多圈跑不成报 P2。
FAIL_ALERT = 3
#: 电量不过这条线(%)不开新的一圈。第四期换成按任务算的出站线。
#: 「不够跑一圈」判过以后这么久内算数(回充 / 换电按它起)。
LOW_FRESH_MS = 120_000
#: 狗上起飞前检查说电量不够出发、带回它算的出发线(外审 r5 #4):这么久(毫秒)内站点的线不低于它,
#: 直到这台跑完一圈。两边的式子(原点、来路)不完全一样时以狗上为准,不一直派一直被拒。
DOG_NEED_MS = 6 * 3_600_000
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


def _need_pct(data: dict[str, Any]) -> float | None:
    """任务结局里狗上说的出发线(起飞前电量不够,外审 r5 #4);没有 → ``None``。"""
    v = data.get("need_pct")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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
        self._no_standby: set[tuple[int, str, str, str]] = set()
        self._waiting: dict[int, str] = {}
        self._route_cache: dict[tuple, dict[str, Any]] = {}
        self._lap_cache: dict[tuple, float | None] = {}
        #: 在停这一圈的方案:停下以后进哪一步(``held`` / ``resume``)、中止发了几次。只在内存:
        #: 站点重启后停下了按 ``resume`` 接着办(还看见人的话下一拍照样再盯)。
        self._stop_then: dict[int, str] = {}
        #: 狗上说的出发线(外审 r5 #4):机器人 → (线 %, 什么时候说的)。只在内存:
        #: 重启了再被拒一次就又知道。
        #: 狗上说过的出发线:``(线, 什么时候, 那一圈的预算上下文)``(外审 r5 #4;r6 #3 绑上下文)
        self._dog_need: dict[str, tuple[float, int, tuple[Any, ...] | None]] = {}
        #: 每台最近派出去那一圈的预算上下文(狗上回「不够」时记到 ``_dog_need`` 上)
        self._sent_key: dict[str, tuple[Any, ...]] = {}
        #: 每个方案最后一次看见机器人在跑派给它的那一趟、或者联系不上它的时刻(``LOST_S`` 从这算)。
        self._alive_ms: dict[int, int] = {}
        #: 每台机器人从什么时候起一直听得见(``SETTLE_HEARD_MS`` 从这算)。
        self._heard_since: dict[str, int] = {}
        self._stop_tries: dict[int, int] = {}
        #: W74:看见人开始盯的时候机器人在哪个防区(内存)。
        self._held_zone: dict[str, tuple[str, str, str]] = {}
        #: W74 加密巡查:机器人 → (到什么时候, 地图, 版本, 防区们(最后看见人的在第一个))。
        #: 只在内存:站点重启就回到平常的值守。
        self._intense: dict[str, tuple[int, str, str, list[str]]] = {}
        #: W74 三期:地图上的出口线、易藏点(:class:`~nexwar_site.map_marks.MapMarks`)。站点主程序
        #: 接上;没接就不判越线、不去看易藏点。
        self.marks: Any = None
        # 看见人这一回的账(跟人、越线、易藏点)落在值守进度 guard_runs.follow:见 _fs(外审 r2)
        dispatcher.on_event(self._on_event)

    # ------------------------------------------------------------ 方案

    def plan(self) -> dict[str, Any]:
        from nexwar_site.site_settings import SiteSettings
        meta = SiteSettings(self.db).get(_KEY, {}) or {}
        entries = [{"id": r["id"], "route": r["route"], "robot": r["robot"],
                    "gap_min": r["gap_min"], "shuffle": bool(r["shuffle"]),
                    "end_standby": bool(r["end_standby"])}
                   for r in self.db.query("SELECT * FROM guard_plan ORDER BY id")]
        limits = {r["zone"]: r["max_gap_min"]
                  for r in self.db.query("SELECT * FROM guard_zone_limits ORDER BY zone")}
        return {"revision": int(meta.get("revision", 0)), "entries": entries, "limits": limits,
                "updated_by": meta.get("by", ""), "updated_ms": meta.get("at", 0)}

    def put_plan(self, d: Any, *, by: str) -> dict[str, Any]:
        """整份换:``{"entries": [{route, robot?, gap_min?, shuffle?, end_standby?}],
        "limits"?: {防区: 分钟},
        "base_revision"}``。"""
        if not isinstance(d, dict):
            raise PlanError("要一个对象")
        raw = d.get("entries")
        if not isinstance(raw, list):
            raise PlanError("entries 要是一个列表")
        if len(raw) > MAX_ENTRIES:
            raise PlanError(f"值守方案最多 {MAX_ENTRIES} 条")
        rows: list[tuple[int, str, str, int, int, int]] = []
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
            end_standby = e.get("end_standby", True)
            if not isinstance(end_standby, bool):
                raise PlanError("end_standby 要是 true/false(每圈结束回待命点)")
            if robot:
                if robot in fixed:
                    raise PlanError(f"同一台机器人 {robot} 不能同时跑两条({fixed[robot]}、{route})")
                fixed[robot] = route
            rows.append((i, route, robot, gap, int(shuffle), int(end_standby)))
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
                                              for i, r, rb, *_ in rows]})
            if got:
                raise PlanError("跟巡逻排程撞了:" + ";".join(got))
        from nexwar_site.site_settings import SiteSettings
        now = self._now()
        with self.db.tx() as c:
            row = c.execute("SELECT value FROM site_settings WHERE key=?", (_KEY,)).fetchone()
            cur = int(json.loads(row["value"]).get("revision", 0)) if row else 0
            if d.get("base_revision") != cur:
                raise PlanConflict(f"值守方案别人刚改过(现在第 {cur} 版,你看到的是第 "
                                   f"{d.get('base_revision')} 版):刷新后再改")
            c.execute("DELETE FROM guard_plan")
            c.executemany("INSERT INTO guard_plan(id, route, robot, gap_min, shuffle, end_standby) "
                          "VALUES (?,?,?,?,?,?)", rows)
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
        """待命点管理器问:这一趟跑完先别回。值守中的任务(一圈、去值守位)是;收队了就不是。
        外审 r1 #3:看见人盯着的时候,跟人和它派生的那几趟(回线内、看易藏点)跑完也别回 —— 机器人要
        留在最后位置 / 出口看着,等保安。"""
        if task_id.startswith(FOLLOW_PREFIX):
            return bool(self.db.query(
                "SELECT 1 FROM guard_runs WHERE robot_id=? AND state IN ('held', 'stopping', "
                "'standdown') LIMIT 1", (robot_id,)))
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
            line = self._lap_need(e, info, rid)[0]
            if pct is not None and pct < line:
                self.alerts.raise_alert(
                    kind="guard_prep", robot=rid, title=f"{rid} 电量不够开始值守",
                    detail=f"{at} 开始布防值守,{rid} 现在 {pct:.0f}%,跑一圈要 ≥ {line:.0f}%:"
                           f"布防前请先充电 / 换电池")
                self._prep_told[rid] = nb                # 真发出去了才记(没发成下一拍补)

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
        line = self._lap_need(e, info, rid)[0]
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
        # 外审 r5 #4:补到的要盖过判「不够」的那条线(可能是狗上的),不然补完回来还是不够;
        # r6 #2:线高过平时最多补到的 95% 也要补过它(最多 100%;线 > 100% 的在 _why_not 里直接报)
        target = max(energy.topup_target(remaining, got[1]),
                     min(energy.MAX_RESUME_PCT, got[0] + 10.0))
        if target < got[0] + 0.5:
            target = min(100.0, got[0] + 1.0)
        return round(target, 1)

    def robots(self) -> set[str]:
        """值守中的机器人(排程别往它们身上派)。"""
        return {r["robot_id"] for r in self.db.query("SELECT robot_id FROM guard_runs")}

    def _battery(self, rid: str) -> float | None:
        c = self._client(rid)
        t = c.telemetry if c is not None else None
        return float(t.battery_pct) if t is not None and t.battery_pct is not None else None

    def _tail_xy(self, e: dict[str, Any] | None, info: dict[str, Any],
                 rid: str) -> tuple[float, float] | None:
        """这个方案每圈结束回的待命点(``end_standby`` 开着、这台在这张图上登记了)。"""
        if e is None or not e.get("end_standby", True) or self.standby is None or not rid:
            return None
        try:
            p = self.standby.swap_point(rid, info["map_id"], info["map_version"])
        except Exception:  # noqa: BLE001 - 拿不到:按不接
            return None
        return (float(p["x"]), float(p["y"])) if p else None

    def _lap_need(self, e: dict[str, Any] | None, info: dict[str, Any], rid: str,
                  pts: list[dict[str, Any]] | None = None) -> tuple[float, float]:
        """跑一圈要的电 → ``(线, 一圈秒数)``。外审 r3 #6:一圈算上回待命点那段;再按狗上出发线的式子
        算一遍(从现在的位置走完各点、到待命点、回原点),取大的 —— 站点放行的,狗上也接。
        ``pts``:这一圈实际要派的点(外审 r4 #3,按它的顺序算、到上限不接待命点);不给按现在布防着的
        防区挑(外审 r5 #3,跟派的时候同一份;一个都没布防 → 整条路线)。圈时也按这些点算。
        狗上说过更高的出发线(外审 r5 #4)→ 不低于它。"""
        from nexwar_site.dispatcher import MAX_PATROL_WAYPOINTS
        if pts is None:
            armed = [z for z in info["zones"] if self._armed(z)]
            pts = self._lap_points(info, armed, None, False, False) if armed else []
            pts = pts or info["points"]
        tail = self._tail_xy(e, info, rid) if len(pts) < MAX_PATROL_WAYPOINTS else None
        lap_s = self._lap_s(info, tail, pts) or 0.0
        line = max(self.min_pct, energy.lap_line(lap_s))
        # 外审 r4 #4:站点不知道狗上的原点就不按狗上的式子算(以前拿待命点当原点,远的待命点
        # 满电都不够),只按站点那条线;狗上真拒了,原因进值守记录
        h = self._home_xy(info, rid)
        if h is not None and pts:
            c = self._client(rid)
            pose = c.telemetry.pose if c is not None and c.telemetry is not None else None
            wps = [(float(p["x"]), float(p["y"])) for p in pts]
            if tail is not None:
                wps.append(tail)
            start = (pose.x, pose.y) if pose is not None and pose.map_id == info["map_id"] else h
            legs = [start, *wps]                         # 跟狗上 ``homing.route_length_m`` 一样
            caps = c.capabilities.tasks.get("goto", {}) if c is not None and c.capabilities else {}
            if str(caps.get("path", "straight")) == "planned":
                legs.append(h)
            else:                                        # 直线的:沿来路倒着回到出发点再回原点
                legs += [*reversed(wps[:-1]), start, h]
            d = sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(legs, legs[1:],
                                                                          strict=False))
            line = max(line, energy.patrol_departure_line(d))
        dog = self._dog_need.get(rid)
        # 外审 r6 #3:狗上的线只对同一份预算上下文(图、路线版本、点、待命点、原点)算数;
        # 改过就重新让狗上判
        if dog is not None and self._now() - dog[1] <= DOG_NEED_MS \
                and dog[2] == self._need_key(e, info, rid, pts):
            line = max(line, dog[0])
        return line, lap_s

    def _home_xy(self, info: dict[str, Any], rid: str) -> tuple[float, float] | None:
        home_fn = getattr(self.standby, "home", None) if self.standby is not None else None
        try:
            home = home_fn(rid, info["map_id"], info["map_version"]) if callable(home_fn) else None
        except Exception:  # noqa: BLE001 - 原点查不到:只按站点那条
            home = None
        return (float(home["x"]), float(home["y"])) if home is not None else None

    def _need_key(self, e: dict[str, Any] | None, info: dict[str, Any], rid: str,
                  pts: list[dict[str, Any]] | None) -> tuple[Any, ...]:
        """一圈电量预算的上下文:图、路线版本、这一圈的点(不管顺序)、待命点、原点。"""
        names = frozenset(str(p.get("name")) for p in (pts or info["points"]))
        return (info["map_id"], info["map_version"], info["id"], info.get("revision"), names,
                self._tail_xy(e, info, rid), self._home_xy(info, rid))

    def _why_not(self, rid: str, info: dict[str, Any], e: dict[str, Any] | None = None,
                 pts: list[dict[str, Any]] | None = None) -> str:
        """这台现在能不能派去跑一圈;能 → 空串,不能 → 为什么。
        ``pts``:这一圈实际要派的点(电量按它算)。"""
        why = self._unfit(rid, info)
        if why:
            return why
        if self.busy is not None and rid in self.busy():
            return f"{rid} 在驱离"
        if self.charging is not None and self.charging(rid):
            return f"{rid} 在回充"
        pct = self._battery(rid)
        if pct is None:
            return f"{rid} 还没报电量"
        line, lap_s = self._lap_need(e, info, rid, pts)
        if line > 100.0:
            # 外审 r6 #2:满电也过不了这条线 —— 补能没用,不去补;直说预算不够
            self.low.pop(rid, None)
            return (f"{rid} 满电也不够跑这一圈(要 ≥ {line:.0f}%):"
                    f"请缩短路线、少布防几个防区,或把待命点、原点设近一点")
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

    def _unfit(self, rid: str, info: dict[str, Any]) -> str:
        """这台根本派不了这条路线(不在线、状态旧、只许手动、要人监护、图不对);能 → 空串。
        不看电量、忙不忙 —— 预报按它挑候选(外审 r5 #5)。"""
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
        caps = c.capabilities.tasks.get("patrol", {}) if c.capabilities is not None else {}
        if (caps.get("map_id"), caps.get("map_version")) != (info["map_id"], info["map_version"]):
            return (f"{rid} 加载的地图不是 {info['map_id']} v{info['map_version']}"
                    f"(防区路线 {info['id']} 画在这一版上)")
        return ""

    def _pick(self, e: dict[str, Any], info: dict[str, Any], taken: set[str],
              pts: list[dict[str, Any]] | None = None) -> tuple[str, str]:
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
            w = self._why_not(rid, info, e, pts)
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
        now = self._now()
        for rid, (until, *_rest) in list(self._intense.items()):
            if now >= until:                             # W74 加密巡查到点了
                del self._intense[rid]
                eid = next((k for k, r in runs.items() if r["robot_id"] == rid), None)
                self._log("intense_end", robot=rid, entry=eid,
                          detail="加密巡查结束,回到平常的值守")
        for eid, r in runs.items():
            e = entries.get(eid)
            if e is None or (e["robot"] and e["robot"] != r["robot_id"]):
                await self._stand_down(r, "值守方案改了")
            elif r["state"] == "standdown":              # 收队中(让出、撤防):每拍看停没停
                await self._stand_down(r, "收队")
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
                    and r["state"] not in ("lap", "held", "stopping", "standdown"):
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
            # 外审 r5 #3:先排出这一圈实际要走的点,挑机器人、算电量都按它
            pts = self._lap_points(info, armed, None, False, bool(e["shuffle"]))
            rid, why = self._pick(e, info, taken, pts or None)
            if not rid:
                self._wait(e, "", why)
                return
            await self._start_lap(e, info, armed, rid, None, pts=pts)
            return
        rid, state = run["robot_id"], run["state"]
        running = self._running(rid)
        settled = self._settled(rid, now)                # 每拍都记:听得见从什么时候起
        if state == "standdown":                         # 收队中:tick 里办(外审 r3 #3)
            return
        if state == "stopping":
            await self._stopping(e, run, running)
            return
        if await self._person_hold(e, run, running):
            return
        if state in ("resume", "post", "wait") and not settled:
            # 联系不上、刚连回来:手上有没有任务还说不准,先不接着跑、不派下一圈
            self._wait(e, rid, f"{rid} 不在线" if not self._heard(rid)
                       else f"{rid} 刚连回来,等它报手上的任务")
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
                if self._lost(e["id"], run, running, now):
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

    def _person_alert(self, rid: str) -> Any:
        """这台还开着的「看见人」告警(最新那条);没有 ``None``。"""
        f = getattr(self.alerts, "open_of", None)
        if not callable(f):
            return None
        try:
            return f(rid, PERSON_KIND)
        except Exception:
            log.exception("%s 开着的看见人告警读不出来", rid)
            return None

    def _person_open(self, rid: str) -> bool:
        """有开着的「看见人」要盯:这一条盯满过(``hold_done``,记在告警上,重启也认)的不算。"""
        if self.alerts is None or not self.alerts.has_open(rid, PERSON_KIND):
            return False
        a = self._person_alert(rid)
        ctx = getattr(a, "context", None) if a is not None else None
        return not (isinstance(ctx, dict) and ctx.get("hold_done"))

    async def _person_hold(self, e: dict[str, Any], run: dict[str, Any], running: str) -> bool:
        """看见人(设计 §5.4):停在原地盯着,等保安下结论;处理了、或盯满 :data:`PERSON_HOLD_MS`
        没人管 → 接着值守。在盯 → ``True``(这一拍别的不办)。

        外审 r2:这一回盯着、跟人的账(:meth:`_fs`)落在值守进度里、绑着那一条告警的 key ——
        换了一条告警(上一条刚关就来新的)当上一回办完了;办完了,跟人和它派生的那几趟没确认停下的,
        挨个按「停下」停了才接着值守(掉线、站点重启都接着停)。"""
        eid, rid, now = e["id"], run["robot_id"], self._now()
        if run["state"] == "held":
            a = self._person_alert(rid)
            fs = self._fs(rid)
            key = getattr(a, "key", "") if a is not None else ""
            if a is not None and not fs.get("alert"):
                fs = self._fs_set(rid, alert=key)        # 老进度(没记是哪一条):认现在这条
            if self._person_open(rid) and key == fs.get("alert") and now < run["until_ms"]:
                await self._maybe_follow(eid, rid)
                await self._follow_todo(eid, rid)
                return True
            timed_out = now >= run["until_ms"] and a is not None and key == fs.get("alert")
            if timed_out:
                self._amend(key, hold_done=True)         # 这一条不再拦(落在告警上,重启也认)
            self._log("person_resume", robot=rid, entry=eid,
                      detail=f"盯了 {PERSON_HOLD_MS // 60_000} 分钟没人管,接着值守(告警还开着)"
                      if timed_out else "保安处理了,接着值守")
            self._start_intense(eid, rid, now)
            p = self._pending_follow(rid, running, now)
            if p:
                # 跟人 / 回线内 / 看易藏点那一趟还没确认停下(在跑、或掉线了看不出来):
                # 记成这一圈要停的那一趟,按「停下」确认停了才接着值守
                # (没停到点再发、发够报警;重启接着停)
                self._set_run(eid, task_id=p)
                await self._stop(eid, {**run, "task_id": p}, "resume")
                return True
            self._set_run(eid, state="resume", until_ms=now)
            return True
        if not self._person_open(rid):
            return False
        a = self._person_alert(rid)
        self._held_zone.pop(rid, None)
        z = self._zone_now(rid, run)
        if z is not None:
            self._held_zone[rid] = z
        # 新一回:从头记
        self._put_fs(rid, {"alert": getattr(a, "key", "") if a is not None else ""})
        self._log("person_hold", robot=rid, entry=eid, zone=run["watch_zone"],
                  detail="看见人:原地停下盯着,等保安处理")
        if running and running == run["task_id"]:        # 一圈 / 去值守位的路上:停在原地
            await self._stop(eid, run, "held")
        else:
            self._set_run(eid, state="held", until_ms=now + PERSON_HOLD_MS)
        return True

    # ------------------------------------------------------------ 跟人的账(落库,外审 r2)

    def _run_of(self, rid: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM guard_runs WHERE robot_id=? LIMIT 1", (rid,))
        return dict(rows[0]) if rows else None

    def _fs(self, rid: str) -> dict[str, Any]:
        """这台这一回看见人的账:``alert`` 哪一条告警、``task`` 跟人那一趟、``unsure_ms`` 派出去还没
        确认收到(什么时候派的)、``retry_ms`` 被拒了下一次能派的时刻、
        ``own`` 回线内 / 看易藏点那一趟、``left`` 越过出口线了、``todo`` 下一拍要办的、
        ``abort`` 越线后停跟人 [下一次, 第几次]、
        ``trail`` 每条出口线最后确定在哪一边。落在值守进度 ``guard_runs.follow``:
        站点重启认得回来。"""
        run = self._run_of(rid)
        try:
            fs = json.loads(run["follow"] or "{}") if run is not None else {}
        except ValueError:
            fs = {}
        return fs if isinstance(fs, dict) else {}

    def _put_fs(self, rid: str, fs: dict[str, Any]) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE guard_runs SET follow=? WHERE robot_id=?",
                      (json.dumps(fs, ensure_ascii=False), rid))

    def _fs_set(self, rid: str, **kw: Any) -> dict[str, Any]:
        fs = {**self._fs(rid), **kw}
        self._put_fs(rid, fs)
        return fs

    def _amend(self, key: str, **ctx: Any) -> None:
        """记进那一条「看见人」告警的现场(按 key,不是「最新开着的那条」;关了的不改)。"""
        f = getattr(self.alerts, "amend", None)
        if not key or not callable(f):
            return
        try:
            f(key, context=ctx)
        except Exception:
            log.exception("%s 记不进告警", key)

    def _pending_follow(self, rid: str, running: str, now: int) -> str:
        """这一回派的跟人、回线内、看易藏点里还没确认停下的那一趟(在跑的先);都停了 ``""``。

        外审 r3 #1:有结果的不算(状态里还挂着它也不算,不遮住别的);刚派出去
        :data:`FOLLOW_SEEN_MS` 以内的,状态里没它也不算停了。"""
        fs = self._fs(rid)
        sent = fs.get("sent") or {}
        live = [t for t in (fs.get("task"), fs.get("own")) if isinstance(t, str) and t
                and self._terminal(rid, t) is None]
        if running in live:
            return running
        return next((t for t in live if not self._settled(rid, now) or running == t
                     or now - int(sent.get(t, 0)) < FOLLOW_SEEN_MS), "")

    async def _maybe_follow(self, eid: int, rid: str) -> None:
        """W74 二期:停下盯着以后派跟人(机器人能力里有 ``follow`` 才派;一回成功派一次)。人在不在动、
        跟丢了怎么办都在机器人上判(设计 §三)。

        外审 r1 #7 / r2 #4:派之前先把「派出去了、还没确认收到」落库;被拒过一阵再试;回执没到的
        (重启以后也一样)对账 —— 机器人手上一直不是它、也没它的消息,才当没派成再派。"""
        now = self._now()
        fs = self._fs(rid)
        tid = fs.get("task")
        if tid:
            sent = int(fs.get("unsure_ms") or 0)
            if not sent:
                return
            if self._running(rid) == tid or self._terminal(rid, tid) is not None:
                self._fs_set(rid, unsure_ms=0)           # 收到了
                return
            if now - sent < FOLLOW_RETRY_MS or not self._settled(rid, now):
                return
            self._log("follow_refused", robot=rid, entry=eid,
                      detail="派跟人的回执没到,机器人手上也一直不是它:当没派成,再派")
            fs = self._fs_set(rid, task="", unsure_ms=0)
            self._amend(fs.get("alert", ""), follow_task="")
        elif now < int(fs.get("retry_ms") or 0):
            return
        c = self._client(rid)
        caps = getattr(getattr(c, "capabilities", None), "tasks", None) or {}
        if "follow" not in caps:
            return
        from nexwar_contract.follow import TASK_PREFIX, FollowRequest
        tid = f"{TASK_PREFIX}{eid}-{uuid.uuid4().hex[:10]}"
        # 先落库:派到一半重启也认得、会对账
        fs = self._fs_set(rid, task=tid, unsure_ms=now, sent={**(fs.get("sent") or {}), tid: now})
        # 告警现场只收这一趟(#14)
        self._amend(fs.get("alert", ""), follow_task=tid, follow_done=False)
        self._log("follow_start", robot=rid, entry=eid, detail="看见人:跟上去,边跟边拍")
        from nexwar_site.dispatcher import DispatchRefused
        try:
            r = await self.dispatcher.follow(rid, tid, FollowRequest(), issued_by="guard")
        except DispatchRefused as exc:
            self._follow_refused(rid, eid, str(exc))
            return
        except Exception:
            log.exception("%s 派跟人没成(回执没到?)", rid)
            return                                       # 说不清收没收:过一阵对账
        if not _accepted(r):
            ack = (r or {}).get("ack", {}) if isinstance(r, dict) else {}
            self._follow_refused(rid, eid, str((ack or {}).get("reason")
                                               or (ack or {}).get("result") or "被拒"))
            return
        self._fs_set(rid, unsure_ms=0)

    def _follow_refused(self, rid: str, eid: int, why: str) -> None:
        fs = self._fs_set(rid, task="", unsure_ms=0, retry_ms=self._now() + FOLLOW_RETRY_MS)
        self._amend(fs.get("alert", ""), follow_task="")  # 没派成:告警现场别再收它
        self._log("follow_refused", robot=rid, entry=eid,
                  detail=f"派跟人没成({why[:160]}):{FOLLOW_RETRY_MS // 1000} 秒后再试")

    # ------------------------------------------------------------ W74 三期:越线、易藏点

    def _on_follow(self, rid: str, e: Any) -> None:
        """机器人跟人路上的轨迹(判越过出口线)、跟丢(去附近易藏点看一圈)。只认还在盯的这一回、
        这一条告警派的那一趟(外审 r2 #5:上一条告警刚关、新的来了,上一回的事件不算)。"""
        d = e.data if isinstance(e.data, dict) else {}
        try:
            run = self._run_of(rid)
            if run is None or run["state"] != "held":
                return
            fs = self._fs(rid)
            a = self._person_alert(rid)
            if a is None or getattr(a, "key", "") != fs.get("alert"):
                return
            tid = fs.get("task")
            if not tid or d.get("task_id") != tid:
                return
            if fs.get("unsure_ms"):
                fs = self._fs_set(rid, unsure_ms=0)      # 有它的消息:收到了
            ref = self._map_of(rid)
            if e.kind == "follow_lost":
                self._follow_lost(rid, run["entry"], fs, d, ref)
                return
            if self.marks is None or ref is None or fs.get("left"):
                return
            pts = sorted((p for p in d.get("points") or [] if isinstance(p, dict)
                          and _num(p.get("x")) and _num(p.get("y"))),
                         key=lambda p: p.get("at_ms") if _num(p.get("at_ms")) else 0)
            trail = dict(fs.get("trail") or {})
            for p in pts:
                cur = (float(p["x"]), float(p["y"]))
                hit = self.marks.track(ref[0], ref[1], trail, cur)
                if hit is not None:
                    self._fs_set(rid, trail=trail)
                    self._crossed(rid, run["entry"], hit[0], hit[1], cur, d, ref)
                    return
            if trail != fs.get("trail"):
                self._fs_set(rid, trail=trail)
        except Exception:
            log.exception("%s 跟人的 %s 处理不了", rid, e.kind)

    def _map_of(self, rid: str) -> tuple[str, str] | None:
        pose = getattr(getattr(self._client(rid), "telemetry", None), "pose", None)
        return (pose.map_id, pose.map_version) if pose is not None else None

    def _crossed(self, rid: str, eid: int, mark: dict[str, Any], prev: tuple[float, float],
                 cur: tuple[float, float], d: dict[str, Any], ref: tuple[str, str]) -> None:
        """人越过出口线(设计 §七):报 P1「人离开值守区」;下一拍停跟人、回线内 3 m 朝出口看。"""
        from nexwar_site.map_marks import INSIDE_M, inside_pose
        x, y, yaw = inside_pose(mark, prev, cur)
        pose = MapPose(map_id=ref[0], map_version=ref[1], frame_id="map", x=x, y=y, yaw=yaw)
        fs = self._fs_set(rid, left=True, todo=["exit", pose.to_wire(), mark["name"]])
        self._amend(fs.get("alert", ""), follow_left=True)
        h = d.get("heading_deg")
        ctx: dict[str, Any] = {"exit": mark["name"], "exit_id": mark["id"],
                               "at": {"x": round(cur[0], 2), "y": round(cur[1], 2),
                                      "at_ms": self._now()}}
        if _num(h):
            ctx["heading_deg"] = float(h)
        self.alerts.raise_alert(
            kind="person_left", robot=rid, title=f"人离开值守区:{mark['name']}",
            detail=f"被跟的人越过了出口线「{mark['name']}」。机器人回到出口线内 {INSIDE_M:g} m、"
                   "朝出口看着;请保安到出口查看", context=ctx)
        self._log("person_left", robot=rid, entry=eid,
                  detail=f"被跟的人越过出口线「{mark['name']}」,回线内 {INSIDE_M:g} m 朝出口看")

    def _follow_lost(self, rid: str, eid: int, fs: dict[str, Any], d: dict[str, Any],
                     ref: tuple[str, str] | None) -> None:
        """跟丢了 / 到上限(机器人自己回最后位置)。跟丢了、附近有易藏点:等它回到最后位置,
        挨个去看一圈(设计 §六,用户 11-04 定)。到上限的人还在眼前,不去。"""
        result = d.get("result")
        self._log("follow_lost", robot=rid, entry=eid,
                  detail="跟丢了:回最后看见人的地方" if result == "lost"
                  else "跟到上限了:停在人后面")
        last = d.get("last")
        if result != "lost" or fs.get("left") or fs.get("todo") or fs.get("own") \
                or self.marks is None or ref is None \
                or not isinstance(last, dict) or not _num(last.get("x")) or not _num(last.get("y")):
            return
        h = d.get("heading_deg")
        heading = float(h) if _num(h) else None
        lx, ly = float(last["x"]), float(last["y"])
        spots = self.marks.hide_near(ref[0], ref[1], lx, ly, heading)
        if spots:
            self._fs_set(rid, todo=["sweep", [ref[0], ref[1]], [lx, ly, heading], spots])

    async def _follow_todo(self, eid: int, rid: str) -> None:
        """越线了 / 跟丢了要去看易藏点:跟人那一趟还没停就先停它(越线)或等它回到最后位置(跟丢)。

        外审 r2 #3 / r3 #2:要办的事落库,派成了(机器人接了、或手上就是它)才消掉 —— 派到一半重启、
        回执没到的,过 :data:`FOLLOW_RETRY_MS` 对账(机器人手上一直不是它才再派);被拒的过一阵再试。"""
        fs = self._fs(rid)
        todo = fs.get("todo")
        if not isinstance(todo, list) or not todo:
            return
        running, now = self._running(rid), self._now()
        what = "回出口线内" if todo[0] == "exit" else "看易藏点"
        own = fs.get("own") or ""
        if own and fs.get("own_unsure"):
            if running == own or self._terminal(rid, own) is not None:
                self._fs_set(rid, todo=None, own_unsure=False)   # 收到了
                return
            sent = int((fs.get("sent") or {}).get(own, 0))
            if now - sent < FOLLOW_RETRY_MS or not self._settled(rid, now):
                return
            self._log("exit_refused" if todo[0] == "exit" else "sweep_refused", robot=rid,
                      entry=eid, detail=f"{what}的回执没到,机器人手上也一直不是它:当没派成,再派")
            fs = self._fs_set(rid, own="", own_unsure=False)
        elif now < int(fs.get("todo_retry") or 0):
            return
        ftid = fs.get("task") or ""
        if ftid and not self._stopped(rid, ftid, running, now):
            if todo[0] == "exit":
                await self._abort_follow(rid, eid, ftid)  # 停了下一拍再派回线内
            return
        from nexwar_contract.follow import FOLLOW_PRIORITY, TASK_PREFIX
        from nexwar_site.dispatcher import DispatchRefused
        if todo[0] == "exit":
            tid = f"{TASK_PREFIX}exit-{eid}-{uuid.uuid4().hex[:10]}"
        else:
            tid = f"{TASK_PREFIX}sweep-{eid}-{uuid.uuid4().hex[:10]}"
        # 先落库「派出去了、还没确认」,再派
        fs = self._fs_set(rid, own=tid, own_unsure=True, abort=None,
                          sent={**(fs.get("sent") or {}), tid: now})
        self._amend(fs.get("alert", ""), follow_own=tid)
        try:
            if todo[0] == "exit":
                r = await self.dispatcher.goto(rid, todo[1], None, issued_by="guard",
                                               priority=FOLLOW_PRIORITY, task_id=tid)
            else:
                _, (m, v), (lx, ly, heading), spots = todo
                mission = self._sweep_mission(rid, m, v, lx, ly, heading, spots)
                names = "、".join(sp["name"] for sp in spots)
                self._log("sweep_start", robot=rid, entry=eid,
                          detail=f"跟丢了:去附近的易藏点看一圈({names}),每个停 {SWEEP_DWELL_S} 秒,"
                                 "看完回最后位置")
                r = await self.dispatcher.patrol(rid, mission, issued_by="guard",
                                                 priority=FOLLOW_PRIORITY, task_id=tid)
        except DispatchRefused as exc:
            self._todo_refused(rid, eid, todo[0], what, str(exc))
            return
        except Exception:
            log.exception("%s %s没派成(回执没到?)", rid, what)
            return                                       # 说不清收没收:过一阵对账
        if not _accepted(r):
            ack = (r or {}).get("ack", {}) if isinstance(r, dict) else {}
            self._todo_refused(rid, eid, todo[0], what, str((ack or {}).get("reason")
                                                            or (ack or {}).get("result") or "被拒"))
            return
        self._fs_set(rid, todo=None, own_unsure=False)

    def _todo_refused(self, rid: str, eid: int, kind: str, what: str, why: str) -> None:
        self._fs_set(rid, own="", own_unsure=False, todo_retry=self._now() + FOLLOW_RETRY_MS)
        self._log("exit_refused" if kind == "exit" else "sweep_refused", robot=rid, entry=eid,
                  detail=f"{what}没派成({why[:160]}):{FOLLOW_RETRY_MS // 1000} 秒后再试")

    async def _abort_follow(self, rid: str, eid: int, ftid: str) -> None:
        """人越线了:停跟人那一趟。没停下每 :data:`STOP_RETRY_MS` 再发,发够 :data:`STOP_ALERT` 次报警
        (外审 r1 #6;发了几次落库,重启接着数)。"""
        now = self._now()
        at, n = self._fs(rid).get("abort") or (0, 0)
        if now < at:
            return
        n += 1
        self._fs_set(rid, abort=[now + STOP_RETRY_MS, n])
        if n > STOP_ALERT and self.alerts is not None \
                and not self.alerts.has_open(rid, "guard_stuck"):
            self._log("stop_failed", robot=rid, entry=eid,
                      detail=f"人越线了,发了 {n - 1} 次中止,跟人那一趟还没停下")
            self.alerts.raise_alert(
                kind="guard_stuck", robot=rid, title=f"{rid} 停不下跟人",
                detail=f"人越过出口线,值守要停下跟人、回线内,发了 {n - 1} 次中止还在跟;"
                       "站点接着发,请看机器人是否在线、必要时到现场或用遥控器停下")
        try:
            await self.dispatcher.abort(rid, ftid, issued_by="guard")
        except Exception:
            log.exception("%s 停跟人没成", rid)

    def _hide_waypoints(self, rid: str, spots: list[dict[str, Any]],
                        prefix: str = "易藏点") -> list[dict[str, Any]]:
        """易藏点 → 巡检点位:站在那里朝标的方向,停 :data:`SWEEP_DWELL_S` 秒,机器人能拍就拍一张。"""
        caps = getattr(getattr(self._client(rid), "capabilities", None), "tasks", None) or {}
        cams = (caps.get("goto_photo") or {}).get("cameras") or []
        out = []
        for i, sp in enumerate(spots, 1):
            name = f"{prefix}{i}-{sp['name']}"
            for bad in ("/", "\\", "..", "\x00"):
                name = name.replace(bad, "_")
            acts: list[dict[str, Any]] = [{"type": "dwell", "seconds": SWEEP_DWELL_S}]
            if cams:
                acts.append({"type": "photo", "camera": cams[0]})
            x, y = sp["points"][0]
            out.append({"name": name, "pose": Pose.from_xy_yaw(x, y, float(sp["yaw"])).to_wire(),
                        "actions": acts})
        return out

    def _hot_hides(self, rid: str, info: dict[str, Any], hot: list[str],
                   n: int) -> list[tuple[dict[str, Any], str]]:
        """W74 三期:加密巡查这一圈先看那几个防区里的易藏点(最后看见人的那个防区的先看);
        连上这一圈的点不超过派遣的上限(留一个给回待命点)。回 ``(巡检点, 所在防区)``。
        外审 r1 #10:撤防了的防区里的不去。"""
        if self.marks is None:
            return []
        from nexwar_site.dispatcher import MAX_PATROL_WAYPOINTS
        room = MAX_PATROL_WAYPOINTS - n - 1
        if room <= 0:
            return []
        m, v = info["map_id"], info["map_version"]
        try:
            got = []
            for sp in self.marks.list(m, v, "hide"):
                z = self.areas.zone_at(m, v, *sp["points"][0])
                if z in hot and self._armed(z):
                    got.append((hot.index(z), sp["name"], sp, z))
        except Exception:                                # 算不出来:照常跑这一圈
            log.exception("%s 加密巡查的易藏点算不出来", rid)
            return []
        got = sorted(got, key=lambda t: (t[0], t[1]))[:room]
        taken = {w["name"] for w in info["wire"]["waypoints"]}
        wps = self._hide_waypoints(rid, [t[2] for t in got], "加密-易藏点")
        return [(w, t[3]) for w, t in zip(wps, got, strict=True) if w["name"] not in taken]

    def _sweep_mission(self, rid: str, m: str, v: str, lx: float, ly: float,
                       heading: float | None, spots: list[dict[str, Any]]) -> dict[str, Any]:
        wps = self._hide_waypoints(rid, spots)
        yaw = math.radians(heading) if heading is not None else 0.0
        wps.append({"name": "最后位置", "pose": Pose.from_xy_yaw(lx, ly, yaw).to_wire()})
        return {"mission": "跟丢后看易藏点", "map_id": m, "map_version": v, "waypoints": wps}

    def _zone_now(self, rid: str, run: dict[str, Any]) -> tuple[str, str, str] | None:
        """机器人此刻在哪个防区:站在值守位上按它看着的;不然按位置算。说不清 ``None``。"""
        c = self._client(rid)
        pose = getattr(getattr(c, "telemetry", None), "pose", None)
        if pose is None:
            return None
        if run.get("watch_zone"):
            return pose.map_id, pose.map_version, run["watch_zone"]
        try:
            z = self.areas.zone_at(pose.map_id, pose.map_version, pose.x, pose.y)
        except Exception:                                # 算不出来就不加密
            log.exception("%s 在哪个防区算不出来", rid)
            return None
        return (pose.map_id, pose.map_version, z) if z else None

    def _start_intense(self, eid: int, rid: str, now: int) -> None:
        held = self._held_zone.pop(rid, None)
        if held is None:
            return
        m, v, z = held
        try:
            zones = list(self.areas.neighbours(m, v, z))
        except Exception:
            log.exception("%s 相邻防区算不出来", z)
            zones = [z]
        self._intense[rid] = (now + INTENSE_MS, m, v, zones)
        self._log("intense_start", robot=rid, entry=eid, zone=z,
                  detail=f"看见人以后加密巡查 {INTENSE_MS // 60_000} 分钟:{'、'.join(zones)},"
                         "每圈不歇")

    def _hot(self, eid: int, rid: str, info: dict[str, Any]) -> list[str] | None:
        """这台在加密巡查、这条路线有那几个防区的点:那几个防区;不然 ``None``。到点了收掉。"""
        got = self._intense.get(rid)
        if got is None:
            return None
        until, m, v, zones = got
        if self._now() >= until:
            del self._intense[rid]
            self._log("intense_end", robot=rid, entry=eid,
                      detail="加密巡查结束,回到平常的值守")
            return None
        if (info["map_id"], info["map_version"]) != (m, v) \
                or not any(p["zone"] in zones for p in info["points"]):
            return None
        return zones

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
        # 联系不上时手上「没任务」是离线状态给的,不算停了;刚连回来那几拍也不算(外审 r3 #2:重连先报
        # 空状态、任务后补)—— 要连着听得见够 :data:`SETTLE_HEARD_MS`,不然只认它的结果事件
        stopped = self._stopped(rid, run["task_id"], running, now)
        if stopped:
            nxt = self._pending_follow(rid, running, now)
            if nxt and nxt != run["task_id"]:
                # 外审 r2:看见人那一回派的还有一趟没确认停下(跟人停了、回线内还在跑):接着停它
                self._set_run(eid, task_id=nxt)
                self._stop_tries[eid] = 0
                await self._send_abort(eid, {**run, "task_id": nxt})
                return
            if nxt:                                      # 就是它、还没确认停下(外审 r3 #1)
                await self._retry_stop(eid, run, now)
                return
            then = self._stop_then.pop(eid, "resume")
            self._stop_tries.pop(eid, None)
            if then == "held":
                self._set_run(eid, state="held", until_ms=now + PERSON_HOLD_MS)
            else:
                self._set_run(eid, state="resume", until_ms=now + SETTLE_MS)
            return
        await self._retry_stop(eid, run, now)

    def _stopped(self, rid: str, tid: str, running: str, now: int) -> bool:
        """这一趟停了:有结果事件;或连着听得见够久、手上已经不是它。"""
        return self._terminal(rid, tid) is not None \
            or (self._settled(rid, now) and running != tid)

    async def _retry_stop(self, eid: int, run: dict[str, Any], now: int) -> None:
        """还没停:到点再发中止,发够 :data:`STOP_ALERT` 次报警。"""
        rid = run["robot_id"]
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

    def _heard(self, rid: str) -> bool:
        c = self._client(rid)
        if c is None or c.status is None or not c.status.online:
            return False
        fresh = getattr(self.dispatcher, "_fresh", None)
        return not callable(fresh) or bool(fresh(c))

    def _settled(self, rid: str, now: int) -> bool:
        """听得见、而且连着听得见够久了:它报的「手上没任务」才算数。"""
        if not self._heard(rid):
            self._heard_since.pop(rid, None)
            return False
        return now - self._heard_since.setdefault(rid, now) >= SETTLE_HEARD_MS

    def _lost(self, eid: int, run: dict[str, Any], running: str, now: int) -> bool:
        """派的那一趟没在跑、也没结果,而且从最后一次看见它在跑(或联系不上)起过了 :data:`LOST_S`。"""
        if running == run["task_id"] or not self._heard(run["robot_id"]):
            self._alive_ms[eid] = now
            return False
        since = max(run["sent_ms"], self._alive_ms.get(eid, 0))
        return now - since >= LOST_S * 1000

    def _off_zones(self, info: dict[str, Any], armed: list[str], run: dict[str, Any]) -> list[str]:
        """这一圈还没走到的点里,所在防区已经撤防的(外审 r1 #4)。

        外审 r2 #4:按**这一趟**到过的点算剩下哪些(机器人报的到点事件),不按防区「最后看过」——
        看过东区 A、还没到 B 时东区撤了,B 照样不去;别的机器人刚看过东区也不算这一趟走过。"""
        passed = {str(r["name"]) for r in self.db.query(
            "SELECT json_extract(data, '$.name') AS name FROM events WHERE robot_id=? "
            "AND kind='patrol_waypoint' AND json_extract(data, '$.task_id')=?",
            (run["robot_id"], run["task_id"]))}
        plan = json.loads(run["plan"] or "[]")
        return list(dict.fromkeys(p["zone"] for p in plan if p.get("zone")
                                  and p["zone"] not in armed and p["name"] not in passed))

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
            if self._lost(e["id"], run, running, now):
                self._log("interrupted", robot=rid, entry=eid,
                          detail="派了没在跑,也没结果(当被打断)")
                self._set_run(eid, state="resume", until_ms=now)
            return
        kind, data = term
        if kind == "task_done":
            self._dog_need.pop(rid, None)
            self._log("lap_done", robot=rid, entry=eid,
                      detail=f"第 {run['lap']} 圈跑完,用时 "
                             f"{(now - run['lap_started_ms']) // 1000} 秒")
            if run["fails"] and self.alerts is not None:
                self.alerts.resolve_all(rid, "guard_stuck", who="site:guard_lap_done")
            if not e["robot"] and rid in self._reserved:
                # 外审 r2 #6:「任意一台」跑完这一圈,机器人让给指定它的方案,不再接着跑下一圈
                await self._stand_down(run, f"{rid} 让给指定它的值守方案")
                return
            gap = int(e["gap_min"]) * 60_000
            if self._hot(eid, rid, info) is not None:
                gap = 0                                  # W74 加密巡查:每圈不歇
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
        elif _need_pct(data) is not None:
            # 外审 r5 #4:狗上起飞前说电量不够出发 —— 不算跑不成(不报堵、不一分钟后重派),
            # 记下狗上的线,下一拍按它判「先补能」
            need = _need_pct(data)
            self._dog_need[rid] = (need, now, self._sent_key.get(rid))
            self._log("lap_low", robot=rid, entry=eid,
                      detail=f"机器人说电量不够出发(它算要 ≥ {need:.0f}%):先补能")
            back = {"task_id": run["task_id"], "lap": run["lap"],
                    "lap_started_ms": run["lap_started_ms"], "plan": run["plan"]}
            self._set_run(eid, state="resume", until_ms=now, **back)
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
                    resume: bool, shuffle: bool,
                    hot: list[str] | None = None) -> list[dict[str, Any]]:
        keep = [p for p in info["points"] if not p["zone"] or p["zone"] in armed]
        if hot is not None:
            # W74 加密巡查:只跑这几个防区的点,从最后看见人的那个防区(``hot[0]``)先跑;不打乱、
            # 这一圈看过的照样再看
            mine = [p for p in keep if p["zone"] in hot]
            if mine:
                first = next((z for z in hot if any(p["zone"] == z for p in mine)), None)
                start = next(i for i, p in enumerate(mine) if p["zone"] == first)
                return mine[start:] + mine[:start]
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
                         rid: str, run: dict[str, Any] | None, *, resume: bool = False,
                         pts: list[dict[str, Any]] | None = None) -> None:
        eid, now = e["id"], self._now()
        if now < self._retry_at.get(eid, 0):
            return
        hot = self._hot(eid, rid, info)
        if pts is None:
            pts = self._lap_points(info, armed, run, resume, bool(e["shuffle"]), hot)
        if not pts:
            self._wait(e, rid, f"防区路线 {info['id']} 上布防着的防区里没有点")
            return
        why = self._why_not(rid, info, e, pts)           # 外审 r4 #3 / r5 #3:按这一圈实际的点算
        if why:
            self._wait(e, rid, why)
            return
        self._waiting.pop(eid, None)
        self._retry_at[eid] = now + RETRY_MS              # 派成了再清
        names = [p["name"] for p in pts]
        by_name = {w["name"]: w for w in info["wire"]["waypoints"]}
        hide_z = self._hot_hides(rid, info, hot, len(names)) if hot else []
        hide = [w for w, _ in hide_z]
        mission = {**info["wire"], "waypoints": hide + [by_name[n] for n in names]
                   + self._standby_tail(e, info, rid, by_name, len(names) + len(hide))}
        tid = f"{GUARD_PREFIX}{eid}-{uuid.uuid4().hex[:10]}"
        lap = (run["lap"] if run else 0) + (0 if resume else 1)
        started = run["lap_started_ms"] if (resume and run) else now
        # 外审 r1 #10:加密巡查先看的易藏点也记进这一圈(撤防检查、到点记账都认它们)
        plan = json.dumps([{"name": w["name"], "zone": z} for w, z in hide_z]
                          + [{"name": p["name"], "zone": p["zone"]} for p in pts],
                          ensure_ascii=False)
        with self.db.tx() as c:                          # 先落库:写不进去就不派
            c.execute("INSERT OR REPLACE INTO guard_runs(entry, robot_id, state, task_id, sent_ms, "
                      "lap, lap_started_ms, next_lap_ms, until_ms, plan, watch_zone, fails) "
                      "VALUES (?,?,?,?,?,?,?,?,0,?,'',?)",
                      (eid, rid, "lap", tid, now, lap, started,
                       started + (0 if hot else int(e["gap_min"])) * 60_000, plan,
                       run["fails"] if run else 0))
        self._sent_key[rid] = self._need_key(e, info, rid, pts)
        self._log("resume" if resume else "lap_start", robot=rid, entry=eid,
                  detail=(f"第 {lap} 圈接着跑" if resume else f"第 {lap} 圈") + ":"
                  + "→".join(names))
        from nexwar_site.dispatcher import DispatchRefused
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

    def _standby_tail(self, e: dict[str, Any], info: dict[str, Any], rid: str,
                      by_name: dict[str, Any], n: int = 0) -> list[dict[str, Any]]:
        """11-04 用户:每圈结束回待命点(方案里 ``end_standby``,默认开)—— 把这台机器人在这张图上的默认
        待命点当成最后一个点接上。它不在任何防区里,不算「看过」;没登记待命点就不接。
        外审 r3 #8:这一圈已经 ``n`` = 上限个点,再接就超,派遣会拒 —— 不接,照常跑。"""
        if not e.get("end_standby", True) or self.standby is None:
            return []
        from nexwar_site.dispatcher import MAX_PATROL_WAYPOINTS
        if n >= MAX_PATROL_WAYPOINTS:
            key = (e["id"], rid, info["map_id"], str(info["map_version"]))
            if key not in self._no_standby:
                self._no_standby.add(key)
                self._log("no_standby", robot=rid, entry=e["id"],
                          detail=f"这一圈已经 {n} 个点(上限 {MAX_PATROL_WAYPOINTS}):"
                                 "每圈结束不回待命点,停在最后一个点")
            return []
        try:
            p = self.standby.swap_point(rid, info["map_id"], info["map_version"])
        except Exception:  # 待命点拿不到:照常跑这一圈
            log.exception("%s 的待命点拿不到", rid)
            return []
        if not p:
            key = (e["id"], rid, info["map_id"], str(info["map_version"]))
            if key not in self._no_standby:              # 11-04:没登记就说一声(每张图每台一次)
                self._no_standby.add(key)
                self._log("no_standby", robot=rid, entry=e["id"],
                          detail=f"{rid} 在地图 {info['map_id']}:{info['map_version']} 上没有默认"
                                 "待命点:每圈结束回不了待命点,停在最后一个点;到待命点页登记一个")
            return []
        self._no_standby.discard((e["id"], rid, info["map_id"], str(info["map_version"])))
        name = STANDBY_WP
        while name in by_name:
            name += "*"
        pose = Pose.from_xy_yaw(float(p["x"]), float(p["y"]), float(p.get("yaw", 0.0)))
        return [{"name": name, "pose": pose.to_wire()}]

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
            home = not e.get("end_standby", True) and self.standby is not None
            where = "回待命点等下一圈" if home else "原地等下一圈"
            self._log("watch_none", robot=rid, entry=eid, zone=zone,
                      detail=f"这个防区没有值守位:{where}")
            if home:
                # 11-04:不停在最后一个点(那里信号可能差);每圈回待命点开着的,本来就停在待命点
                try:
                    await self.standby.return_to(rid, issued_by="guard")
                except Exception as exc:  # noqa: BLE001 - 回不去:原地等
                    log.warning("%s 回待命点没派成:%s", rid, exc)
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
        """收队:手上这一趟先停下,停了再删进度、回待命点。

        外审 r3 #3:以前先删进度、只发一次中止 —— 中止没发成 / 被拒,机器人接着跑这一圈,值守却
        已经不管它了。现在记成 ``standdown``,跟 ``stopping`` 一样确认停下(结果事件,或连着听得见
        够久、手上已经不是它),没停到点再发、发够报警;``tick`` 每拍都会再叫到这里。"""
        rid, tid, eid, now = run["robot_id"], run["task_id"], run["entry"], self._now()
        running = self._running(rid)
        p = self._pending_follow(rid, running, now)
        if not tid or self._stopped(rid, tid, running, now):
            if p and p != tid:
                # 外审 r1 #4 / r2 #2:盯着的时候撤防 / 改方案:跟人、回线内、看易藏点那一趟
                # 是值守自己派的,没确认停下(在跑、或掉线看不出来)先停它,不当别人的任务直接收队
                tid = p
                self._set_run(eid, task_id=tid)
                run = {**run, "task_id": tid}
        if tid and (p == tid or not self._stopped(rid, tid, running, now)):
            if run["state"] != "standdown":
                self._log("stand_down", robot=rid, entry=eid, detail=f"{why}:先停下这一趟")
                self._retry_at.pop(eid, None)
                self._stop_tries[eid] = 0
                self._set_run(eid, state="standdown", until_ms=0)
                run = {**run, "state": "standdown", "until_ms": 0}
            await self._retry_stop(eid, run, now)
            return
        # 停了。外审 r4 #2:结果事件可能比「手上没任务」的状态先到 —— 等状态跟上(连着听得见够久、
        # 手上已经空了)再回待命点、删进度;别的任务接手了(人派的、报警)就不回,直接收
        if running == tid and self._terminal(rid, tid) is not None:
            running = ""                                 # 有结果了:状态里还挂着它是旧的
        taken = bool(running) and running != tid
        if not taken and (running or not self._settled(rid, now)):
            if run["state"] != "standdown":
                self._log("stand_down", robot=rid, entry=eid, detail=f"{why}:等它空下来再回待命点")
                self._retry_at.pop(eid, None)
                self._set_run(eid, state="standdown", until_ms=0)
            return
        self._drop_run(eid)
        self._retry_at.pop(eid, None)
        self._stop_tries.pop(eid, None)
        self._stop_then.pop(eid, None)
        if run["state"] != "standdown":
            self._log("stand_down", robot=rid, entry=eid, detail=why)
        if taken or self.standby is None:
            return
        try:
            await self.standby.return_to(rid, issued_by="guard", homing=True)
        except Exception as exc:  # noqa: BLE001 - 收不了队:记下,机器人原地;人看值守记录
            log.warning("%s 收队没办成:%s", rid, exc)
            self._log("stand_down_failed", robot=rid, entry=eid, detail=str(exc)[:200])

    # ------------------------------------------------------------ 看着

    def _on_event(self, robot_id: str, e: Any) -> None:
        if e.kind in ("follow_track", "follow_lost"):
            self._on_follow(robot_id, e)                    # W74 三期
            return
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
            ready = getattr(c.status, "ready", None)
            at = getattr(self.dispatcher, "telemetry_at", {}).get(r["robot_id"])
            if not getattr(ready, "loc_ok", False) or at is None \
                    or self._now() - at > POSE_FRESH_MS:
                continue                                 # 外审 r2 #7:定位失效、位置停更了也不算
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

    def _lap_s(self, info: dict[str, Any], tail: tuple[float, float] | None = None,
               pts: list[dict[str, Any]] | None = None) -> float | None:
        """一圈多久(秒)。``tail``:每圈结束回的待命点(外审 r3 #6)—— 最后一个点 → 待命点 → 下一圈
        第一个点,代替最后一个点直接回第一个点那段。``pts``:只跑这些点、按这个顺序(外审 r5 #3);
        不给 = 整条路线。"""
        names = tuple(p["name"] for p in pts) if pts is not None else None
        if names == tuple(p["name"] for p in info["points"]):
            names = None
        key = (info["id"], info["revision"], tail, names)
        if key not in self._lap_cache:
            from nexwar_site.route_eta import estimate
            wire = {w["name"]: w for w in info["wire"]["waypoints"]}
            src = info["points"] if names is None else list(pts or [])
            flat = {"id": info["id"], "map_id": info["map_id"], "map_version": info["map_version"],
                    "waypoints": [{"name": p["name"], "x": p["x"], "y": p["y"],
                                   "yaw": float((wire[p["name"]].get("pose") or {})
                                                .get("yaw", 0.0) or 0.0),
                                   **({"actions": wire[p["name"]]["actions"]}
                                      if wire[p["name"]].get("actions") else {})}
                                  for p in src]}
            try:
                got = estimate(self.db, self.reach, flat)
                total = float(got["total_s"])
                from nexwar_site.route_eta import NOMINAL_SPEED_MPS
                a, b = flat["waypoints"][-1], flat["waypoints"][0]
                legs = [((a["x"], a["y"]), tail), (tail, (b["x"], b["y"]))] if tail is not None \
                    else [((a["x"], a["y"]), (b["x"], b["y"]))] if len(flat["waypoints"]) > 1 \
                    else []                      # 一圈跑完回到第一个点(或经待命点)那一段
                for p0, p1 in legs:
                    m = self.reach.path_m(info["map_id"], info["map_version"], p0, p1) \
                        if self.reach is not None else None
                    if m is None:
                        m = math.hypot(p0[0] - p1[0], p0[1] - p1[1])
                    total += m / NOMINAL_SPEED_MPS
                self._lap_cache[key] = round(total, 1) if total > 0 else None
            except Exception:  # 估不出:预报里说
                log.exception("%s 一圈多久估不出来", info["id"])
                self._lap_cache[key] = None
        return self._lap_cache[key]

    def forecast(self, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        from nexwar_site.guard_forecast import forecast
        plan = plan or self.plan()
        runs = self._runs()
        lap_s: dict[str, float | None] = {}
        zones_of: dict[str, list[str]] = {}
        maps: set[tuple[str, str]] = set()
        for e in plan["entries"]:
            info = self._route(e["route"])
            if info is None:
                continue
            # 外审 r3 #6 / r4 #5:一圈算上回待命点那段;「任意一台」挑中了按它,没挑中按能挑的里最远的;
            # 同一路线取最长的
            run = runs.get(e["id"])
            # 外审 r5 #5:没挑中时只算能派这条路线的(在线、图对…);一台都没有 → 不知道(None)
            cands = [e["robot"]] if e["robot"] else [run["robot_id"]] if run else \
                [r for r in sorted(self.dispatcher.clients) if not self._unfit(r, info)]
            laps = [self._lap_s(info, self._tail_xy(e, info, r)) for r in cands] or [None]
            got = max((x for x in laps if x is not None), default=None)
            prev = lap_s.get(e["route"])
            lap_s[e["route"]] = got if prev is None or (got is not None and got > prev) else prev
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
        why = self._why_not(rid, info, e)
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


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
