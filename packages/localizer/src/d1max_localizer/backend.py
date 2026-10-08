"""MOLA 后端(W09b 决定 2),不依赖 ROS:ROS 那一层(:mod:`d1max_localizer.mola`)把位姿、质量、点云时刻喂
进来,把「调 MOLA 的重定位服务」交给这里用。

- **换先验**:先验目录里要有 :data:`PRIOR_FILE`(MOLA 的局部地图)与 :data:`FRAMES_FILE`(坐标换算,见
  :mod:`d1max_localizer.frames`);代理没给目录(狗按启动参数载的图)就用本机配的默认目录。找齐了当场回
  「收下」,再(重)起 MOLA;同一张图、MOLA 在跑就当没事。
- **重定位**:平面位姿按 ``frames`` 换成 MOLA 系里雷达的三维初值,调服务;MOLA 收下回空串,并告诉核心。
  **MOLA 起来之后还没出过位姿就先记着**(回「收下」),它出了第一帧再下发 —— 2026-09-27 实跑:MOLA 还没
  收到点云时收下的重定位,会被它第一帧点云上的「初始定位」按默认原点盖掉。
- **看门**(:meth:`check`,每拍调,不等):核心说 MOLA 卡住了 → 重启(一次,后台做);记着的重定位、核心要
  的重定位(没照办再请、重启之后重发或按最后可信的位置)→ MOLA 出过位姿了就下发(没成每
  :data:`AUTO_RELOC_RETRY_S` 再试);记着的超过 :data:`PENDING_MAX_S` 就扔掉(人可能已经挪过狗)。
- :class:`Pairer`:MOLA 的位姿与 ICP 质量是两个话题(质量没有消息头),按到达先后配成一帧。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError
from d1max_localizer.core import Estimate, LocalizerCore, RelocWant
from d1max_localizer.frames import Frames

log = logging.getLogger(__name__)

PRIOR_FILE = "prior.mm"
FRAMES_FILE = "frames.json"
AUTO_RELOC_RETRY_S = 1.0
PENDING_MAX_S = 60.0

RelocService = Callable[[tuple[float, float, float], tuple[float, float, float, float], float],
                        Awaitable[bool]]



def _same_file(a: Path | None, b: Path) -> bool:
    """同一个文件(W34:代理重启后给的路径可能经过 ``active`` 链接,字面不同、文件相同)。"""
    if a is None:
        return False
    try:
        return a == b or a.resolve() == b.resolve()
    except OSError:
        return a == b

class MolaBackend:
    def __init__(self, core: LocalizerCore, *, supervisor: Any, reloc_service: RelocService,
                 default_prior_dir: Path | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.core = core
        self.supervisor = supervisor
        self._reloc = reloc_service
        self.default_prior_dir = default_prior_dir
        self._now = monotonic
        self._map: tuple[str, str] | None = None
        self._frames: Frames | None = None
        self._tasks: set[asyncio.Task] = set()
        self._auto_at = -1e18
        self._alive = False                              # 这次起来之后 MOLA 出过位姿没有
        self._pending: RelocWant | None = None           # 人给的、等 MOLA 起来再下发的
        self._pending_at = 0.0
        self._reloc_errs = 0

    # ------------------------------------------------------------ 代理请的

    async def load_prior(self, map_ref: tuple[str, str], dir: str) -> str:
        d = Path(dir) if dir else self.default_prior_dir
        if d is None:
            return "代理没有给先验目录,本机也没配默认的"
        mm, fj = d / PRIOR_FILE, d / FRAMES_FILE
        if not mm.is_file():
            return f"{d} 里没有 {PRIOR_FILE}"
        try:
            frames = Frames.load(fj)
        except ContractError as exc:
            return str(exc)[:200]
        if map_ref == self._map and self.supervisor.running and \
                _same_file(self.supervisor.prior, mm):
            return ""                                    # 同一份先验(经链接也算):不重启 MOLA
        self._map, self._frames = map_ref, frames
        self._pending = None                             # 旧图上给的位置作废
        self._alive = False                              # 旧 MOLA 出过位姿不算新的
        self.core.prior_loading(map_ref)
        # 不取消上一次还没做完的起动:看管器一把锁串着做,后来的这次最后生效(W09b 内审:取消会把正在
        # 关的旧进程丢成孤儿)
        self._spawn(self.supervisor.start(mm), "起 MOLA")
        return ""

    async def relocalize(self, x: float, y: float, yaw: float, sigma: float, *,
                         req: int | None = None, human: bool = True) -> str:
        """请 MOLA 重定位;收下(或者先记着)回空串,不收回原因。"""
        if self._frames is None:
            return "先验还没载好"
        want = RelocWant(float(x), float(y), float(yaw), float(sigma), req, human)
        if not self._alive:
            self._queue(want)
            return ""
        why, _ = await self._apply(want)
        return why

    def _queue(self, w: RelocWant) -> None:
        self._pending, self._pending_at = w, self._now()
        self.core.reloc_queued()

    def mola_output(self) -> None:
        """MOLA 出了一帧(不管核心收不收):它起来了,记着的重定位可以下发了。"""
        self._alive = True

    async def _apply(self, w: RelocWant) -> tuple[str, bool]:
        """下发;回 (不收的原因, 核心收下了没有)。MOLA 收了、核心这会儿收不了(在换图)就记着。"""
        assert self._frames is not None
        p, q = self._frames.to_mola(w.x, w.y, w.yaw)
        try:
            ok = await self._reloc(p, q, w.sigma)
        except Exception as exc:  # noqa: BLE001 —— 服务超时、出错都当没收下;连着出错只记第一回
            self._reloc_errs += 1
            if self._reloc_errs == 1:
                log.warning("调 MOLA 的重定位出错:%s", exc)
            ok = False
        else:
            self._reloc_errs = 0
        if not ok:
            return "定位程序没收下重定位", False
        if not self.core.relocalized(req=w.req, x=w.x, y=w.y, yaw=w.yaw, sigma=w.sigma,
                                     human=w.human):
            self._queue(w)                               # 核心这会儿收不了(在换图):记着(W09b 内审)
            return "", False
        return "", True

    # ------------------------------------------------------------ 看管器回调

    def on_up(self) -> None:
        if self._map is not None and self._frames is not None:
            self.core.prior_loaded(self._map, self._frames)
        self.core.backend_started()
        self._auto_at = -1e18
        self._alive = False

    def on_down(self, why: str) -> None:
        self.core.backend_down(why)

    # ------------------------------------------------------------ 每拍

    async def check(self) -> None:
        if self.core.take_restart():
            self._spawn(self.supervisor.restart("定位程序卡住了,在重启"), "重启 MOLA")
            return
        if self._pending is not None and self._now() - self._pending_at > PENDING_MAX_S:
            log.warning("人给的位置记了 %g 秒还没用上,扔掉", PENDING_MAX_S)
            self._pending = None
            self.core.reloc_expired()
        want = self._pending or self.core.want_reloc()
        if (want is None or not self._alive or self._frames is None
                or self._now() - self._auto_at < AUTO_RELOC_RETRY_S):
            return
        self._auto_at = self._now()
        why, taken = await self._apply(want)
        if why:
            log.info("请 MOLA 重定位没成(%s),过一会儿再试", why)
            return
        if taken and want is self._pending:
            self._pending = None

    def _spawn(self, coro: Any, what: str) -> None:
        """后台做(关 MOLA 最长要等几秒,不能卡住每拍);出错记下来,不悄悄吞掉(W09b 内审)。"""
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("%s 出错:%s", what, t.exception())
                self.core.backend_down(f"{what}出错:{t.exception()}"[:120])
        t.add_done_callback(_done)


class Pairer:
    """位姿、质量谁先到都行,配上一对交给 ``emit``;配不上的(另一个 ``max_gap_s`` 内没来,或者下一帧
    位姿已经来了)丢掉。"""

    def __init__(self, emit: Callable[[Estimate], None], *, max_gap_s: float = 0.2) -> None:
        self._emit = emit
        self._gap = max_gap_s
        self._pose: tuple[float, Any, Any, float] | None = None     # stamp, p, q, 到达时刻
        self._q: tuple[float, float] | None = None                  # 质量, 到达时刻

    def pose(self, stamp: float, p: Any, q: Any, *, at: float) -> None:
        if self._q is not None and at - self._q[1] <= self._gap:
            quality, self._q = self._q[0], None
            self._pose = None
            self._emit(Estimate(stamp=stamp, p=tuple(p), q=tuple(q), quality=quality))
            return
        self._q = None
        self._pose = (stamp, p, q, at)

    def quality(self, value: float, *, at: float) -> None:
        if self._pose is not None and at - self._pose[3] <= self._gap:
            stamp, p, q, _ = self._pose
            self._pose = None
            self._emit(Estimate(stamp=stamp, p=tuple(p), q=tuple(q), quality=float(value)))
            return
        self._pose = None
        self._q = (float(value), at)
