"""仿真定位器(W09a,W08 决定 10):连代理的本机定位桥,按仿真狗的**真实位置**出位姿 —— 仿真里地图系就是
里程系。测试、W10/W11 的仿真用它;真狗的定位器是 W09b 的 ROS 节点,走同一个桥、同一份报文。

能注入的毛病:噪声、慢漂移、一下偏出去(可以报 ``jump``,也可以不报 —— 不报就是「自信地跳错」,代理
靠跟里程交叉校验抓)、停发几帧(过期)、说丢了、拒重定位、换先验失败、断开。

``step()`` 发一帧(测试一拍调一次,确定性);``run()`` 按频率一直发(仿真跑起来用)。
"""

from __future__ import annotations

import asyncio
import logging
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


class SimLocalizer:
    def __init__(self, robot: Any, path: Path | str, *, name: str = "sim-loc",
                 sigma_xy: float = 0.05, sigma_yaw: float = 0.01, noise_xy: float = 0.0,
                 noise_yaw: float = 0.0, seed: int = 0) -> None:
        self.robot = robot
        self.path = Path(path)
        self.name = name
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
        self.error: str = ""
        self._jump_next = False
        self._reloc_id: int | None = None
        self._seq = 0
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

    async def step(self) -> None:
        """发一帧位姿(连着、有图、没在停发)与一条心跳。"""
        if not self.connected:
            return
        self.offset[0] += self.drift_per_step[0]
        self.offset[1] += self.drift_per_step[1]
        self._seq += 1
        await self._say(Heartbeat(seq=self._seq))
        if self.map is None:
            return
        if self.paused > 0:
            self.paused -= 1
            return
        r = self.robot
        self._seq += 1
        await self._say(Pose(
            seq=self._seq, stamp_ns=int(r._now()) * 1_000_000, map_id=self.map[0],
            map_version=self.map[1],
            x=r.x + self.offset[0] + self._rng.gauss(0.0, self.noise_xy),
            y=r.y + self.offset[1] + self._rng.gauss(0.0, self.noise_xy),
            yaw=r.yaw + self.offset[2] + self._rng.gauss(0.0, self.noise_yaw),
            sigma_xy=self.sigma_xy, sigma_yaw=self.sigma_yaw, source="scan_match",
            jump=self._jump_next, reloc_id=self._reloc_id))
        self._jump_next = False
        self._reloc_id = None

    async def run(self, rate_hz: float = 10.0) -> None:
        while self.connected:
            await self.step()
            await asyncio.sleep(1.0 / rate_hz)

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
                self.offset = [0.0, 0.0, 0.0]          # 仿真:按真实位置对上了
                self._jump_next, self._reloc_id = True, msg.req
                await self._say(Reply(req=msg.req, ok=True))
            elif isinstance(msg, Error):
                self.error = msg.reason
        if self._writer is not None:
            self._writer.close()
