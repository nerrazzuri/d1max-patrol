"""全天候(W29,决策 41)。站点知道现在的天气,按它管狗:

- ``normal`` 正常:排程巡检、入侵派狗照常,不限速。
- ``rain`` 下雨:照常巡、照常派;全狗限速 0.3 m/s(湿地防打滑;近障从更远开始减速)。
- ``storm`` 雷暴(大雨也算):排程巡检**暂停**,在跑的撤掉、狗回待命点;入侵**照派**(安防优先),
  告警里写明雷暴中出动;限速 0.3 m/s。

**天气从哪来**:
- **联网查**(Open-Meteo,免费、不用钥匙):站点配了庄园坐标(``D1MAX_SITE_LATLON``)才查,只发**到小数点后
  两位**(约 1 km)的坐标,每 10 分钟一次。雷暴天气码(95/96/99)、或者大雨(天气码 65/67/82,或者每小时
  ≥ 7.6 mm)→ 雷暴;别的下雨码、或者有降水 → 下雨;否则正常。查不到的话最后一次结果 45 分钟内照用,
  再往后算「不知道」(按正常管,手机上写明查不到)。
- **手动切**:保安、业主、管理员在手机上切「正常 / 下雨 / 雷暴」,带时长(默认 3 小时,最长 24 小时),
  **以手动为准**,到点回到联网查的;也能直接切回「自动」。

**怎么管**都是按当前天气**每拍对账**(W24 的教训:不靠「变了的那一下」):
- 雷暴时每拍看一遍:还在跑的排程巡检(``sched-`` 开头)撤掉(每趟 15 秒最多撤一次);排程执行器到点不起跑
  (记 ``weather``)。
- **撤了要派回待命点**(W29 外审 1:待命点管理器只在 ``task_done`` 后自动回,
  撤掉的是 ``task_aborted``):撤之前先把「这台狗撤完要回待命点」落库(``weather_returns``)。
  每拍对账:那一趟还在跑 → 等;狗空了(没任务,或者任务是那一趟、已经结束)→ 派回待命点,
  派成了才删;**狗在跑别的了(人派的、入侵派的)→ 作废**,不抢。派不成每 15 秒再派。
  站点重启后照样接着办。
- 每拍对每台在线、新鲜、报了 ``speed_cap`` 的狗:狗上的限速跟该有的不一样就发;一样的话,
  **按站点自己记的上次发成的时刻**,过了有效期的一半就续(W29 外审 2:狗只在限速变了时重发能力,
  能力里的 ``left_s`` 是那时的快照,不是倒计时)。站点重启后不知道上次什么时候发的:先补发一次。
  每台 15 秒最多发一次。限速命令带有效期(10 分钟),站点挂了狗上到点自己取消。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable
from typing import Any

from d1max_contract.speedcap import RAIN_MPS, SpeedCap
from d1max_site.db import SiteDB

log = logging.getLogger(__name__)

CONDITIONS = ("normal", "rain", "storm")
LABEL = {"normal": "正常", "rain": "下雨", "storm": "雷暴", "unknown": "不知道"}
POLL_S = 600
AUTO_STALE_S = 45 * 60
DEFAULT_MANUAL_H = 3
MAX_MANUAL_H = 24
CAP_TTL_S = 600
RESEND_S = 15
STORM_CODES = frozenset({95, 96, 99})
HEAVY_CODES = frozenset({65, 67, 82})
RAIN_CODES = frozenset(range(51, 68)) | frozenset({80, 81, 82})
HEAVY_MM_H = 7.6
SCHEDULE_PREFIX = "sched-"
URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat:.2f}&longitude={lon:.2f}"
       "&current=precipitation,weather_code&timezone=UTC")


class WeatherError(ValueError):
    """不合规矩的手动切换(API 回 400)。"""


def classify(code: Any, precip: Any) -> str:
    """天气码(WMO)+ 这一小时的降水(mm)→ normal / rain / storm。"""
    c = int(code) if isinstance(code, (int, float)) and not isinstance(code, bool) else -1
    p = float(precip) if isinstance(precip, (int, float)) and not isinstance(precip, bool) else 0.0
    if c in STORM_CODES or c in HEAVY_CODES or p >= HEAVY_MM_H:
        return "storm"
    if c in RAIN_CODES or p > 0.0:
        return "rain"
    return "normal"


def parse_latlon(s: str | None) -> tuple[float, float] | None:
    """``"3.14,101.69"`` → 坐标(只留两位小数);不对就是 None(不联网查)。"""
    if not s:
        return None
    try:
        lat, lon = (float(x) for x in s.split(","))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return (round(lat, 2), round(lon, 2))


def fetch_open_meteo(lat: float, lon: float, timeout_s: float = 10.0) -> dict[str, Any]:
    """联网查一次(在线程里调)。回 ``current`` 那一段。"""
    with urllib.request.urlopen(URL.format(lat=lat, lon=lon), timeout=timeout_s) as r:
        d = json.loads(r.read(65536).decode("utf-8"))
    cur = d.get("current") if isinstance(d, dict) else None
    if not isinstance(cur, dict):
        raise ValueError("回的东西里没有 current")
    return cur


class WeatherDesk:
    def __init__(self, db: SiteDB, dispatcher: Any, *, now_ms: Callable[[], int],
                 latlon: tuple[float, float] | None = None,
                 fetch: Callable[[float, float], dict[str, Any]] = fetch_open_meteo,
                 publish: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self.latlon = latlon
        self._fetch = fetch
        self._publish = publish
        self._polled_ms: int | None = None
        self._sent: dict[str, int] = {}                 # 狗 → 上次发限速的时刻(含没发成的,限次用)
        self._capped: dict[str, int] = {}               # 狗 → 上次限速**发成**的时刻(续期用)
        self._back_ms: dict[str, int] = {}              # 狗 → 上次派回程的时刻
        #: 待命点(``standby.StandbyManager``):雷暴撤了巡检派回去。站点主程序接上。
        self.standby: Any = None
        self._aborted: dict[str, int] = {}              # 任务 → 上次撤的时刻
        self._told: str = ""
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO weather(id) VALUES (1)")

    # ------------------------------------------------------------ 读

    def _row(self) -> dict[str, Any]:
        return dict(self.db.query("SELECT * FROM weather WHERE id=1")[0])

    def current(self) -> str:
        """现在按什么管:手动(没到点)> 联网查的(45 分钟内)> ``unknown``。"""
        r, now = self._row(), self._now()
        if r["manual"] and (r["manual_until_ms"] or 0) > now:
            return r["manual"]
        if r["auto"] and r["auto_ms"] is not None and now - r["auto_ms"] <= AUTO_STALE_S * 1000:
            return r["auto"]
        return "unknown"

    def storm(self) -> bool:
        return self.current() == "storm"

    def speed_cap(self) -> float | None:
        return RAIN_MPS if self.current() in ("rain", "storm") else None

    def view(self) -> dict[str, Any]:
        r, now = self._row(), self._now()
        cond = self.current()
        manual = bool(r["manual"]) and (r["manual_until_ms"] or 0) > now
        return {"condition": cond, "label": LABEL[cond],
                "source": "manual" if manual else ("auto" if cond != "unknown" else "none"),
                "manual": ({"condition": r["manual"], "until_ms": r["manual_until_ms"],
                            "by": r["manual_by"], "at_ms": r["manual_ms"]} if manual else None),
                "auto": {"enabled": self.latlon is not None, "condition": r["auto"] or None,
                         "at_ms": r["auto_ms"], "detail": r["auto_detail"],
                         "error": r["auto_error"]},
                "speed_cap_mps": self.speed_cap(), "patrols_paused": cond == "storm"}

    # ------------------------------------------------------------ 改

    def set_manual(self, condition: Any, *, by: str, hours: Any = None) -> dict[str, Any]:
        """手动切。``auto`` = 取消手动,回到联网查的。"""
        if condition == "auto":
            with self.db.tx() as c:
                c.execute("UPDATE weather SET manual='', manual_until_ms=NULL, manual_by=?, "
                          "manual_ms=? WHERE id=1", (by, self._now()))
            return self._changed()
        if condition not in CONDITIONS:
            raise WeatherError(f"天气只有 {'、'.join(CONDITIONS)} 或 auto,给的是 {condition!r}")
        if hours is None:
            hours = DEFAULT_MANUAL_H
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_MANUAL_H:
            raise WeatherError(f"hours 是 1–{MAX_MANUAL_H} 的整数小时")
        now = self._now()
        with self.db.tx() as c:
            c.execute("UPDATE weather SET manual=?, manual_until_ms=?, manual_by=?, manual_ms=? "
                      "WHERE id=1", (condition, now + hours * 3_600_000, by, now))
        return self._changed()

    async def poll(self) -> bool:
        """到点就联网查一次(配了坐标才查)。查成回真。"""
        import asyncio
        if self.latlon is None:
            return False
        now = self._now()
        if self._polled_ms is not None and now - self._polled_ms < POLL_S * 1000:
            return False
        self._polled_ms = now
        try:
            cur = await asyncio.to_thread(self._fetch, *self.latlon)
            code, precip = cur.get("weather_code"), cur.get("precipitation")
            cond = classify(code, precip)
        except Exception as exc:  # noqa: BLE001 - 查不到:记下原因,按最后一次(45 分钟内)
            log.warning("天气查不到:%s", exc)
            with self.db.tx() as c:
                c.execute("UPDATE weather SET auto_error=? WHERE id=1", (str(exc)[:200],))
            self._changed()
            return False
        with self.db.tx() as c:
            c.execute("UPDATE weather SET auto=?, auto_ms=?, auto_detail=?, auto_error='' "
                      "WHERE id=1", (cond, now, f"天气码 {code},降水 {precip} mm", ))
        self._changed()
        return True

    # ------------------------------------------------------------ 每拍

    async def tick(self) -> None:
        """站点主循环每拍:该查就查、雷暴撤排程巡检、对账每台狗的限速。"""
        try:
            await self.poll()
        except Exception:
            log.exception("天气这一拍没查成")
        cond = self.current()
        if cond != self._told:                          # 到点(手动过期、联网的旧了)也推一次
            self._told = cond
            self._changed()
        if cond == "storm":
            await self._stop_patrols()
        await self._returns()
        await self._caps()

    async def _stop_patrols(self) -> None:
        now = self._now()
        for rid, c in list(self.dispatcher.clients.items()):
            t = c.status.task if c.status is not None else None
            if t is None or not t.task_id.startswith(SCHEDULE_PREFIX) \
                    or t.state.value not in ("running", "pending"):
                continue
            if now - self._aborted.get(t.task_id, -10**12) < RESEND_S * 1000:
                continue
            self._aborted[t.task_id] = now
            try:
                with self.db.tx() as tx:                # 先记「撤完要回待命点」,再撤(外审 1)
                    tx.execute("INSERT OR REPLACE INTO weather_returns(robot_id, task_id, "
                               "created_ms) VALUES (?,?,?)", (rid, t.task_id, now))
                await self.dispatcher.abort(rid, t.task_id, issued_by="weather")
                log.info("雷暴:撤掉 %s 的排程巡检 %s", rid, t.task_id)
            except Exception as exc:  # noqa: BLE001 - 下一拍再撤
                log.warning("雷暴撤 %s 的巡检没成:%s", rid, exc)

    async def _returns(self) -> None:
        """雷暴撤了巡检的狗:那一趟结束、狗空着了就派回待命点(派成才删);狗在跑别的了就作废。"""
        rows = self.db.query("SELECT robot_id, task_id FROM weather_returns")
        if not rows:
            return
        now = self._now()
        for r in rows:
            rid, tid = r["robot_id"], r["task_id"]
            c = self.dispatcher.clients.get(rid)
            t = c.status.task if c is not None and c.status is not None else None
            if t is not None and t.state.value in ("running", "pending"):
                if t.task_id == tid:
                    continue                            # 还在撤:等它停
                self._drop_return(rid, f"狗在跑别的了({t.task_id}),不抢")
                continue
            if self.standby is None:
                continue
            if now - self._back_ms.get(rid, -10**12) < RESEND_S * 1000:
                continue
            self._back_ms[rid] = now
            try:
                got = await self.standby.return_to(rid, issued_by="weather:storm")
            except Exception as exc:  # noqa: BLE001 - 不在线、没就绪:过一会儿再派
                log.warning("雷暴撤巡检后 %s 回待命点没派成(%d 秒后再派):%s", rid, RESEND_S, exc)
                continue
            ack = got.get("ack", {}) if isinstance(got, dict) else {}
            if ack.get("result") not in ("accepted", "duplicate"):
                log.warning("雷暴撤巡检后 %s 回待命点被拒(%s):%d 秒后再派", rid,
                            ack.get("reason"), RESEND_S)
                continue
            self._drop_return(rid, "派回待命点了")

    def _drop_return(self, rid: str, why: str) -> None:
        with self.db.tx() as tx:
            tx.execute("DELETE FROM weather_returns WHERE robot_id=?", (rid,))
        log.info("雷暴撤巡检后 %s 的回程:%s", rid, why)

    async def _caps(self) -> None:
        want = self.speed_cap()
        now = self._now()
        fresh = getattr(self.dispatcher, "_fresh", None)
        for rid, c in list(self.dispatcher.clients.items()):
            caps = c.capabilities.tasks.get("speed_cap") if c.capabilities is not None else None
            if not isinstance(caps, dict) or (callable(fresh) and not fresh(c)):
                continue
            have = caps.get("max_speed_mps")
            sent = self._capped.get(rid)
            if have == want and (want is None or (
                    sent is not None and now - sent < CAP_TTL_S * 1000 // 2)):
                continue
            if now - self._sent.get(rid, -10**12) < RESEND_S * 1000:
                continue
            self._sent[rid] = now
            try:
                r = await self.dispatcher.speed_cap(rid, SpeedCap(max_speed_mps=want,
                                                                  ttl_s=CAP_TTL_S),
                                                    issued_by="weather")
            except Exception as exc:  # noqa: BLE001 - 下一次再发
                log.warning("给 %s 发限速没成:%s", rid, exc)
                continue
            ack = r.get("ack", {}) if isinstance(r, dict) else {}
            if ack.get("result") == "accepted":
                self._capped[rid] = now                 # 发成了:从这一刻起算续期
            else:
                log.warning("给 %s 发限速被拒:%s", rid, ack.get("reason"))

    def _changed(self) -> dict[str, Any]:
        v = self.view()
        if self._publish is not None:
            try:
                self._publish({"kind": "weather", "weather": v})
            except Exception:
                log.exception("天气变了,推给手机没推出去")
        return v
