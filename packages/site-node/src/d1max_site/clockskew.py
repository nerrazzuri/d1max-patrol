"""狗的钟跟站点差多少(W09d)。

每条遥测(1 Hz)带狗的墙钟 ``stamp``;站点收到的时刻减去在途,就是同一时刻站点的钟。在途不知道、
只会是正的,所以「狗的时刻 − 站点收到的时刻」只会比真的钟差**小**:最近 :data:`WINDOW` 条里取最大的,
最接近真的(broker 憋了几秒的那一条不会把估计拉偏)。原来单条样本、60 s 的线只抓得住差半年那种;
对上 NTP 之后局域网里是毫秒级。
"""

from __future__ import annotations

from collections import deque

#: 估计用最近几条遥测(1 Hz:半分钟)。
WINDOW = 30
#: 钟差超过它报「狗的钟不准」(NTP 没配上、不通、慢慢漂);回到 :data:`CLEAR_S` 以内才重新武装。
ALARM_S = 2.0
CLEAR_S = 1.0
#: 钟差超过它不派任务命令:命令有效期 60 s、狗按自己的钟判,差一半以上旧命令不过期、新命令一到就过期。
DISPATCH_MAX_S = 30.0


class ClockSkew:
    """一台一台狗地记;:meth:`skew_s` 没收到过遥测是 ``None``(不知道,不当它准)。"""

    def __init__(self, window: int = WINDOW) -> None:
        self._window = window
        self._samples: dict[str, deque[int]] = {}

    def note(self, robot_id: str, dog_ms: int, received_ms: int) -> None:
        d = self._samples.setdefault(robot_id, deque(maxlen=self._window))
        d.append(dog_ms - received_ms)

    def skew_s(self, robot_id: str) -> float | None:
        d = self._samples.get(robot_id)
        return None if not d else max(d) / 1000.0


__all__ = ["ALARM_S", "CLEAR_S", "DISPATCH_MAX_S", "WINDOW", "ClockSkew"]
