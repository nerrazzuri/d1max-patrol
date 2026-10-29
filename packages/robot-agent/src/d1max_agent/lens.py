"""热像镜头被挡(W46,上装 V2):雨天红外会被镜头上的水膜挡住,脏东西也会。

做法(**门槛没拿真画面定,到货后标**):
- 每 ``every_s`` 秒让 ffmpeg 从热像那一路取一帧,缩成 64×48 的灰度小图(``-f rawvideo``,纯 Python 算,
  不装图像库)。
- 算画面细节:相邻像素差的平均值(横竖都算)。被水膜挡住的热像糊成一片,细节掉到平时的零头。
- 基线 = 最近 ``BASELINE_N`` 张正常画面细节的中位数;攒够 ``MIN_BASELINE`` 张才开始判。糊的不进基线
  (低于基线的 ``BLOCK_RATIO``,或者低于绝对下限 ``ABS_MIN_DETAIL``:起来时就挡着的不学进去)。
- 连着 ``BLOCK_AFTER`` 次低于基线的 ``BLOCK_RATIO`` 才报 ``thermal_lens_blocked``(刚扫过一面白墙、
  对着天空也会糊一下);连着 ``CLEAR_AFTER`` 次正常报 ``thermal_lens_clear``。
- 取不到图不算被挡:录像、网络设备那几条已经会报。
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

log = logging.getLogger(__name__)

W, H = 64, 48
EVERY_S = 60.0
BASELINE_N = 30
MIN_BASELINE = 10
BLOCK_RATIO = 0.35
#: 细节低于这个的画面一律不进基线(起来时镜头就挡着,别把糊的学成平时)。
ABS_MIN_DETAIL = 2.0
BLOCK_AFTER = 5
CLEAR_AFTER = 3
#: 开机以来连着这么多次糊得没细节(低于 :data:`ABS_MIN_DETAIL`)、基线一直攒不起来:报一次「看不清」
#: (外审 R9:开机前就被挡住的镜头原来永远停在「攒基线」)。
BOOT_STUCK_N = 30
GRAB_TIMEOUT_S = 15.0


def detail(gray: bytes, w: int = W, h: int = H) -> float:
    """灰度小图的细节:相邻像素差的平均值。"""
    if len(gray) != w * h:
        raise ValueError(f"灰度图要 {w}×{h}={w * h} 字节,来的是 {len(gray)}")
    tot = n = 0
    for y in range(h):
        row = gray[y * w:(y + 1) * w]
        tot += sum(abs(row[x + 1] - row[x]) for x in range(w - 1))
        n += w - 1
        if y + 1 < h:
            nxt = gray[(y + 1) * w:(y + 2) * w]
            tot += sum(abs(nxt[x] - row[x]) for x in range(w))
            n += w
    return tot / n


def ffmpeg_gray(input_args: Sequence[str], ffmpeg: str = "ffmpeg"
                ) -> Callable[[], Awaitable[bytes]]:
    """从一路相机取一帧 64×48 灰度图的协程函数。"""
    async def grab() -> bytes:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *input_args,
            "-frames:v", "1", "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), GRAB_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0 or len(out) != W * H:
            from d1max_contract.video import scrub_tail  # 热像地址里有口令
            raise OSError(f"取不到热像画面:{scrub_tail(err, 200)}")
        return out
    return grab


class LensWatch:
    def __init__(self, grab: Callable[[], Awaitable[bytes]] | None, *,
                 emit: Callable[[str, dict[str, Any]], Any],
                 mono: Callable[[], float] = time.monotonic, camera: str = "thermal",
                 every_s: float = EVERY_S) -> None:
        self._grab = grab
        self.emit = emit
        self._mono = mono
        self.camera = camera
        self.every_s = every_s
        self._next = 0.0
        self._good: deque[float] = deque(maxlen=BASELINE_N)
        self._low = self._ok = 0
        self._boot_low = 0
        self._boot_blocked = False
        self.state = "clear"
        self.last: float | None = None
        self.error = ""

    def baseline(self) -> float:
        return statistics.median(self._good) if self._good else 0.0

    def due(self) -> bool:
        """到点该取一帧了(运行时据此起后台任务:取一帧最长要等 ``GRAB_TIMEOUT_S``,不许卡主循环)。"""
        return self._grab is not None and self._mono() >= self._next

    async def step(self) -> None:
        if not self.due():
            return
        self._next = self._mono() + self.every_s
        try:
            gray = await self._grab()
            d = await asyncio.to_thread(detail, gray)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                        # noqa: BLE001 - 下一次再取
            self.error = str(exc)[:200]
            return
        self.error = ""
        self.last = d
        enough = len(self._good) >= MIN_BASELINE
        low = d < ABS_MIN_DETAIL or (enough and d < BLOCK_RATIO * self.baseline())
        if not low:
            self._good.append(d)
        if not enough:
            self._boot_low = self._boot_low + 1 if d < ABS_MIN_DETAIL else 0
            if self.state == "clear" and self._boot_low >= BOOT_STUCK_N:
                self.state, self._boot_blocked = "blocked", True
                log.warning("热像镜头开机以来一直糊(细节 %.1f),基线攒不起来:可能被挡了", d)
                self.emit("thermal_lens_blocked", {"camera": self.camera, "detail": round(d, 2),
                                                    "baseline": 0.0})
            return
        self._low, self._ok = (self._low + 1, 0) if low else (0, self._ok + 1)
        if self.state == "clear" and self._low >= BLOCK_AFTER:
            self.state = "blocked"
            log.warning("热像镜头可能被挡了:细节 %.1f,平时 %.1f", d, self.baseline())
            self.emit("thermal_lens_blocked", {"camera": self.camera, "detail": round(d, 2),
                                                "baseline": round(self.baseline(), 2)})
        elif self.state == "blocked" and self._ok >= CLEAR_AFTER:
            self.state, self._boot_blocked = "clear", False
            log.info("热像镜头又看得清了")
            self.emit("thermal_lens_clear", {"camera": self.camera})

    def health(self) -> dict[str, Any]:
        from d1max_contract.devices import device
        did = f"lens:{self.camera}"
        if self.state == "blocked" and self._boot_blocked:
            return device(did, "degraded", "开机以来画面一直糊成一片:镜头可能被挡住了,"
                                           "也可能一直对着没有细节的东西(这种时候检查不了镜头)")
        if self.state == "blocked":
            return device(did, "degraded", "镜头可能被挡住了(雨水、脏东西),画面糊成一片")
        if self.error:
            return device(did, "online", f"这一次没取到画面:{self.error}")
        if len(self._good) < MIN_BASELINE:
            return device(did, "online", f"还在攒基线({len(self._good)}/{MIN_BASELINE})")
        return device(did, "online", "看得清")
