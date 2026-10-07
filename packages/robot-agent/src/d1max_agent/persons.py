"""人员检测,代理这一头(W24,决策 39)。检测节点经本机人员桥(:mod:`d1max_contract.persbridge`)一帧一帧
报「看到几个人、在哪个方向、多远」;这里维护**当前人员状态**,放进能力里给站点(站点按它每拍对账驱离),
状态变了也记一条事件(存档用,**不拿事件控制驱离**,W24 外审)。

**有没有人是三种状态**(``present``):
- ``True`` 有人:同一个相机最近 :data:`WINDOW` 帧里至少 :data:`NEED` 帧看到人 —— 一帧误检不算。
- ``False`` 确认没人:**连续有效观测** :data:`GONE_S`(20 秒)一帧都没看到人。
- ``None`` 不知道:刚起来、重连、检测中断(没报帧、``check`` 不是 ``ok``、断开)之后,直到上面两样之一。

**每次收帧、每拍都按下面的规矩重算**(W24 复查:每个相机各存各的观测,不许一个相机的画面覆盖另一个的):
- 有一个健康的相机正看到人(它最近 3 帧里至少 2 帧有人)→ **有人**;
- 没人看到人,但**在看的相机**(报过正常帧的每一台,断开也记着)有停了的 → **不知道**(中断:旧的人数、
  距离、「近」都作废,没人的时间重新数)—— 后相机停了、前相机照样报空画面,不算人走了;
- 在看的相机都健康、都没看到人 → 开始数;原来是「有人」的照样先算有人,
  **连续满 20 秒**变成**确认没人**。

**靠近**(``near``):**每个相机各自判**(进到 :data:`NEAR_M` 5 米以内算近、退到 :data:`NEAR_CLEAR_M`
以外才算不近),有一个**确认看到人的**相机近就算近 —— 远处一个人不许把另一边近处的人盖掉。
人数是各个确认看到人的相机加起来,最近的距离取它们里最近的。**只有确认了的相机才算数**(W24 复查二):
另一台相机一帧误检出 3 米,不许把「近」带进来。

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
    count: int = 0
    nearest: float | None = None
    bearing: float = 0.0
    near: bool = False

    def sees(self) -> bool:
        return sum(self.win) >= NEED and bool(self.win and self.win[-1])


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
        self._last_check = ""
        self._cams: dict[str, _Cam] = {}
        #: 在看的相机:报过正常帧的每一台。**断开也记着**(W24 复查):重连后它们都要回来、都健康,
        #: 才能判确认没人。
        self._watch: set[str] = set()
        #: 有没有人:True / False / None(不知道)。
        self.present: bool | None = None
        self.near = False
        #: 从哪一刻起在看的相机都健康、都没看到人(None = 没在数)。
        self._empty_since: float | None = None
        self.count = 0
        self.nearest_m: float | None = None

    # ------------------------------------------------------------ 健康

    def _healthy(self, cam: str) -> bool:
        c = self._cams.get(cam)
        return (self.connected and c is not None and c.ok
                and self._now() - c.last <= STALE_S)

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

    def target(self) -> tuple[float, float] | None:
        """最近的那个人(W25 保持距离用):``(方向°, 距离 m)``,狗身系(朝前 0°、朝左为正)。只看此刻
        确认看到人、测到距离的相机;不是「有人」、没测到距离都回 ``None``(不知道在哪:原地不动)。"""
        if self.present is not True:
            return None
        got = [(c.nearest, c.bearing) for k, c in self._cams.items()
               if self._healthy(k) and c.sees() and c.nearest is not None]
        if not got:
            return None
        r, b = min(got)
        return (b, r)

    def key(self) -> tuple:
        """能力该重发的时候(这几样变了)。人数、距离天天变,不在里面(站点按 ``near`` 判)。"""
        return (self.state(), self.present, self.present is True and self.near)

    # ------------------------------------------------------------ 桥

    def on_connect(self) -> None:
        self.connected = True

    def on_disconnect(self) -> None:
        self.connected = False
        self.reason, self._last_check = "", ""
        self._cams.clear()                                 # 健康都没了;在看的相机还记着
        self._update()

    def on_persons(self, m: Persons) -> None:
        if m.snapshot:
            self._take_snapshot(m)
        c = self._cams.setdefault(m.camera, _Cam())
        c.last, c.ok = self._now(), m.check == "ok"
        self._last_check, self.reason = m.check, m.reason
        if not c.ok:
            c.win.clear()
            c.count, c.nearest, c.near, c.bearing = 0, None, False, 0.0
        else:
            self._watch.add(m.camera)
            c.win.append(bool(m.people))
            near = m.nearest()
            c.count = len(m.people)
            c.nearest = near.range_m if near is not None else None
            c.bearing = near.bearing_deg if near is not None else 0.0
            if c.nearest is not None and c.nearest <= NEAR_M:
                c.near = True
            elif c.nearest is None or c.nearest > NEAR_CLEAR_M:
                c.near = False
        self._update(m)

    def tick(self) -> None:
        self._update()

    # ------------------------------------------------------------ 重算

    def _update(self, m: Persons | None = None) -> None:
        now = self._now()
        live = [c for k, c in self._cams.items() if self._healthy(k)]
        sure = [c for c in live if c.sees()]               # 确认看到人的相机(三帧里两帧、这帧也有)
        if sure:
            self.count = sum(c.count for c in sure)
            ranged = [c.nearest for c in sure if c.nearest is not None]
            self.nearest_m = min(ranged) if ranged else None
            was_near = self.near and self.present is True
            self.near = any(c.near for c in sure)
            self._empty_since = None
            if self.present is not True:
                self.present = True
                self._emit("person_seen", self._data())
            if self.near and not was_near:
                self._emit("person_near", self._data())
            return
        if not self._watch_healthy():
            # 中断:没人看到人、在看的相机又有停了的(或者断开了)→ 不知道;旧的人数、距离、近都作废
            if self.present is not None or self._empty_since is not None:
                log.info("人员检测中断:有没有人先当不知道")
            self.present, self.near = None, False
            self.count, self.nearest_m = 0, None
            self._empty_since = None
            return
        self.near = False
        if self._empty_since is None:
            self._empty_since = now                        # 在看的都健康、都没人:从这一刻起数
            return
        if now - self._empty_since < GONE_S or self.present is False:
            return
        was = self.present
        self.present = False
        self.count, self.nearest_m = 0, None
        if was is True:
            log.info("人走了(连续 %.0f 秒有效观测没看到)", now - self._empty_since)
            self._emit("person_gone", {"after_s": round(now - self._empty_since)})

    def _data(self) -> dict[str, Any]:
        d: dict[str, Any] = {"count": self.count}
        if self.nearest_m is not None:
            d["nearest_m"] = round(self.nearest_m, 1)
        return d

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
