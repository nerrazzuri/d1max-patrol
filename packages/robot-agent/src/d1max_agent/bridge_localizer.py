"""定位来源:本机桥上的定位器(W09a,W08 决定 3、4)。接口跟
:class:`~d1max_agent.localization.OdomAnchor` 一样(导航桥、设位置、标原点、遥测都按这一套用),另加
异步的 :meth:`BridgeLocalizer.relocalize`。

**不做 I/O**:定位器的报文由本机桥(``locbridge.py``)喂进来(``on_connect``、``on_pose``、
``on_state``、``on_disconnect``),里程由导航桥每拍喂(``update``);要问定位器的(换先验、重定位)
经 ``link.request``。

能不能信(按下面的顺序,第一条不满足的就是原因):

1. 定位器连着(握过手);
2. 当前这张图的先验它换好了(``set_prior`` 回了 ok);
3. 它自己没说 ``initializing``/``lost``;没在按人给的位置重定位;
4. 最近一帧在当前这张图上;
5. 没在跳变之后的稳定期里(跳变:它自己报 ``jump``,或相邻两帧它的位移跟里程对不上;要连着
   :data:`SETTLE_FIXES` 帧对得上才恢复,恢复时把修正量交给引擎 —— 来路、出发点按它挪);
6. 新鲜:按**代理收到的单调钟**,:data:`FRESH_S` 内正常;再往后到 :data:`DR_MAX_S`、里程推算不超过
   :data:`DR_MAX_M` 照用(来源 ``dead_reckoning``);里程不新鲜、一拍跳了都不推;
7. σ_xy 不过 :data:`SIGMA_LOST_M`(推算时按走过的距离涨)。

两次定位之间的地图位姿 = 最近一帧 ∘(那一刻的里程)⁻¹ ∘ 此刻的里程。
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from d1max_agent.localization import (
    DRIFT_XY_PER_M,
    DRIFT_YAW_PER_M,
    JUMP_M,
    JUMP_YAW_RAD,
    LocEstimate,
    _wrap,
    compose,
    inverse,
)
from d1max_contract.locbridge import Pose, Relocalize, SetPrior, State

log = logging.getLogger(__name__)

#: 代理收到之后多久内算新鲜(秒)。
FRESH_S = 0.5
#: 不新鲜之后还能拿里程往前推多久、多远(先到先停,W08 决定 3)。
DR_MAX_S = 2.0
DR_MAX_M = 1.0
#: σ_xy 过这条线就不可信(点云匹配正常在 0.3 m 以内;标原点还要 ≤ 0.5 m)。
SIGMA_LOST_M = 1.0
#: 跳变之后要连着这么多帧前后对得上才恢复。
SETTLE_FIXES = 5
#: 相邻两帧:定位器的位移跟里程的位移差超过 XCHECK_M + XCHECK_FRAC × 里程位移,或朝向差超过
#: XCHECK_YAW_RAD,就当跳了。
XCHECK_M = 0.3
XCHECK_FRAC = 0.2
XCHECK_YAW_RAD = 0.3
#: 人给位置时告诉定位器的初值不确定度(米)。
RELOC_SIGMA_M = 0.5
RELOC_TIMEOUT_S = 5.0
PRIOR_TIMEOUT_S = 10.0


class LocalizerUnavailable(RuntimeError):
    """定位器没连上(``link.request`` 抛)。"""


@dataclass(frozen=True)
class _Fix:
    pose: tuple[float, float, float]
    sigma_xy: float
    sigma_yaw: float
    source: str
    at: float                                        # 代理收到时的单调钟
    odom: tuple[float, float, float] | None          # 那一刻的里程(不新鲜是 None)
    epoch: int                                       # 那一刻的里程段(断过、跳过就换一段)

    @property
    def T(self) -> tuple[float, float, float] | None:
        """地图系 ← 里程系。"""
        return None if self.odom is None else compose(self.pose, inverse(self.odom))


class BridgeLocalizer:
    identity = False

    def __init__(self, *, monotonic: Callable[[], float]) -> None:
        self._now = monotonic
        #: 本机桥(``request(make, timeout_s) -> Reply``,没连上抛 :class:`LocalizerUnavailable`)。
        self.link: Any = None
        #: 跳变稳下来、人重定位完:修正量(新 ``T`` ∘ 旧 ``T``⁻¹;给不出是 ``None``)交给引擎。
        self.on_corrected: Callable[[tuple[float, float, float] | None], None] | None = None
        self.prior_task: asyncio.Task | None = None
        self._connected = False
        self._map: tuple[str, str] | None = None
        self._dir = ""
        self._prior_ok: tuple[str, str] | None = None
        self._prior_err = ""
        self._state = ""
        self._state_reason = ""
        self._fix: _Fix | None = None
        self._wrong_map = False
        self._settle = 0
        self._settle_why = ""
        self._pre: _Fix | None = None
        self._reloc_pending: int | None = None
        self._odom: tuple[float, float, float] | None = None
        self._epoch = 0
        self._dr = 0.0

    # ------------------------------------------------------------ 本机桥喂进来的

    def on_connect(self) -> None:
        self._connected = True
        self._reset_fix()
        self._send_prior()

    def on_disconnect(self) -> None:
        self._connected = False
        self._prior_ok = None
        self._state = ""
        self._reset_fix()

    def on_state(self, s: State) -> None:
        self._state, self._state_reason = s.state, s.reason

    def on_pose(self, p: Pose) -> None:
        if (p.map_id, p.map_version) != self._map:
            self._wrong_map = True
            return
        self._wrong_map = False
        odom = self._odom
        new = _Fix(pose=(p.x, p.y, p.yaw), sigma_xy=p.sigma_xy, sigma_yaw=p.sigma_yaw,
                   source=p.source, at=self._now(), odom=odom, epoch=self._epoch)
        prev, self._fix = self._fix, new
        self._dr = 0.0
        if self._reloc_pending is not None and p.reloc_id == self._reloc_pending:
            self._reloc_pending = None
            self._jumped(prev, "定位器按人给的位置重定位了,等它稳下来")
            return
        if p.jump:
            self._jumped(prev, "定位器刚跳过(重定位或匹配跳变),等它稳下来")
            return
        bad = self._mismatch(prev, new)
        if bad:
            self._jumped(prev, f"定位器跟里程对不上({bad}),等它稳下来")
            return
        if self._settle > 0:
            self._settle -= 1
            if self._settle == 0:
                self._settled(new)

    # ------------------------------------------------------------ 代理这边

    @property
    def source(self) -> str:
        f = self._fix
        if f is None:
            return "localizer"
        return "dead_reckoning" if self._now() - f.at > FRESH_S else f.source

    @property
    def anchored(self) -> bool:
        return self._fix is not None and self._prior_ok == self._map and self._map is not None

    @property
    def map_ref(self) -> tuple[str, str] | None:
        return self._map

    def on_map(self, map_ref: tuple[str, str] | None, dir: str = "") -> None:
        """狗换了(或刚载入)一张图:以前的位置作废,请定位器换先验。"""
        if map_ref == self._map and dir == self._dir and self._prior_ok == map_ref:
            return
        self._map, self._dir = map_ref, dir
        self._prior_ok = None
        self._prior_err = ""
        self._reset_fix()
        self._send_prior()

    def update(self, odom: tuple[float, float, float], odom_ok: bool = True) -> None:
        """每拍一帧里程:两次定位之间推算用。断了、一拍跳了都换一段(推算与修正量都不跨段)。"""
        if not odom_ok:
            if self._odom is not None:
                self._epoch += 1
            self._odom = None
            return
        last, self._odom = self._odom, odom
        if last is None:
            return
        d = math.hypot(odom[0] - last[0], odom[1] - last[1])
        if d > JUMP_M or abs(_wrap(odom[2] - last[2])) > JUMP_YAW_RAD:
            self._epoch += 1
            return
        self._dr += d

    @property
    def sigma_xy(self) -> float:
        f = self._fix
        return SIGMA_LOST_M if f is None else f.sigma_xy + self._dr * DRIFT_XY_PER_M

    @property
    def sigma_yaw(self) -> float:
        f = self._fix
        return math.pi if f is None else f.sigma_yaw + self._dr * DRIFT_YAW_PER_M

    def _pushable(self) -> bool:
        """最近一帧能不能拿此刻的里程往前推(同一段里程、推算没超上限)。"""
        f = self._fix
        return (f is not None and f.odom is not None and self._odom is not None
                and f.epoch == self._epoch and self._dr <= DR_MAX_M)

    def estimate(self, odom: tuple[float, float, float]) -> LocEstimate | None:
        f, m = self._fix, self._map
        if f is None or m is None:
            return None
        if self._pushable():
            x, y, yaw = compose(f.T, odom)
        else:
            x, y, yaw = f.pose
        return LocEstimate(map_id=m[0], map_version=m[1], x=x, y=y, yaw=yaw,
                           sigma_xy_m=self.sigma_xy, sigma_yaw_rad=self.sigma_yaw,
                           source=self.source)

    def why_not(self, odom_ok: bool) -> str:
        if not self._connected:
            return "定位器没连上"
        if self._map is None:
            return "没有加载地图"
        if self._prior_ok != self._map:
            return f"定位器换不了这张图的先验:{self._prior_err}" if self._prior_err \
                else "定位器在换图"
        if self._state in ("initializing", "lost"):
            what = "还在初始化" if self._state == "initializing" else "说定位丢了"
            return f"定位器{what}" + (f":{self._state_reason}" if self._state_reason else "")
        if self._reloc_pending is not None:
            return "定位器在按人给的位置重定位"
        if self._wrong_map:
            return "定位器在别的图上"
        f = self._fix
        if f is None:
            return "定位器还没给出位置"
        if self._settle > 0:
            return self._settle_why
        age = self._now() - f.at
        if age > FRESH_S and not (odom_ok and age <= DR_MAX_S and self._pushable()):
            return f"定位器 {age:.1f} 秒没来位姿"
        if self.sigma_xy > SIGMA_LOST_M:
            return f"定位偏差可能到 {self.sigma_xy:.1f} m"
        return ""

    def ok(self, odom_ok: bool) -> bool:
        return not self.why_not(odom_ok)

    def quality(self, odom_ok: bool) -> float:
        if not self.ok(odom_ok):
            return 0.0
        return max(0.0, 1.0 - self.sigma_xy / SIGMA_LOST_M)

    def to_wire(self, odom_ok: bool) -> dict[str, Any]:
        return {"source": self.source, "anchored": self.anchored,
                "sigma_m": round(self.sigma_xy, 2), "reason": self.why_not(odom_ok),
                "localizer": "bridge"}

    def health(self) -> Any:
        """升级前检查的一项(W00c6d):连着、新鲜、没丢定位。"""
        from d1max_agent.release_precheck import SourceCheck
        why = self.why_not(self._odom is not None)
        return SourceCheck("定位器", not why, why or "连着、定位可信")

    async def relocalize(self, map_ref: tuple[str, str], pose: tuple[float, float, float],
                         sigma_xy: float = RELOC_SIGMA_M) -> str:
        """请定位器按人给的位姿(初值)重定位。收下回空串;否则回拒绝原因。收敛看之后的帧。"""
        def make(req: int) -> Relocalize:
            return Relocalize(req=req, map_id=map_ref[0], map_version=map_ref[1], x=pose[0],
                              y=pose[1], yaw=pose[2], sigma_xy=sigma_xy)
        try:
            r = await self._ask(make, RELOC_TIMEOUT_S)
        except LocalizerUnavailable as exc:
            return f"localizer_unavailable: {exc}"
        except asyncio.TimeoutError:
            return f"localizer_unavailable: 定位器 {RELOC_TIMEOUT_S:g} 秒没回"
        if not r.ok:
            return f"localizer_refused: {r.reason}"[:200]
        self._reloc_pending = r.req
        return ""

    # ------------------------------------------------------------ 内部

    async def _ask(self, make: Callable[[int], Any], timeout_s: float) -> Any:
        if self.link is None:
            raise LocalizerUnavailable("没有本机桥")
        return await self.link.request(make, timeout_s)

    def _reset_fix(self) -> None:
        self._fix = None
        self._wrong_map = False
        self._settle = 0
        self._pre = None
        self._reloc_pending = None
        self._dr = 0.0

    def _send_prior(self) -> None:
        if not self._connected or self._map is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.prior_task = loop.create_task(self._prior(self._map, self._dir))

    async def _prior(self, ref: tuple[str, str], dir: str) -> None:
        def make(req: int) -> SetPrior:
            return SetPrior(req=req, map_id=ref[0], map_version=ref[1], dir=dir)
        try:
            r = await self._ask(make, PRIOR_TIMEOUT_S)
        except LocalizerUnavailable:
            return                                   # 连上时再发
        except asyncio.TimeoutError:
            if self._map == ref:
                self._prior_err = f"{PRIOR_TIMEOUT_S:g} 秒没回"
            return
        if self._map != ref:
            return                                   # 等回复的时候又换了图
        if r.ok:
            self._prior_ok, self._prior_err = ref, ""
        else:
            self._prior_err = r.reason or "没说为什么"

    def _mismatch(self, prev: _Fix | None, new: _Fix) -> str:
        """相邻两帧:定位器的位移跟里程的位移对不对得上。对不上回一句说明,对得上(或没法比)回空串。"""
        if prev is None or prev.odom is None or new.odom is None or prev.epoch != new.epoch:
            return ""
        d_loc = math.hypot(new.pose[0] - prev.pose[0], new.pose[1] - prev.pose[1])
        d_odom = math.hypot(new.odom[0] - prev.odom[0], new.odom[1] - prev.odom[1])
        gap = abs(d_loc - d_odom)
        if gap > XCHECK_M + XCHECK_FRAC * d_odom:
            return f"位移差 {gap:.2f} m"
        turn_loc = _wrap(new.pose[2] - prev.pose[2])
        turn_odom = _wrap(new.odom[2] - prev.odom[2])
        if abs(_wrap(turn_loc - turn_odom)) > XCHECK_YAW_RAD:
            return f"朝向差 {math.degrees(abs(_wrap(turn_loc - turn_odom))):.0f}°"
        return ""

    def _jumped(self, prev: _Fix | None, why: str) -> None:
        if self._settle == 0:
            self._pre = prev                          # 跳之前那一帧:算修正量用
        self._settle = SETTLE_FIXES
        self._settle_why = why

    def _settled(self, new: _Fix) -> None:
        pre, self._pre = self._pre, None
        delta = None
        if pre is not None and pre.T is not None and new.T is not None and pre.epoch == new.epoch:
            delta = compose(new.T, inverse(pre.T))
        log.info("定位器稳下来了,修正量 %s", delta)
        if self.on_corrected is not None:
            self.on_corrected(delta)
