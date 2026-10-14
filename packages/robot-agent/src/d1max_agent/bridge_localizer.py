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

**稳下来的地方跟锚对不对得上**(W35):交叉校验只看最近几秒 —— 定位器来回跳一阵(每跳一次窗口就清),
最后在错的地方稳住几帧,窗口里全是错的,照样过(2026-10-13 C40221 3d.9:人给偏 2 m 的初值,稳在错
8 m、朝向差 160° 的地方还报 σ 0.1,代理认了;狗一直没动)。所以跳变之后要认之前,拿「锚」按里程推到
现在,再比:锚 = 最近一帧可信的,或者刚收下的那次重定位给的位置(人给的、RTK 给的都算)。离推出来的
位置超过 :data:`ANCHOR_M`(重定位时是 max(它, 3σ);给的朝向不准,这时只比离给的位置多远、里程挪了
多远放宽多远)+ :data:`XCHECK_FRAC` × 这期间走过的路,或者朝向
差超过 :data:`ANCHOR_YAW_RAD`(加里程朝向漂的量)→ 不认,一直说「请重新设位置」,直到它自己回到
对得上的地方、或者有人(或 RTK)再给一次位置。

**没有能核的锚**(对账 S1,2026-10-14):原来「没有锚 / 锚跟这一帧不在同一段里程 / 锚没配上里程」就照旧
认 —— 代理刚重启、里程断过之后定位器再跳一下,稳在哪都认。现在:

- 稳下来那一帧**自己**没配上里程、锚是好的:不认,等下一帧配上了再核(一会儿就好);
- 别的没法比的情况,只有一种照旧认:代理手里**根本没有锚**,而且代理连上定位器以后它**一帧都没跳过**
  (没报 ``jump``、没重定位、没跟里程对不上)—— 这是「定位器一直连续在跟」,代理只是刚起来(W34:代理
  重启不用人重给位置;仿真定位器一连上就有位置)。跳过的记着,断开重连也不清(不然断一下就洗白了),
  到换图或者按锚核过一次才清;
- 其余(跳过 / 重定位过而没有锚、里程断过、锚当时没配上里程)一律「请重新设位置」,跟锚对不上一样卡着,
  等人(或 RTK)给位置。

