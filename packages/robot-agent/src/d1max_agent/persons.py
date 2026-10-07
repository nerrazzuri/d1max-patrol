"""人员检测,代理这一头(W24,决策 39)。检测节点经本机人员桥(:mod:`d1max_contract.persbridge`)一帧一帧
报「看到几个人、在哪个方向、多远」;这里判「有没有人」,有变化就记事件给站点(站点据此告警、联动驱离)。

- **有人**:同一个相机最近 :data:`WINDOW` 帧里至少 :data:`NEED` 帧看到人 —— 一帧误检不算。
  变成有人那一下记 ``person_seen``(几个人、最近的多远、哪个方向、哪个相机),截图存成一趟归档传站点。
- **靠近**:有人、最近的进到 :data:`NEAR_M`(5 米)以内,记一次 ``person_near``;退到
  :data:`NEAR_CLEAR_M` 以外才重新武装(不在 5 米线上来回刷)。
- **走了**:有人之后连续 :data:`GONE_S`(20 秒)一帧都没看到,记 ``person_gone``。
- **检测节点不在**(没连上、断了、``check`` 不是 ``ok``):**不报走了**(看不见不等于没人);
  能力里报出来,站点就只按时间管驱离。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract.persbridge import Persons

log = logging.getLogger(__name__)

WINDOW = 3
NEED = 2
NEAR_M = 5.0
NEAR_CLEAR_M = 6.0
GONE_S = 20.0
#: 这么久没收到一帧当检测节点没在看(桥上 2 秒没声会断开;这里管「连着但不报帧」)。
STALE_S = 3.0


class PersonView:
    def __init__(self, *, emit: Callable[[str, dict[str, Any]], Any],
                 monotonic: Callable[[], float] = time.monotonic,
                 snapshot_dir: Path | None = None,
                 save_snapshot: Callable[[Path, str], str] | None = None) -> None:
        self._emit = emit
        self._now = monotonic
        #: 检测节点存截图的目录(``persons {snapshot}`` 是这里面的文件名)。
        self.snapshot_dir = snapshot_dir
        #: 把截图存成一趟归档(代理接上):``(截图路径, 相机) → 那一趟的名字``(空 = 没存成)。
        self.save_snapshot = save_snapshot
        self.connected = False
        self.check = ""
        self.reason = ""
        self._frame_at = -1e18
        self._win: dict[str, deque[bool]] = {}
        self.present = False
        self._near = False
        self._last_seen = -1e18
        self.count = 0
        self.nearest_m: float | None = None

    # ------------------------------------------------------------ 桥

    def on_connect(self) -> None:
        self.connected = True

    def on_disconnect(self) -> None:
        self.connected = False
        self.check, self.reason = "", ""
        self._win.clear()

    def state(self) -> str:
        """``off`` 没连上;``stale`` 连着但不报帧;否则检测节点报的 ``check``。"""
        if not self.connected:
            return "off"
        if self._now() - self._frame_at > STALE_S:
            return "stale"
        return self.check or "initializing"

    def caps(self) -> dict[str, Any]:
        d: dict[str, Any] = {"state": self.state()}
        if self.reason and self.state() not in ("ok", "off", "stale"):
            d["reason"] = self.reason[:200]
        return d

    def on_persons(self, m: Persons) -> None:
        now = self._now()
        self._frame_at = now
        self.check, self.reason = m.check, m.reason
        if m.check != "ok":
            return
        w = self._win.setdefault(m.camera, deque(maxlen=WINDOW))
        w.append(bool(m.people))
        if m.people:
            self._last_seen = now
            near = m.nearest()
            self.count = len(m.people)
            self.nearest_m = near.range_m if near is not None else None
        if not self.present and sum(w) >= NEED and m.people:
            self.present = True
            self._seen(m)
        if self.present and m.people:
            self._check_near(m)

    def _data(self, m: Persons) -> dict[str, Any]:
        near = m.nearest()
        d: dict[str, Any] = {"camera": m.camera, "count": len(m.people)}
        if near is not None:
            d["bearing_deg"] = round(near.bearing_deg, 1)
            if near.range_m is not None:
                d["nearest_m"] = round(near.range_m, 1)
        return d

    def _seen(self, m: Persons) -> None:
        d = self._data(m)
        if m.snapshot and self.snapshot_dir is not None and self.save_snapshot is not None:
            try:
                run = self.save_snapshot(self.snapshot_dir / m.snapshot, m.camera)
            except Exception:
                log.exception("看到人的截图存不下")
                run = ""
            if run:
                d["snapshot_run"] = run
        log.info("看到人了: %s", d)
        self._emit("person_seen", d)

    def _check_near(self, m: Persons) -> None:
        near = m.nearest()
        r = near.range_m if near is not None else None
        if r is None:
            return
        if not self._near and r <= NEAR_M:
            self._near = True
            self._emit("person_near", self._data(m))
        elif self._near and r > NEAR_CLEAR_M:
            self._near = False

    def tick(self) -> None:
        """每拍:有人之后连续 ``GONE_S`` 秒没看到 → ``person_gone``。检测节点不在就不判。"""
        if not self.present or self.state() != "ok":
            return
        gap = self._now() - self._last_seen
        if gap >= GONE_S:
            self.present, self._near = False, False
            self.count, self.nearest_m = 0, None
            self._win.clear()
            log.info("人走了(%.0f 秒没看到)", gap)
            self._emit("person_gone", {"after_s": round(gap)})
