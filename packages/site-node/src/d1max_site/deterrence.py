"""分级驱离(W22,决策 37)。入侵派出去的狗**到了拦截点**,站点开一场驱离,
按级给狗发上装命令(W21 ``deter``):

| 级 | 做什么 |
|---|---|
| L0 观察 | 什么都不开(到了已经拍过一张,W17) |
| L1 灯光 | 警灯、聚光灯 |
| L2 语音警告 | L1 + 喇叭轮放「这里是私人领地,请离开」(``warn-<语言>``,中文、英文、马来文) |
| L3 警笛 | L2 的灯 + 警笛 + 喇叭轮放「已通知保安」(``notified-<语言>``) |
| L4 人工 | 跟 L3 一样开着,但只有人能进;表示保安接手了 |

- **自动升**:到了之后每级停 ``STEP_S``(30 秒)升一级,自动最多到 L3。**人动过一次级别(跳级、往回退)
  就不再自动升**:人在管了。
- **解除**:保安、管理员、业主都能(``abort`` 权限,跟「叫停」一个道理):全关、狗回待命点。
- **最长 10 分钟**(从开始或最后一次人动过算):到点自动收(全关、回待命点)。
  W24 人员检测没做,不知道人走没走,没人管的时候不许一直响。
- 狗被派去干别的了(开始跑别的任务):这一场结束、全关,不派回程。
- **声光自己会停**:每次开都带 ``KEEP_S``(45 秒),站点每 ``RENEW_S``(20 秒)续一次;站点挂了、断网了,
  狗上最多 45 秒全关。喇叭的优先级用事件档(``EVENT``),手动按的(``MANUAL``)打断不了。
- 驱离中的狗:事件派遣不选它去别的拦截点;待命点管理器不自动派它回去(驱离结束时这里派)。
- 每一场落库(``deter_sessions``):站点重启接着管(级别、开始时刻、人动没动过都在)。

**一致性**(W22 外审):
- 每台狗一把锁:切级、每拍、解除都在锁里做 —— 解除等在途的那条命令回来、再全关,
  结束之后不会再有「开」发出去。
- 关的依据是「这一场确认关上过」(``off``),不是「记得发过开」:发过开(回执成没成都算)
  就从 ``off`` 里去掉;该关又没确认关过的一律发关。站点重启后 ``off`` 是空的,
  第一拍把不该开的全关一遍。
- 先落库、再改内存:切级、自动升落库没成就这一下不改。
- 结束:**先在库里标「收尾中」**(原因、回不回待命点)→ 全关 → 删库 → 删成了才从内存拿掉、派回程。
  删库没成,下一拍接着收;**站点重启读到「收尾中」的只接着收尾,不再开任何东西**(W22 复查)。
  连「收尾中」都写不进库:照样先全关、内存里标收尾中、每拍重试,解除回报错让人知道。

**保持距离**(W25,决策 40):开场给狗派一趟 ``standoff``(优先级最低;判定在狗上:人进 3 m 就退,
只退不进,退不了原地站定)。每拍按狗能力里的 ``standoff`` 对账:狗没在守这一场的就补派(每 15 秒最多
一次);狗报 ``cornered``(无路可退)→ 报 P1 ``deter_cornered``(「这一场无路可退过」跟待报告警一个事务
落库,同 W24 的告警意图)。这一趟保持距离不算「狗被派去干别的了」。

W25 外审:
- **拴绳中心固定**:开场时取这一场出警 goto 的目标点(拦截点)落库,每次派都带上;补派、狗重启都按它算。
  取不到目标点就不派(驱离照旧只管声光)。
- **撤是收尾的一步**:全关之后、删库之前,按这一场固定的任务号撤(不看内存里狗报的状态,也不看拦截点
  在不在:升级前派的那一场拦截点是空的,W25 复查);狗回执收下了、
  或者说没这个任务、已经结束了,才删库;没撤成就停在「收尾中」(落了库),下一拍、站点重启后接着撤
  (每 15 秒最多发一次)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from d1max_contract.messages import Event, MapPose
from d1max_contract.standoff import MAX_S as STANDOFF_MAX_S
from d1max_contract.standoff import TASK_PREFIX as STANDOFF_PREFIX
from d1max_contract.standoff import StandoffRequest
from d1max_site.db import SiteDB
from d1max_site.priorities import EVENT, INCIDENT_PREFIX

log = logging.getLogger(__name__)

STEP_S = 30
AUTO_MAX = 3
MAX_LEVEL = 4
CAP_S = 600
#: 狗没在守这一场的保持距离:隔这么久(秒)补派一次。
STANDOFF_RESEND_S = 15
KEEP_S = 45.0
RENEW_S = 20
CLIP_EVERY_S = 10
CLIP_MAX_S = 15.0
LANGS = ("zh", "en", "ms")
LABEL = {0: "观察", 1: "灯光", 2: "语音警告", 3: "警笛", 4: "人工"}
#: 每一级开哪几路继电器、喇叭放哪一套话术(``None`` = 不放)。
RELAYS = {0: frozenset(), 1: frozenset({"strobe", "spotlight"}),
          2: frozenset({"strobe", "spotlight"}),
          3: frozenset({"strobe", "spotlight", "siren"}),
          4: frozenset({"strobe", "spotlight", "siren"})}
CLIPS = {0: None, 1: None, 2: "warn", 3: "notified", 4: "notified"}
#: 狗上人员检测记的事件(W24):**只存档,不拿来控制驱离**(W24 外审:事件会丢、会补投、会比会话早到)。
#: 控制按狗能力里的**当前人员状态**每拍对账(``capabilities.tasks.persons``)。
PERSON_KINDS = frozenset({"person_seen", "person_near", "person_gone", "person_snapshot"})


class DeterrenceError(ValueError):
    """没有这一场、级别不对、落库没成(API 回 400/404/503)。"""


def _standoff_id(s: Session) -> str:
    return f"{STANDOFF_PREFIX}{s.task_id}"


@dataclass
class Session:
    robot_id: str
    incident_id: int
    zone: str
    task_id: str
    level: int
    started_ms: int
    level_ms: int
    human_ms: int = 0
    auto: bool = True
    by: str = ""
    #: 不落库:每一路上次发「开」成了的时刻(续期用)、这一场确认关上过的几路、喇叭放到第几段。
    sent: dict[str, int] = field(default_factory=dict)
    off: set[str] = field(default_factory=set)
    clip_i: int = 0
    clip_ms: int = 0
    #: 人员检测(W24):最近一次对账看到的(不落库,给人看);这一场看到过人没有(落库)。
    #: 人员告警要报的意图另存在 ``pending_alerts``(W24 复查:人走了、会话结束都不许把没报成的丢掉)。
    #: ``person_alerted`` 是老字段,留着不用。
    persons: dict[str, Any] | None = None
    seen_person: bool = False
    person_alerted: bool = False
    #: 保持距离(W25):狗报的状态(不落库,给人看);上次补派的时刻(不落库);这一场无路可退过没有(落库)。
    standoff: str | None = None
    standoff_ms: int = 0
    #: 拦截点(地图位姿的 JSON,开场时取这一场出警 goto 的目标点;落库)。空 = 取不到,不派保持距离。
    center: str = ""
    #: 收尾时上次撤保持距离的时刻、撤成了没有(都不落库:站点重启后再撤一次,无害)。
    abort_ms: int = 0
    standoff_stopped: bool = False
    cornered: bool = False
    #: 收尾中(落库没成,下一拍接着收):``(原因, 回不回待命点)``。收尾中不再开任何东西。
    ending: tuple[str, bool] | None = None
    #: 「收尾中」落进库了没有(没落进去的话站点重启会把这一场当成还在驱离)。
    ending_saved: bool = False

    def row(self) -> tuple:
        return (self.robot_id, self.incident_id, self.zone, self.task_id, self.level,
                self.started_ms, self.level_ms, self.human_ms, int(self.auto), self.by,
                int(self.seen_person), int(self.person_alerted), int(self.cornered),
                self.center)

    def view(self, now_ms: int) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items()
             if k not in ("sent", "off", "clip_i", "clip_ms", "ending", "ending_saved",
                          "seen_person", "person_alerted", "standoff_ms", "center",
                          "abort_ms", "standoff_stopped")}
        d["label"] = LABEL[self.level]
        d["next_in_s"] = (max(0, (self.level_ms + STEP_S * 1000 - now_ms) // 1000)
                          if self.auto and self.level < AUTO_MAX else None)
        d["ends_in_s"] = max(0, (max(self.started_ms, self.human_ms) + CAP_S * 1000 - now_ms)
                             // 1000)
        return d


class DeterrenceDesk:
    def __init__(self, db: SiteDB, dispatcher: Any, *, now_ms: Callable[[], int],
                 standby: Any = None) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self.standby = standby
        #: 告警台(W24:驱离中看到人报 ``intrusion_person``)。站点主程序接上;没接就不报。
        self.alerts: Any = None
        self.sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for r in db.query("SELECT * FROM deter_sessions"):
            s = Session(robot_id=r["robot_id"], incident_id=r["incident_id"], zone=r["zone"],
                        task_id=r["task_id"], level=r["level"], started_ms=r["started_ms"],
                        level_ms=r["level_ms"], human_ms=r["human_ms"], auto=bool(r["auto"]),
                        by=r["by"], seen_person=bool(r["person_seen"]),
                        person_alerted=bool(r["person_alerted"]), cornered=bool(r["cornered"]),
                        center=r["standoff_center"])
            if r["ending"]:
                # 上次收尾到一半(删库没成)就停了:只接着收尾
                s.ending, s.ending_saved = (r["ending"], bool(r["ending_back"])), True
            self.sessions[s.robot_id] = s
        dispatcher.on_event(self._on_event)

    def _lock(self, rid: str) -> asyncio.Lock:
        lk = self._locks.get(rid)
        if lk is None:
            lk = self._locks[rid] = asyncio.Lock()
        return lk

    # ------------------------------------------------------------ 进出

    def holds(self, robot_id: str, task_id: str) -> bool:
        """待命点管理器问:这一趟跑完要不要先别回(驱离接管回程)。到拦截点的事件任务、狗装了上装才接。"""
        return task_id.startswith(INCIDENT_PREFIX) and self._can(robot_id)

    def busy(self) -> set[str]:
        """正在驱离的狗(事件派遣不选它)。收尾中的也算。"""
        return set(self.sessions)

    def _can(self, robot_id: str) -> bool:
        c = self.dispatcher.clients.get(robot_id)
        return bool(c and c.capabilities and "deter" in c.capabilities.tasks)

    def _center_of(self, task_id: str) -> str:
        """这一场的拦截点:出警那条 goto 的目标点(W25 外审 2)。取不到回空串。"""
        try:
            rows = self.db.query("SELECT payload FROM commands WHERE task_id=? AND kind='goto' "
                                 "ORDER BY issued_at DESC LIMIT 1", (task_id,))
            target = json.loads(rows[0]["payload"])["target"] if rows else None
            return json.dumps(MapPose.from_wire(target).to_wire()) if target else ""
        except Exception:
            log.exception("%s 的拦截点取不到(不派保持距离)", task_id)
            return ""

    def _on_event(self, robot_id: str, e: Event) -> None:
        if e.kind in PERSON_KINDS:
            return                       # W24 外审:只存档(派遣器已落库),控制按当前状态对账
        task_id = e.data.get("task_id") if isinstance(e.data, dict) else None
        if e.kind != "task_done" or not isinstance(task_id, str) \
                or not task_id.startswith(INCIDENT_PREFIX) or not self._can(robot_id):
            return
        rows = self.db.query("SELECT id, zone FROM incidents WHERE task_id=? ORDER BY id LIMIT 1",
                             (task_id,))
        if not rows or robot_id in self.sessions:
            return
        now = self._now()
        s = Session(robot_id=robot_id, incident_id=rows[0]["id"], zone=rows[0]["zone"],
                    task_id=task_id, level=0, started_ms=now, level_ms=now,
                    center=self._center_of(task_id))
        try:
            self._save(s)
        except Exception:
            # 落不了库就不开这一场(开了也接不住重启)。待命点那头已经没派回程:这里照常派回去
            log.exception("%s 到了拦截点,驱离落不了库:不开,照常回待命点", robot_id)
            if self.standby is not None:
                asyncio.get_running_loop().create_task(self._back(robot_id, task_id))
            return
        self.sessions[robot_id] = s
        log.info("%s 到了拦截点(防区 %s):开始驱离", robot_id, s.zone)
        self._publish(s)
        # 到之前就已经看到的人(W24 外审 3):开场马上对账一次,不等下一拍
        asyncio.get_running_loop().create_task(self._reconcile_now(robot_id))

    async def _back(self, rid: str, after: str, by: str = "auto") -> None:
        try:
            await self.standby.return_to(rid, issued_by=f"deterrence:{by}")
        except Exception as exc:                        # noqa: BLE001 - 回不去:推给值守的人
            log.warning("%s 回待命点没派成: %s", rid, exc)
            self.dispatcher.feed.publish({"kind": "standby_failed", "robot_id": rid,
                                          "after": after, "reason": str(exc)})

    def _save(self, s: Session) -> None:
        with self.db.tx() as c:
            self._save_in(c, s)

    @staticmethod
    def _save_in(c: Any, s: Session) -> None:
        c.execute("INSERT OR REPLACE INTO deter_sessions(robot_id, incident_id, zone, "
                  "task_id, level, started_ms, level_ms, human_ms, auto, by, person_seen, "
                  "person_alerted, cornered, standoff_center) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", s.row())

    def _publish(self, s: Session | None, robot_id: str = "", ended: str = "") -> None:
        if s is not None:
            item: dict[str, Any] = {"kind": "deterrence", "robot_id": s.robot_id,
                                    "session": s.view(self._now())}
        else:
            item = {"kind": "deterrence", "robot_id": robot_id, "session": None, "ended": ended}
        try:
            self.dispatcher.feed.publish(item)
        except Exception:
            log.exception("驱离推给手机没推出去")

    def view(self) -> list[dict[str, Any]]:
        now = self._now()
        return [s.view(now) for s in sorted(self.sessions.values(), key=lambda s: s.robot_id)
                if s.ending is None]

    def _live(self, rid: str) -> Session:
        s = self.sessions.get(rid)
        if s is None or s.ending is not None:
            raise DeterrenceError(f"{rid} 没在驱离")
        return s

    # ------------------------------------------------------------ 人员检测(W24)

    def _persons_now(self, rid: str) -> dict[str, Any] | None:
        """狗能力里的**当前人员状态**;狗不在线、不新鲜、检测不在正常看(``state`` 不是 ``ok``)都回
        ``None``(不知道:驱离照旧按时间管)。"""
        c = self.dispatcher.clients.get(rid)
        if c is None or c.capabilities is None:
            return None
        fresh = getattr(self.dispatcher, "_fresh", None)
        if callable(fresh) and not fresh(c):
            return None
        p = c.capabilities.tasks.get("persons")
        if not isinstance(p, dict) or p.get("state") != "ok":
            return None
        return p

    async def _reconcile_now(self, rid: str) -> None:
        try:
            async with self._lock(rid):
                s = self.sessions.get(rid)
                if s is not None and s.ending is None:
                    await self._reconcile_persons(s)
                if s is not None and s.ending is None and rid in self.sessions:
                    await self._reconcile_standoff(s)
        except Exception:
            log.exception("%s 开场对账人员状态没成(下一拍再对)", rid)

    async def _reconcile_persons(self, s: Session) -> None:
        """按狗的当前人员状态对账(W24,外审改):开场、每拍、站点重启后都走这里。
        - 有人:记下「这一场看到过人」(落库);人员告警没报成就报(告警簿里还挂着就不重报);
          系统还在自动升、有人近(5 米内)→ 直接升到 L3;
        - **确认没人**(``present`` 是 ``False``)、这一场看到过人、系统还在自动管 → 收场;
        - 不知道(``None``、检测不在):什么都不动。
        人接手了(跳过级、退过级)只记、只显示,不升不收。"""
        p = self._persons_now(s.robot_id)
        if p is None:
            return
        rid, now = s.robot_id, self._now()
        present = p.get("present")
        if present is True:
            s.persons = {k: p[k] for k in ("count", "nearest_m") if k in p} | {
                "near": bool(p.get("near")), "at_ms": now}
            if not s.seen_person:
                # 「看到过人」和「要报的告警」同一个事务落库:要么都记上、要么都没记(下一拍再来)
                new = Session(**{**asdict(s), "seen_person": True})
                n = p.get("count", 1)
                near = p.get("nearest_m")
                title = f"拦截点看到 {n} 个人" + (f",最近 {near} m" if near is not None else "")
                context = {"task_id": s.task_id,
                           "persons": {k: p[k] for k in ("count", "nearest_m") if k in p}}
                try:
                    with self.db.tx() as c:
                        self._save_in(c, new)
                        c.execute("INSERT INTO pending_alerts(kind, robot, title, detail, context, "
                                  "created_ms) VALUES (?,?,?,?,?,?)",
                                  ("intrusion_person", rid, title,
                                   f"防区 {s.zone};驱离 L{s.level} {LABEL[s.level]}",
                                   json.dumps(context, ensure_ascii=False), now))
                except Exception:
                    log.exception("%s 看到人了,落库没成(下一拍再记)", rid)
                    return
                s.seen_person = True
                self._flush_alerts()
            if p.get("near") and s.auto and s.level < AUTO_MAX:
                new = Session(**{**asdict(s), "level": AUTO_MAX, "level_ms": now})
                try:
                    self._save(new)
                except Exception:
                    log.exception("%s 有人靠近要升 L%d,落库没成(下一拍再升)", rid, AUTO_MAX)
                else:
                    s.level, s.level_ms, s.clip_i, s.clip_ms = AUTO_MAX, now, 0, 0
                    log.info("%s 有人进到 5 米内:驱离直接升到 L%d", rid, AUTO_MAX)
                    await self._apply(s)
                    self._publish(s)
        elif present is False and s.seen_person:
            s.persons = {"count": 0, "gone": True, "at_ms": now}
            if s.auto:
                await self._end(rid, "人走了(看到过人,之后连续 20 秒确认没人)", go_back=True)

    async def _stop_standoff(self, s: Session) -> bool:
        """收尾时撤这一场的保持距离(W25 外审 1):按固定的任务号撤,不看内存里狗报过什么。撤成了
        (收下、重复、狗说没这个任务、已经结束了)回真;没成回假(每 15 秒最多发一次)。
        **一律要撤,不看拦截点在不在**(W25 复查):拦截点为空只说明这一版站点派不了新的,证明不了狗上
        没有旧的在跑(升级前派的那一场,库迁移后拦截点是空的)。没派过的,狗回「没这个任务」,一样算撤成。"""
        if s.standoff_stopped:
            return True
        now = self._now()
        if s.abort_ms and now - s.abort_ms < STANDOFF_RESEND_S * 1000:
            return False
        s.abort_ms = now
        try:
            r = await self.dispatcher.abort(s.robot_id, _standoff_id(s), issued_by="deterrence")
        except Exception as exc:                        # noqa: BLE001 - 狗不在线、回执没到:再撤
            log.warning("%s 撤保持距离没成(%d 秒后再撤):%s", s.robot_id, STANDOFF_RESEND_S, exc)
            return False
        ack = r.get("ack", {}) if isinstance(r, dict) else {}
        if ack.get("result") in ("accepted", "duplicate") or ack.get("reason") in (
                "no_such_task", "already_finished"):
            s.standoff_stopped = True
            return True
        log.warning("%s 撤保持距离被拒(%s):再撤", s.robot_id, ack.get("reason"))
        return False

    async def _reconcile_standoff(self, s: Session) -> None:
        """按狗能力里的 ``standoff`` 对账(W25)。狗不支持(没配人员检测、避障、规划后端)就不管。"""
        rid, now = s.robot_id, self._now()
        c = self.dispatcher.clients.get(rid)
        if c is None or c.capabilities is None or "standoff" not in c.capabilities.tasks:
            s.standoff = None
            return
        fresh = getattr(self.dispatcher, "_fresh", None)
        if callable(fresh) and not fresh(c):
            return
        if not s.center:
            s.standoff = None                           # 不知道拦截点在哪:不派(拴绳没中心)
            return
        st = c.capabilities.tasks.get("standoff") or {}
        tid = _standoff_id(s)
        mine = st.get("task_id") == tid
        s.standoff = str(st.get("state")) if mine else "idle"
        if not mine:
            if now - s.standoff_ms < STANDOFF_RESEND_S * 1000:
                return
            s.standoff_ms = now
            left = (max(s.started_ms, s.human_ms) + CAP_S * 1000 - now) // 1000
            req = StandoffRequest(max_s=int(max(30, min(STANDOFF_MAX_S, left + 60))),
                                  center=MapPose.from_wire(json.loads(s.center)))
            try:
                await self.dispatcher.standoff(rid, tid, req, issued_by="deterrence")
            except Exception as exc:                    # noqa: BLE001 - 派不出去:下次再派
                log.warning("%s 保持距离没派成(%d 秒后再派):%s", rid, STANDOFF_RESEND_S, exc)
            return
        if st.get("state") == "cornered" and not s.cornered:
            why = str(st.get("reason") or "")
            new = Session(**{**asdict(s), "cornered": True})
            try:
                with self.db.tx() as tx:
                    self._save_in(tx, new)
                    tx.execute("INSERT INTO pending_alerts(kind, robot, title, detail, context, "
                               "created_ms) VALUES (?,?,?,?,?,?)",
                               ("deter_cornered", rid, "驱离中的狗无路可退,原地站定",
                                f"防区 {s.zone};" + (why or "四周都退不了"),
                                json.dumps({"task_id": s.task_id}, ensure_ascii=False), now))
            except Exception:
                log.exception("%s 无路可退,落库没成(下一拍再记)", rid)
                return
            s.cornered = True
            log.warning("%s 无路可退,原地站定:%s", rid, why)
            self._flush_alerts()

    def _flush_alerts(self) -> int:
        """把 ``pending_alerts`` 里的告警报出去,报成才删(W24 复查)。告警簿里这台狗这一种还挂着的
        当报过了(不重报)。每拍都调,跟有没有会话无关。回报成了几条。

        **按意图去重**(W24 复查二):报的时候把意图号(``pending_alerts:<id>``)写进告警的现场。
        报成了、删待报那一下没成,下一拍(哪怕保安已经把那条告警解决了、站点重启了)在告警表里查到
        这个意图号,就只删待报、不再报一次。意图号自增、不复用。"""
        if self.alerts is None:
            return 0
        n = 0
        has_open = getattr(self.alerts, "has_open", None)
        for r in self.db.query("SELECT * FROM pending_alerts ORDER BY id"):
            intent = f"pending_alerts:{r['id']}"
            try:
                done = bool(self.db.query(
                    "SELECT 1 FROM alerts WHERE robot=? AND kind=? "
                    "AND json_extract(context, '$.intent')=? LIMIT 1",
                    (r["robot"], r["kind"], intent)))
                if not done and not (callable(has_open) and has_open(r["robot"], r["kind"])):
                    self.alerts.raise_alert(kind=r["kind"], robot=r["robot"], title=r["title"],
                                            detail=r["detail"],
                                            context=json.loads(r["context"]) | {"intent": intent})
                with self.db.tx() as c:
                    c.execute("DELETE FROM pending_alerts WHERE id=?", (r["id"],))
                n += 1
            except Exception:
                log.exception("%s 的 %s 告警还没报成(下一拍再报)", r["robot"], r["kind"])
        return n

    # ------------------------------------------------------------ 人

    async def set_level(self, robot_id: str, level: Any, *, by: str) -> dict[str, Any]:
        """跳级、往回退(保安、管理员)。人动过一次就不再自动升。先落库,成了再改。"""
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= MAX_LEVEL:
            raise DeterrenceError(f"级别是 0–{MAX_LEVEL} 的整数")
        async with self._lock(robot_id):
            s = self._live(robot_id)
            now = self._now()
            new = Session(**{**asdict(s), "level": level, "level_ms": now, "human_ms": now,
                             "auto": False, "by": by})
            try:
                self._save(new)
            except Exception as exc:
                raise DeterrenceError(f"落库没成,级别没改: {exc}") from exc
            s.level, s.level_ms, s.human_ms, s.auto, s.by = level, now, now, False, by
            s.clip_i, s.clip_ms = 0, 0                  # 换了级:话术从头(中文)、马上放
            await self._apply(s)
            self._publish(s)
            return s.view(now)

    async def release(self, robot_id: str, *, by: str) -> None:
        """解除(保安、管理员、业主):全关、狗回待命点。等在途的那条命令回来再收。"""
        async with self._lock(robot_id):
            self._live(robot_id)
            await self._end(robot_id, f"{by} 解除", go_back=True, by=by)
            s = self.sessions.get(robot_id)
            if s is not None and not s.ending_saved:
                raise DeterrenceError("声光已全关,但「解除」没落进库(站点重启可能恢复这一场):"
                                      "稍后再按一次")

    # ------------------------------------------------------------ 每拍

    async def tick(self) -> None:
        """站点每几秒调一次:待报的告警接着报、收尾中的接着收、到点收、自动升、续声光、轮放话术。
        一台炸了不挡别的台。"""
        try:
            self._flush_alerts()
        except Exception:
            log.exception("待报的告警这一拍没办成")
        for rid in list(self.sessions):
            try:
                async with self._lock(rid):
                    if rid in self.sessions:
                        await self._tick_one(rid)
            except Exception:
                log.exception("%s 的驱离这一拍没办成", rid)

    async def _tick_one(self, rid: str) -> None:
        s = self.sessions[rid]
        if s.ending is not None:
            return await self._end(rid, s.ending[0], go_back=s.ending[1])
        now = self._now()
        if now - max(s.started_ms, s.human_ms) >= CAP_S * 1000:
            return await self._end(rid, f"到 {CAP_S // 60} 分钟自动收", go_back=True)
        c = self.dispatcher.clients.get(rid)
        task = c.status.task if c is not None and c.status is not None else None
        if task is not None and task.task_id not in (s.task_id, _standoff_id(s)) \
                and task.state.value in ("running", "pending"):
            return await self._end(rid, f"狗被派去干别的了({task.task_id})", go_back=False)
        await self._reconcile_persons(s)                # W24:按狗的当前人员状态对账
        if rid not in self.sessions or s.ending is not None:
            return                                      # 对账时收场了
        await self._reconcile_standoff(s)               # W25:保持距离在不在守、是不是无路可退
        if s.auto and s.level < AUTO_MAX and now - s.level_ms >= STEP_S * 1000:
            new = Session(**{**asdict(s), "level": s.level + 1, "level_ms": now})
            try:
                self._save(new)
            except Exception:
                log.exception("%s 驱离要升到 L%d,落库没成:下一拍再升", rid, s.level + 1)
            else:
                s.level, s.level_ms = s.level + 1, now
                s.clip_i, s.clip_ms = 0, 0
                log.info("%s 驱离自动升到 L%d(%s)", rid, s.level, LABEL[s.level])
                self._publish(s)
        await self._apply(s)

    def _outputs(self, rid: str) -> list[str]:
        c = self.dispatcher.clients.get(rid)
        caps = c.capabilities.tasks.get("deter", {}) if c and c.capabilities else {}
        return list(caps.get("outputs", []))

    def _clips(self, rid: str, kind: str) -> list[str]:
        c = self.dispatcher.clients.get(rid)
        caps = c.capabilities.tasks.get("deter", {}) if c and c.capabilities else {}
        have = set(caps.get("clips", []))
        return [f"{kind}-{lang}" for lang in LANGS if f"{kind}-{lang}" in have]

    async def _send(self, rid: str, payload: dict[str, Any]) -> bool:
        try:
            r = await self.dispatcher.deter(rid, payload, issued_by=f"deterrence:{rid}",
                                            priority=EVENT)
        except Exception as exc:                        # noqa: BLE001 - 下一拍再来
            log.warning("%s 驱离的 %s 没发出去: %s", rid, payload.get("output"), exc)
            return False
        ack = r.get("ack", {}) if isinstance(r, dict) else {}
        if ack.get("result") not in (None, "accepted"):
            log.warning("%s 驱离的 %s 狗没做: %s", rid, payload.get("output"),
                        ack.get("reason") or ack.get("result"))
            return False
        return True

    async def _on(self, s: Session, payload: dict[str, Any]) -> bool:
        """发一条「开」:发之前就从「确认关过」里去掉(回执丢了也可能开了)。"""
        s.off.discard(payload["output"])
        return await self._send(s.robot_id, payload)

    async def _off(self, s: Session, out: str) -> None:
        if out in s.off:
            return
        if await self._send(s.robot_id, {"output": out, "on": False}):
            s.off.add(out)
            s.sent.pop(out, None)

    async def _apply(self, s: Session) -> None:
        """按这一级:该开的开、续;不该开的、又没确认关过的关;喇叭轮放。"""
        now = self._now()
        have = self._outputs(s.robot_id)
        want = RELAYS[s.level]
        for out in ("strobe", "spotlight", "siren"):
            if out not in have:
                continue
            if out in want:
                if now - s.sent.get(out, -10**12) >= RENEW_S * 1000 and await self._on(
                        s, {"output": out, "on": True, "max_s": KEEP_S}):
                    s.sent[out] = now
            else:
                await self._off(s, out)
        kind = CLIPS[s.level]
        clips = self._clips(s.robot_id, kind) if kind and "speaker" in have else []
        if clips and now - s.clip_ms >= CLIP_EVERY_S * 1000:
            clip = clips[s.clip_i % len(clips)]
            if await self._on(s, {"output": "speaker", "on": True, "max_s": CLIP_MAX_S,
                                  "clip": clip}):
                s.clip_i, s.clip_ms = s.clip_i + 1, now
                s.sent["speaker"] = now
        elif not clips and "speaker" in have:
            await self._off(s, "speaker")

    async def _end(self, rid: str, why: str, *, go_back: bool, by: str = "") -> None:
        """收尾:**先全关**,再删库;删成了才从内存拿掉、派回程。删库没成:标收尾中,下一拍接着收。
        调用方拿着这台狗的锁。"""
        s = self.sessions[rid]
        if s.ending is None:
            s.ending = (why, go_back)
            if by:
                s.by = by
            log.info("%s 驱离收尾: %s", rid, why)
        if not s.ending_saved:
            # 先把「要收尾」落库:之后哪一步没成、站点重启,都只会接着收尾,不会恢复开
            try:
                with self.db.tx() as c:
                    c.execute("UPDATE deter_sessions SET ending=?, ending_back=?, by=? "
                              "WHERE robot_id=?",
                              (s.ending[0] or "收尾", int(s.ending[1]), s.by, rid))
                s.ending_saved = True
            except Exception:
                log.exception("%s 驱离「收尾中」落不了库:先全关,下一拍再写", rid)
        for out in self._outputs(rid):
            s.off.discard(out)                          # 收尾:每一路都发一遍关(不信记录)
            await self._off(s, out)                     # 没发出去也没事:45 秒自己关
        if not await self._stop_standoff(s):
            return                                      # 保持距离没撤成:停在收尾中,下一拍接着撤
        try:
            with self.db.tx() as c:
                c.execute("DELETE FROM deter_sessions WHERE robot_id=?", (rid,))
        except Exception:
            log.exception("%s 驱离收尾删库没成:下一拍接着收", rid)
            return
        self.sessions.pop(rid, None)
        self._publish(None, rid, why)
        if go_back and self.standby is not None:
            await self._back(rid, s.task_id, s.by or "auto")
