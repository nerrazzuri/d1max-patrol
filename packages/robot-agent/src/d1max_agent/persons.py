"""人员检测,代理这一头(W24,决策 39)。检测节点经本机人员桥(:mod:`d1max_contract.persbridge`)一帧一帧
报「看到几个人、在哪个方向、多远」;这里维护**当前人员状态**,放进能力里给站点(站点按它每拍对账驱离),
状态变了也记一条事件(存档用,**不拿事件控制驱离**,W24 外审)。

**有没有人是三种状态**(``present``):
- ``True`` 有人:同一个相机最近 :data:`WINDOW` 帧里至少 :data:`NEED` 帧看到人 —— 一帧误检不算。
- ``False`` 确认没人:**连续有效观测** :data:`GONE_S`(20 秒)一帧都没看到人。
- ``None`` 不知道:刚起来、重连、检测中断(没报帧、``check`` 不是 ``ok``、断开)之后,直到上面两样之一。

**中断期间不累计没人的时间**(W24 外审 1):有效观测要从中断之后重新数满 20 秒。**每个相机分开看健康**:
这一轮在看的相机(有人时在报帧的那几台)只要有一台停了,就不能判「确认没人」—— 后相机停了、前相机照样
报空画面,不算人走了。

**靠近**(``near``):有人、最近的进到 :data:`NEAR_M`(5 米)以内;退到 :data:`NEAR_CLEAR_M` 以外才算不近
(不在 5 米线上来回翻)。

**截图**:检测节点每存一张(``persons {snapshot}``)这里就收一张,存成归档传站点、删掉原文件,不管这一帧
是不是「刚看到人」那一下(W24 外审 5)。存不下的留着,检测节点那头有张数上限。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d1max_contract.persbridge import Persons

log = logging.getLogger(__name__)

WINDOW = 3
NEED = 2
NEAR_M = 5.0
NEAR_CLEAR_M = 6.0
GONE_S = 20.0
#: 一个相机这么久没报帧当它停了(中断)。检测节点每个相机每秒报 2 帧。
STALE_S = 3.0


@dataclass
class _Cam:
    last: float = -1e18
    ok: bool = False
    win: deque = field(default_factory=lambda: deque(maxlen=WINDOW))


class PersonView:
    def __init__(self, *, emit: Callable[[str, dict[str, Any]], Any],
                 monotonic: Callable[[], float] = time.monotonic,
                 snapshot_dir: Path | None = None,
                 save_snapshot: Callable[[Path, str], str] | None = None) -> None:
        self._emit = emit
        self._now = monotonic
        self.snapshot_dir = snapshot_dir
        #: 把截图存成一趟归档(代理接上):``(截图路径, 相机) → 那一趟的名字``(空 = 没存成)。
        self.save_snapshot = save_snapshot
        self.connected = False
        self.reason = ""
        self._cams: dict[str, _Cam] = {}
        #: 有没有人:True / False / None(不知道)。
        self.present: bool | None = None
        self.near = False
        #: 这一轮在看的相机(判「确认没人」时它们都要健康)。
        self._watch: set[str] = set()
        #: 从哪一刻起连续有效观测都没人(None = 没在数)。
        self._empty_since: float | None = None
        self.count = 0
        self.nearest_m: float | None = None

    # ------------------------------------------------------------ 健康

    def _healthy(self, cam: str) -> bool:
        c = self._cams.get(cam)
        return c is not None and c.ok and self._now() - c.last <= STALE_S

    def _watch_healthy(self) -> bool:
        return self.connected and bool(self._watch) and all(self._healthy(c) for c in self._watch)

    def state(self) -> str:
        """``off`` 没连上;``stale`` 连着但没有一个相机在正常报帧;否则 ``ok`` 或检测节点报的问题。"""
        if not self.connected:
            return "off"
        live = [c for c in self._cams.values() if self._now() - c.last <= STALE_S]
        if not live:
            return "stale"
        if any(c.ok for c in live):
            return "ok"
        return self._last_check or "initializing"

    _last_check = ""

    def caps(self) -> dict[str, Any]:
        """能力里的 ``persons``:站点按它对账驱离(``present`` 是 True / False / None)。"""
        d: dict[str, Any] = {"state": self.state(), "present": self.present,
                             "near": self.present is True and self.near}
        if self.present:
            d["count"] = self.count
            if self.nearest_m is not None:
                d["nearest_m"] = round(self.nearest_m, 1)
        if self.reason and d["state"] not in ("ok", "off", "stale"):
            d["reason"] = self.reason[:200]
        return d

    def key(self) -> tuple:
        """能力该重发的时候(这几样变了)。人数、距离天天变,不在里面(站点按 ``near`` 判)。"""
        return (self.state(), self.present, self.present is True and self.near)

    # ------------------------------------------------------------ 桥

    def _interrupt(self, why: str) -> None:
        """检测中断:不知道有没有人了,没人的时间重新数。"""
        if self._empty_since is not None or self.present is not None:
            log.info("人员检测中断(%s):有没有人先当不知道", why)
        self._empty_since = None
        if self.present is not True:
            self.present = None

    def on_connect(self) -> None:
        self.connected = True

    def on_disconnect(self) -> None:
        self.connected = False
        self.reason, self._last_check = "", ""
        self._cams.clear()
        self._watch.clear()
        self._interrupt("检测节点断开")

    def on_persons(self, m: Persons) -> None:
        now = self._now()
        if m.snapshot:
            self._take_snapshot(m)
        c = self._cams.setdefault(m.camera, _Cam())
        c.last, c.ok = now, m.check == "ok"
        self._last_check, self.reason = m.check, m.reason
        if not c.ok:
            c.win.clear()
            if m.camera in self._watch or not self._watch:
                self._interrupt(f"{m.camera} 报 {m.check}")
            return
        self._watch.add(m.camera)
        c.win.append(bool(m.people))
        if m.people:
            self._empty_since = None
            near = m.nearest()
            self.count = len(m.people)
            self.nearest_m = near.range_m if near is not None else None
            if self.present is not True and sum(c.win) >= NEED:
                self.present = True
                self._watch = {k for k in self._cams if self._healthy(k)}
                self._emit("person_seen", self._data(m))
            if self.present is True:
                self._check_near(m)
        elif not self._watch_healthy():
            if self._empty_since is not None:              # 收帧时也看健康,不光靠 tick
                self._interrupt("在看的相机有停了的")
        elif self._empty_since is None:
            self._empty_since = now                        # 有效观测、没人:从这一刻起数

    def _data(self, m: Persons) -> dict[str, Any]:
        near = m.nearest()
        d: dict[str, Any] = {"camera": m.camera, "count": len(m.people)}
        if near is not None:
            d["bearing_deg"] = round(near.bearing_deg, 1)
            if near.range_m is not None:
                d["nearest_m"] = round(near.range_m, 1)
        return d

    def _check_near(self, m: Persons) -> None:
        near = m.nearest()
        r = near.range_m if near is not None else None
        if r is None:
            return
        if not self.near and r <= NEAR_M:
            self.near = True
            self._emit("person_near", self._data(m))
        elif self.near and r > NEAR_CLEAR_M:
            self.near = False

    def _take_snapshot(self, m: Persons) -> None:
        if self.snapshot_dir is None or self.save_snapshot is None:
            return
        try:
            run = self.save_snapshot(self.snapshot_dir / m.snapshot, m.camera)
        except Exception:
            log.exception("截图 %s 存不进归档(留在截图目录,检测节点那头有张数上限)", m.snapshot)
            return
        if run:
            self._emit("person_snapshot", {"camera": m.camera, "run": run})

    def tick(self) -> None:
        """每拍:看着的相机有停了的 → 中断;连续有效观测没人满 ``GONE_S`` → 确认没人。"""
        if not self._watch_healthy():
            if self._empty_since is not None:
                self._interrupt("在看的相机有停了的")
            return
        if self._empty_since is None or self._now() - self._empty_since < GONE_S:
            return
        was = self.present
        self.present, self.near = False, False
        self.count, self.nearest_m = 0, None
        if was is True:
            log.info("人走了(连续 %.0f 秒有效观测没看到)", self._now() - self._empty_since)
            self._emit("person_gone", {"after_s": round(self._now() - self._empty_since)})
