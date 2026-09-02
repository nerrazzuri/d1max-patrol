"""页面上的方向键:一拍一拍地走,松手就停。

**为什么是"脉冲"而不是"持续速度"。** 这台机器的运动接口是
``walk(seconds, forward, lateral, yaw)`` —— 开环直驱,给一个时长和一组
控制量,到点自己停(见 ``DeviceBackend.walk``)。所以"按住前进"这件事在
底下就是一串短脉冲,页面每按一下发一拍,松手就没有下一拍了。

**为什么控制量要顶到死区之上。** 控制量是**百分比**不是 m/s,而且死区不
小(清单 #37/#38):``0.11`` 几乎不动,``0.3~0.5`` 才真的走。想让狗慢一点
的正确做法是**把一拍缩短**,不是把控制量压低 —— 压低的结果是原地不动,
而且不报错,页面上看起来像是掉线了。

**守死人开关。** 页面每隔一小会儿要送一次心跳,超过
``HEARTBEAT_TIMEOUT_S`` 没送到就停车。检查放在一条后台任务里周期性地做,
理由和 ``LocalNavBackend._watch_loc`` 一样:**不能只在有人来问的时候才
发现**。浏览器崩了、WiFi 断了、人走开了 —— 这些情况下恰恰没有人来问,
而狗还在走。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Callable

from d1max_patrol.backends.base import DeviceBackend
from d1max_patrol.engine.machine import MissionEngine

#: 心跳断了多久就停车。0.6s ≈ 半米内(死区控制量下的粗略估计)。
HEARTBEAT_TIMEOUT_S = 0.6

#: 一拍默认多长。
DEFAULT_PULSE_S = 0.4

#: 一拍最长多久。再长就等于把守死人开关架空了 —— 心跳断了也还要走这么久。
MAX_PULSE_S = 2.0

#: 死区之上的最小控制量。见清单 #37/#38。
MIN_FWD = 0.30
MIN_YAW = 0.30

#: 守死人的检查周期。取超时的三分之一:够快地发现,又不至于空转。
WATCH_PERIOD_S = HEARTBEAT_TIMEOUT_S / 3

#: 四个轴全零 —— "停"就是这个。
_STOP = (0.0, 0.0, 0.0)


class TeleopBusy(RuntimeError):
    """现在不能遥控:任务在跑,或者急停按着。"""


def _clamp_above_deadband(value: float, floor: float) -> float:
    """零还是零,非零顶到死区之上,并且不超过满量程。

    零必须原样传下去:那是"停",不是"慢慢走"。要是这里也给它顶到 0.3,
    松手之后狗会继续走 —— 这是这个函数里唯一真正危险的一行。
    """
    if value == 0.0:
        return 0.0
    return math.copysign(min(max(abs(value), floor), 1.0), value)


class Teleop:
    """一拍一拍的手动遥控,带守死人开关。

    和 ``MissionEngine`` 是互斥的:任务在跑的时候不许遥控,遥控在动的时候
    不许起任务。后一半靠 ``engine.add_busy_check`` 反过来告诉引擎 —— 引擎
    不认识这个类(全局约束:业务层不许知道外壳)。
    """

    def __init__(self, device: DeviceBackend, engine: MissionEngine,
                 *, clock: Callable[[], float] = time.monotonic,
                 watch_period_s: float = WATCH_PERIOD_S) -> None:
        self._device = device
        self._engine = engine
        self._clock = clock
        self._period = watch_period_s
        self._last_beat = 0.0
        self._active = False
        self._watch: asyncio.Task[None] | None = None
        engine.add_busy_check(self._busy_reason)

    # ------------------------------------------------------------------ 对外

    @property
    def active(self) -> bool:
        """是不是正有一拍在走。"""
        return self._active

    async def pulse(self, fwd: float, lat: float, yaw: float,
                    seconds: float = DEFAULT_PULSE_S) -> None:
        """走一拍。

        控制量取 [-1, 1],会被顶到死区之上;想走慢就把 ``seconds`` 调小。
        三个轴全零等于 :meth:`stop`。
        """
        _check_range(fwd, "fwd")
        _check_range(lat, "lat")
        _check_range(yaw, "yaw")
        if not 0.0 < seconds <= MAX_PULSE_S:
            raise ValueError(f"一拍得在 (0, {MAX_PULSE_S}] 秒之间,给的是 {seconds}")

        if (fwd, lat, yaw) == _STOP:
            await self.stop()
            return

        if self._engine.running:
            raise TeleopBusy("任务在跑,先停任务再遥控 —— 两边一起动是在抢腿")
        if await self._device.emergency():
            raise TeleopBusy("急停按着,松开急停再遥控")

        self._last_beat = self._clock()
        self._active = True
        self._ensure_watch()
        await self._device.walk(
            seconds,
            _clamp_above_deadband(fwd, MIN_FWD),
            _clamp_above_deadband(lat, MIN_FWD),
            _clamp_above_deadband(yaw, MIN_YAW),
        )

    def heartbeat(self) -> None:
        """页面还在,人还在看着。守死人开关靠这个续命。"""
        self._last_beat = self._clock()

    async def stop(self) -> None:
        """停车。停不动也要把 ``active`` 落下来 —— 它是"该不该继续看着"。"""
        self._active = False
        self._cancel_watch()
        await self._device.walk(0.0, 0.0, 0.0, 0.0)

    async def emergency_stop(self, reason: str = "按了急停") -> None:
        """页面上那个红按钮:停遥控,**并且**打断正在跑的任务。

        两件事都要做:急停的语义是"现在什么都别动了",而遥控和任务是两条
        各自独立的动腿路径,只停一条等于没停。
        """
        with contextlib.suppress(Exception):
            await self.stop()
        if self._engine.running:
            await self._engine.abort(reason)

    async def aclose(self) -> None:
        """收尾。只停后台任务,不给狗发指令 —— 关服务不该让它动一下。"""
        self._active = False
        self._cancel_watch()
        if self._watch is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        self._watch = None

    # ------------------------------------------------------------------ 内部

    def _busy_reason(self) -> str:
        return "遥控在动,先松手再起任务" if self._active else ""

    def _ensure_watch(self) -> None:
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watchdog())

    def _cancel_watch(self) -> None:
        if self._watch is not None and not self._watch.done():
            self._watch.cancel()

    async def _watchdog(self) -> None:
        """周期地看心跳还在不在。

        只在这里停车,不在 ``pulse`` 里顺手判 —— 心跳断了的时候恰恰没有人
        在调 ``pulse``。
        """
        while True:
            await asyncio.sleep(self._period)
            if not self._active:
                continue
            if self._clock() - self._last_beat > HEARTBEAT_TIMEOUT_S:
                self._active = False
                with contextlib.suppress(Exception):
                    await self._device.walk(0.0, 0.0, 0.0, 0.0)
                return


def _check_range(value: float, name: str) -> None:
    if not -1.0 <= value <= 1.0:
        raise ValueError(f"{name} 得在 [-1, 1] 之间,给的是 {value}")


__all__ = [
    "DEFAULT_PULSE_S",
    "HEARTBEAT_TIMEOUT_S",
    "MAX_PULSE_S",
    "MIN_FWD",
    "MIN_YAW",
    "WATCH_PERIOD_S",
    "Teleop",
    "TeleopBusy",
]
