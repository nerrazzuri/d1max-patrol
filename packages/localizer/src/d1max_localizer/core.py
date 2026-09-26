"""定位核心(W09b 决定 3):MOLA 的一帧估计 → 给代理的 ``pose``/``status``(契约
:mod:`d1max_contract.locbridge`)。**不做 I/O、不依赖 ROS**:狗上的 ROS 适配层、录包回放工具喂的是
同一份。

进:先验在载 / 载好了(:meth:`prior_loading`、:meth:`prior_loaded`)、MOLA(重新)起来了
(:meth:`backend_started`)、重定位做了(:meth:`relocalized`)、一帧点云到了(:meth:`on_scan`,只要时刻)、
MOLA 给了一帧估计(:meth:`on_estimate`)、每拍(:meth:`tick`)。出::meth:`drain` 取要发的报文;
:meth:`want_restart`(MOLA 卡住了要重启)、:meth:`want_reloc`(重启之后要按最后可信的位置自己重定位)。

规则:

- **初始化**:先验没载好 → ``initializing``;载好了、没给初值 → ``initializing``「等人给初始位置」
  (探路:MOLA 不给初值不能用),这期间 MOLA 的输出不发。
- **σ_xy** = max(ICP 质量给的, 最近跳过给的, 重定位之后的):质量 ≥ :data:`Q_GOOD` 给
  :data:`SIGMA_MIN_M`,往 :data:`Q_BAD` 线性涨到 :data:`SIGMA_BAD_M`(代理 1.0 m 就不信);
  :data:`JUMP_MEMORY_S` 里跳过 → 不小于 :data:`SIGMA_JUMPED_M`;重定位之后从给的初值开始,
  :data:`RELOC_SETTLE_S` 里线性降下来(探路:初值偏大时会带着偏差、质量分还很高地跑一阵)。
- **跳变**:相邻两帧挪得比狗能跑的还远、转得比能转的还快 → ``jump``;:data:`JUMP_WINDOW_S` 里跳
  :data:`JUMP_LOST_N` 次 → ``lost``「匹配在来回跳」,:data:`JUMP_CLEAR_S` 不跳才恢复。重定位之后
  第一帧 ``jump`` + ``reloc_id``。
- **meas_age_ms**:质量低于 :data:`Q_MATCH` 的那帧当 MOLA 靠运动模型推的,报离上一次真匹配多久。
- **看门**:点云 :data:`NO_SCAN_S` 没来 → ``lost``「雷达没数据」(不重启);点云在来、MOLA
  :data:`STALL_S` 不出位姿 → ``lost`` 并要重启(刚起来、刚重定位时载图要一阵,给
  :data:`START_TIMEOUT_S`)。
- MOLA 重启之后:有最后可信的位置(同一张图)就 :meth:`want_reloc` 给它(σ
  :data:`AUTO_RELOC_SIGMA_M`),不等人。
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
SIGMA_BAD_M = 1.0
SIGMA_JUMPED_M = 0.5
JUMP_MEMORY_S = 5.0
#: 狗最快能跑多快、能转多快(米/秒、弧度/秒);一帧挪得比这还远,再加上余量,就算跳。
MAX_SPEED_MPS = 1.5
MAX_TURN_RPS = 2.0
JUMP_MARGIN_M = 0.3
JUMP_MARGIN_RAD = 0.3
JUMP_WINDOW_S = 5.0
JUMP_LOST_N = 3
JUMP_CLEAR_S = 5.0
RELOC_SETTLE_S = 30.0
AUTO_RELOC_SIGMA_M = 1.0
NO_SCAN_S = 1.0
STALL_S = 1.0
START_TIMEOUT_S = 60.0
#: 最后可信的位置:σ 不大于这个、没在丢定位。
TRUST_SIGMA_M = 0.5


@dataclass(frozen=True)
class Estimate:
    """MOLA 的一帧估计。"""
    stamp: float                                     # 那一帧点云的时间(秒,ROS 时间)
    p: tuple[float, float, float]                    # MOLA 系里雷达的位置
    q: tuple[float, float, float, float]             # MOLA 系里雷达的姿态(x, y, z, w)
    quality: float                                   # ICP 质量,0–1


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
        self._started_at: float | None = None          # MOLA(重新)起来 / 刚重定位的时刻
        self._got_since_start = False
        self._last_scan: float | None = None
        self._last_est_at: float | None = None
        self._stamp: float | None = None
        self._last: tuple[float, float, float, float] | None = None   # 上一帧:stamp, x, y, yaw
        self._last_match: float | None = None           # 最后一次真匹配的 stamp
        self._jumps: deque[float] = deque()
        self._jump_lost = False
        self._reloc_tag: tuple[int | None] | None = None   # 下一帧要带的 (reloc_id,)
        self._reloc_sigma = 0.0
        self._reloc_at = -1e18
        self._good: tuple[float, float, float] | None = None
        self._restart = False
        self._want_reloc = False

    # ------------------------------------------------------------ 进

    def prior_loading(self, map_ref: tuple[str, str]) -> None:
        self._map, self._frames, self._loading = map_ref, None, True
        self._need_init = True
        self._good = None                               # 别的图:旧位置不能当初值
        self._want_reloc = False

    def prior_loaded(self, map_ref: tuple[str, str], frames: Frames) -> None:
        if map_ref != self._map:
            self._good = None
        self._map, self._frames, self._loading = map_ref, frames, False
        self._need_init = True
        self._want_reloc = False

    def backend_started(self) -> None:
        """MOLA(重新)起来了:它的位置作废,要重新给初值;有最后可信的位置就自己给。"""
        self._restart = False
        self._need_init = True
        self._started_at = self._now()
        self._got_since_start = False
        self._last = None
        self._stamp = None
        self._want_reloc = self._good is not None and self._frames is not None

    def relocalized(self, *, req: int | None, x: float, y: float, yaw: float, sigma: float,
                    human: bool) -> bool:
        """MOLA 收下了按 ``(x, y, yaw)`` 重定位(适配层调了它的服务)。没有先验回假(不收)。"""
        if self._frames is None or self._loading:
            return False
        self._need_init = False
        self._want_reloc = False
        self._reloc_tag = (req,)
        self._reloc_sigma = float(sigma)
        self._reloc_at = self._now()
        self._started_at = self._now()
        self._got_since_start = False
        self._jumps.clear()
        self._jump_lost = False
        log.info("重定位到 (%.2f, %.2f, %.2f),σ %.2f(%s)", x, y, yaw, sigma,
                 "人给的" if human else "自动")
        return True

    def on_scan(self) -> None:
        self._last_scan = self._now()

    def on_estimate(self, e: Estimate) -> None:
        if self._frames is None or self._loading or self._need_init or self._map is None:
            return
        if self._stamp is not None and e.stamp <= self._stamp:
            return                                       # 同一帧、乱序
        now = self._now()
        self._stamp = e.stamp
        self._last_est_at = now
        self._got_since_start = True
        x, y, yaw = self._frames.to_map2d(e.p, e.q)
        tag, self._reloc_tag = self._reloc_tag, None
        jump = tag is not None
        if not jump and self._last is not None:
            jump = self._jumped(e.stamp - self._last[0], x - self._last[1], y - self._last[2],
                                yaw - self._last[3])
            if jump:
                self._jumps.append(now)
        self._last = (e.stamp, x, y, yaw)
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
            self._good = (x, y, yaw)

    def tick(self) -> None:
        now = self._now()
        while self._jumps and now - self._jumps[0] > max(JUMP_WINDOW_S, JUMP_CLEAR_S):
            self._jumps.popleft()
        recent = [t for t in self._jumps if now - t <= JUMP_WINDOW_S]
        if len(recent) >= JUMP_LOST_N:
            self._jump_lost = True
        elif self._jump_lost and (not self._jumps or now - self._jumps[-1] > JUMP_CLEAR_S):
            self._jump_lost = False
        self._set_state(*self._judge(now))

    # ------------------------------------------------------------ 出

    def drain(self) -> list[Any]:
        out, self._out = self._out, []
        return out

    def hello(self) -> list[Any]:
        """刚连上代理:把当前状态再发一遍。"""
        if self._state is None:
            return []
        self._seq += 1
        return [State(seq=self._seq, state=self._state[0], reason=self._state[1])]

    def want_restart(self) -> bool:
        return self._restart

    def want_reloc(self) -> tuple[float, float, float, float] | None:
        if not self._want_reloc or self._good is None:
            return None
        return (*self._good, AUTO_RELOC_SIGMA_M)

    # ------------------------------------------------------------ 内部

    def _judge(self, now: float) -> tuple[str, str]:
        if self._map is None:
            return "initializing", "还没有先验"
        if self._loading or self._frames is None:
            return "initializing", "在载入先验"
        if self._need_init:
            why = "在按最后的位置重定位" if self._want_reloc else "等人给初始位置"
            return "initializing", why
        if self._last_scan is None or now - self._last_scan > NO_SCAN_S:
            ago = "" if self._last_scan is None else f" {now - self._last_scan:.0f} 秒"
            return "lost", f"雷达{ago}没数据"
        since = self._last_est_at if self._got_since_start else self._started_at
        limit = STALL_S if self._got_since_start else START_TIMEOUT_S
        if since is not None and now - since > limit:
            self._restart = True
            return "lost", "定位程序卡住了,在重启"
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
        if any(now - t <= JUMP_MEMORY_S for t in self._jumps):
            s = max(s, SIGMA_JUMPED_M)
        since = now - self._reloc_at
        if 0 <= since < RELOC_SETTLE_S:
            s = max(s, self._reloc_sigma * (1.0 - since / RELOC_SETTLE_S))
        return s
