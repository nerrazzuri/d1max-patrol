"""定位核心(W09b 决定 3):MOLA 的一帧估计 → 给代理的 ``pose``/``status``(契约
:mod:`d1max_contract.locbridge`)。**不做 I/O、不依赖 ROS**:狗上的 ROS 适配层、录包回放工具喂的是
同一份。

进:先验在载 / 载好了(:meth:`prior_loading`、:meth:`prior_loaded`)、MOLA 没了 / (重新)起来了
(:meth:`backend_down`、:meth:`backend_started`)、重定位做了(:meth:`relocalized`)、一帧点云到了
(:meth:`on_scan`,只要时刻)、MOLA 吐了一帧估计(:meth:`on_estimate`,收不收都算它活着)、每拍
(:meth:`tick`)。出::meth:`drain` 取要发的报文;:meth:`take_restart`(MOLA 卡住了要重启,拿走一次
就清)、:meth:`want_reloc`(要适配层去请 MOLA 重定位:没照办再请、重启之后重发或按最后可信的位置)。

规则:

- **初始化**:先验没载好 → ``initializing``;载好了、没给初值 → ``initializing``「等人给初始位置」
  (探路:MOLA 不给初值不能用),这期间 MOLA 的输出不发。
- **σ_xy** = max(ICP 质量给的, 最近跳过给的, 近几秒低质量帧多给的, 重定位之后的):质量 ≥
  :data:`Q_GOOD` 给 :data:`SIGMA_MIN_M`,往 :data:`Q_BAD` 线性涨到 :data:`SIGMA_BAD_M`(明显高于代理
  那条线:代理 σ 大于 1.0 m 才不信);:data:`JUMP_MEMORY_S` 里跳过 → :data:`SIGMA_JUMPED_M`(同样高于
  那条线);近 :data:`LOWQ_WINDOW_S` 里低质量帧占比过线 → :data:`SIGMA_BAD_M`;重定位之后从给的初值
  开始、:data:`RELOC_SETTLE_S` 里线性降下来(探路:初值偏大时会带着偏差、质量分还很高地跑一阵)。
- **跳变**:相邻两帧挪得比狗能跑的还远、转得比能转的还快 → ``jump``;:data:`JUMP_WINDOW_S` 里跳
  :data:`JUMP_LOST_N` 次 → ``lost``「匹配在来回跳」,从最后一次跳起 :data:`JUMP_CLEAR_S` 不跳才恢复。
  重定位之后第一帧 ``jump`` + ``reloc_id``,离给的位置太远当 MOLA 没照办、再请。
- **meas_age_ms**:质量低于 :data:`Q_MATCH` 的那帧当 MOLA 靠运动模型推的,报离上一次真匹配多久。
- **看门**(不管在不在等初值):点云 :data:`NO_SCAN_S` 没来 → 「雷达没数据」(不重启 MOLA);点云在来、
  MOLA 这次起来之后一帧都没吐过,:data:`START_TIMEOUT_S` 算卡住;吐过,从「上一帧」与「点云恢复」
  两者较晚的那个起 :data:`STALL_S` 不吐算卡住 —— 雷达断过一阵再恢复,不能把断的那段算成它卡住。
- **MOLA 重启之后**:人给的、还没确认的那次重定位按原请求号重发(代理在等这个号);没有就按最后可信的
  位置(同一张图、σ :data:`AUTO_RELOC_SIGMA_M`)自己请,不等人。
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from d1max_contract.locbridge import Pose, State
from d1max_localizer.frames import Frames

log = logging.getLogger(__name__)

Q_GOOD = 0.8
Q_BAD = 0.5
Q_MATCH = 0.5
SIGMA_MIN_M = 0.1
#: 「不可信」的 σ:要**明显高于**代理的线(``BridgeLocalizer.SIGMA_LOST_M``,W34 起 0.5;σ 大于它才
#: 不信 —— 2026-09-27 回放:原来给 1.0 正好压线,代理照样信)。
SIGMA_BAD_M = 1.5
#: 跳过之后这么久 σ 给「不可信」(W09b 内审:只给 0.5 的话代理稳 5 帧就信了跳过去的位置)。
SIGMA_JUMPED_M = SIGMA_BAD_M
JUMP_MEMORY_S = 3.0
#: 近 :data:`LOWQ_WINDOW_S` 里 ICP 质量低于 :data:`LOWQ_Q` 的帧占比过 :data:`LOWQ_SHARE` → σ 不小于
#: :data:`SIGMA_BAD_M`(代理不信)。2026-09-27 回放(coverage 放在 coverage2 的先验上):平滑地错到
#: 2 m 的两段,质量中位数照样 0.96,但低于 0.9 的帧占 17–25%;对得上的几段只占 0–6%。单帧质量看不出来,
#: 窗口占比看得出来。**只拿一个跨次录包定的线**,真机再标。
LOWQ_Q = 0.9
LOWQ_WINDOW_S = 3.0
LOWQ_SHARE = 0.2
LOWQ_MIN_FRAMES = 10
#: 狗最快能跑多快、能转多快(米/秒、弧度/秒);一帧挪得比这还远,再加上余量,就算跳。
MAX_SPEED_MPS = 1.5
MAX_TURN_RPS = 2.0
JUMP_MARGIN_M = 0.3
JUMP_MARGIN_RAD = 0.3
JUMP_WINDOW_S = 5.0
JUMP_LOST_N = 3
JUMP_CLEAR_S = 5.0
RELOC_SETTLE_S = 30.0
#: 自动重定位(MOLA 重启之后按最后可信的位置)的初值 σ:高于代理那条线,稳定期里先不信(W09b 内审:
#: 给 1.0 压线,重启 0.7 s 代理就信了)。
AUTO_RELOC_SIGMA_M = SIGMA_BAD_M
NO_SCAN_S = 1.0
STALL_S = 1.0
START_TIMEOUT_S = 60.0
#: 最后可信的位置:σ 不大于这个(跳过之后 σ 给「不可信」)、不是跳变帧、没在丢定位;重定位时换成
#: 给的位置。
TRUST_SIGMA_M = 0.5
#: 重定位之后第一帧离给的位置超过 max(3σ, 这个) 就当 MOLA 没照办(2026-09-27 实跑:MOLA 还没收到过
#: 点云时收下的重定位,会被它第一帧点云上的「初始定位」按默认原点盖掉),再请;请
#: :data:`RELOC_MAX_TRIES` 次不成就报丢。
RELOC_MISS_M = 1.5
RELOC_MAX_TRIES = 3
#: MOLA 的重定位是异步生效的(2026-09-27 真 ROS 实跑):收下之后这么久里离给的位置远的帧先丢掉、
#: 不算没照办。
RELOC_APPLY_S = 1.0


@dataclass(frozen=True)
class Estimate:
    """MOLA 的一帧估计。"""
    stamp: float                                     # 那一帧点云的时间(秒,ROS 时间)
    p: tuple[float, float, float]                    # MOLA 系里雷达的位置
    q: tuple[float, float, float, float]             # MOLA 系里雷达的姿态(x, y, z, w)
    quality: float                                   # ICP 质量,0–1


@dataclass(frozen=True)
class RelocWant:
    """核心要适配层去请 MOLA 重定位:没照办再请、重启之后重发,或者按最后可信的位置。"""
    x: float
    y: float
    yaw: float
    sigma: float
    req: int | None
    human: bool


class LocalizerCore:
    def __init__(self, *, monotonic: Callable[[], float], source: str = "scan_match") -> None:
        self._now = monotonic
        self.source = source
        self._seq = 0
        self._out: list[Any] = []
        self._state: tuple[str, str] | None = None
        self._map: tuple[str, str] | None = None
        self._frames: Frames | None = None
        self._loading = False
        self._need_init = True
        # 看门
        self._started_at: float | None = None          # MOLA 这次起来的时刻
        self._raw_at: float | None = None              # MOLA 最近一次吐估计(收不收都算)
        self._last_scan: float | None = None
        self._scan_back: float = -1e18                 # 点云断过之后恢复的时刻
        self._restart = False                          # 判了卡住、适配层还没拿走
        self._restarting = False                       # 拿走了,等它重新起来
        self._down = ""
        # 帧
        self._stamp: float | None = None
        self._last: tuple[float, float, float, float] | None = None   # 上一帧:stamp, x, y, yaw
        self._last_match: float | None = None           # 最后一次真匹配的 stamp
        self._jumps: deque[float] = deque()
        self._lowq: deque[tuple[float, bool]] = deque()   # 近几秒每帧:stamp, 质量低不低
        self._jump_lost = False
        self._good: tuple[float, float, float] | None = None
        # 重定位
        self._reloc_tag: tuple[int | None] | None = None   # 下一帧要带的 (reloc_id,)
        self._reloc_sigma = 0.0
        self._reloc_at = -1e18
        self._want_reloc = False
        self._queued = False
        self._expired = False
        self._target: RelocWant | None = None           # 最近一次重定位给的位置(核第一帧用)
        self._retry: RelocWant | None = None
        self._retry_why = ""
        self._misses = 0
        self._reloc_failed = ""

    # ------------------------------------------------------------ 进

    def prior_loading(self, map_ref: tuple[str, str]) -> None:
        if map_ref != self._map:
            self._good = None                           # 别的图:旧位置不能当初值
            self._forget_reloc()
        # 同一张图重新载(W34:2026-10-08 C40221 代理每次重启定位器都要人重新给初始位置):最后可信的
        # 位置留着,MOLA 起来以后按它自己重定位(跟 MOLA 重启一样)
        self._map, self._frames, self._loading = map_ref, None, True
        self._need_init = True
        self._want_reloc = False

    def prior_loaded(self, map_ref: tuple[str, str], frames: Frames) -> None:
        if map_ref != self._map:
            self._good = None
            self._forget_reloc()                        # 同一张图(MOLA 重启)留着没确认的重定位
        self._map, self._frames, self._loading = map_ref, frames, False
        self._need_init = True
        self._want_reloc = False

    def backend_down(self, reason: str) -> None:
        """MOLA 进程没了(退出、被杀、起不来):在它重新起来之前一帧都不信。"""
        self._down = reason
        self._need_init = True

    def backend_started(self) -> None:
        """MOLA(重新)起来了:它的位置作废,要重新给初值。人给的、还没确认的那次重定位按原请求号
        重发;没有就按最后可信的位置自己请。"""
        self._down = ""
        self._restart = self._restarting = False
        self._need_init = True
        self._started_at = self._now()
        self._raw_at = None
        self._last = None
        self._stamp = None
        self._lowq.clear()
        pending = self._retry or (self._target if self._reloc_tag is not None else None)
        self._reloc_tag = None
        if pending is not None:
            self._retry = pending
            self._retry_why = ("定位程序重启了,按人给的位置再请一次" if pending.human
                               else "定位程序重启了,按最后的位置再请一次")
            self._want_reloc = False
        else:
            self._want_reloc = self._good is not None and self._frames is not None

    def relocalized(self, *, req: int | None, x: float, y: float, yaw: float, sigma: float,
                    human: bool) -> bool:
        """MOLA 收下了按 ``(x, y, yaw)`` 重定位(适配层调了它的服务)。先验没载好回假(不收)。"""
        if not self.can_relocalize():
            return False
        want = RelocWant(float(x), float(y), float(yaw), float(sigma), req, human)
        if self._retry is None or (self._retry.x, self._retry.y, self._retry.req) != (x, y, req):
            self._misses = 0                             # 新的一次(不是再请的)
        self._target, self._retry = want, None
        self._reloc_failed = ""
        self._queued = self._expired = False
        self._need_init = False
        self._want_reloc = False
        self._reloc_tag = (req,)
        # 重定位之前记的「最后可信」是旧的看法:给的这个位置才是现在的(MOLA 这时重启就按它请)
        self._good = (float(x), float(y), float(yaw))
        self._reloc_sigma = float(sigma)
        self._reloc_at = self._now()
        self._jumps.clear()
        self._jump_lost = False
        log.info("重定位到 (%.2f, %.2f, %.2f),σ %.2f(%s)", x, y, yaw, sigma,
                 "人给的" if human else "自动")
        return True

    def reloc_queued(self) -> None:
        """人给了位置,但 MOLA 刚起来、还没出过位姿:适配层先记着,它出了第一帧再下发。"""
        self._queued = True
        self._expired = False

    def reloc_expired(self) -> None:
        """记着的那次放太久了(人可能已经挪过狗),适配层扔掉了。"""
        self._queued = False
        self._expired = True

    def on_scan(self) -> None:
        now = self._now()
        if self._last_scan is None or now - self._last_scan > NO_SCAN_S:
            self._scan_back = now                        # 断过一阵之后又来了
        self._last_scan = now

    def on_estimate(self, e: Estimate) -> None:
        now = self._now()
        self._raw_at = now                               # 吐了就算它活着,收不收另说
        if self._frames is None or self._loading or self._need_init or self._map is None:
            return                                       # MOLA 退出时 backend_down 也标了要给初值
        vals = (e.stamp, e.quality, *e.p, *e.q)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals):
            log.warning("MOLA 给了一帧不是有限数的估计,丢掉")
            return                                       # 先查,再改任何状态(W09b 内审)
        if self._stamp is not None and e.stamp <= self._stamp:
            return                                       # 同一帧、乱序
        self._stamp = e.stamp
        x, y, yaw = self._frames.to_map2d(e.p, e.q)
        if self._reloc_tag is not None and self._target is not None and self._missed(x, y, now):
            return
        tag, self._reloc_tag = self._reloc_tag, None
        jump = tag is not None
        if not jump and self._last is not None:
            jump = self._jumped(e.stamp - self._last[0], x - self._last[1], y - self._last[2],
                                yaw - self._last[3])
            if jump:
                self._jumps.append(now)
        self._last = (e.stamp, x, y, yaw)
        self._lowq.append((e.stamp, e.quality < LOWQ_Q))
        while e.stamp - self._lowq[0][0] > LOWQ_WINDOW_S:
            self._lowq.popleft()
        if e.quality >= Q_MATCH or self._last_match is None:
            self._last_match = e.stamp
        age_ms = 0 if e.quality >= Q_MATCH else round((e.stamp - self._last_match) * 1000)
        sigma = self._sigma(e.quality, now)
        self._seq += 1
        self._out.append(Pose(
            seq=self._seq, stamp_ns=round(e.stamp * 1e9), map_id=self._map[0],
            map_version=self._map[1], x=x, y=y, yaw=yaw, sigma_xy=sigma,
            sigma_yaw=0.01 + 0.2 * sigma, source=self.source, jump=jump,
            reloc_id=tag[0] if tag is not None else None, meas_age_ms=max(0, age_ms)))
        if sigma <= TRUST_SIGMA_M and not self._jump_lost and not jump:
            self._good = (x, y, yaw)                     # 跳过之后 σ 给「不可信」,自然进不来

    def tick(self) -> None:
        now = self._now()
        while self._jumps and now - self._jumps[0] > max(JUMP_WINDOW_S, JUMP_CLEAR_S):
            self._jumps.popleft()
        recent = [t for t in self._jumps if now - t <= JUMP_WINDOW_S]
        if len(recent) >= JUMP_LOST_N:
            self._jump_lost = True
        elif self._jump_lost and (not self._jumps or now - self._jumps[-1] > JUMP_CLEAR_S):
            self._jump_lost = False
        self._watch(now)
        self._set_state(*self._judge(now))

    # ------------------------------------------------------------ 出

    @property
    def map_ref(self) -> tuple[str, str] | None:
        return self._map

    def can_relocalize(self) -> bool:
        return self._frames is not None and not self._loading

    def drain(self) -> list[Any]:
        out, self._out = self._out, []
        return out

    def hello(self) -> list[Any]:
        """刚连上代理:把当前状态再发一遍。"""
        if self._state is None:
            return []
        self._seq += 1
        return [State(seq=self._seq, state=self._state[0], reason=self._state[1])]

    def take_restart(self) -> bool:
        """判了 MOLA 卡住:回真一次(适配层去重启),之后等它重新起来(W09b 内审:原来一直回真,适配层
        每拍重启一次,退避永远排不到,MOLA 再也起不来)。"""
        if not self._restart:
            return False
        self._restart, self._restarting = False, True
        return True

    def want_reloc(self) -> RelocWant | None:
        if self._retry is not None:
            return self._retry
        if not self._want_reloc or self._good is None:
            return None
        return RelocWant(*self._good, AUTO_RELOC_SIGMA_M, None, False)

    # ------------------------------------------------------------ 内部

    def _watch(self, now: float) -> None:
        """点云在来、MOLA 却不吐(不管在不在等初值)→ 要重启。"""
        if self._restart or self._restarting or self._down or self._started_at is None:
            return
        if self._last_scan is None or now - self._last_scan > NO_SCAN_S:
            return                                       # 雷达没数据:不怪 MOLA
        if self._raw_at is None:
            since, limit = max(self._started_at, self._scan_back), START_TIMEOUT_S
        else:
            since, limit = max(self._raw_at, self._scan_back), STALL_S
        # 「点云在来」= MOLA 最后一次输出(或起来、点云恢复)之后,点云又持续来了这么久 —— 不拿「现在」
        # 比:点云停的那一刻最后一帧比 MOLA 的输出晚到一点,拿现在比会误判(2026-09-27 真 ROS 实跑)
        if self._last_scan - since > limit:
            log.warning("点云在来,MOLA %.1f 秒没吐估计:要重启", self._last_scan - since)
            self._restart = True

    def _judge(self, now: float) -> tuple[str, str]:
        if self._map is None:
            return "initializing", "还没有先验"
        was = "lost" if self._good is not None else "initializing"
        if self._down:
            return was, self._down                       # 起不来、退了:先说这个(在载图也一样)
        if self._loading or self._frames is None:
            return "initializing", "在载入先验"
        if self._restart or self._restarting:
            return was, "定位程序卡住了,在重启"
        if self._reloc_failed:
            return "lost", self._reloc_failed
        if self._need_init:
            if self._queued:
                why = "定位程序在起,起来就按人给的位置定位"
            elif self._retry is not None:
                why = self._retry_why
            elif self._expired:
                why = "人给的位置放太久没用上,请重新设位置"
            elif self._want_reloc:
                why = "在按最后的位置重定位"
            else:
                why = "等人给初始位置"
            return "initializing", why
        if self._last_scan is None or now - self._last_scan > NO_SCAN_S:
            return "lost", "雷达没数据"
        if self._jump_lost:
            return "lost", "匹配在来回跳"
        return "tracking", ""

    def _set_state(self, state: str, reason: str) -> None:
        if (state, reason) == self._state:
            return
        self._state = (state, reason)
        self._seq += 1
        self._out.append(State(seq=self._seq, state=state, reason=reason))
        log.info("定位器状态:%s %s", state, reason)

    def _missed(self, x: float, y: float, now: float) -> bool:
        """重定位之后的第一帧:离给的位置太远就不发;收下之后 :data:`RELOC_APPLY_S` 里当它还没生效、
        等着,过了还远就当 MOLA 没照办,再请(或者报丢)。"""
        t = self._target
        assert t is not None
        d = math.hypot(x - t.x, y - t.y)
        if d <= max(3 * t.sigma, RELOC_MISS_M):
            return False
        if now - self._reloc_at < RELOC_APPLY_S:
            self._stamp = None                           # 这帧不算数:下一帧照样核
            return True
        self._misses += 1
        self._need_init = True
        self._reloc_tag = None
        log.warning("重定位之后第一帧离给的位置 %.1f m:MOLA 没照办(第 %d 次)", d, self._misses)
        if self._misses >= RELOC_MAX_TRIES:
            self._reloc_failed = f"定位程序没按给的位置重定位(差 {d:.1f} m),请重新设位置"
            self._retry = None
        else:
            self._retry, self._retry_why = t, "定位程序没照给的位置定位,再请一次"
        return True

    def _forget_reloc(self) -> None:
        self._queued = self._expired = False
        self._target = self._retry = None
        self._reloc_tag = None
        self._misses = 0
        self._reloc_failed = ""

    def _jumped_lately(self, now: float) -> bool:
        return any(now - t <= JUMP_MEMORY_S for t in self._jumps)

    @staticmethod
    def _jumped(dt: float, dx: float, dy: float, dyaw: float) -> bool:
        dt = max(dt, 0.0)
        step = math.hypot(dx, dy)
        turn = abs(math.remainder(dyaw, 2 * math.pi))
        return (step > MAX_SPEED_MPS * dt + JUMP_MARGIN_M
                or turn > MAX_TURN_RPS * dt + JUMP_MARGIN_RAD)

    def _sigma(self, quality: float, now: float) -> float:
        if quality >= Q_GOOD:
            s = SIGMA_MIN_M
        elif quality <= Q_BAD:
            s = SIGMA_BAD_M
        else:
            s = SIGMA_MIN_M + (SIGMA_BAD_M - SIGMA_MIN_M) * (Q_GOOD - quality) / (Q_GOOD - Q_BAD)
        if self._jumped_lately(now):
            s = max(s, SIGMA_JUMPED_M)
        if (len(self._lowq) >= LOWQ_MIN_FRAMES
                and sum(low for _, low in self._lowq) / len(self._lowq) >= LOWQ_SHARE):
            s = max(s, SIGMA_BAD_M)
        since = now - self._reloc_at
        if 0 <= since < RELOC_SETTLE_S:
            s = max(s, self._reloc_sigma * (1.0 - since / RELOC_SETTLE_S))
        return s
