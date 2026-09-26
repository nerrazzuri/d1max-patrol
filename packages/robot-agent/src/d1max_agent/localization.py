"""定位来源(W00c6e,W08 决定 4 的过渡实现):**里程锚定**。

W09 的定位器落地之前,代理的直线导航桥拿运控里程直接当地图位姿 —— 里程开机若归零,每次换电重启原点和
所有点整体平移;腿式里程约 9 m 漂 2 m,没有任何东西告诉人「现在的位置不可信了」。这里做的是:

- **人给一个地图位姿**(或者说「狗在原点」),同时记下当时的里程,锁定 ``T_map_odom``;之后地图位姿 =
  ``T_map_odom`` ∘ 里程。
- **不确定度**按锚定之后走过的距离线性变大(按 9 m 漂 2 m 取);σ_xy 过 :data:`SIGMA_LOST_M` 就
  不可信 —— 导航桥报 ``LOC_LOST``,引擎按 ``on_loc_lost`` 暂停等人重新给位置。
- **里程跳了**(一拍里挪了 :data:`JUMP_M` 以上:旁路进程重启、里程归零)→ 锚定作废。
- **没锚过、换了地图** → 不可信,要人给一次位置。

接口跟 W09 的定位器一致(地图位姿、σ、来源、为什么不可信);定位器落地后换实现、不换接口。

**仿真**(``identity=True``):仿真的里程就是真实位置、不漂 —— 按原样锚定、σ 恒为 0,里程「跳」(测试里
把狗瞬移)也不作废。

漂移率、σ 的线、跳变的线都是**待真机标定**的(W00d 量里程漂移),跟 W08 的粗测对齐。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: 人给位置时的不确定度(米、弧度)。
SIGMA0_XY_M = 0.2
SIGMA0_YAW_RAD = 0.1
#: 每走一米 σ 涨多少(按 9 m 漂 2 m 取;航向按每米 0.02 rad)。
DRIFT_XY_PER_M = 2.0 / 9.0
DRIFT_YAW_PER_M = 0.02
#: σ_xy 过这条线就不可信(约走 8 m)。
SIGMA_LOST_M = 2.0
#: 一拍里里程挪了这么多就当里程跳了(旁路进程重启、里程归零)。
JUMP_M = 1.0


def _wrap(a: float) -> float:
    w = math.remainder(a, 2 * math.pi)
    return math.pi if w == -math.pi else w


def compose(a: tuple[float, float, float], b: tuple[float, float, float]
            ) -> tuple[float, float, float]:
    """``a ∘ b``:把 ``b`` 这个位姿放到 ``a`` 这个坐标系里。"""
    ax, ay, ayaw = a
    bx, by, byaw = b
    c, s = math.cos(ayaw), math.sin(ayaw)
    return (ax + c * bx - s * by, ay + s * bx + c * by, _wrap(ayaw + byaw))


def inverse(a: tuple[float, float, float]) -> tuple[float, float, float]:
    ax, ay, ayaw = a
    c, s = math.cos(ayaw), math.sin(ayaw)
    return (-(c * ax + s * ay), -(-s * ax + c * ay), _wrap(-ayaw))


@dataclass(frozen=True)
class LocEstimate:
    """地图位姿与它的不确定度(跟 W09 定位器报的同一个形状)。"""

    map_id: str
    map_version: str
    x: float
    y: float
    yaw: float
    sigma_xy_m: float
    sigma_yaw_rad: float
    source: str


class OdomAnchor:
    """里程锚定。**不做 I/O**:里程由调用方(导航桥每拍)喂进来。"""

    def __init__(self, *, identity: bool = False) -> None:
        self.identity = identity
        self._T: tuple[float, float, float] | None = None
        self._map: tuple[str, str] | None = None
        self._dist = 0.0
        self._last: tuple[float, float, float] | None = None
        self.reason = "还没设位置:开机、换图之后要人给一次(或者说「狗在原点」)"

    @property
    def source(self) -> str:
        return "odom_identity" if self.identity else "odom_anchor"

    @property
    def anchored(self) -> bool:
        return self._T is not None and self._map is not None

    @property
    def map_ref(self) -> tuple[str, str] | None:
        return self._map

    def on_map(self, map_ref: tuple[str, str] | None) -> None:
        """狗换了(或刚载入)一张图。锚定是按图的:换了图坐标就不一样了,作废(仿真按原样锚到新图)。"""
        if self.identity:
            self._map = map_ref
            self._T = (0.0, 0.0, 0.0) if map_ref is not None else None
            self.reason = "" if map_ref is not None else "没有加载地图"
            return
        if map_ref != self._map:
            self.clear("换了地图,要重新设位置")

    def anchor(self, map_ref: tuple[str, str], pose: tuple[float, float, float],
               odom: tuple[float, float, float]) -> None:
        """人给的地图位姿 ``pose``,此刻的里程 ``odom``:锁定 ``T_map_odom``,σ 从头算。"""
        self._T = compose(pose, inverse(odom))
        self._map = map_ref
        self._dist = 0.0
        self._last = odom
        self.reason = ""

    def clear(self, reason: str) -> None:
        if self.identity:
            return
        self._T = None
        self.reason = reason

    def update(self, odom: tuple[float, float, float]) -> None:
        """每拍喂一次里程:累计走过的距离;一拍挪太多就当里程跳了。"""
        last, self._last = self._last, odom
        if last is None:
            return
        d = math.hypot(odom[0] - last[0], odom[1] - last[1])
        if d > JUMP_M and not self.identity:
            self.clear(f"里程一拍跳了 {d:.1f} m(旁路进程重启或里程归零),要重新设位置")
            return
        self._dist += d

    @property
    def sigma_xy(self) -> float:
        return 0.0 if self.identity else SIGMA0_XY_M + self._dist * DRIFT_XY_PER_M

    @property
    def sigma_yaw(self) -> float:
        return 0.0 if self.identity else SIGMA0_YAW_RAD + self._dist * DRIFT_YAW_PER_M

    def estimate(self, odom: tuple[float, float, float]) -> LocEstimate | None:
        """此刻的地图位姿;没锚过是 ``None``。**σ 过线照样给**(调用方看 :meth:`ok`)。"""
        if self._T is None or self._map is None:
            return None
        x, y, yaw = compose(self._T, odom)
        return LocEstimate(map_id=self._map[0], map_version=self._map[1], x=x, y=y, yaw=yaw,
                           sigma_xy_m=self.sigma_xy, sigma_yaw_rad=self.sigma_yaw,
                           source=self.source)

    def why_not(self, odom_ok: bool) -> str:
        """为什么不可信;可信是空串。"""
        if not self.anchored:
            return self.reason or "还没设位置"
        if not odom_ok:
            return "运控里程不新鲜(旁路进程断了?)"
        if self.sigma_xy > SIGMA_LOST_M:
            return (f"设位置之后走了 {self._dist:.1f} m,位置偏差可能到 {self.sigma_xy:.1f} m,"
                    f"要重新设位置")
        return ""

    def ok(self, odom_ok: bool) -> bool:
        return not self.why_not(odom_ok)

    def quality(self, odom_ok: bool) -> float:
        """0–1:丢了是 0;越走越低(1 − σ/线)。"""
        if not self.ok(odom_ok):
            return 0.0
        return 1.0 if self.identity else max(0.0, 1.0 - self.sigma_xy / SIGMA_LOST_M)

    def to_wire(self, odom_ok: bool) -> dict[str, Any]:
        """遥测里的 ``loc`` 块(站点、手机显示)。"""
        return {"source": self.source, "anchored": self.anchored,
                "sigma_m": round(self.sigma_xy, 2), "reason": self.why_not(odom_ok)}