**锚记在盘上**(给了 ``keep``):代理重启以后锚还在 —— 重启前最后可信的位置(或人刚给的)连同那一刻的
里程(运控自己的里程,代理重启不归零)。只拿来**核**,从不当成可信的位置用;同一次开机(``boot_id``)、
同一张图才读回,换图就删。这样「代理重启、这期间定位器跳到了别处」也抓得住,而定位器一直在跟的照样不用人。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
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
#: σ_xy 过这条线就不可信(点云匹配正常在 0.3 m 以内;标原点还要 ≤ 0.5 m)。W34:原来是 1.0 —— 2026-10-08
#: C40221 3d.8,杀掉 MOLA 自己重定位时 σ 从 1.5 往下降,还在 1.0 时代理就认了,对导航太松;收到 0.5。
SIGMA_LOST_M = 0.5
#: 可信、但变差了(对账 S6):σ 过这条线,自动走要限速(导航桥 ``hal_nav._vlim``)。正常点云匹配给
#: 0.1 m(ICP 质量 ≥ 0.8);过 0.25 m = 质量掉到 0.77 以下、或刚重定位完还在收敛(定位器的 σ 从给的
#: 初值 30 s 降下来,人给的 0.5 头 15 s 在线上)、或代理拿里程推了 0.7 m 以上。离不可信的线(0.5 m)
#: 还差一半 —— 变差了还能走,只是走慢点,不至于一路快跑到丢了才急停。
SIGMA_DEGRADED_M = 0.25
#: 变差过之后这么久照样算变差(秒):σ 在线上下抖、推算一阵一阵的,速度不跟着一快一慢。
DEGRADED_HOLD_S = 3.0
#: 跳变之后要连着这么多帧前后对得上才恢复。
SETTLE_FIXES = 5
#: 交叉校验(见模块说明)。
XCHECK_M = 0.3
XCHECK_FRAC = 0.2
XCHECK_YAW_RAD = 0.3
XCHECK_WINDOW_S = 5.0
#: 跳变之后稳下来的地方离「锚按里程推到现在」最多差这么多(见模块说明,W35)。3 m:人在地图上点位置
#: 偏 2 m、朝向偏 45° 要能收敛回来(3d.9 的做法);点云重新匹配上的正常修正在 1 m 以内
#: (2026-10-08 转圈那次 0.66 m、2026-10-13 雷达半挡 0.5 m)。
ANCHOR_M = 3.0
ANCHOR_YAW_RAD = math.pi / 2
#: 里程朝向站着也在漂(2026-10-08 C40221:约 0.002 rad/s);锚放久了朝向那条线按这个放宽。
ANCHOR_YAW_DRIFT_RPS = 0.005
#: 锚是重定位给的位置(不是定位器出的一帧)时,它的 ``source``。
GIVEN = "given"
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
#: 锚记在盘上(对账 S1):可信的帧一直在换锚(一秒十帧),盘上那份挪了这么远、转了这么多、或者隔了
#: 这么久才再写一次,两次之间最少隔 :data:`KEEP_MIN_S`。记的是「地图位姿 + 那一刻的里程」一对,哪一刻
#: 的都能按里程推到现在,所以不用写得很勤;勤一点只是让重启后推的那段路短一些(里程走得越远偏得越多)。
#: 换了种类(人给的 / 可信的帧)、换了里程段马上写。
KEEP_MOVE_M = 0.5
KEEP_TURN_RAD = 0.2
KEEP_EVERY_S = 30.0
KEEP_MIN_S = 1.0
KEEP_VERSION = 1
#: 这一次开机的号(Linux):记下的锚只在同一次开机里用(重开机之后里程、定位器都从头来)。
BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")


class LocalizerUnavailable(RuntimeError):
    """定位器没连上(``link.request`` 抛)。"""


def _read_boot_id() -> str:
    try:
        return BOOT_ID_FILE.read_text().strip()
    except OSError:
        return ""


def _finite(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"不是有限数:{v!r}")
    return float(v)


