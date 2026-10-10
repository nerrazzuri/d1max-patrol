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
        #: 防区多边形(B1c,:class:`d1max_site.areas.AreaBook`):按狗的位置算防区。站点主程序接上。
        self.areas: Any = None
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
        # 这一回记成了什么(B 阶段外审 I1):'p1' 报过 P1;'authorized' 授权在场只记了 P3
        seen = {r["robot_id"]: r["kind"]
                for r in self.db.query("SELECT robot_id, kind FROM person_sightings")}
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
            elif present is True and armed and rid not in busy:
                kind = seen.get(rid)
                if kind == "p1":
                    continue                                  # 这一回报过 P1 了
                who = self._authorized(rid)          # B1c:名单(每拍按当前授权、当前位置重核)
                if kind is None and who is not None:
                    self._authorized_seen(rid, p, who, now)
                elif who is None:
                    # 新的一回没授权 → 报 P1;授权在场的这一回,授权失效 / 狗走出授权区 / 位置说不清了
                    # 而人还在 → 补报一次 P1(B 阶段外审 I1)
                    self._raise(rid, p, now, lapsed=kind == "authorized")
        flush(self.db, self.alerts)

    def _zone(self, rid: str) -> str | None:
        """狗现在在哪个防区(B1c:按地图上画的防区多边形算);说不清是 ``None``。

        B 阶段外审 I3:只认**可靠的现在位置** —— 狗说定位正常(``ready.loc_ok``),而且站点最近
        (``stale_ms`` 以内,按站点自己的钟收到的时刻)收到过它的位置。定位丢了、位置停更了,旧坐标
        不代表它还在那块地上:按说不清算(只认全站授权)。"""
        if self.areas is None:
            return None
        c = self.dispatcher.clients.get(rid)
        if c is None or c.telemetry is None or c.status is None:
            return None
        ready = getattr(c.status, "ready", None)
        if not getattr(ready, "loc_ok", False):
            return None
        at = getattr(self.dispatcher, "telemetry_at", {}).get(rid)
        stale = getattr(self.dispatcher, "stale_ms", None)
        if at is None or stale is None or self._now() - at > stale:
            return None
        pose = c.telemetry.pose
        if pose is None:
            return None
        try:
            return self.areas.zone_at(pose.map_id, pose.map_version, pose.x, pose.y)
        except Exception:
            log.exception("%s 在哪个防区算不出来", rid)
            return None

    def _authorized(self, rid: str) -> str | None:
        if self.arming is None or not hasattr(self.arming, "authorized"):
            return None
        return self.arming.authorized(self._zone(rid))

    def _authorized_seen(self, rid: str, p: dict[str, Any], who: str, now: int) -> None:
        """授权在场:不报 P1,记一条 P3(历史里查得到),这一回也算报过(人走了再看见再判)。"""
        zone = self._zone(rid)
        with self.db.tx() as tx:
            tx.execute("INSERT INTO person_sightings(robot_id, started_ms, kind) "
                       "VALUES (?,?,'authorized')", (rid, now))
            queue(tx, kind="authorized_person", robot=rid, title=f"授权在场:{who}",
                  detail=f"狗看见 {p.get('count', 1)} 个人;{who} 的授权"
                         + (f"(防区 {zone})" if zone else "(全站)") + "生效中,不报 P1",
                  context={"task_id": "persons", "authorized": who,
                           **({"zone": zone} if zone else {})}, now_ms=now)
        log.info("%s 看见人了,授权在场:%s", rid, who)

    def _raise(self, rid: str, p: dict[str, Any], now: int, *, lapsed: bool = False) -> None:
        """报 P1。``lapsed``:这一回本来是授权在场,授权失效(或狗走出授权区、位置说不清)了人还在,
        补报;跟这一回的记录在同一个事务里改成 'p1'(报不出去的 pending 下一拍 flush 再报)。"""
        n = p.get("count", 1)
        near = p.get("nearest_m")
        title = f"狗看见人了:{n} 个人" + (f",最近 {near} m" if near is not None else "")
        if lapsed:
            title += "(授权已不适用)"
        c = self.dispatcher.clients.get(rid)
        pose = c.telemetry.pose if c is not None and c.telemetry is not None else None
        context: dict[str, Any] = {"task_id": "persons",
                                   "persons": {k: p[k] for k in ("count", "nearest_m") if k in p}}
        if pose is not None:
            context["pose"] = {"map_id": pose.map_id, "map_version": pose.map_version,
                               "x": round(pose.x, 2), "y": round(pose.y, 2), "at_ms": now}
        with self.db.tx() as tx:
            tx.execute("INSERT INTO person_sightings(robot_id, started_ms, kind) VALUES (?,?,'p1') "
                       "ON CONFLICT(robot_id) DO UPDATE SET kind='p1'", (rid, now))
            queue(tx, kind="dog_sees_person", robot=rid, title=title,
                  detail="布防中,狗不在驱离;看现场照片,要驱离就点「就地驱离」",
                  context=context, now_ms=now)
        log.info("%s 看见人了(布防中):%s", rid, title)
