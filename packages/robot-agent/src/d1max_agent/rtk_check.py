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
#: 连续按 RTK 请了这么多次还对不上:不再自动请(可能是 RTK 的「假固定」,地图匹配很坚定),发事件交给人
#: (W09e 内审再议 1、应修 4)。
MAX_AUTO_RELOCS = 2
#: 人给了位置之后这么久不按 RTK 自动重定位(人正看着;内审应修 4)。
HUMAN_QUIET_S = 60.0
#: 标了不可信之后 RTK 一直用不上(树下只剩浮点解)这么久:标记降级解除,不一直粘着(内审应修 4)。
FLAG_TTL_S = 30.0
#: 基站坐标跟建图时差这么多:这份配准对不上了(内审应修 3)。
BASE_MOVED_M = 0.05


@dataclass(frozen=True)
class Verdict:
    """这一拍的结论。``reason`` 非空 = 定位器不可信;``reloc`` 给了 = 请它在这个位置(x, y, yaw)
    重定位;``event`` 给了 = 发这个事件(``rtk_disagree`` / ``rtk_gave_up`` / ``rtk_base_moved``,
    带 ``data``)。"""
    reason: str = ""
    reloc: tuple[float, float, float] | None = None
    gap_m: float | None = None
    event: str | None = None
    data: dict[str, Any] | None = None


class RtkCheck:
    def __init__(self) -> None:
        self.geo: GeoRef | None = None
        self._reset()

    def _reset(self) -> None:
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._unusable_since: float | None = None
        self._flagged = ""
        self._last_reloc: float | None = None
        self._relocs = 0
        self._gave_up = False
        self._quiet_until = -math.inf
        self.base_moved = False
        self._base_told = False

    def on_map(self, geo: GeoRef | None) -> None:
        """换了图:换配准,之前的判断作废。"""
        self.geo = geo
        self._reset()

    def human_override(self, now: float) -> None:
        """人给了位置(重定位):之前的结论作废,:data:`HUMAN_QUIET_S` 内不按 RTK 自动重定位。"""
        base_moved, base_told = self.base_moved, self._base_told
        self._reset()
        self.base_moved, self._base_told = base_moved, base_told
        self._quiet_until = now + HUMAN_QUIET_S

    def on_base(self, ecef: tuple[float, float, float] | None) -> Verdict | None:
        """改正里的基站坐标(自己的驱动才有)。跟建图时的差 5 cm 以上:不再核对,第一次发事件。"""
        ref = self.geo.base_ecef if self.geo is not None else None
        if ecef is None or ref is None:
            return None
        moved = math.dist(ecef, ref) > BASE_MOVED_M
        self.base_moved = moved
        if moved and not self._base_told:
            self._base_told = True
            self._flagged = ""
            return Verdict(event="rtk_base_moved",
                           data={"moved_m": round(math.dist(ecef, ref), 3)})
        return None

    def usable(self, fix: dict[str, Any] | None) -> bool:
        if self.geo is None or self.base_moved or not fix or fix.get("stale") \
                or fix.get("fix") != "fixed":
            return False
        std, age = fix.get("std_h_m"), fix.get("age_s")
        if not isinstance(std, (int, float)) or not math.isfinite(std) or std > STD_MAX_M:
            return False
        if isinstance(age, (int, float)) and age > AGE_MAX_S:
            return False
        lat, lon = fix.get("lat"), fix.get("lon")
        return isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and \
            math.isfinite(lat) and math.isfinite(lon)

    def step(self, fix: dict[str, Any] | None, est: tuple[float, float, float] | None,
             now: float) -> Verdict:
        """``est``:定位器此刻给的狗身中心 ``(x, y, yaw)``(地图系);没有、定位器正在重定位是 None。"""
        if est is None or not self.usable(fix):
            self._bad_since = self._good_since = None       # 没法比:保持原来的结论……
            if self._flagged and not (est is None and fix is not None and self.usable(fix)):
                if self._unusable_since is None:
                    self._unusable_since = now
                elif now - self._unusable_since >= FLAG_TTL_S:
                    self._flagged = ""                        # ……但不一直粘着(内审应修 4)
                    self._relocs = 0
            return Verdict(self._flagged)
        self._unusable_since = None
        assert self.geo is not None and fix is not None
        ax, ay = self.geo.llh_to_map(float(fix["lat"]), float(fix["lon"]))
        bx, by = self.geo.body_from_antenna(ax, ay, est[2])
        gap = math.hypot(bx - est[0], by - est[1])
        if gap > DIFF_M:
            self._good_since = None
            if self._bad_since is None:
                self._bad_since = now
            if now - self._bad_since < HOLD_S:
                return Verdict(self._flagged, None, gap)
            event = None
            if not self._flagged:
                event = "rtk_disagree"
            self._flagged = f"RTK 说位置差 {gap:.1f} m,定位器不可信"
            data = {"gap_m": round(gap, 2), "rtk_xy": [round(bx, 2), round(by, 2)]}
            due = self._last_reloc is None or now - self._last_reloc >= RELOC_COOLDOWN_S
            if due and now >= self._quiet_until and not self._gave_up:
                if self._relocs >= MAX_AUTO_RELOCS:
                    self._gave_up = True                    # 请过两次还是对不上:交给人
                    return Verdict(self._flagged, None, gap, "rtk_gave_up",
                                   data | {"relocs": self._relocs})
                self._last_reloc = now
                self._relocs += 1
                return Verdict(self._flagged, (bx, by, est[2]), gap, event, data)
            return Verdict(self._flagged, None, gap, event, data if event else None)
        self._bad_since = None
        if gap <= AGREE_M:
            if self._good_since is None:
                self._good_since = now
            if self._flagged and now - self._good_since >= HOLD_S:
                self._flagged = ""
                self._last_reloc = None
                self._relocs = 0
                self._gave_up = False
        return Verdict(self._flagged, None, gap)
