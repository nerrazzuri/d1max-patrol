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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from d1max_contract.messages import Event
from d1max_site.db import SiteDB
from d1max_site.priorities import EVENT, INCIDENT_PREFIX

log = logging.getLogger(__name__)

STEP_S = 30
AUTO_MAX = 3
MAX_LEVEL = 4
CAP_S = 600
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


class DeterrenceError(ValueError):
    """没有这一场、级别不对、落库没成(API 回 400/404/503)。"""


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
    #: 收尾中(落库没成,下一拍接着收):``(原因, 回不回待命点)``。收尾中不再开任何东西。
    ending: tuple[str, bool] | None = None
    #: 「收尾中」落进库了没有(没落进去的话站点重启会把这一场当成还在驱离)。
    ending_saved: bool = False

    def row(self) -> tuple:
        return (self.robot_id, self.incident_id, self.zone, self.task_id, self.level,
                self.started_ms, self.level_ms, self.human_ms, int(self.auto), self.by)

    def view(self, now_ms: int) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items()
             if k not in ("sent", "off", "clip_i", "clip_ms", "ending", "ending_saved")}
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
        self.sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for r in db.query("SELECT * FROM deter_sessions"):
            s = Session(robot_id=r["robot_id"], incident_id=r["incident_id"], zone=r["zone"],
                        task_id=r["task_id"], level=r["level"], started_ms=r["started_ms"],
                        level_ms=r["level_ms"], human_ms=r["human_ms"], auto=bool(r["auto"]),
                        by=r["by"])
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

    def _on_event(self, robot_id: str, e: Event) -> None:
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
                    task_id=task_id, level=0, started_ms=now, level_ms=now)
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

    async def _back(self, rid: str, after: str, by: str = "auto") -> None:
        try:
            await self.standby.return_to(rid, issued_by=f"deterrence:{by}")
        except Exception as exc:                        # noqa: BLE001 - 回不去:推给值守的人
            log.warning("%s 回待命点没派成: %s", rid, exc)
            self.dispatcher.feed.publish({"kind": "standby_failed", "robot_id": rid,
                                          "after": after, "reason": str(exc)})

    def _save(self, s: Session) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO deter_sessions(robot_id, incident_id, zone, "
                      "task_id, level, started_ms, level_ms, human_ms, auto, by) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", s.row())

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
        """站点每几秒调一次:收尾中的接着收、到点收、自动升、续声光、轮放话术。一台炸了不挡别的台。"""
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
        if task is not None and task.task_id != s.task_id and task.state.value in (
                "running", "pending"):
            return await self._end(rid, f"狗被派去干别的了({task.task_id})", go_back=False)
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
