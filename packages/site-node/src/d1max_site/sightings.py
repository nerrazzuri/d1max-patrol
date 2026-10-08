"""狗没在驱离时自己看见人(W33,决策 48)。

W24 的人员检测原来只在驱离里用:狗待命、巡检时看见人,站点什么都不做(C40221 现场:人站在狗前面没反应)。
决策 48:

- **布防**(模式 ``armed``)时,狗**确认**看见人(代理判的 ``present``:同一相机 3 帧里 2 帧)、又不在驱离
  里(驱离中的归 W24 管)→ 报 **P1** ``dog_sees_person``「狗看见人了」,带人数、最近距离、狗的位置;
  现场的照片在 ``persons`` 那几趟里(代理没有事件任务时截图归它),手机现场页「现场照片」就能看。
- 保安看过现场决定要不要驱离:手机上一键「就地驱离」(:meth:`DeterrenceDesk.start_here`)。
- **在家、访客、撤防不报**(在家有的防区撤了,站点不知道狗在哪个防区,一律不报)。
- **一回看见只报一次**:记在 ``person_sightings``,跟待报告警同一个事务落库;狗确认人走了(``present``
  是假:连续 20 秒没看到)才删,再看见再报。说不清(检测断了、狗掉线)就留着,不重报也不删。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from d1max_site.db import SiteDB
from d1max_site.pending_alerts import flush, queue

log = logging.getLogger(__name__)


class PersonWatch:
    def __init__(self, db: SiteDB, dispatcher: Any, *, now_ms: Callable[[], int],
                 arming: Any = None, deterrence: Any = None) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self.arming = arming
        self.deterrence = deterrence
        #: 告警台。站点主程序接上;没接就只落库(下一拍有了再报)。
        self.alerts: Any = None

    def _armed(self) -> bool:
        if self.arming is None:
            return True
        try:
            return self.arming.view()["mode"] == "armed"
        except Exception:
            log.exception("布防模式读不出来:当布防(宁可多报)")
            return True

    def _persons(self, rid: str) -> dict[str, Any] | None:
        """狗能力里的当前人员状态;不在线、不新鲜、检测不在正常看都是 ``None``(说不清)。"""
        c = self.dispatcher.clients.get(rid)
        if c is None or c.capabilities is None or c.status is None or not c.status.online:
            return None
        fresh = getattr(self.dispatcher, "_fresh", None)
        if callable(fresh) and not fresh(c):
            return None
        p = c.capabilities.tasks.get("persons")
        return p if isinstance(p, dict) and p.get("state") == "ok" else None

    def tick(self) -> None:
        seen = {r["robot_id"] for r in self.db.query("SELECT robot_id FROM person_sightings")}
        armed = self._armed()
        busy = set(getattr(self.deterrence, "sessions", {}) or {})
        now = self._now()
        for rid in list(self.dispatcher.clients):
            p = self._persons(rid)
            if p is None:
                continue                                  # 说不清:不报、不删
            present = p.get("present")
            if present is False and rid in seen:
                with self.db.tx() as tx:                  # 人走了:这一回完了,再看见再报
                    tx.execute("DELETE FROM person_sightings WHERE robot_id=?", (rid,))
            elif present is True and rid not in seen and armed and rid not in busy:
                self._raise(rid, p, now)
        flush(self.db, self.alerts)

    def _raise(self, rid: str, p: dict[str, Any], now: int) -> None:
        n = p.get("count", 1)
        near = p.get("nearest_m")
        title = f"狗看见人了:{n} 个人" + (f",最近 {near} m" if near is not None else "")
        c = self.dispatcher.clients.get(rid)
        pose = c.telemetry.pose if c is not None and c.telemetry is not None else None
        context: dict[str, Any] = {"task_id": "persons",
                                   "persons": {k: p[k] for k in ("count", "nearest_m") if k in p}}
        if pose is not None:
            context["pose"] = {"map_id": pose.map_id, "map_version": pose.map_version,
                               "x": round(pose.x, 2), "y": round(pose.y, 2), "at_ms": now}
        with self.db.tx() as tx:
            tx.execute("INSERT INTO person_sightings(robot_id, started_ms) VALUES (?,?)",
                       (rid, now))
            queue(tx, kind="dog_sees_person", robot=rid, title=title,
                  detail="布防中,狗不在驱离;看现场照片,要驱离就点「就地驱离」",
                  context=context, now_ms=now)
        log.info("%s 看见人了(布防中):%s", rid, title)
