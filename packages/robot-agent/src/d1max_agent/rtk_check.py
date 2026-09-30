"""RTK 核对定位器(W09e 决定 8):抓 W09a 的已知限制「定位器自信地跳错、之后一直稳定地错」。

正在用的图有 ``geo.json``、RTK 是**固定解**且水平标准差 ≤ :data:`STD_MAX_M`、改正龄期 ≤
:data:`AGE_MAX_S`、没过时 → 把 RTK 换到地图系(天线杆臂换回狗身中心,朝向用定位器的)跟定位器的位置比:
连续 :data:`HOLD_S` 差 > :data:`DIFF_M` → 定位器不可信(「RTK 说差 X m」),请定位器在 RTK 的位置附近
重定位(隔 :data:`RELOC_COOLDOWN_S` 才再请);连续 :data:`HOLD_S` 差 ≤ :data:`AGREE_M` → 解除。
浮点解、单点解不参与。第一版不拿 RTK 顶替定位器、不做融合。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from d1max_contract.geo import GeoRef

STD_MAX_M = 0.10
AGE_MAX_S = 5.0
DIFF_M = 1.0
AGREE_M = 0.5
HOLD_S = 3.0
RELOC_COOLDOWN_S = 10.0
#: 按 RTK 重定位时给定位器的初值不确定度(米)。
RELOC_SIGMA_M = 0.5


@dataclass(frozen=True)
class Verdict:
    """这一拍的结论。``reason`` 非空 = 定位器不可信;``reloc`` 给了 = 请它在这个位置(x, y, yaw)
    重定位。"""
    reason: str = ""
    reloc: tuple[float, float, float] | None = None
    gap_m: float | None = None


class RtkCheck:
    def __init__(self) -> None:
        self.geo: GeoRef | None = None
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._flagged = ""
        self._last_reloc: float | None = None

    def on_map(self, geo: GeoRef | None) -> None:
        """换了图:换配准,之前的判断作废。"""
        self.geo = geo
        self._bad_since = self._good_since = None
        self._flagged = ""
        self._last_reloc = None

    def usable(self, fix: dict[str, Any] | None) -> bool:
        if self.geo is None or not fix or fix.get("stale") or fix.get("fix") != "fixed":
            return False
        std, age = fix.get("std_h_m"), fix.get("age_s")
        if not isinstance(std, (int, float)) or std > STD_MAX_M:
            return False
        if isinstance(age, (int, float)) and age > AGE_MAX_S:
            return False
        return isinstance(fix.get("lat"), (int, float)) and isinstance(fix.get("lon"), (int, float))

    def step(self, fix: dict[str, Any] | None, est: tuple[float, float, float] | None,
             now: float) -> Verdict:
        """``est``:定位器此刻给的狗身中心 ``(x, y, yaw)``(地图系);没有是 None。"""
        if est is None or not self.usable(fix):
            self._bad_since = self._good_since = None       # 没法比:保持原来的结论
            return Verdict(self._flagged)
        assert self.geo is not None and fix is not None
        ax, ay = self.geo.llh_to_map(float(fix["lat"]), float(fix["lon"]))
        bx, by = self.geo.body_from_antenna(ax, ay, est[2])
        gap = math.hypot(bx - est[0], by - est[1])
        if gap > DIFF_M:
            self._good_since = None
            if self._bad_since is None:
                self._bad_since = now
            if now - self._bad_since >= HOLD_S:
                self._flagged = f"RTK 说位置差 {gap:.1f} m,定位器不可信"
                if self._last_reloc is None or now - self._last_reloc >= RELOC_COOLDOWN_S:
                    self._last_reloc = now
                    return Verdict(self._flagged, (bx, by, est[2]), gap)
            return Verdict(self._flagged, None, gap)
        self._bad_since = None
        if gap <= AGREE_M:
            if self._good_since is None:
                self._good_since = now
            if self._flagged and now - self._good_since >= HOLD_S:
                self._flagged = ""
                self._last_reloc = None
        return Verdict(self._flagged, None, gap)
