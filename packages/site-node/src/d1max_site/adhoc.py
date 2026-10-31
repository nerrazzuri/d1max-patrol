"""临时巡逻(W53,用户 2026-10-30):保安、经理手动「派巡逻」。

- **不存进日程**:派的那一刻整份任务定下来,记在 ``adhoc_queue``(派了谁、哪条、几点、怎么样了)。
- **日程优先**:派之前估这一趟要多久(:meth:`AdhocDesk.estimate_s`),从现在到「现在 + 估时」之间这台
  机器人有日程要起跑、或者它正在跑日程,就**排到那趟日程预计走完之后**(``queued``),不打断日程;
  排程执行器每拍(:meth:`AdhocDesk.tick`)看排队的到没到点、机器人空没空、后面还撞不撞。
- **走完回起点**(用户 2026-10-31):派出那一刻机器人站的地方就是起点,任务末尾加上走回去的那一段
  (:meth:`AdhocDesk.with_return`);估时把这一段算进去。
- 一旦开始跑,日程到点**不打断它**(优先级照旧是手动的 ``MANUAL``,比日程高):估时已经避开了日程,
  只有跑超时才会这样,日程在自己的窗口里等它跑完(排程执行器原有的「忙」)。
- 「去这里」、遥控、报警联动派单不排队(要马上看的事),不走这里。
- 通知:派出(或排上)就记一条 P3 ``adhoc_patrol`` 推给经理和管理员;排队的那一趟真正起跑、作废、
  派不出去,记一条 ``adhoc_queue``,只推给派的人(``context.to``)。两种都不在保安的报警列表里
  (``alert_audience`` 归 ``owner``):保安在「今天的巡逻」里看。

估时(设计 D4):同一台机器人这条路线最近 5 趟走完的(从派出到 ``task_done``)取中位数;没有就按
路长 —— 相邻点直线 × :data:`DETOUR` ÷ :data:`CRUISE_MPS` + 每点动作的时间 + 末点回待命点;
两种都再乘 :data:`MARGIN`(宁可估长,免得撞日程)。
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from d1max_contract.geometry import Pose
from d1max_contract.mission import Mission, MissionWaypoint, parse_mission
from d1max_contract.schedule import ScheduleEntry, occurrences_between
from d1max_site.catalog import active_bundle
from d1max_site.dispatcher import DispatchRefused
from d1max_site.priorities import MANUAL

log = logging.getLogger(__name__)

#: 临时巡逻任务的 task_id 前缀。
ADHOC_PREFIX = "adhoc-"
#: 估时用的巡航速度(米 / 秒)。D1 Max 巡逻限速 0.5–0.8,取慢的。
CRUISE_MPS = 0.5
#: 直线距离 × 这个数 ≈ 实际走的路(绕障、转弯)。
DETOUR = 1.3
#: 一个动作没写时长(拍照、看一眼)按几秒算。
ACTION_S = 5.0
#: 估出来再乘这个数。
MARGIN = 1.2
#: 历史取最近几趟。
HISTORY_N = 5
#: 排了这么久还没起跑就作废(设计 D6)。
QUEUE_TTL_MS = 2 * 3_600_000
#: 往后推的次数上限(一串日程挨着,推到最后一个后面)。
MAX_PUSHES = 20

#: 告警种类。
KIND_SENT = "adhoc_patrol"
KIND_QUEUE = "adhoc_queue"

STATES = ("queued", "started", "expired", "cancelled", "failed")


class AdhocError(ValueError):
    """请求本身不对(没有这一条、不是你的……)。"""


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def geom_estimate_s(mission: Mission, *, back_to: tuple[float, float] | None = None) -> float:
    """按路长粗估(不乘余量):相邻点直线 × 绕路系数 ÷ 巡航速度 + 动作时间(× 圈数)
    + 末点回 ``back_to``。"""
    pts = [(w.pose.position.x, w.pose.position.y) for w in mission.waypoints]
    loops = max(1, int(getattr(mission.policy, "loops", 1) or 1))
    one = sum(_dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    if loops > 1 and len(pts) > 1:
        one += _dist(pts[-1], pts[0])                       # 一圈走完回第一点再来
    acts = sum((a.seconds if a.seconds > 0 else ACTION_S)
               for w in mission.waypoints for a in w.actions)
    back = _dist(pts[-1], back_to) if pts and back_to is not None else 0.0
    return (one * loops + back) * DETOUR / CRUISE_MPS + acts * loops


_OK_ACKS = ("accepted", "duplicate")


def _ack_result(r: Any) -> tuple[str, str]:
    """派遣器回的 ``{ack: {result, reason}}`` → (结果, 原因);没带回执算收下。"""
    ack = r.get("ack") if isinstance(r, dict) else None
    if not isinstance(ack, dict):
        return "accepted", ""
    return str(ack.get("result") or "accepted"), str(ack.get("reason") or "")


class AdhocDesk:
    def __init__(self, db: Any, dispatcher: Any, *, now_ms: Callable[[], int],
                 alerts: Any = None, refusal: Callable[[str], str] | None = None) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        #: 站点的告警台(``alerts.AlertStore`` 那一类;测试里可以不接)。
        self.alerts = alerts
        #: 站点这一道监护关(``supervision.SupervisionDesk.refusal``):要人监护的机器人
        #: 没人监护就不派。
        #: 排队的到点起跑前**再查一遍**(外审 W53-1 #1:提交时有人监护,到点时可能没了)。
        self.refusal = refusal
        on_ack = getattr(dispatcher, "on_ack", None)
        if on_ack is not None:
            on_ack(self._on_ack)                    # 晚到的拒收(外审 #4)

    # ------------------------------------------------------------ 估时

    def _standby_xy(self, robot: str, map_id: str) -> tuple[float, float] | None:
        rows = self.db.query("SELECT x, y FROM standby_points WHERE robot_id=? AND map_id=? "
                             "ORDER BY is_default DESC LIMIT 1", (robot, map_id))
        return (float(rows[0]["x"]), float(rows[0]["y"])) if rows else None

    def history_s(self, robot: str, mission_name: str) -> float | None:
        """这台这条最近 :data:`HISTORY_N` 趟走完的用了多久(派出 → ``task_done``)的中位数。"""
        rows = self.db.query(
            "SELECT c.issued_at AS t0, MIN(e.received_at) AS t1 FROM commands c "
            "JOIN events e ON e.robot_id = c.robot_id AND e.kind = 'task_done' "
            "AND json_extract(e.data, '$.task_id') = c.task_id "
            "WHERE c.robot_id=? AND c.kind='patrol' "
            "AND json_extract(c.payload, '$.mission.mission')=? "
            "GROUP BY c.task_id ORDER BY c.issued_at DESC LIMIT ?",
            (robot, mission_name, HISTORY_N))
        got = [(r["t1"] - r["t0"]) / 1000 for r in rows if r["t1"] and r["t1"] > r["t0"]]
        return statistics.median(got) if got else None

    def _here(self, robot: str) -> tuple[float, float, float] | None:
        """机器人此刻在哪(最近一份遥测的位姿);不知道是 ``None``。"""
        c = self.dispatcher.clients.get(robot)
        p = getattr(getattr(c, "telemetry", None), "pose", None)
        return (float(p.x), float(p.y), float(p.yaw)) if p is not None else None

    def _straight(self, robot: str) -> bool:
        c = self.dispatcher.clients.get(robot)
        caps = getattr(c, "capabilities", None)
        g = caps.tasks.get("goto", {}) if caps is not None else {}
        return str(g.get("path", "straight")) != "planned"

    def with_return(self, robot: str, wire: dict[str, Any]) -> dict[str, Any]:
        """末尾加上「走回起点」(用户 2026-10-31:保安派的最后一点不是起点,走完机器人必须自己回起点)。

        起点 = **派出那一刻机器人站的地方**。不靠待命点:刚加入的机器人只许手动派、站点不自动让它
        回待命点,也可能还没设待命点。直线走的机器人(没有规划器)沿来路倒着走回去(从最后一点直线回
        起点会穿墙,
        跟 ``standby.py`` 回待命点一个做法);会规划的直接回起点。不知道在哪、本来就停在起点附近、
        跑好几圈的,不加。"""
        here = self._here(robot)
        m = parse_mission(wire)
        if here is None or not m.waypoints or int(m.policy.loops or 1) > 1:
            return wire
        last = m.waypoints[-1].pose.position
        if _dist((last.x, last.y), (here[0], here[1])) < 0.5:
            return wire
        back: list[dict[str, Any]] = []
        if self._straight(robot):
            for i, w in enumerate(reversed(m.waypoints[:-1])):
                back.append(MissionWaypoint(name=f"return-{i + 1}", pose=w.pose).to_wire())
        back.append(MissionWaypoint(name="start", pose=Pose.from_xy_yaw(*here)).to_wire())
        if len(m.waypoints) + len(back) > 500:
            back = back[-1:]
        return {**wire, "waypoints": [*wire.get("waypoints", []), *back]}

    def estimate_s(self, robot: str, mission: Mission) -> int:
        """这一趟连走回起点(不知道起点就回待命点)大概要多少秒(已乘余量)。"""
        here = self._here(robot)
        hist = self.history_s(robot, mission.mission)
        if hist is not None:
            back = ((here[0], here[1]) if here is not None
                    else self._standby_xy(robot, mission.map_id))
            last = mission.waypoints[-1].pose.position if mission.waypoints else None
            # 历史量的是派出到最后一点;回待命点那一段另算
            extra = (_dist((last.x, last.y), back) * DETOUR / CRUISE_MPS
                     if back is not None and last is not None else 0.0)
            base = hist + extra
        else:
            back = (here[0], here[1]) if here is not None else self._standby_xy(robot,
                                                                                 mission.map_id)
            base = geom_estimate_s(mission, back_to=back)
        return int(math.ceil(max(base, 60.0) * MARGIN))

    # ------------------------------------------------------------ 能不能派

    def precheck(self, robot: str, wire: dict[str, Any]) -> str:
        """不发命令,先看这一趟派不派得出去(外审 #8:排队之前、起跑之前都要查)。能派回空串。"""
        if self.refusal is not None:
            why = self.refusal(robot)
            if why:
                return why
        why = self.dispatcher.dispatchable(robot, "patrol")
        if why:
            return why
        m = parse_mission(wire)
        loaded = self.dispatcher._loaded(self.dispatcher.clients.get(robot))
        if loaded is None:
            return f"{robot} 未上报已加载的地图"
        if (m.map_id, m.map_version) != loaded:
            return (f"任务用的地图 {m.map_id}:{m.map_version},{robot} 加载的是 "
                    f"{loaded[0]}:{loaded[1]}")
        return ""

    def _can_run(self, rid: str, mission: Mission) -> bool:
        """这台机器人现在接不接得了这条日程(跟排程执行器挑机器人同一套:能派、自主、图对得上)。"""
        if self.dispatcher.dispatchable(rid, "patrol"):
            return False
        autonomy = getattr(self.dispatcher, "autonomy", None)
        if autonomy is not None and autonomy(rid) != "autonomous":
            return False
        loaded = self.dispatcher._loaded(self.dispatcher.clients.get(rid))
        return loaded == (mission.map_id, mission.map_version)

    # ------------------------------------------------------------ 撞日程

    def _entries_for(self, robot: str) -> tuple[Any, list[ScheduleEntry]]:
        """会落到这台机器人头上的日程:写明了这台的;没写哪台、这台接得了、别的台都接不了的
        (外审 #6:按实际能跑的机器人算,不按登记了几台)。"""
        act = active_bundle(self.db)
        if act is None:
            return None, []
        live = [r.robot_id for r in self.dispatcher.registry.list() if not r.revoked]
        out = []
        for e in act.schedule.entries:
            if e.robot:
                if e.robot == robot:
                    out.append(e)
                continue
            m = act.missions.get(e.mission)
            if m is None or not self._can_run(robot, m):
                continue
            if not any(self._can_run(o, m) for o in live if o != robot):
                out.append(e)
        return act, out

    def _settled(self, entry_id: str, occ_ms: int) -> bool:
        """这一轮日程有没有定论(派出去了、跳过了、报过了、派不出去)。"""
        return bool(self.db.query(
            "SELECT 1 FROM schedule_runs WHERE entry_id=? AND scheduled_ms=? AND outcome IN "
            "('started','skip','alarm','dispatch_failed') LIMIT 1", (entry_id, occ_ms)))

    def _entry_est_s(self, act: Any, robot: str, entry: ScheduleEntry) -> int:
        m = act.missions.get(entry.mission)
        return self.estimate_s(robot, m) if m is not None else 30 * 60

    def collision(self, robot: str, start_ms: int, dur_s: int) -> tuple[int, str] | None:
        """``[start, start + dur]`` 这段这台机器人有没有日程。→ ``(那趟日程预计走完的时刻, 说明)``,
        没有 → ``None``。正在跑的日程也算(``sched-`` 开头的任务)。"""
        act, entries = self._entries_for(robot)
        if act is None:
            return None
        found: list[tuple[int, str]] = []
        running = self.dispatcher.busy(robot)
        if running and running.startswith("sched-"):
            rows = self.db.query(
                "SELECT r.entry_id, c.issued_at FROM schedule_runs r JOIN commands c "
                "ON c.task_id = r.task_id WHERE r.task_id=? LIMIT 1", (running,))
            entry = next((e for e in act.schedule.entries
                          if rows and e.id == rows[0]["entry_id"]), None)
            t0 = rows[0]["issued_at"] if rows else self._now()
            est = self._entry_est_s(act, robot, entry) if entry is not None else 30 * 60
            # 预计走完的时刻是定的(外审 #5:原来按候选起点往后挪,越推越远);估短了、还在跑的,至少
            # 按「现在再过一分钟」算 —— 不然手动的那趟优先级高,会把日程打断
            end = max(t0 + est * 1000, self._now() + 60_000)
            if end > start_ms:
                found.append((end, f"正在执行的日程「{entry.mission if entry else running}」"))
        tz = act.schedule.tz()
        a = datetime.fromtimestamp(start_ms / 1000, tz=tz)
        b = a + timedelta(seconds=dur_s)
        for e in entries:
            # 已经到点、还在窗口里、执行器还没派的那一轮也算(外审 #7)
            back = a - timedelta(minutes=int(e.window_min))
            for occ in occurrences_between(e, back, b):
                occ_ms = int(occ.timestamp() * 1000)
                if occ_ms < act.since(e.id):
                    continue
                if occ_ms < start_ms and (self._settled(e.id, occ_ms)
                                          or occ_ms + int(e.window_min) * 60_000 < start_ms):
                    continue
                end = occ_ms + self._entry_est_s(act, robot, e) * 1000
                if end > start_ms:
                    found.append((end, f"{occ:%H:%M} 日程「{e.mission}」"))
        return max(found) if found else None

    def slot(self, robot: str, start_ms: int, dur_s: int) -> tuple[int, str]:
        """最早能起跑的时刻(不撞日程)和推后的原因(没推是空串)。"""
        t, because = start_ms, ""
        for _ in range(MAX_PUSHES):
            c = self.collision(robot, t, dur_s)
            if c is None:
                return t, because
            t, because = c[0] + 60_000, because or c[1]
        return t, because

    # ------------------------------------------------------------ 派

    def plan(self, robot: str, wire: dict[str, Any]) -> dict[str, Any]:
        """不派,只说会怎样:估时、什么时候能起跑、为什么推后(App 派单框里先给人看)。"""
        m = parse_mission(wire)
        est = self.estimate_s(robot, m)
        now = self._now()
        t, because = self.slot(robot, now, est)
        return {"estimate_s": est, "start_ms": t, "queued": t > now, "because": because,
                "utc_offset_min": self.offset_min()}

    async def submit(self, robot: str, wire: dict[str, Any], *, label: str,
                     by: str) -> dict[str, Any]:
        """派一趟临时巡逻:不撞就马上派(撞了 ``DispatchRefused`` 照旧抛给调用方);撞了就排队。"""
        m = parse_mission(wire)
        est = self.estimate_s(robot, m)
        now = self._now()
        t, because = self.slot(robot, now, est)
        aid = uuid.uuid4().hex[:12]
        if t <= now:
            task_id = f"{ADHOC_PREFIX}{aid}"

            def 发之前记账(_cmd, tx) -> None:
                tx.execute("INSERT INTO adhoc_queue(id, robot, mission, label, by, created_ms, "
                           "not_before_ms, estimate_s, state, task_id, started_ms) "
                           "VALUES (?,?,?,?,?,?,?,?,'started',?,?)",
                           (aid, robot, json.dumps(wire, ensure_ascii=False), label, by, now, now,
                            est, task_id, now))
            r = await self.dispatcher.patrol(robot, self.with_return(robot, wire), issued_by=by,
                                             priority=MANUAL, task_id=task_id,
                                             before_send=发之前记账)
            res, why = _ack_result(r)
            if res not in _OK_ACKS:
                # 机器人明确没收(忙、电量低……):记成没走成,不当它在跑(外审 #4)
                self._fail_started(task_id, f"机器人回 {res}" + (f":{why}" if why else ""),
                                   tell=False)
                return {**r, "adhoc_id": aid, "queued": False, "estimate_s": est}
            self._tell_sent(robot, label, by, est, None)
            return {**r, "adhoc_id": aid, "queued": False, "estimate_s": est,
                    "utc_offset_min": self.offset_min()}
        why = self.precheck(robot, wire)
        if why:
            raise DispatchRefused(why)               # 排队之前就派不出去的,当场说(外审 #8)
        with self.db.tx() as c:
            c.execute("INSERT INTO adhoc_queue(id, robot, mission, label, by, created_ms, "
                      "not_before_ms, estimate_s, state, note) VALUES (?,?,?,?,?,?,?,?,'queued',?)",
                      (aid, robot, json.dumps(wire, ensure_ascii=False), label, by, now, t, est,
                       because))
        self._tell_sent(robot, label, by, est, (t, because))
        return {"adhoc_id": aid, "queued": True, "start_after_ms": t, "estimate_s": est,
                "because": because, "utc_offset_min": self.offset_min()}

    def cancel(self, aid: str, *, by: str, may_others: bool) -> None:
        rows = self.db.query("SELECT * FROM adhoc_queue WHERE id=?", (str(aid)[:32],))
        if not rows:
            raise AdhocError("没有这一条临时巡逻")
        r = rows[0]
        if r["state"] != "queued":
            raise AdhocError("本次任务已不在排队了(开始跑的用「叫停」)")
        if r["by"] != by and not may_others:
            raise PermissionError("只有派的人、经理、管理员能取消")
        with self.db.tx() as c:
            c.execute("UPDATE adhoc_queue SET state='cancelled', note=?, ended_ms=? WHERE id=? "
                      "AND state='queued'", (f"{by} 取消了", self._now(), r["id"]))

    async def tick(self) -> None:
        """排队的:过期作废;到点、机器人空、后面不撞就派;撞了就再往后排。"""
        now = self._now()
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM adhoc_queue WHERE state='queued' ORDER BY not_before_ms, created_ms")]
        took: set[str] = set()
        for r in rows:
            rid = r["robot"]
            if now - r["created_ms"] > QUEUE_TTL_MS:
                self._end(r, "expired", "排了两个小时尚未轮上,作废了")
                continue
            if r["not_before_ms"] > now or rid in took:
                continue
            if self.dispatcher.busy(rid) is not None:
                continue                                    # 还在跑别的:下一拍再看
            t, because = self.slot(rid, now, int(r["estimate_s"]))
            if t > now:
                with self.db.tx() as c:
                    c.execute("UPDATE adhoc_queue SET not_before_ms=?, note=? WHERE id=?",
                              (t, because, r["id"]))
                continue
            why = self.precheck(rid, json.loads(r["mission"]))
            if why:
                self._end(r, "failed", f"到点派不出去:{why}")   # 外审 #1、#8:起跑前再查一遍
                continue
            task_id = f"{ADHOC_PREFIX}{r['id']}"

            def 发之前记账(_cmd, tx, _id=r["id"], _task=task_id) -> None:
                n = tx.execute("UPDATE adhoc_queue SET state='started', task_id=?, started_ms=? "
                               "WHERE id=? AND state='queued'", (_task, now, _id)).rowcount
                if n != 1:
                    raise DispatchRefused("本次任务刚被取消了")
            took.add(rid)
            try:
                full = self.with_return(rid, json.loads(r["mission"]))
                got = await self.dispatcher.patrol(rid, full,
                                                   issued_by=r["by"], priority=MANUAL,
                                                   task_id=task_id, before_send=发之前记账)
            except DispatchRefused as exc:
                self._end(r, "failed", f"派不出去:{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 —— 回执超时等:可能在跑了,当它起跑过
                log.warning("临时巡逻 %s 派单出了岔子:%s", r["id"], exc)
                continue
            res, why = _ack_result(got)
            if res not in _OK_ACKS:
                self._fail_started(task_id, f"机器人回 {res}" + (f":{why}" if why else ""))
                continue
            self._tell(r, "排队的临时巡逻开始了", f"{rid} 开始走「{r['label']}」")

    def _fail_started(self, task_id: str, note: str, *, tell: bool = True) -> None:
        """已经记成起跑的那一趟机器人没收:改成没走成、告诉派的人。"""
        with self.db.tx() as c:
            n = c.execute("UPDATE adhoc_queue SET state='failed', note=?, ended_ms=? "
                          "WHERE task_id=? AND state='started'",
                          (note, self._now(), task_id)).rowcount
            rows = c.execute("SELECT * FROM adhoc_queue WHERE task_id=?", (task_id,)).fetchall()
        if n and rows and tell:
            r = dict(rows[0])
            self._tell(r, f"临时巡逻未完成:{r['robot']}「{r['label']}」", note)

    def _on_ack(self, ack: Any) -> None:
        """回执(包括超时之后才到的):临时巡逻那一趟机器人明确没收,改成没走成。"""
        task_id = str(getattr(ack, "task_id", "") or "")
        res = getattr(getattr(ack, "result", None), "value", str(getattr(ack, "result", "")))
        if not task_id.startswith(ADHOC_PREFIX) or res in _OK_ACKS:
            return
        try:
            self._fail_started(task_id, f"机器人回 {res}:{getattr(ack, 'reason', '')}")
        except Exception:
            log.exception("临时巡逻的回执记不下来")

    def _end(self, r: dict[str, Any], state: str, note: str) -> None:
        with self.db.tx() as c:
            n = c.execute("UPDATE adhoc_queue SET state=?, note=?, ended_ms=? WHERE id=? "
                          "AND state='queued'", (state, note, self._now(), r["id"])).rowcount
        if n:
            self._tell(r, f"临时巡逻未完成:{r['robot']}「{r['label']}」", note)

    # ------------------------------------------------------------ 通知

    def _raise(self, *, kind: str, robot: str, title: str, detail: str,
               context: dict[str, Any]) -> None:
        if self.alerts is None:
            return
        try:
            # 一次一条、不合并(外审 #2、#3);告警台自己记时刻
            self.alerts.raise_alert(kind=kind, robot=robot, title=title[:120], detail=detail[:500],
                                    context=context, merge=False)
        except Exception:
            log.exception("临时巡逻的通知记不下来")

    def _tell_sent(self, robot: str, label: str, by: str, est: int,
                   queued: tuple[int, str] | None) -> None:
        mins = max(1, round(est / 60))
        if queued is None:
            title = f"{by} 派 {robot} 走「{label}」(临时,约 {mins} 分钟)"
        else:
            at = datetime.fromtimestamp(queued[0] / 1000, tz=self._tz())
            title = f"{by} 派 {robot} 走「{label}」(临时),跟{queued[1]}撞了,排到 {at:%H:%M}"
        self._raise(kind=KIND_SENT, robot=robot, title=title, detail="临时巡逻不进日程",
                    context={"by": by, "label": label, "estimate_s": est})

    def _tell(self, r: dict[str, Any], title: str, detail: str) -> None:
        self._raise(kind=KIND_QUEUE, robot=r["robot"], title=title, detail=detail,
                    context={"to": r["by"], "adhoc_id": r["id"], "label": r["label"]})

    def _tz(self) -> Any:
        act = active_bundle(self.db)
        if act is not None:
            return act.schedule.tz()
        from d1max_site.sitetime import site_tz
        return site_tz(self.db)

    def offset_min(self) -> int:
        """站点时区此刻离 UTC 几分钟(手机、网页按站点的钟显示时刻,外审 #12)。"""
        off = datetime.fromtimestamp(self._now() / 1000, tz=self._tz()).utcoffset()
        return int(off.total_seconds() // 60) if off is not None else 0

    # ------------------------------------------------------------ 看

    def between(self, a_ms: int, b_ms: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, robot, label, by, created_ms, not_before_ms, estimate_s, state, task_id, "
            "started_ms, ended_ms, note FROM adhoc_queue WHERE "
            # 今天起跑 / 排到今天的、今天派的(排到明天的也列出来、能取消)、还在排队的(外审 #11)
            "COALESCE(started_ms, not_before_ms) BETWEEN ? AND ? OR created_ms BETWEEN ? AND ? "
            "OR state='queued' ORDER BY COALESCE(started_ms, not_before_ms)",
            (a_ms, b_ms, a_ms, b_ms))
        return [dict(r) for r in rows]
