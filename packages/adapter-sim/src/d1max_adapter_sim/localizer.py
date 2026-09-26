"""仿真定位器(W09a,W08 决定 10):连代理的本机定位桥,按仿真狗的**真实位置**出位姿。地图系 ← 仿真
世界(= 狗的里程系)默认重合,``frame`` 给了就按它换(地图系跟里程系不重合时代理算得对不对,要这个才
测得出来)。测试、W10/W11 的仿真用它;真狗的定位器是 W09b 的 ROS 节点,走同一个桥、同一份报文。

能注入的毛病:噪声、慢漂移、一下偏出去(可以报 ``jump``,也可以不报 —— 不报就是「自信地跳错」,代理
靠跟里程交叉校验抓)、冻住(匹配线程死了、还在重发最后那个位置)、自己拿里程推着报(``meas_age_ms``
一直涨)、链路延迟、卡一下再一批一起到、停发几帧(过期)、说丢了、拒重定位、初值离得太远重定位不上、
换先验失败、断开。

``step()`` 发一帧(测试一拍调一次,确定性);``run()`` 按频率一直发(仿真跑起来用)。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.locbridge import (
    MAX_LINE,
    PROTO,
    Error,
    Heartbeat,
    Hello,
    Pose,
    Relocalize,
    Reply,
    SetPrior,
    State,
    encode,
    parse,
)

log = logging.getLogger(__name__)


def compose(a: tuple[float, float, float], b: tuple[float, float, float]
            ) -> tuple[float, float, float]:
    """``a ∘ b``:把 ``b`` 这个位姿放到 ``a`` 这个坐标系里。"""
    c, s = math.cos(a[2]), math.sin(a[2])
    return (a[0] + c * b[0] - s * b[1], a[1] + s * b[0] + c * b[1],
            math.remainder(a[2] + b[2], 2 * math.pi))


class SimLocalizer:
    def __init__(self, robot: Any, path: Path | str, *, name: str = "sim-loc",
                 sigma_xy: float = 0.05, sigma_yaw: float = 0.01, noise_xy: float = 0.0,
                 noise_yaw: float = 0.0, seed: int = 0,
                 frame: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        self.robot = robot
        self.path = Path(path)
        self.name = name
        #: 地图系 ← 仿真世界(狗的里程系)。
        self.frame = frame
        self.sigma_xy, self.sigma_yaw = sigma_xy, sigma_yaw
        self.noise_xy, self.noise_yaw = noise_xy, noise_yaw
        self._rng = random.Random(seed)
        #: 代理要它换的先验(依次)、要它做的重定位(依次);测试看。
        self.priors: list[SetPrior] = []
        self.relocs: list[Relocalize] = []
        #: 换先验怎么回;重定位要拒的话写原因。
        self.prior_answer: tuple[bool, str] = (True, "")
        self.refuse_reloc = ""
        #: 当前出位姿用的图(换先验成了才换)。
        self.map: tuple[str, str] | None = None
        #: 加在真实位置上的偏差(一下偏出去、慢漂移)。
        self.offset = [0.0, 0.0, 0.0]
        self.drift_per_step = (0.0, 0.0)
        #: 还要停发几帧。
        self.paused = 0
        #: 位姿晚这么多拍才发出去(链路、算法延迟;时间戳是算出来那一刻的)。
        self.latency_steps = 0
        #: 还要卡几拍:这几拍算出来的位姿攒着,卡完一起发。
        self.held = 0
        #: 冻住时一直重发的那个位置(``freeze()``)。
        self.frozen: tuple[float, float, float] | None = None
        #: 自己拿里程推着报(没真匹配上)从哪一刻开始(毫秒);``None`` = 在正常匹配。
        self.coast_from_ms: int | None = None
        #: 人给的初值离真实位置超过这么远,重定位不上(收下之后报 lost)。
        self.reloc_basin_m = 1.0
        self.error: str = ""
        self._jump_next = False
        self._reloc_id: int | None = None
        self._seq = 0
        self._stamp_ns = 0
        self._queue: list[tuple[int, Pose]] = []
        self._steps = 0
        self._last: tuple[float, float, float] | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> bool:
        """连上、握手。被拒(比如已经有一个定位器连着)回假,原因在 :attr:`error`。"""
        self._reader, self._writer = await asyncio.open_unix_connection(str(self.path),
                                                                        limit=MAX_LINE + 2)
        await self._say(Hello(proto=PROTO, name=self.name, version="sim"))
        line = await self._reader.readline()
        got = parse(line) if line else Error(reason="连上就断了")
        if not isinstance(got, Hello):
            self.error = got.reason if isinstance(got, Error) else f"握手回的是 {got!r}"
            await self.close()
            return False
        self._task = asyncio.get_running_loop().create_task(self._read())
        return True

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def truth(self) -> tuple[float, float, float]:
        """狗此刻在地图上的真实位置。"""
        r = self.robot
        return compose(self.frame, (r.x, r.y, r.yaw))

    async def step(self) -> None:
        """一拍:发一条心跳;算一帧位姿(连着、有图、没在停发),按延迟、卡顿发出去。"""
        if not self.connected:
            return
        self._steps += 1
        self.offset[0] += self.drift_per_step[0]
        self.offset[1] += self.drift_per_step[1]
        self._seq += 1
        await self._say(Heartbeat(seq=self._seq))
        if self.map is not None and self.paused > 0:
            self.paused -= 1
        elif self.map is not None:
            self._queue.append((self._steps + self.latency_steps, self._make()))
        if self.held > 0:
            self.held -= 1
            return
        due = [m for at, m in self._queue if at <= self._steps]
        self._queue = [(at, m) for at, m in self._queue if at > self._steps]
        for m in due:
            await self._say(m)

    def _make(self) -> Pose:
        now_ms = int(self.robot._now())
        self._stamp_ns = max(now_ms * 1_000_000, self._stamp_ns + 1)
        if self.frozen is not None:
            x, y, yaw = self.frozen
        else:
            t = self.truth()
            x = t[0] + self.offset[0] + self._rng.gauss(0.0, self.noise_xy)
            y = t[1] + self.offset[1] + self._rng.gauss(0.0, self.noise_xy)
            yaw = t[2] + self.offset[2] + self._rng.gauss(0.0, self.noise_yaw)
            self._last = (x, y, yaw)
        age = 0 if self.coast_from_ms is None else max(0, now_ms - self.coast_from_ms)
        self._seq += 1
        p = Pose(seq=self._seq, stamp_ns=self._stamp_ns, map_id=self.map[0],
                 map_version=self.map[1], x=x, y=y, yaw=yaw, sigma_xy=self.sigma_xy,
                 sigma_yaw=self.sigma_yaw, source="scan_match", jump=self._jump_next,
                 reloc_id=self._reloc_id, meas_age_ms=age)
        self._jump_next = False
        self._reloc_id = None
        return p

    async def run(self, rate_hz: float = 10.0) -> None:
        while self.connected:
            await self.step()
            await asyncio.sleep(1.0 / rate_hz)

    def freeze(self) -> None:
        """匹配线程死了、定时器还在:一直重发最后那个位置(时间戳、序号照涨)。``frozen = None``
        解冻。"""
        self.frozen = self._last if self._last is not None else self.truth()

    def stall(self, steps: int) -> None:
        """卡 ``steps`` 拍:这几拍的位姿攒着,卡完一起到(时间戳是各自算出来的那一刻)。"""
        self.held = steps

    def jump(self, dx: float, dy: float, dyaw: float = 0.0, *, flag: bool = True) -> None:
        """一下偏出去;``flag`` 为假就是不报 ``jump``(「自信地跳错」)。"""
        self.offset[0] += dx
        self.offset[1] += dy
        self.offset[2] += dyaw
        self._jump_next = flag

    async def lose(self, reason: str = "匹配不上") -> None:
        self._seq += 1
        await self._say(State(seq=self._seq, state="lost", reason=reason))

    async def recover(self) -> None:
        self._seq += 1
        await self._say(State(seq=self._seq, state="tracking"))

    # ------------------------------------------------------------ 内部

    async def _say(self, msg: Any) -> None:
        if self._writer is None:
            return
        try:
            self._writer.write(encode(msg))
            await self._writer.drain()
        except (ConnectionError, OSError):
            self._writer.close()

    async def _read(self) -> None:
        assert self._reader is not None
        while True:
            try:
                line = await self._reader.readline()
            except (ConnectionError, OSError, ValueError):
                break
            if not line:
                break
            try:
                msg = parse(line)
            except ContractError as exc:
                log.warning("仿真定位器收到一行不成形:%s", exc)
                continue
            if isinstance(msg, SetPrior):
                self.priors.append(msg)
                ok, why = self.prior_answer
                if ok:
                    self.map = (msg.map_id, msg.map_version)
                await self._say(Reply(req=msg.req, ok=ok, reason=why))
            elif isinstance(msg, Relocalize):
                self.relocs.append(msg)
                if self.refuse_reloc:
                    await self._say(Reply(req=msg.req, ok=False, reason=self.refuse_reloc))
                    continue
                await self._say(Reply(req=msg.req, ok=True))
                t = self.truth()
                if math.hypot(msg.x - t[0], msg.y - t[1]) > self.reloc_basin_m:
                    self._seq += 1                     # 初值离得太远:在那附近对不上
                    await self._say(State(seq=self._seq, state="lost",
                                          reason="人给的初值附近对不上"))
                    continue
                self.offset = [0.0, 0.0, 0.0]          # 仿真:在初值附近按真实位置对上了
                self.frozen = None
                self._jump_next, self._reloc_id = True, msg.req
            elif isinstance(msg, Error):
                self.error = msg.reason
        if self._writer is not None:
            self._writer.close()
