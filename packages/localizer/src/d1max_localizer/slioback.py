"""Super-LIO 后端(W68):定位程序换成 Super-LIO 的重定位模式(``relocation_node``),其余照 MOLA 后端。

跟 MOLA 不一样的地方:

- **先验**是地图包里的 :data:`~d1max_localizer.slioloc.MAP_FILE`(没有 = 旧版 MOLA 建的图,要重建);
  载图时顺带把点云交给套图打分(``on_map``)。
- **初值只认一次**(参数文件里给,起来以后不收):重定位 = 换参数文件**重启进程**。没有初值(新图、
  没人给过位置)就**不起**,核心报「等人给初始位置」。
- 进程(重)起时拿哪个位置:这次请的 > 核心在重试的 > 人给了还没确认的 > 最后可信的
  (:meth:`LocalizerCore.restart_want`)。崩了、卡住、雷达挡过再起,都按最后可信的位置,不按开机时给的。
- 起来时就告诉核心「按这个位置重定位了」;核心照旧核第一帧离给的位置多远、没对上再请(= 再重启)。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_localizer import slioloc as L
from d1max_localizer.backend import MolaBackend
from d1max_localizer.core import LocalizerCore, RelocWant

CONFIG_NAME = "superlio-reloc.yaml"


async def _no_service(*_: Any) -> bool:                 # Super-LIO 没有重定位服务
    return False


class SlioBackend(MolaBackend):
    prior_file = L.MAP_FILE

    def __init__(self, core: LocalizerCore, *, supervisor: Any, ext: L.Extrinsic,
                 base_config: str, root: Path, run_dir: Path,
                 on_map: Callable[[L.MapFit], None] = lambda _fit: None,
                 default_prior_dir: Path | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        super().__init__(core, supervisor=supervisor, reloc_service=_no_service,
                         default_prior_dir=default_prior_dir, monotonic=monotonic)
        self.ext = ext
        self._base = base_config
        self.root = Path(root)
        self.run_dir = Path(run_dir)
        self._on_map = on_map
        self._fit_for: Path | None = None
        self._next: RelocWant | None = None              # 这次请的(下一次起用它)
        self._cur: RelocWant | None = None               # 这次起用的
        self._starting = False

    # ------------------------------------------------------------ 代理请的

    async def load_prior(self, map_ref: tuple[str, str], dir: str) -> str:
        if not L.reloc_exe(self.root).is_file():
            return f"Super-LIO 未装({self.root}):先跑 deploy/d1max-superlio-build"
        try:
            L.source_dir(self.root)
        except L.SlioLocError as exc:
            return str(exc)
        d = Path(dir) if dir else self.default_prior_dir
        pcd = d / self.prior_file if d is not None else None
        if pcd is not None and pcd.is_file() and not _same(self._fit_for, pcd):
            try:
                fit = await asyncio.to_thread(lambda: L.MapFit(L.read_pcd(pcd)))
            except Exception as exc:  # noqa: BLE001 —— 坏文件、读不了:说出来,不动现在的
                return f"地图点云读不了({pcd}):{exc}"[:200]
            self._on_map(fit)
            self._fit_for = pcd
        why = await super().load_prior(map_ref, dir)
        if not why:
            self._alive = True                           # 不用等它出过位姿才收重定位
        return why

    def _missing(self, d: Path) -> str:
        return f"此地图需要重建:{d} 里没有 Super-LIO 用的点云 {self.prior_file}(旧版建的图)"

    async def relocalize(self, x: float, y: float, yaw: float, sigma: float, *,
                         req: int | None = None, human: bool = True) -> str:
        if self._frames is None:
            return "先验尚未载好"
        why, _ = await self._apply(RelocWant(float(x), float(y), float(yaw), float(sigma), req,
                                             human))
        return why

    async def _apply(self, w: RelocWant) -> tuple[str, bool]:
        prior = self._resume_prior or self.supervisor.prior
        if self._frames is None or prior is None:
            return "先验尚未载好", False
        if self._starting and not w.human:
            return "", False                             # 正在起:自动的不再叠一次
        self._next = w
        if self.core.lidar_blocked:
            self._resume_pending = True                  # 雷达挡着:恢复了按它起
            self.core.reloc_queued()
            return "", False
        self._restart(prior)
        return "", True

    # ------------------------------------------------------------ 起进程

    def _start(self, prior: Path) -> None:
        if self._next is None and self.core.restart_want() is None:
            if self._map is not None and self._frames is not None:
                self.core.prior_loaded(self._map, self._frames)   # 不起:等人给初始位置
            return
        self._restart(prior)

    def _restart(self, prior: Path) -> None:
        self._starting = True

        async def go() -> None:
            try:
                await self.supervisor.start(prior)
            finally:
                self._starting = False
        self._spawn(go(), "起 Super-LIO")

    def command(self, prior: Path) -> list[str]:
        """看管器起进程时调:按这次该用的位置写参数文件,回命令行。"""
        w = self._next or self.core.restart_want()
        if w is None or self._frames is None:
            raise L.SlioLocError("没有初始位置,不起 Super-LIO")
        self._next, self._cur = None, w
        p, R = L.imu_pose(self._frames, self.ext, w.x, w.y, w.yaw)
        text = L.reloc_config(self._base, map_dir=Path(prior).parent,
                              src_dir=L.source_dir(self.root), init=L.init_param(p, R))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.run_dir / CONFIG_NAME
        tmp = cfg.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, cfg)
        return L.node_argv(L.reloc_exe(self.root), cfg)

    # ------------------------------------------------------------ 看管器回调

    def on_up(self) -> None:
        super().on_up()
        self._alive = True
        w = self._cur
        if w is not None:
            self.core.relocalized(req=w.req, x=w.x, y=w.y, yaw=w.yaw, sigma=w.sigma,
                                  human=w.human)


def _same(a: Path | None, b: Path) -> bool:
    if a is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b
