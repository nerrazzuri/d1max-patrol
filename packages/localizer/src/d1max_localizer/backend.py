"""MOLA 后端(W09b 决定 2),不依赖 ROS:ROS 那一层(:mod:`d1max_localizer.mola`)把位姿、质量、点云时刻喂
进来,把「调 MOLA 的重定位服务」交给这里用。

- **换先验**:先验目录里要有 :data:`PRIOR_FILE`(MOLA 的局部地图)与 :data:`FRAMES_FILE`(坐标换算,见
  :mod:`d1max_localizer.frames`);代理没给目录(狗按启动参数载的图)就用本机配的默认目录。找齐了当场回
  「收下」,再(重)起 MOLA;同一张图、MOLA 在跑就当没事。
- **重定位**:平面位姿按 ``frames`` 换成 MOLA 系里雷达的三维初值,调服务;MOLA 收下回空串,并告诉核心。
  **MOLA 起来之后还没出过位姿就先记着**(回「收下」),它出了第一帧再下发 —— 2026-09-27 实跑:MOLA 还没
  收到点云时收下的重定位,会被它第一帧点云上的「初始定位」按默认原点盖掉。
- **看门**(:meth:`check`,每拍调):核心说 MOLA 卡住了 → 重启;记着的重定位、核心要的重定位(没照办再请、
  重启之后按最后可信的位置)→ MOLA 出过位姿了就下发(没成每 :data:`AUTO_RELOC_RETRY_S` 再试)。
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

RelocService = Callable[[tuple[float, float, float], tuple[float, float, float, float], float],
                        Awaitable[bool]]


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
        self._start_task: asyncio.Task | None = None
        self._auto_at = -1e18
        self._alive = False                              # 这次起来之后 MOLA 出过位姿没有
        self._pending: RelocWant | None = None           # 人给的、等 MOLA 起来再下发的

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
        if map_ref == self._map and self.supervisor.prior == mm and self.supervisor.running:
            return ""
        self._map, self._frames = map_ref, frames
        self._pending = None                             # 旧图上给的位置作废
        self.core.prior_loading(map_ref)
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        self._start_task = asyncio.get_running_loop().create_task(self.supervisor.start(mm))
        return ""

    async def relocalize(self, x: float, y: float, yaw: float, sigma: float, *,
                         req: int | None = None, human: bool = True) -> str:
        """请 MOLA 重定位;收下(或者先记着)回空串,不收回原因。"""
        if self._frames is None:
            return "先验还没载好"
        want = RelocWant(float(x), float(y), float(yaw), float(sigma), req, human)
        if not self._alive:
            self._pending = want
            self.core.reloc_queued()
            return ""
        return await self._apply(want)

    def mola_output(self) -> None:
        """MOLA 出了一帧(不管核心收不收):它起来了,记着的重定位可以下发了。"""
        self._alive = True

    async def _apply(self, w: RelocWant) -> str:
        assert self._frames is not None
        p, q = self._frames.to_mola(w.x, w.y, w.yaw)
        try:
            ok = await self._reloc(p, q, w.sigma)
        except Exception:
            log.exception("调 MOLA 的重定位出错")
            ok = False
        if not ok:
            return "定位程序没收下重定位"
        self.core.relocalized(req=w.req, x=w.x, y=w.y, yaw=w.yaw, sigma=w.sigma, human=w.human)
        return ""

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
        if self.core.want_restart():
            await self.supervisor.restart("定位程序卡住了,在重启")
            return
        want = self._pending or self.core.want_reloc()
        if (want is None or not self._alive or self._frames is None
                or self._now() - self._auto_at < AUTO_RELOC_RETRY_S):
            return
        self._auto_at = self._now()
        why = await self._apply(want)
        if why:
            log.info("请 MOLA 重定位没成(%s),过一会儿再试", why)
            return
        if want is self._pending:
            self._pending = None


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
