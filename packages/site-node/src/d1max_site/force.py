"""狗翻倒了、被抱起来了(W26,决策 52),站点这一头。

狗上判(``d1max_agent.force``),能力里报当前状态 ``force.state``(``ok``/``flipped``/``lifted``)。
站点按**当前状态对账**(同 W30b 复查的做法:不靠一次性事件,报失败了下一拍再来):

- 进了 ``flipped``、``lifted`` 的新一回 → 记 ``force_episodes``,跟待报告警同一个事务落库,报 **P1**:
  「狗翻倒了」(狗已经软急停,要人去扶、人工解除)、「狗被抱起来了」(可能有人偷狗;狗已经停下)。
  带狗最后的位置(告警现场);录像按告警时刻前后看。
- **被抱起来、又在布防**:狗上的警笛、警灯响 ``SIREN_S`` 秒(有上装的话)。发不出去下一拍再发,到点就
  不再发(狗上 ``max_s`` 到了自己关)。
- 回到 ``ok``(扶正了、放回地上)→ 删记录,这一回完了;告警留着等人看、人解决。
- 说不清(狗掉线、状态不新鲜、老代理不报)不报、不删。

被撞(P2)是一下一下的事,走事件(``alert_sources`` 的 ``force_bump``)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from d1max_site.db import SiteDB
from d1max_site.pending_alerts import flush, queue

log = logging.getLogger(__name__)

#: 被抱起来时警笛、警灯响多久(决策 52)。
SIREN_S = 45
#: 响着的时候隔多久再发一次「开」(PR #87 复查 R4:狗重启、上装重新初始化会全关,站点不知道;
#: 定时按剩余时长再开一次,最多晚这么久补回来;过了 45 秒就不发)。
RENEW_S = 5

_TITLES = {"flipped": "狗翻倒了", "lifted": "狗被抱起来了"}
_DETAILS = {
    "flipped": "狗已经停下、软急停(不让它自己乱蹬);派人去扶起来,检查没坏再在手机上解除急停",
    "lifted": "四条腿上不承重:有人把狗抱起来了(可能是偷狗)。狗已经停下;看现场、录像",
}


class ForceWatch:
    def __init__(self, db: SiteDB, dispatcher: Any, *, now_ms: Callable[[], int],
                 arming: Any = None) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self.arming = arming
        #: 告警台。站点主程序接上;没接就只落库(下一拍有了再报)。
        self.alerts: Any = None

    def _armed(self) -> bool:
        if self.arming is None:
            return False
        try:
            return self.arming.view()["mode"] == "armed"
        except Exception:
            log.exception("布防模式读不出来:当布防(宁可多响)")
            return True

    def _state(self, rid: str) -> str | None:
        """狗能力里的当前受力状态;不在线、不新鲜、老代理都是 ``None``(说不清)。"""
        c = self.dispatcher.clients.get(rid)
        if c is None or c.capabilities is None or c.status is None or not c.status.online:
            return None
        fresh = getattr(self.dispatcher, "_fresh", None)
        if callable(fresh) and not fresh(c):
            return None
        f = c.capabilities.tasks.get("force")
        st = f.get("state") if isinstance(f, dict) else None
        return st if st in ("ok", "flipped", "lifted") else None

    async def tick(self) -> None:
        self.reconcile()
        await self.sound()

    def reconcile(self) -> None:
        """对账、报 P1(不等任何回执:系统审查 S06,站点主程序每拍直接调,不许被别的命令等住)。"""
        rows = {r["robot_id"]: dict(r) for r in self.db.query("SELECT * FROM force_episodes")}
        now = self._now()
        for rid in list(self.dispatcher.clients):
            st = self._state(rid)
            if st is None:
                continue                                  # 说不清:不报、不删
            row = rows.get(rid)
            if st == "ok":
                if row is not None:
                    with self.db.tx() as tx:              # 扶正了、放回地上:这一回完了
                        tx.execute("DELETE FROM force_episodes WHERE robot_id=?", (rid,))
                    rows.pop(rid)
            elif row is None or row["state"] != st:
                rows[rid] = self._raise(rid, st, now)
        flush(self.db, self.alerts)

    async def sound(self) -> None:
        """要响的警笛、警灯发出去(要等回执:站点主程序放在自己那条道上跑)。没响成的(``siren=1``)
        每拍发;响着的(``siren=2``)每 :data:`RENEW_S` 秒按剩余时长再发一次(R4)。"""
        now = self._now()
        for row in [dict(r) for r in self.db.query(
                "SELECT * FROM force_episodes WHERE state='lifted' AND siren IN (1, 2)")]:
            if now - row["started_ms"] >= SIREN_S * 1000:
                with self.db.tx() as tx:                  # 过了点:不响了,也不再归它
                    tx.execute("UPDATE force_episodes SET siren=0 WHERE robot_id=? "
                               "AND started_ms=?", (row["robot_id"], row["started_ms"]))
                continue
            if row["siren"] == 2 and now - row["siren_ms"] < RENEW_S * 1000:
                continue
            await self._siren(row["robot_id"], row, now)

    def _raise(self, rid: str, st: str, now: int) -> dict[str, Any]:
        c = self.dispatcher.clients.get(rid)
        pose = c.telemetry.pose if c is not None and c.telemetry is not None else None
        context: dict[str, Any] = {"force": st}
        if pose is not None:
            context["pose"] = {"map_id": pose.map_id, "map_version": pose.map_version,
                               "x": round(pose.x, 2), "y": round(pose.y, 2), "at_ms": now}
        # 被抱起来、又在布防:要响警笛(1 = 要响还没响成,0 = 不响或者响过了)
        siren = 1 if st == "lifted" and self._armed() else 0
        with self.db.tx() as tx:
            tx.execute("INSERT INTO force_episodes(robot_id, state, started_ms, siren) "
                       "VALUES (?,?,?,?) ON CONFLICT(robot_id) DO UPDATE SET state=excluded.state, "
                       "started_ms=excluded.started_ms, siren=excluded.siren",
                       (rid, st, now, siren))
            queue(tx, kind=f"force_{st}", robot=rid, title=_TITLES[st],
                  detail=_DETAILS[st] + (";布防中,狗上警笛、警灯响 45 秒" if siren else ""),
                  context=context, now_ms=now)
        log.warning("%s %s", rid, _TITLES[st])
        return {"robot_id": rid, "state": st, "started_ms": now, "siren": siren}

    def held(self, rid: str) -> set[str]:
        """这台狗此刻归受力警报的输出(布防中被抱起来、这一回开始后 ``SIREN_S`` 秒内)。驱离收场、
        换级关灯时问它(系统审查 S04)。"""
        rows = self.db.query("SELECT state, started_ms, siren FROM force_episodes "
                             "WHERE robot_id=?", (rid,))
        if not rows or rows[0]["state"] != "lifted" or rows[0]["siren"] not in (1, 2) \
                or self._now() - rows[0]["started_ms"] >= SIREN_S * 1000:
            return set()
        return {"siren", "strobe"}

    def _outputs(self, rid: str) -> list[str]:
        c = self.dispatcher.clients.get(rid)
        caps = c.capabilities.tasks.get("deter", {}) if c and c.capabilities else {}
        return [o for o in ("siren", "strobe") if o in caps.get("outputs", [])]

    async def _siren(self, rid: str, row: dict[str, Any], now: int) -> None:
        """警笛、警灯开到这一回开始后 ``SIREN_S`` 秒;都开成了(或者没有上装、过了点)就不再发。"""
        from d1max_site.priorities import EVENT
        left = SIREN_S - (now - row["started_ms"]) / 1000
        outs = self._outputs(rid)
        ok = left <= 1 or not outs
        if not ok:
            ok = True
            for out in outs:
                # PR #88 复查:前一路等回执可能等了好几秒 —— 每一路发之前按**此刻**重算剩余时长、
                # 重核这一回还在(没放下、没换一回),过了点就不发
                left = SIREN_S - (self._now() - row["started_ms"]) / 1000
                cur = self.db.query("SELECT state, started_ms FROM force_episodes "
                                    "WHERE robot_id=?", (rid,))
                if left <= 1 or not cur or cur[0]["state"] != "lifted" \
                        or cur[0]["started_ms"] != row["started_ms"]:
                    break
                try:
                    r = await self.dispatcher.deter(rid, {"output": out, "on": True,
                                                          "max_s": round(left)},
                                                    issued_by=f"force:{rid}", priority=EVENT)
                    ack = r.get("ack", {}) if isinstance(r, dict) else {}
                    ok = ok and ack.get("result") in (None, "accepted")
                except Exception as exc:                  # noqa: BLE001 - 下一拍再来
                    log.warning("%s 被抱起来了,%s 没发出去:%s", rid, out, exc)
                    ok = False
        if ok:
            # 2 = 开成了(这一回的 45 秒内这几路归受力警报,驱离收尾不许关:系统审查 S04)
            with self.db.tx() as tx:
                tx.execute("UPDATE force_episodes SET siren=2, siren_ms=? WHERE robot_id=? "
                           "AND started_ms=?", (now, rid, row["started_ms"]))
