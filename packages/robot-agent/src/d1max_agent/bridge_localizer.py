"""定位来源:本机桥上的定位器(W09a,W08 决定 3、4)。接口跟
:class:`~d1max_agent.localization.OdomAnchor` 一样(导航桥、设位置、标原点、遥测都按这一套用),另加
异步的 :meth:`BridgeLocalizer.relocalize` 与 :meth:`BridgeLocalizer.reset`。

**不做 I/O**:定位器的报文由本机桥(``locbridge.py``)喂进来(``on_connect``、``on_pose``、
``on_state``、``on_disconnect``),里程由导航桥每拍喂(``update``);要问定位器的(换先验、重定位)
经 ``link.request``。

能不能信(按下面的顺序,第一条不满足的就是原因):

1. 定位器连着(握过手);
2. 当前这张图的先验它换好了(``set_prior`` 回了 ok);
3. 它自己没说 ``initializing``/``lost``;没在重定位(收下了、还没出那一帧);
4. 最近一帧在当前这张图上;
5. 没在跳变之后的稳定期里(跳变:它自己报 ``jump``、重定位完、连上或换图后的第一帧,或者跟里程对
   不上;要连着 :data:`SETTLE_FIXES` 帧对得上才恢复,恢复时把修正量交给引擎 —— 来路、出发点按它挪);
6. 新鲜:这一帧的位置是多久以前的 = 代理收到到现在 + 比平常晚到的 + 定位器自己推算的
   (``meas_age_ms``);:data:`FRESH_S` 内正常;再往后到 :data:`DR_MAX_S`、推算的路不超过
   :data:`DR_MAX_M` 照用(来源 ``dead_reckoning``);里程不新鲜、一拍跳了都不推;
7. σ_xy 不过 :data:`SIGMA_LOST_M`(推算时按走过的路涨)。

**跟里程对不上**(交叉校验):这一帧跟上次跳变以来、:data:`XCHECK_WINDOW_S` 内收到的每一帧比,定位器
的相对位移跟里程的相对位移差超过 :data:`XCHECK_M` + :data:`XCHECK_FRAC` × 这期间里程走过的路,或者
朝向差超过 :data:`XCHECK_YAW_RAD`。按窗口比(不只比相邻两帧):定位器冻住(一直报同一个位置)、慢慢漂走
都抓得住。

**位姿配里程**:定位器的时间戳跟代理的钟不是一个钟,但差是定的 —— 「收到时刻 − 时间戳」比近
:data:`LAG_WINDOW_S` 里最小的那个大多少,这一帧就比平常晚到了多少;配那一刻的里程(代理留着近几秒的
里程)。卡了一下之后一批一起到的,各配各的,不会被当成跳、也不会算错修正量。

两次定位之间的地图位姿 = 最近一帧 ∘(配上的里程)⁻¹ ∘ 此刻的里程。
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
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

#: 这一帧的位置是多久以前的:这么久以内算新鲜(秒)。
FRESH_S = 0.5
#: 不新鲜之后还能拿里程往前推多久、多远(先到先停,W08 决定 3;定位器自己推算的也算在里面)。
DR_MAX_S = 2.0
DR_MAX_M = 1.0
#: σ_xy 过这条线就不可信(点云匹配正常在 0.3 m 以内;标原点还要 ≤ 0.5 m)。
SIGMA_LOST_M = 1.0
#: 跳变之后要连着这么多帧前后对得上才恢复。
SETTLE_FIXES = 5
#: 交叉校验(见模块说明)。
XCHECK_M = 0.3
XCHECK_FRAC = 0.2
XCHECK_YAW_RAD = 0.3
XCHECK_WINDOW_S = 5.0
#: 「比平常晚到多少」按近这么久里最小的「收到时刻 − 时间戳」算(秒)。
LAG_WINDOW_S = 10.0
#: 配里程:离那一刻最近的里程样本差过这么多就配不上(秒)。
PAIR_TOL_S = 0.25
#: 代理留多久的里程(秒):配得上的最晚到 + 定位器自己推算的,超过 DR_MAX_S 本来也不能用了。
ODOM_KEEP_S = DR_MAX_S + 1.0
#: 时间戳往回跳超过这么多(秒):当定位器的钟被拨过(对时),从头算;往回不到这么多的当重发、乱序丢掉。
STAMP_STEP_S = 1.0
#: 人给位置时告诉定位器的初值不确定度(米)。
RELOC_SIGMA_M = 0.5
#: 丢定位后引擎要重置时,按最后可信的位置请定位器重定位,初值不确定度(米)。
AUTO_RELOC_SIGMA_M = 1.0
#: 重定位只等「收下没有」(收敛看之后的帧);设位置是在命令锁里等的,不能久。
RELOC_TIMEOUT_S = 1.0
#: 换先验等回复(回复只说收下没有;载入进度走 ``status initializing``);没回就再发。
PRIOR_TIMEOUT_S = 10.0


class LocalizerUnavailable(RuntimeError):
    """定位器没连上(``link.request`` 抛)。"""


@dataclass(frozen=True)
class _Odo:
    at: float                                        # 单调钟
    pose: tuple[float, float, float]
    epoch: int                                       # 里程段(断过、跳过就换一段)
    path: float                                      # 到这一刻里程累计走过的路(不算跳的那一下)


@dataclass(frozen=True)
class _Fix:
    pose: tuple[float, float, float]
    sigma_xy: float
    sigma_yaw: float
    source: str
    at: float                                        # 代理收到时的单调钟
    born: float                                      # 定位器最后一次真匹配上的时刻(代理的钟)
    odom: tuple[float, float, float] | None          # 配上的里程(配不上是 None)
    epoch: int                                       # 配上的那一段里程
    path: float                                      # 配上那一刻里程累计走过的路
    dr0: float                                       # 真匹配上之后到配上那一刻走过的路(它推算的)

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
        #: 跳变稳下来、重定位完:``on_corrected(修正量, 人给的)``。修正量 = 新 ``T`` ∘ 旧 ``T``⁻¹
        #: (给不出是 ``None``);``人给的`` 为真 = 人设位置触发的(引擎据此把丢定位的次数从头算)。
        self.on_corrected: Callable[[tuple[float, float, float] | None, bool], None] | None = None
        self.prior_task: asyncio.Task | None = None
        self._connected = False
        self._map: tuple[str, str] | None = None
        self._dir = ""
        self._prior_ok: tuple[str, str] | None = None
        self._prior_err = ""
        self._state = ""
        self._state_reason = ""
        self._fix: _Fix | None = None
        #: 修正量的参照:不在稳定期时最近那一帧(稳下来那一帧也算)。断开不清(连回来按它算修正量),
        #: 换图清。
        self._ref: _Fix | None = None
        #: 最近一帧可信的(σ 在线内、定位器没说丢):丢定位后自动重定位的初值。断开不清,换图清。
        self._good: _Fix | None = None
        self._wrong_map = False
        self._settle = 0
        self._settle_why = ""
        self._settle_human = False
        #: 交叉校验的窗口:上次跳变以来、配上了里程的帧。
        self._win: deque[_Fix] = deque()
        #: 在等的重定位:(请求号, 人给的);收下之前就登记(回复跟那一帧挨着到也不漏)。
        self._reloc: tuple[int, bool] | None = None
        self._stamp: float | None = None
        self._lags: deque[tuple[float, float]] = deque()
        self._odom: tuple[float, float, float] | None = None
        self._odos: deque[_Odo] = deque()
        self._epoch = 0
        self._path = 0.0

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
        now = self._now()
        stamp = p.stamp_ns / 1e9
        if self._stamp is not None and stamp <= self._stamp:
            if self._stamp - stamp <= STAMP_STEP_S:
                return                               # 重发的、乱序的
            log.warning("定位器的时间戳往回跳了 %.1f s:当它的钟被拨过,从头算", self._stamp - stamp)
            self._lags.clear()
        self._stamp = stamp
        self._wrong_map = False
        new = self._pair(p, now, stamp)
        prev, self._fix = self._fix, new
        if self._reloc is not None and p.reloc_id is not None and p.reloc_id >= self._reloc[0]:
            human = self._reloc[1]
            self._reloc = None
            self._jumped(new, "定位器按人给的位置重定位了,等它稳下来" if human
                         else "定位器重定位了,等它稳下来", human=human)
            return
        if p.jump:
            self._jumped(new, "定位器刚跳过(重定位或匹配跳变),等它稳下来")
            return
        if prev is None:
            self._jumped(new, "定位器刚连上(或刚换图),等它稳下来")
            return
        bad = self._mismatch(new)
        if bad:
            self._jumped(new, f"定位器跟里程对不上({bad}),等它稳下来")
            return
        self._win.append(new)
        if self._settle > 0:
            self._settle -= 1
            if self._settle == 0:
                self._settled(new)
            return
        self._ref = new
        if self._trusted(new):
            self._good = new

    # ------------------------------------------------------------ 代理这边

    @property
    def source(self) -> str:
        f = self._fix
        if f is None:
            return "localizer"
        return "dead_reckoning" if self._now() - f.born > FRESH_S else f.source

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
        if map_ref != self._map:
            self._ref = self._good = None            # 别的图:旧位置不能当参照
        self._map, self._dir = map_ref, dir
        self._prior_ok = None
        self._prior_err = ""
        self._reset_fix()
        self._send_prior()

    def update(self, odom: tuple[float, float, float], odom_ok: bool = True) -> None:
        """每拍一帧里程:配位姿、推算用。断了、一拍跳了都换一段(推算、交叉校验、修正量都不跨段)。"""
        if not odom_ok:
            if self._odom is not None:
                self._epoch += 1
            self._odom = None
            return
        last, self._odom = self._odom, odom
        if last is not None:
            d = math.hypot(odom[0] - last[0], odom[1] - last[1])
            if d > JUMP_M or abs(_wrap(odom[2] - last[2])) > JUMP_YAW_RAD:
                self._epoch += 1
            else:
                self._path += d
        now = self._now()
        self._odos.append(_Odo(at=now, pose=odom, epoch=self._epoch, path=self._path))
        while self._odos and now - self._odos[0].at > ODOM_KEEP_S:
            self._odos.popleft()

    def _dr(self) -> float:
        """最近一帧真匹配上之后里程走过的路(定位器自己推算的 + 代理推算的)。"""
        f = self._fix
        return 0.0 if f is None else max(0.0, self._path - f.path) + f.dr0

    @property
    def sigma_xy(self) -> float:
        f = self._fix
        return SIGMA_LOST_M if f is None else f.sigma_xy + self._dr() * DRIFT_XY_PER_M

    @property
    def sigma_yaw(self) -> float:
        f = self._fix
        return math.pi if f is None else f.sigma_yaw + self._dr() * DRIFT_YAW_PER_M

    def _pushable(self) -> bool:
        """最近一帧能不能拿此刻的里程往前推(配上了里程、同一段、推算没超上限)。"""
        f = self._fix
        return (f is not None and f.odom is not None and self._odom is not None
                and f.epoch == self._epoch and self._dr() <= DR_MAX_M)

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
        if self._reloc is not None:
            return "定位器在按人给的位置重定位" if self._reloc[1] else "定位器在重定位"
        if self._wrong_map:
            return "定位器在别的图上"
        f = self._fix
        if f is None:
            return "定位器还没给出位置"
        if self._settle > 0:
            return self._settle_why
        now = self._now()
        age = now - f.born
        if age > FRESH_S and not (odom_ok and age <= DR_MAX_S and self._pushable()):
            if now - f.at > FRESH_S:
                return f"定位器 {now - f.at:.1f} 秒没来位姿"
            return f"定位器给的位置是 {age:.1f} 秒前的(晚到或它自己在推算)"
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
                         sigma_xy: float = RELOC_SIGMA_M, *, human: bool = True) -> str:
        """请定位器按给的位姿(初值)重定位。收下回空串;否则回拒绝原因。收敛看之后的帧(带这次的
        ``reloc_id`` 那一帧按跳变稳下来)。

        请求号**发出去之前**就登记:回复跟那一帧挨着到的时候,那一帧可能先被读进来。"""
        mine: list[int] = []

        def make(req: int) -> Relocalize:
            mine.append(req)
            self._reloc = (req, human)
            return Relocalize(req=req, map_id=map_ref[0], map_version=map_ref[1], x=pose[0],
                              y=pose[1], yaw=pose[2], sigma_xy=sigma_xy)
        try:
            r = await self._ask(make, RELOC_TIMEOUT_S)
        except LocalizerUnavailable as exc:
            self._forget_reloc(mine)
            return f"localizer_unavailable: {exc}"
        except asyncio.TimeoutError:
            self._forget_reloc(mine)
            return f"localizer_unavailable: 定位器 {RELOC_TIMEOUT_S:g} 秒没回"
        if self._map != map_ref:
            self._forget_reloc(mine)
            return "busy: 等定位器回复的时候换了图"
        if not r.ok:
            self._forget_reloc(mine)
            return f"localizer_refused: {r.reason}"[:200]
        return ""

    async def reset(self) -> None:
        """丢定位后引擎要重置(W08 决定 4:``reset_localization`` 的真实现):请定位器在最后可信的
        位置附近重定位。没有可信过的位置、不在线、拒了都只记日志 —— 引擎接着等定位回来。"""
        g, m = self._good, self._map
        if g is None or m is None:
            log.info("丢定位后的重置:没有可信过的位置,等定位器自己找回来")
            return
        if g.odom is not None and self._odom is not None and g.epoch == self._epoch:
            pose = compose(g.T, self._odom)
        else:
            pose = g.pose
        why = await self.relocalize(m, pose, AUTO_RELOC_SIGMA_M, human=False)
        if why:
            log.warning("丢定位后请定位器在 (%.2f, %.2f) 附近重定位,没成:%s", pose[0], pose[1], why)

    # ------------------------------------------------------------ 内部

    async def _ask(self, make: Callable[[int], Any], timeout_s: float) -> Any:
        if self.link is None:
            raise LocalizerUnavailable("没有本机桥")
        return await self.link.request(make, timeout_s)

    def _forget_reloc(self, mine: list[int]) -> None:
        if mine and self._reloc is not None and self._reloc[0] == mine[0]:
            self._reloc = None

    def _reset_fix(self) -> None:
        self._fix = None
        self._wrong_map = False
        self._settle = 0
        self._settle_human = False
        self._win.clear()
        self._reloc = None
        self._stamp = None
        self._lags.clear()

    def _odo_at(self, t: float, tol: float | None) -> _Odo | None:
        """离 ``t`` 最近的里程样本(一样近取后来的);``tol`` 给了就要在这么近以内。"""
        best = None
        for s in self._odos:
            if best is None or abs(s.at - t) <= abs(best.at - t):
                best = s
        if best is None or (tol is not None and abs(best.at - t) > tol):
            return None
        return best

    def _pair(self, p: Pose, now: float, stamp: float) -> _Fix:
        """这一帧比平常晚到了多少,配那一刻的里程;再往前 ``meas_age_ms`` 是它最后一次真匹配上。"""
        lag = now - stamp
        self._lags.append((now, lag))
        while now - self._lags[0][0] > LAG_WINDOW_S:
            self._lags.popleft()
        late = lag - min(v for _, v in self._lags)
        t_pair = now - late
        age = p.meas_age_ms / 1000
        s = self._odo_at(t_pair, PAIR_TOL_S)
        if s is None:
            odom, epoch, path, dr0 = None, self._epoch, self._path, 0.0
        else:
            odom, epoch, path = s.pose, s.epoch, s.path
            m = self._odo_at(t_pair - age, None) if age > 0 else s
            dr0 = max(0.0, path - m.path) if m is not None else 0.0
        return _Fix(pose=(p.x, p.y, p.yaw), sigma_xy=p.sigma_xy, sigma_yaw=p.sigma_yaw,
                    source=p.source, at=now, born=t_pair - age, odom=odom, epoch=epoch,
                    path=path, dr0=dr0)

    def _trusted(self, f: _Fix) -> bool:
        return f.sigma_xy <= SIGMA_LOST_M and self._state not in ("initializing", "lost")

    def _send_prior(self) -> None:
        if not self._connected or self._map is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.prior_task is not None and not self.prior_task.done():
            self.prior_task.cancel()
        self.prior_task = loop.create_task(self._prior(self._map, self._dir))

    async def _prior(self, ref: tuple[str, str], dir: str) -> None:
        """换先验:没回就再发(定位器可能在忙着载入上一张);拒了停(原因给人看),断了等连上再发。"""
        def make(req: int) -> SetPrior:
            return SetPrior(req=req, map_id=ref[0], map_version=ref[1], dir=dir)
        while self._connected and self._map == ref:
            try:
                r = await self._ask(make, PRIOR_TIMEOUT_S)
            except LocalizerUnavailable:
                return                               # 连上时再发
            except asyncio.TimeoutError:
                if self._map == ref:
                    self._prior_err = f"{PRIOR_TIMEOUT_S:g} 秒没回,再发一次"
                continue
            if self._map != ref:
                return                               # 等回复的时候又换了图
            if r.ok:
                self._prior_ok, self._prior_err = ref, ""
            else:
                self._prior_err = r.reason or "没说为什么"
            return

    def _mismatch(self, new: _Fix) -> str:
        """这一帧跟窗口里的每一帧比:定位器的相对位移跟里程的对不对得上。对不上回一句说明,对得上
        (或没法比)回空串。"""
        while self._win and new.at - self._win[0].at > XCHECK_WINDOW_S:
            self._win.popleft()
        if new.odom is None:
            return ""
        for old in self._win:
            if old.odom is None or old.epoch != new.epoch:
                continue
            rl = compose(inverse(old.pose), new.pose)
            ro = compose(inverse(old.odom), new.odom)
            gap = math.hypot(rl[0] - ro[0], rl[1] - ro[1])
            if gap > XCHECK_M + XCHECK_FRAC * (new.path - old.path):
                return f"位移差 {gap:.2f} m"
            turn = abs(_wrap(rl[2] - ro[2]))
            if turn > XCHECK_YAW_RAD:
                return f"朝向差 {math.degrees(turn):.0f}°"
        return ""

    def _jumped(self, new: _Fix, why: str, *, human: bool = False) -> None:
        """跳了(或刚连上、刚重定位):从这一帧起重新数;修正量的参照还是跳之前最后可信的那一帧
        (稳定期里又跳也不换)。"""
        self._settle = SETTLE_FIXES
        self._settle_why = why
        self._settle_human = self._settle_human or human
        self._win.clear()
        self._win.append(new)

    def _settled(self, new: _Fix) -> None:
        ref, human = self._ref, self._settle_human
        self._settle_human = False
        delta = None
        if ref is not None and ref.T is not None and new.T is not None and ref.epoch == new.epoch:
            delta = compose(new.T, inverse(ref.T))
        self._ref = new                              # 引擎挪过了:以后的修正量从这一帧算
        if self._trusted(new):
            self._good = new
        log.info("定位器稳下来了,修正量 %s%s", delta, "(人给的位置)" if human else "")
        if self.on_corrected is not None:
            self.on_corrected(delta, human)