def _num3(v: Any) -> tuple[float, float, float]:
    if not isinstance(v, list) or len(v) != 3:
        raise ValueError(f"要三个数:{v!r}")
    return (_finite(v[0]), _finite(v[1]), _finite(v[2]))


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
    #: 丢了能自己找回来(雷达定位;W29:引擎只等 30 秒,等不回来叫人)。
    AUTO_RECOVERS = True

    def __init__(self, *, monotonic: Callable[[], float], keep: Path | str | None = None,
                 boot_id: str | None = None, odometry: bool = True) -> None:
        """``keep``:锚记在这个文件里(重启读回,只拿来核;``None`` = 不记)。``monotonic`` 要是整台机器
        同一个钟(``time.monotonic``:开机以来,代理重启不归零)—— 记下的锚按它算放了多久。``boot_id``:
        这一次开机的号(默认读 :data:`BOOT_ID_FILE`;读不到就不记)。``odometry`` 为假 = 根本没有里程
        (**只有离线回放**这么用,``replay.agent_view`` 没给里程时):核不了锚,稳下来就认(跟 W35 之前
        一样);真狗、仿真都有里程,不许这么用。"""
        self._now = monotonic
        self._odometry = odometry
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
        self._prior_rejected = False
        self._state = ""
        self._state_reason = ""
        self._fix: _Fix | None = None
        #: 修正量的参照:不在稳定期时最近那一帧(稳下来那一帧也算)。断开不清(连回来按它算修正量),
        #: 换图清。
        self._ref: _Fix | None = None
        #: 最近一帧可信的(σ 在线内、定位器没说丢):丢定位后自动重定位的初值。断开不清,换图清。
        self._good: _Fix | None = None
        #: 锚(W35):跳变之后稳下来的地方拿它按里程推到现在来比。最近一帧可信的,或者刚收下的那次
        #: 重定位给的位置;``_anchor_m`` 是位置那条线,``_anchor_what`` 是给人看的叫法。
        self._anchor: _Fix | None = None
        self._anchor_m = ANCHOR_M
        self._anchor_what = "最后可信的位置"
        #: 锚是哪一次重定位请求给的、还没有哪一帧按它稳下来过(请求号;没有是 ``None``)。这期间普通的
        #: 可信帧**不许**把锚盖掉(W35 外审 I1)。
        self._anchor_hold: int | None = None
        #: 稳住了、可是跟锚对不上(非空 = 卡在这儿,每来一帧再比一次)。
        self._off_why = ""
        #: 代理连上定位器以后它跳过(报 ``jump``、重定位、跟里程对不上),还没按锚核过一回(对账 S1):
        #: 没有能核的锚时,这种稳下来的不认。断开重连不清,换图、核过一回才清。
        self._unverified = False
        #: 锚记在盘上(对账 S1):文件、开机号、上次写的那一份和时刻;``_kept`` 是起来时读回的、还没
        #: 用上的(头一回 ``on_map`` 到同一张图才用)。
        self._keep = Path(keep) if keep is not None else None
        self._boot = (boot_id if boot_id is not None
                      else _read_boot_id() if self._keep is not None else "")
        if not self._boot:
            self._keep = None                        # 分不出重开过机没有:不记
        self._kept_fix: _Fix | None = None
        self._kept_at = -math.inf
        self._kept: tuple[tuple[str, str], _Fix, float, str] | None = self._load_keep()
        self._wrong_map = False
        #: RTK 核对说定位器不对(W09e,``RtkCheck``;代理每拍写):非空就不可信。
        self.rtk_disagree = ""
        #: 最近一次「变差了」(对账 S6):(时刻, 原因)。
        self._degraded: tuple[float, str] | None = None
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
        self._prior_rejected = False
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
            # 刚连上不算跳:定位器那边可能一直连续在跟(代理重启),核法见 ``_off_anchor``
            self._jumped(new, "定位器刚连上(或刚换图),等它稳下来", seen=False)
            return
        bad = self._mismatch(new)
        if bad:
            self._jumped(new, f"定位器跟里程对不上({bad}),等它稳下来")
            return
        self._win.append(new)
        if self._settle > 0:
            if self._settle == 1:
                was, self._off_why = self._off_why, self._off_anchor(new)
                if self._off_why:
                    if not was:
                        log.warning("%s", self._off_why)
                    return                           # 稳住了但对不上:不认,下一帧再比
            self._settle -= 1
            if self._settle == 0:
                self._settled(new)
            return
        self._ref = new
        if self._trusted(new):
            self._set_good(new)

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

    def prior_status(self, ref: tuple[str, str]) -> tuple[str, str]:
        """定位器对 ``ref`` 这张图先验的态度(R5,换图要等它):``("ok", "")`` 收下了;``("rejected",
        原因)`` 明说不收;``("pending", 原因)`` 还没回(没连上、在载、超时重发中;原因是最近一次的
        说明)。当前的图不是 ``ref`` 一律 pending(又换了图)。"""
        if self._map != ref:
            return "pending", "当前的图已经不是这一张"
        if self._prior_ok == ref:
            return "ok", ""
        if self._prior_rejected:
            return "rejected", self._prior_err
        if not self._connected:
            return "pending", "定位器没连上"
        return "pending", self._prior_err

    @property
    def map_ref(self) -> tuple[str, str] | None:
        return self._map

    def on_map(self, map_ref: tuple[str, str] | None, dir: str = "") -> None:
        """狗换了(或刚载入)一张图:以前的位置作废,请定位器换先验。"""
        if map_ref == self._map and dir == self._dir and self._prior_ok == map_ref:
            return
        if map_ref != self._map:
            self._ref = self._good = self._anchor = None   # 别的图:旧位置不能当参照
            self._anchor_m, self._anchor_what = ANCHOR_M, "最后可信的位置"
            self._anchor_hold = None
            self._unverified = False
            self.rtk_disagree = ""
            kept, self._kept = self._kept, None
            if kept is not None and map_ref is not None and kept[0] == map_ref:
                # 代理重启前记下的锚(同一次开机、同一张图):只拿来核(对账 S1)
                _, fix, self._anchor_m, what = kept
                self._anchor = replace(fix, epoch=self._epoch, path=self._path)
                self._anchor_what = f"代理重启前{what}"
                self._kept_fix, self._kept_at = self._anchor, self._now()
                log.info("读回代理重启前记下的锚:%s (%.2f, %.2f, %.2f),放了 %.0f 秒", what,
                         *fix.pose, self._now() - fix.at)
            else:
                self._save_keep()                    # 别的图(或没有图):记下的那份作废
        self._map, self._dir = map_ref, dir
        self._prior_ok = None
        self._prior_err = ""
        self._prior_rejected = False
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
        a = self._anchor
        if a is not None and a.odom is None and a.source == GIVEN and self._anchor_hold is not None:
            # 人给位置那一刻代理手里还没有里程(运控刚连上):狗是停着给的,头一个里程就当那一刻的,
            # 不然这个位置永远核不了(对账 S1)
            self._anchor = replace(a, odom=odom, epoch=self._epoch, path=self._path)
            self._save_keep()

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
        if self.rtk_disagree:
            return self.rtk_disagree
        f = self._fix
        if f is None:
            return "定位器还没给出位置"
        if self._off_why:
            return self._off_why
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

    def degraded(self, odom_ok: bool) -> str:
        """可信、但变差了(对账 S6):在拿里程推算(这一帧的位置过了 :data:`FRESH_S`:晚到,或定位器自己
        在推),或者 σ 过 :data:`SIGMA_DEGRADED_M`。回原因;没变差、不可信(导航本来就停)回空串。变差过
        的 :data:`DEGRADED_HOLD_S` 内照样算(说法后面带「刚变差过」)。"""
        if not self.ok(odom_ok):
            return ""
        now, f = self._now(), self._fix
        why = ""
        if f is not None and now - f.born > FRESH_S:
            why = f"在拿里程推算(定位器的位置是 {now - f.born:.1f} 秒前的)"
        elif self.sigma_xy > SIGMA_DEGRADED_M:
            why = f"定位偏差可能到 {self.sigma_xy:.2f} m"
        if why:
            self._degraded = (now, why)
            return why
        if self._degraded is not None and now - self._degraded[0] < DEGRADED_HOLD_S:
            return f"{self._degraded[1]}(刚变差过)"
        return ""

    def quality(self, odom_ok: bool) -> float:
        if not self.ok(odom_ok):
            return 0.0
        return max(0.0, 1.0 - self.sigma_xy / SIGMA_LOST_M)

    def to_wire(self, odom_ok: bool) -> dict[str, Any]:
        out = {"source": self.source, "anchored": self.anchored,
               "sigma_m": round(self.sigma_xy, 2), "reason": self.why_not(odom_ok),
               "localizer": "bridge"}
        degraded = self.degraded(odom_ok)
        if degraded:
            out["degraded"] = degraded                   # 对账 S6:自动走限着速(老站点不认,不碍事)
        return out

    def health(self) -> Any:
        """升级前检查的一项(W00c6d):连着、新鲜、没丢定位。"""
        from d1max_agent.release_precheck import SourceCheck
        why = self.why_not(self._odom is not None)
        return SourceCheck("定位器", not why, why or "连着、定位可信")

    async def relocalize(self, map_ref: tuple[str, str], pose: tuple[float, float, float],
                         sigma_xy: float = RELOC_SIGMA_M, *, human: bool = True) -> str:
        """请定位器按给的位姿(初值)重定位。收下回空串;否则回拒绝原因。收敛看之后的帧(带这次的
        ``reloc_id`` 那一帧按跳变稳下来)。

        请求号**发出去之前**就登记:回复跟那一帧挨着到的时候,那一帧可能先被读进来。

        **锚也在发出去之前就换成给的这个位置**(W35 外审 I1):原来等回执回来才换,回执之前到的那一帧
        和后面几帧稳定帧是按旧锚(或者没有锚)核的 —— 错解已经认下了、通知了引擎,回执回来再换锚也不会
        重新核;下一帧可信的还会把刚换的锚盖成那个错解。没成(拒了、没回、没连上)就退回原来的锚;
        等回复的时候换了图,新图上不留这个锚。"""
        mine: list[int] = []
        before = (self._anchor, self._anchor_m, self._anchor_what, self._anchor_hold)

        def make(req: int) -> Relocalize:
            mine.append(req)
            self._reloc = (req, human)
            now = self._now()
            self._anchor = _Fix(pose=(float(pose[0]), float(pose[1]), float(pose[2])),
                                sigma_xy=sigma_xy, sigma_yaw=0.0, source=GIVEN, at=now, born=now,
                                odom=self._odom, epoch=self._epoch, path=self._path, dr0=0.0)
            self._anchor_m = max(ANCHOR_M, 3 * sigma_xy)
            self._anchor_what = "人给的位置" if human else "重定位给的位置"
            self._anchor_hold = req
            self._off_why = ""
            self._save_keep()
            return Relocalize(req=req, map_id=map_ref[0], map_version=map_ref[1], x=pose[0],
                              y=pose[1], yaw=pose[2], sigma_xy=sigma_xy)
        def undo() -> None:
            """这次没成:请求不等了;锚还是这次给的那个(没被换图清掉、没被更新的请求换掉)就退回去。"""
            self._forget_reloc(mine)
            if mine and self._anchor_hold == mine[0]:
                self._anchor, self._anchor_m, self._anchor_what, self._anchor_hold = before
                self._off_why = ""
                self._save_keep()
        try:
            r = await self._ask(make, RELOC_TIMEOUT_S)
        except LocalizerUnavailable as exc:
            undo()
            return f"localizer_unavailable: {exc}"
        except asyncio.TimeoutError:
            undo()
            return f"localizer_unavailable: 定位器 {RELOC_TIMEOUT_S:g} 秒没回"
        if self._map != map_ref:
            undo()
            return "busy: 等定位器回复的时候换了图"
        if not r.ok:
            undo()
            return f"localizer_refused: {r.reason}"[:200]
        return ""

    async def reset(self) -> None:
        """丢定位后引擎要重置(W08 决定 4:``reset_localization`` 的真实现):请定位器在最后可信的
        位置附近重定位。没有可信过的位置、不在线、拒了都只记日志 —— 引擎接着等定位回来。"""
        g, m = self._good, self._map
        if self.rtk_disagree:
            # RTK 说它不对:按 RTK 的位置重定位归代理的 RTK 核对那一路,这里不按(可能是错的)最后可信
            # 位置再请一次、盖掉那一路(W09e 内审应修 2)
            log.info("丢定位后的重置:RTK 说定位器不对,按 RTK 那一路重定位,这里不请")
            return
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
        self._off_why = ""
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
        # RTK 说它不对的时候不算「可信」:不然最后可信的位置一路跟着错的走,丢定位后的自动重定位
        # 又把它拉回错处(W09e 内审应修 2)
        return f.sigma_xy <= SIGMA_LOST_M and self._state not in ("initializing", "lost") \
            and not self.rtk_disagree

    @property
    def busy(self) -> bool:
        """正在重定位、或重定位 / 跳变之后还在等稳定(W09e:这时候不拿 RTK 核对)。稳住了但跟锚对不上
        (W35)不算:它不会自己好,RTK 核对要能来救。"""
        return self._reloc is not None or (self._settle > 0 and not self._off_why)

    def _set_good(self, f: _Fix) -> None:
        self._good = f
        if self._anchor_hold is not None:
            return                               # 刚给的锚还没人按它稳下来过:不许盖(W35 外审 I1)
        a = self._anchor
        if f.odom is None and a is not None and a.odom is not None and a.epoch == f.epoch:
            return                               # 这一帧没配上里程:留着同一段里能推的那个(对账 S1)
        self._anchor = f
        self._anchor_m, self._anchor_what = ANCHOR_M, "最后可信的位置"
        self._save_keep(force=False)

    def _off_anchor(self, new: _Fix) -> str:
        """稳下来的这一帧跟锚按里程推到现在的位置对不对得上;对不上(或没法比、不该照旧认)回一句说明,
        对得上回空串。没法比的时候怎么办见模块说明「没有能核的锚」(对账 S1)。"""
        if not self._odometry:
            return ""                            # 离线回放、没有里程:核不了(见 __init__)
        a = self._anchor
        if a is None or a.odom is None or a.epoch != new.epoch:
            if a is None and not self._unverified:
                return ""                        # 连上以后一直连续、代理手里也没有锚:照旧认(W34)
            if a is None:
                why = "代理手里没有可信过的位置"
            elif a.odom is None:
                why = f"{self._anchor_what}那会儿没配上里程"
            else:
                why = f"里程断过(换了段),推不到{self._anchor_what}"
            did = "跳过(或重定位过)" if self._unverified else "刚连上"
            return f"定位器{did},{why},核不了它稳住的地方:请重新设位置"
        if new.odom is None:
            return f"定位器稳住了,这一帧没配上里程:等配上了再跟{self._anchor_what}核对"
        want = compose(a.T, new.odom)
        if a.source == GIVEN:
            # 给的朝向可能偏几十度:拿它推「往哪走了」会越走越偏(2026-10-13 回放:给偏 45°、边走边
            # 收敛到真实位置的那一趟被冤枉了)。只比离给的位置多远,里程挪了多远就放宽多远
            gap = max(0.0, math.hypot(new.pose[0] - a.pose[0], new.pose[1] - a.pose[1])
                      - math.hypot(new.odom[0] - a.odom[0], new.odom[1] - a.odom[1]))
        else:
            gap = math.hypot(new.pose[0] - want[0], new.pose[1] - want[1])
        turn = abs(_wrap(new.pose[2] - want[2]))
        if gap > self._anchor_m + XCHECK_FRAC * max(0.0, new.path - a.path):
            bad = f"{gap:.1f} m"
        elif turn > ANCHOR_YAW_RAD + ANCHOR_YAW_DRIFT_RPS * max(0.0, new.at - a.at):
            bad = f"朝向 {math.degrees(turn):.0f}°"
        else:
            return ""
        return (f"定位器稳住的地方跟{self._anchor_what}差 {bad},里程说狗没这么动过:"
                "请重新设位置")

    # ------------------------------------------------------------ 锚记在盘上(对账 S1)

    def _save_keep(self, *, force: bool = True) -> None:
        """把锚写到盘上(没有锚、锚没配上里程就删掉)。``force`` 为假:按 :data:`KEEP_MOVE_M` 等几条
        攒着写(可信的帧一秒十帧)。写不进去只记日志 —— 重启后没有锚,按「没有锚」那条规矩办。"""
        if self._keep is None:
            return
        a, now = self._anchor, self._now()
        try:
            if a is None or a.odom is None or self._map is None:
                self._kept_fix = None
                self._keep.unlink(missing_ok=True)
                return
            p = self._kept_fix
            if not force and p is not None and p.epoch == a.epoch and p.source == a.source:
                if now - self._kept_at < KEEP_MIN_S:
                    return
                if (math.hypot(a.pose[0] - p.pose[0], a.pose[1] - p.pose[1]) < KEEP_MOVE_M
                        and abs(_wrap(a.pose[2] - p.pose[2])) < KEEP_TURN_RAD
                        and now - self._kept_at < KEEP_EVERY_S):
                    return
            what = self._anchor_what.removeprefix("代理重启前")
            body = {"version": KEEP_VERSION, "boot_id": self._boot, "map": list(self._map),
                    "pose": list(a.pose), "odom": list(a.odom), "sigma_xy": a.sigma_xy,
                    "source": a.source, "at": a.at, "anchor_m": self._anchor_m, "what": what}
            tmp = self._keep.with_name(self._keep.name + ".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, self._keep)                  # 不 fsync:断电就是重开机,本来就不用
            self._kept_fix, self._kept_at = a, now
        except OSError as exc:
            log.warning("锚写不进 %s:%s(代理重启后没有锚可核)", self._keep, exc)

    def _load_keep(self) -> tuple[tuple[str, str], _Fix, float, str] | None:
        """读回代理重启前记下的锚:(哪张图, 锚, 位置那条线, 叫法)。没有、别的开机、坏了都是 None。"""
        if self._keep is None or not self._keep.exists():
            return None
        try:
            d = json.loads(self._keep.read_text(encoding="utf-8"))
            if d.get("version") != KEEP_VERSION or d.get("boot_id") != self._boot:
                log.info("盘上的锚不是这一次开机记的:不用")
                return None
            m = d["map"]
            if not (isinstance(m, list) and len(m) == 2 and all(isinstance(s, str) for s in m)):
                raise ValueError("map")
            pose, odom = _num3(d["pose"]), _num3(d["odom"])
            sigma, at, line = (_finite(d[k]) for k in ("sigma_xy", "at", "anchor_m"))
            source, what = d["source"], d["what"]
            if not (isinstance(source, str) and isinstance(what, str)) or sigma < 0 \
                    or line <= 0 or at > self._now():
                raise ValueError("source/what/sigma_xy/anchor_m/at")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("盘上的锚读不懂,不用:%s", exc)
            return None
        fix = _Fix(pose=pose, sigma_xy=sigma, sigma_yaw=0.0, source=source, at=at, born=at,
                   odom=odom, epoch=0, path=0.0, dr0=0.0)
        return (m[0], m[1]), fix, line, what

    def _send_prior(self) -> None:
        if not self._connected or self._map is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prior_rejected = False
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
                self._prior_rejected = True
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

    def _jumped(self, new: _Fix, why: str, *, human: bool = False, seen: bool = True) -> None:
        """跳了(或刚连上、刚重定位):从这一帧起重新数;修正量的参照还是跳之前最后可信的那一帧
        (稳定期里又跳也不换)。``seen`` 为假 = 只是刚连上,不算定位器跳过(对账 S1)。"""
        self._settle = SETTLE_FIXES
        self._off_why = ""
        self._settle_why = why
        self._settle_human = self._settle_human or human
        self._unverified = self._unverified or seen
        self._win.clear()
        self._win.append(new)

    def _settled(self, new: _Fix) -> None:
        ref, human = self._ref, self._settle_human
        self._settle_human = False
        self._unverified = False                     # 按锚核过了(或一直连续、没有锚):从头记
        delta = None
        if ref is not None and ref.T is not None and new.T is not None and ref.epoch == new.epoch:
            delta = compose(new.T, inverse(ref.T))
        self._ref = new                              # 引擎挪过了:以后的修正量从这一帧算
        # 按给的锚核过、稳下来了:以后跟着可信的帧走。**只有这次请求的那一帧到过之后的稳定轮才算**
        # (W35 复查 R1):还在等那一帧(``_reloc`` 没清)时稳下来的是别的轮 —— 刚连上的普通帧、它
        # 自己跳了一下 —— 不许放手,不然锚被盖成那一轮的位置,真正那一帧来了按错的锚比。
        # 请求跟着连接一起没了(断开时 ``_reloc`` 清了)也算到头:连回来稳的那一轮放手。
        if self._reloc is None:
            self._anchor_hold = None
        if self._trusted(new):
            self._set_good(new)
        log.info("定位器稳下来了,修正量 %s%s", delta, "(人给的位置)" if human else "")
        if self.on_corrected is not None:
            self.on_corrected(delta, human)
