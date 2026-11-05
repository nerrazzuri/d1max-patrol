"""开机就绪播报(POC 11-05,用户听过 4 段:「可以,挺好的」)。

开机后机器人自己判,不等站点:

| 情况 | 话术 |
|---|---|
| 连上站点、拿到运控、定位可信、Nav2 就绪都齐了(开机只一次) | ``ready`` |
| 开机 :data:`HINT_AFTER_S` 还连不上站点 | ``not_ready_site`` |
| 开机 :data:`HINT_AFTER_S` 定位还不可信 | ``not_ready_location`` |

- 提示每一段 :data:`HINT_EVERY_S` 最多一次,一拍只放一段(站点在前);齐了放 ``ready``,之后不再管
  (开机以后的掉线、丢定位有各自的报警)。
- 运控、Nav2 没好:不报齐,没有专门的提示(那 4 段里没有)。
- 钟不对时代理还没起来(``deploy/nexwar_wait_clock`` 在等),``not_ready_time`` 由那个脚本放。
- 只放机器人上**有的**话术(``/var/lib/nexwar/clips/<名>.wav``),
  优先级同 :mod:`nexwar_agent.autovoice`。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from nexwar_agent.autovoice import AUTO_PRIORITY, CLIP_MAX_S

HINT_AFTER_S = 300.0
HINT_EVERY_S = 300.0


class BootVoice:
    def __init__(self, deter: Any, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.deter = deter
        self._mono = monotonic
        self._t0 = monotonic()
        self.done = False
        self._last: dict[str, float] = {}

    def _has(self, clip: str) -> bool:
        clips = getattr(self.deter.hal, "sound_clips", None)
        try:
            return callable(clips) and clip in clips()
        except Exception:                                # noqa: BLE001 - 读不到当没有
            return False

    async def tick(self, *, site: bool, control: bool, loc: bool, nav2: bool) -> None:
        if self.done:
            return
        now = self._mono()
        if site and control and loc and nav2:
            if not self._has("ready"):
                self.done = True
                return
            if await self.deter.auto_sound("ready", priority=AUTO_PRIORITY, max_s=CLIP_MAX_S,
                                           why="boot_ready"):
                self.done = True
            return
        if now - self._t0 < HINT_AFTER_S:
            return
        for name, ok in (("site", site), ("location", loc)):
            clip = f"not_ready_{name}"
            last = self._last.get(clip)
            if ok or (last is not None and now - last < HINT_EVERY_S):
                continue
            if await self.deter.auto_sound(clip, priority=AUTO_PRIORITY, max_s=CLIP_MAX_S,
                                           why=f"boot_{name}"):
                self._last[clip] = now
                return
