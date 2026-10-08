"""自动回充,站点这一头(W13,决策 25、45;规矩见 :mod:`d1max_contract.charging`)。

每台狗登记一个**桩前对准点**(``chargers``:桩前约 1.5 m、正对桩,地图位姿)。站点每拍对账:

- **起**:狗在线、新鲜、自己能走(``autonomous``)、报了 ``dock``、登记了桩、空着(没在跑任务,或者只在
  回待命点)、不在驱离、电量 ≤ 30% → 起一轮回充:派 ``goto`` 到对准点(``charge-goto-…``)。
- **接**:每一轮的进度**按站点库里落着的任务终态事件对账**(``events`` 表,站点重启照样认;W24 的教训:
  不靠一次性的回调):
  - 走到了(``task_done``)→ 派 ``dock``(``charge-dock-…``);
    对桩、充满、出了桩(``task_done``)→ 这一轮完;
  - **被抢了**(``task_preempted``,入侵派遣、人手动派的)→ 记「被打断」,**狗空了接着充**(决策 45:
    处理完回来接着充;不再看电量线);
  - 失败(``task_failed``,走不到、没对上桩、充电断了、出不了桩)→ 报 P1 ``charge_failed``(跟「这一轮
    作废」同一个事务记进待报告警),10 分钟后电量还低再试;
  - 人中止(``task_aborted``)→ 这一轮作废,10 分钟内不自己再起(人有人的打算)。
  - 派了、狗上没在跑、也没终态事件,过了 :data:`LOST_S`(狗重启、命令丢了)→ 当被打断,接着充。
- **入侵**(决策 45):正在回充的狗,电量 ≥ 50% 才派(``incidents.pick_robot`` 问 :meth:`refuse`);
  ``dock`` 被抢时狗上先出桩再交。
- 回充这几趟结束时,待命点管理器不自动派回程(:meth:`holds`):充完的狗就停在桩前。
- **先落库、再派**(W13 外审 4):要派的那一趟的任务号先写进 ``charge_cycles``(写不进去就不派);
  狗明确拒收 → 退回上一步(走到了的退回 ``arrived``、刚起的作废)、30 秒后再派;发出去回执没到
  (说不清狗收没收)→ 留着,按那个任务号的终态事件对账,过了 :data:`LOST_S` 还没有就当被打断。
- **桩上危险**(W13 外审 1–3):狗报 ``dock.hazard``(出桩、停对桩确认不了,狗上运动锁住)→ 报 P1
  ``dock_stuck``,一次挂一次(``dock_hazards``,跟待报告警同一个事务),狗上摘了就清。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from d1max_contract.charging import (
    CHARGE_PRIORITY,
    DOCK_TIMEOUT_S,
    INTRUSION_MIN_PCT,
    LOW_PCT,
    RESUME_PCT,
    TASK_PREFIX,
    DockRequest,
)
from d1max_contract.messages import MapPose
from d1max_site.db import SiteDB
from d1max_site.pending_alerts import flush, queue
from d1max_site.priorities import STANDBY_PREFIX

log = logging.getLogger(__name__)

#: 派了之后狗上既没在跑、也没终态事件,等这么久(毫秒)当被打断。
LOST_S = 90
#: 失败、人中止之后多久不自己再起(毫秒)。
COOLDOWN_MS = 10 * 60_000
#: 派不出去(抛错、被拒)之后,这台狗隔多久(毫秒)再派:别每拍都发。派成了不限(下一步马上接)。
RESEND_MS = 30_000
_TERMINAL = ("task_done", "task_failed", "task_aborted", "task_preempted")


class ChargeError(ValueError):
    """登记桩不合规矩(API 回 400)。"""


class ChargeDesk:
    def __init__(self, db: SiteDB, dispatcher: Any, *, now_ms: Callable[[], int],
                 low_pct: float = LOW_PCT, resume_pct: float = RESUME_PCT,
                 dock_timeout_s: int = DOCK_TIMEOUT_S) -> None:
        self.db = db
        self.dock_timeout_s = dock_timeout_s
        self.dispatcher = dispatcher
        self._now = now_ms
        self.low_pct, self.resume_pct = low_pct, resume_pct
        #: 告警台(``charge_failed``);站点主程序接上。
        self.alerts: Any = None
        #: 正在驱离的狗(``DeterrenceDesk.busy``):驱离中不去充。
        self.busy: Callable[[], set[str]] | None = None
        self._retry_at: dict[str, int] = {}

    # ------------------------------------------------------------ 桩

    def set_charger(self, robot_id: str, *, map_id: str, map_version: str, x: float, y: float,
                    yaw: float, by: str) -> dict[str, Any]:
        try:
            MapPose(map_id=map_id, map_version=map_version, frame_id="map", x=x, y=y, yaw=yaw)
        except Exception as exc:
            raise ChargeError(f"桩前对准点不成形:{exc}") from exc
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO chargers(robot_id, map_id, map_version, x, y, yaw, "
                      "set_ms, set_by) VALUES (?,?,?,?,?,?,?,?)",
                      (robot_id, map_id, map_version, float(x), float(y), float(yaw),
                       self._now(), by))
        return self.charger(robot_id) or {}

    def remove_charger(self, robot_id: str) -> bool:
        with self.db.tx() as c:
            c.execute("DELETE FROM charge_cycles WHERE robot_id=?", (robot_id,))
            return c.execute("DELETE FROM chargers WHERE robot_id=?", (robot_id,)).rowcount > 0

    def charger(self, robot_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM chargers WHERE robot_id=?", (robot_id,))
        return dict(rows[0]) if rows else None

    def view(self) -> dict[str, Any]:
        return {"chargers": [dict(r) for r in self.db.query(
                    "SELECT * FROM chargers ORDER BY robot_id")],
                "cycles": [dict(r) for r in self.db.query(
                    "SELECT * FROM charge_cycles ORDER BY robot_id")],
                "low_pct": self.low_pct, "resume_pct": self.resume_pct,
                "intrusion_min_pct": INTRUSION_MIN_PCT}

    # ------------------------------------------------------------ 别的台问的

    def holds(self, robot_id: str, task_id: str) -> bool:
        """回充这几趟结束时,待命点管理器别自动派回程。"""
        return task_id.startswith(TASK_PREFIX)

    def refuse(self, robot_id: str) -> str:
        """入侵派遣问:这台狗正在回充、电量不到 50%(决策 45)→ 不派的理由;不然空串。"""
        c = self.dispatcher.clients.get(robot_id)
        running = c.status.task.task_id if c is not None and c.status and c.status.task else ""
        if not running.startswith(TASK_PREFIX):
            return ""
        pct = self._battery(robot_id)
        if pct is None or pct < INTRUSION_MIN_PCT:
            return (f"{robot_id} 在充电、电量不够"
                    f"({'不知道' if pct is None else f'{pct:.0f}%'},要 ≥ {INTRUSION_MIN_PCT:.0f}%)")
        return ""

    def _battery(self, robot_id: str) -> float | None:
        c = self.dispatcher.clients.get(robot_id)
        t = c.telemetry if c is not None else None
        return float(t.battery_pct) if t is not None else None

    # ------------------------------------------------------------ 每拍

    async def tick(self) -> None:
        try:
            flush(self.db, self.alerts)
        except Exception:
            log.exception("待报的告警这一拍没办成")
        for ch in [dict(r) for r in self.db.query("SELECT * FROM chargers")]:
            try:
                await self._one(ch)
            except Exception:
                log.exception("%s 的回充这一拍没办成", ch["robot_id"])
        try:
            self._hazards()
        except Exception:
            log.exception("桩上危险这一拍没对上账")

    def _hazards(self) -> None:
        """狗报的「桩上危险」对账:新挂上的报 P1(一次挂一次),摘了的清掉记录。"""
        seen = {r["robot_id"] for r in self.db.query("SELECT robot_id FROM dock_hazards")}
        now = self._now()
        for rid, c in list(self.dispatcher.clients.items()):
            d = c.capabilities.tasks.get("dock") if c.capabilities is not None else None
            if not isinstance(d, dict):
                continue
            hz = d.get("hazard")
            if hz and rid not in seen:
                with self.db.tx() as tx:
                    tx.execute("INSERT INTO dock_hazards(robot_id, reason, created_ms) "
                               "VALUES (?,?,?)", (rid, str(hz)[:200], now))
                    queue(tx, kind="dock_stuck", robot=rid,
                          title="狗可能还在充电桩上,运动锁住了",
                          detail=f"{str(hz)[:200]};要人去看:把狗挪下桩,它确认离了桩自己解锁",
                          context={}, now_ms=now)
            elif not hz and rid in seen:
                with self.db.tx() as tx:
                    tx.execute("DELETE FROM dock_hazards WHERE robot_id=?", (rid,))
        flush(self.db, self.alerts)

    def _cycle(self, rid: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM charge_cycles WHERE robot_id=?", (rid,))
        return dict(rows[0]) if rows else None

    def _terminal(self, rid: str, task_id: str) -> tuple[str, dict] | None:
        import json
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

    def _idle(self, rid: str, c: Any) -> bool:
        t = c.status.task if c.status is not None else None
        if t is not None and not t.task_id.startswith(STANDBY_PREFIX):
            return False
        return not (self.busy is not None and rid in self.busy())

    async def _one(self, ch: dict[str, Any]) -> None:
        rid, now = ch["robot_id"], self._now()
        c = self.dispatcher.clients.get(rid)
        fresh = getattr(self.dispatcher, "_fresh", None)
        if c is None or c.status is None or not c.status.online or \
                (callable(fresh) and not fresh(c)):
            return
        cyc = self._cycle(rid)
        if cyc is None:
            pct = self._battery(rid)
            caps = c.capabilities.tasks if c.capabilities is not None else {}
            if pct is not None and pct <= self.low_pct and "dock" in caps and self._idle(rid, c) \
                    and self.dispatcher.autonomy(rid) == "autonomous":
                log.info("%s 电量 %.0f%%:去充", rid, pct)
                await self._goto(rid, ch, None)
            return
        state = cyc["state"]
        if state == "arrived":
            await self._dock(rid, cyc)                  # 走到了、对桩还没派成
            return
        if state == "cooldown":
            if now >= (cyc["until_ms"] or 0):
                self._set(rid, None)
            return
        if state == "resume":
            if self._idle(rid, c):
                log.info("%s 回充被打断过:狗空了,接着充", rid)
                await self._goto(rid, ch, cyc)
            return
        term = self._terminal(rid, cyc["task_id"])
        if term is None:
            running = c.status.task.task_id if c.status.task else ""
            if running != cyc["task_id"] and now - cyc["sent_ms"] >= LOST_S * 1000:
                self._set(rid, "resume")                # 狗重启了、命令丢了:当被打断
            return
        kind, data = term
        if kind == "task_done":
            if state == "goto":
                self._set(rid, "arrived")               # 先记「走到了」,再派对桩
                await self._dock(rid, {**cyc, "state": "arrived"})
            else:
                log.info("%s 充完了,出了桩", rid)
                self._set(rid, None)
        elif kind == "task_preempted":
            self._set(rid, "resume")
        elif kind == "task_aborted":
            self._set(rid, "cooldown", until_ms=now + COOLDOWN_MS)
        else:
            why = str(data.get("reason") or data.get("error") or "")[:200]
            what = "走到桩前" if state == "goto" else "对桩、充电"
            with self.db.tx() as tx:
                tx.execute("UPDATE charge_cycles SET state='cooldown', until_ms=? WHERE robot_id=?",
                           (now + COOLDOWN_MS, rid))
                queue(tx, kind="charge_failed", robot=rid, title=f"自动回充没成:{what}失败",
                      detail=(why or "看狗的日志") + ";10 分钟后电量还低会再试,还不行请人去看桩",
                      context={"task_id": cyc["task_id"]}, now_ms=now)
            flush(self.db, self.alerts)

    def _set(self, rid: str, state: str | None, *, until_ms: int | None = None) -> None:
        with self.db.tx() as c:
            if state is None:
                c.execute("DELETE FROM charge_cycles WHERE robot_id=?", (rid,))
            else:
                c.execute("UPDATE charge_cycles SET state=?, until_ms=? WHERE robot_id=?",
                          (state, until_ms, rid))

    async def _goto(self, rid: str, ch: dict[str, Any], cyc: dict[str, Any] | None) -> None:
        now = self._now()
        if now < self._retry_at.get(rid, 0):
            return
        self._retry_at[rid] = now + RESEND_MS              # 派成了再清
        tid = f"{TASK_PREFIX}goto-{uuid.uuid4().hex[:10]}"
        target = MapPose(map_id=ch["map_id"], map_version=ch["map_version"], frame_id="map",
                         x=ch["x"], y=ch["y"], yaw=ch["yaw"]).to_wire()
        with self.db.tx() as c:                         # 先落库(W13 外审 4):写不进去就不派
            c.execute("INSERT OR REPLACE INTO charge_cycles(robot_id, state, task_id, sent_ms, "
                      "started_ms, until_ms) VALUES (?,?,?,?,?,NULL)",
                      (rid, "goto", tid, now, cyc["started_ms"] if cyc else now))
        try:
            r = await self.dispatcher.goto(rid, target, None, issued_by="charge",
                                           priority=CHARGE_PRIORITY, task_id=tid, charge=True)
        except Exception as exc:  # noqa: BLE001 - 说不清狗收没收:留着,按终态 / 丢了对账
            log.warning("%s 去桩前没派成(回执没到?):%s", rid, exc)
            return
        if not _accepted(r):
            log.warning("%s 去桩前被拒:%s", rid, (r or {}).get("ack"))
            if cyc is None:
                self._set(rid, None)                    # 刚起的:作废,电量线还低下次再起
            else:
                self._set(rid, "resume")
            return
        self._retry_at.pop(rid, None)

    async def _dock(self, rid: str, cyc: dict[str, Any]) -> None:
        now = self._now()
        if now < self._retry_at.get(rid, 0):
            return
        self._retry_at[rid] = now + RESEND_MS              # 派成了再清
        tid = f"{TASK_PREFIX}dock-{uuid.uuid4().hex[:10]}"
        with self.db.tx() as c:                         # 先落库(W13 外审 4):写不进去就不派
            c.execute("UPDATE charge_cycles SET state='dock', task_id=?, sent_ms=? "
                      "WHERE robot_id=?", (tid, now, rid))
        try:
            r = await self.dispatcher.dock(rid, tid, DockRequest(
                resume_pct=self.resume_pct, dock_timeout_s=self.dock_timeout_s),
                issued_by="charge")
        except Exception as exc:  # noqa: BLE001 - 说不清狗收没收:留着,按终态 / 丢了对账
            log.warning("%s 对桩没派成(回执没到?):%s", rid, exc)
            return
        if not _accepted(r):
            log.warning("%s 对桩被拒:%s", rid, (r or {}).get("ack"))
            self._set(rid, "arrived")                   # 退回「走到了」,30 秒后再派
            return
        self._retry_at.pop(rid, None)


def _accepted(r: Any) -> bool:
    ack = r.get("ack", {}) if isinstance(r, dict) else {}
    return ack.get("result") in ("accepted", "duplicate")
