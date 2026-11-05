"""机器人被遮挡(W74 四期,设计 ``docs/W74-看见人处置-设计.md`` §八)。

被人用东西罩住:雷达一帧有效点太少(定位器报 :data:`~nexwar_contract.locbridge.LIDAR_BLOCKED_REASON`,
W34 的判据),**而且每一路相机**画面都全黑或几乎单色(外审 r1 #11:每一路各自连着 :data:`HOLD_S`
秒,不同相机不接力;哪一路取不到图就说不清、不判)→ 报 ``robot_covered``,回调 ``on_change(True)``
(运行时停车、不收会动的任务);雷达恢复了报 ``robot_uncovered``。

只有一样不算:暗处、对着白墙相机会黑 / 单色;人从雷达跟前过一下雷达会挡一下。只有一路黑也不算
(另一路看得见,多半是那一路对着暗处)。

取图跟热像镜头检测一样起 ffmpeg 缩成 64×48 灰度(:func:`~nexwar_agent.lens.ffmpeg_gray`);只在雷达
挡着的时候取(平时不起 ffmpeg),最快每 :data:`GRAB_EVERY_S` 秒一次。取不到图不算黑。

**门槛没拿真画面定**(设计 §十二:暗处、对着白墙的误报要在真机上确认)。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nexwar_agent.lens import detail

log = logging.getLogger(__name__)

#: 两样同时满足连着这么久才算被遮挡(秒,设计 §八)。
HOLD_S = 3.0
#: 雷达挡着的时候多久取一次图(秒)。
GRAB_EVERY_S = 1.0
#: 平均亮度低于这个(0–255)= 全黑。
DARK_MEAN = 16.0
#: 细节(相邻像素差的平均)低于这个 = 几乎单色(罩着布、贴着镜头)。
FLAT_DETAIL = 1.5


def blind(gray: bytes) -> bool:
    """一帧 64×48 灰度图是不是全黑或几乎单色。"""
    return sum(gray) / len(gray) < DARK_MEAN or detail(gray) < FLAT_DETAIL


class CoverWatch:
    def __init__(self, grabs: dict[str, Callable[[], Awaitable[bytes]]], *,
                 lidar_blocked: Callable[[], bool], monotonic: Callable[[], float],
                 emit: Callable[[str, dict[str, Any]], None] | None = None,
                 on_change: Callable[[bool], Any] | None = None) -> None:
        self.grabs = grabs
        self.lidar_blocked = lidar_blocked
        self._mono = monotonic
        self.emit = emit or (lambda k, d: None)
        self.on_change = on_change
        self.covered = False
        #: 每一路相机从什么时候起连着黑(取到图、看着黑才有)。
        self._since: dict[str, float] = {}
        self._last: float | None = None
        self._blind: list[str] = []

    def due(self) -> bool:
        """该看一眼了:雷达挡着(到点取图)、或者之前报了被遮挡(看揭开没有)。"""
        if not self.lidar_blocked():
            return self.covered or bool(self._since)
        return self._last is None or self._mono() - self._last >= GRAB_EVERY_S

    async def step(self) -> None:
        now = self._mono()
        if not self.lidar_blocked():
            self._since, self._blind = {}, []
            if self.covered:
                self.covered = False
                log.warning("机器人不再被遮挡(雷达恢复了)")
                self.emit("robot_uncovered", {})
                self._changed(False)
            return
        self._last = now
        names = list(self.grabs)
        got = await asyncio.gather(*(self.grabs[n]() for n in names), return_exceptions=True)
        dark = [n for n, g in zip(names, got, strict=True) if isinstance(g, bytes) and _blind(g)]
        for n in names:
            if n in dark:
                self._since.setdefault(n, now)
            else:
                self._since.pop(n, None)                 # 看得见、或取不到图:这一路重新算
        self._blind = dark
        if not names or len(dark) < len(names):
            return
        if not self.covered and all(now - self._since[n] >= HOLD_S for n in names):
            self.covered = True
            log.warning("机器人被遮挡:雷达被挡、相机 %s 看不见", "、".join(dark))
            self.emit("robot_covered", {"cameras": sorted(dark),
                                        "detail": "雷达被挡住,相机 " + "、".join(sorted(dark))
                                        + " 全黑或几乎单色"})
            self._changed(True)

    def _changed(self, covered: bool) -> None:
        if self.on_change is not None:
            try:
                self.on_change(covered)
            except Exception:
                log.exception("被遮挡的回调炸了")


def _blind(g: bytes) -> bool:
    try:
        return blind(g)
    except ValueError:                                   # 尺寸不对:不算黑
        return False
