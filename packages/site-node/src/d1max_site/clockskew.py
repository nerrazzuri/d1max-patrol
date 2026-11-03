"""狗的钟跟站点差多少(W09d)。

每条遥测(1 Hz)带狗的墙钟 ``stamp``;站点收到的时刻减去在途,就是同一时刻站点的钟。在途不知道、
只会是正的,所以「狗的时刻 − 站点收到的时刻」只会比真的钟差**小**:最近 :data:`WINDOW` 条里取最大的,
最接近真的(broker 憋了几秒的那一条不会把估计拉偏)。原来单条样本、60 s 的线只抓得住差半年那种;
对上 NTP 之后局域网里是毫秒级。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

#: 估计用最近几条遥测(1 Hz:半分钟)。
WINDOW = 30
#: 钟差超过它报「狗的钟不准」(NTP 没配上、不通、慢慢漂);回到 :data:`CLEAR_S` 以内才重新武装。
ALARM_S = 2.0
CLEAR_S = 1.0
#: 钟差超过它不派任务命令:命令有效期 60 s、狗按自己的钟判,差一半以上旧命令不过期、新命令一到就过期。
DISPATCH_MAX_S = 30.0


#: 样本只留这么久(按站点收到的时刻):离线的狗不一直挂着旧值;NTP 把狗往回拨了,旧的大样本也很快滑走。
MAX_AGE_MS = 60_000
#: 估计是负的(狗慢)时至少要几条才算:样本 = 真钟差 − 在途,取最大是真钟差的**下界** —— 正的一定不虚报,
#: 负的可能只是这几条在途长(站点刚起、事件循环卡了一下)。
MIN_SAMPLES_BEHIND = 10


class ClockSkew:
    """一台一台狗地记;:meth:`skew_s` 不知道(没样本、狗慢的样本还不够)是 ``None``(不当它准)。"""

    def __init__(self, *, now_ms: Callable[[], int], window: int = WINDOW) -> None:
        self._now = now_ms
        self._window = window
        self._samples: dict[str, deque[tuple[int, int]]] = {}

    def note(self, robot_id: str, dog_ms: int, received_ms: int) -> None:
        d = self._samples.setdefault(robot_id, deque(maxlen=self._window))
        d.append((received_ms, dog_ms - received_ms))

    def skew_s(self, robot_id: str) -> float | None:
        d = self._samples.get(robot_id)
        if not d:
            return None
        cutoff = self._now() - MAX_AGE_MS
        while d and d[0][0] < cutoff:
            d.popleft()
        if not d:
            return None
        best = max(x for _, x in d)
        if best < 0 and len(d) < MIN_SAMPLES_BEHIND:
            return None
        return best / 1000.0


__all__ = ["ALARM_S", "CLEAR_S", "DISPATCH_MAX_S", "MAX_AGE_MS", "MIN_SAMPLES_BEHIND", "WINDOW",
           "ClockSkew"]


#: 同一台机器人最多这么久(毫秒)发一次带站点时刻的对时(W70)。
NUDGE_EVERY_MS = 60_000


class ClockNudger:
    """W70(用户 2026-11-03 现场,选「代理向站点对时」):站点在笔记本上、机器人经 SSH 隧道接进来、
    上不了外网时,机器人的 NTP 对不上(隧道只转 TCP),钟慢慢偏到 −28 s,站点拒发所有命令。钟差过
    :data:`ALARM_S` 的,站点发 ``ops_timesync``,载荷带站点此刻的墙钟 ``site_ms``;机器人报了
    ``ops_timesync.site_time`` 才发(老代理不认这个键,只会 makestep,白发)。``ops_timesync`` 本来
    就不过钟差闸、机器人不判过期。告警主循环每拍调 :meth:`tick`。"""

    def __init__(self, dispatcher: object, *, now_ms: Callable[[], int]) -> None:
        self.d = dispatcher
        self._now = now_ms
        self._sent: dict[str, int] = {}

    async def tick(self) -> None:
        import logging
        log = logging.getLogger(__name__)
        for rid, c in list(getattr(self.d, "clients", {}).items()):
            caps = getattr(c, "capabilities", None)
            spec = caps.tasks.get("ops_timesync") if caps is not None else None
            if not isinstance(spec, dict) or spec.get("site_time") is not True:
                continue
            skew = self.d.clock_skew_s(rid)
            if skew is None or abs(skew) <= ALARM_S:
                continue
            now = self._now()
            if now - self._sent.get(rid, -NUDGE_EVERY_MS) < NUDGE_EVERY_MS:
                continue
            self._sent[rid] = now
            log.info("%s 的钟跟站点差 %+.1f 秒:发站点时刻让它对时", rid, skew)
            try:
                await self.d.map_command(rid, "ops_timesync", {"site_ms": now},
                                         issued_by="site:clock")
            except Exception as exc:                     # noqa: BLE001
                log.warning("%s 对时没发出去:%s", rid, exc)
