"""仿真旁路进程自身的行为。

``test_sidecar_device.py`` 测的是"客户端面对这台仿真器时表现对不对";这里测的
是"这台仿真器本身像不像真机"。两边分开,是因为仿真器同时也是
``motion/patrol_agent.cpp`` 的**可执行契约** —— C++ 那头要照着它写,所以它自己
的行为得先被钉死,不能只在客户端的用例里被间接观察到。

用裸 asyncio 流客户端说话,不经 ``SidecarDeviceBackend``,免得两边一起错还测不出来。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import pytest

from d1max_patrol.protocol.agent_frames import (
    PROTO_VERSION,
    Ack,
    ControlLostFrame,
    EmergencyStatus,
    FaultFrame,
    Hello,
    MotionStatus,
    OdomFrame,
    StateFrame,
    decode_frame,
    encode_command,
)
from d1max_sim.agent_server import STAND_SECONDS, WALK_DEADBAND, SimAgentServer


class _RawClient:
    """一条裸 TCP + JSONL 连接,只做收发,不做任何解释。"""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._id = 0

    async def recv(self, timeout_s: float = 3.0):
        line = await asyncio.wait_for(self._reader.readline(), timeout_s)
        if not line:
            raise AssertionError("仿真器把连接关了")
        return decode_frame(line)

    async def recv_of(self, kind: type, timeout_s: float = 3.0):
        """一直读到指定类型的帧 —— 遥测是持续流,回执会夹在中间。"""
        async def _pump():
            while True:
                frame = await self.recv(timeout_s)
                if isinstance(frame, kind):
                    return frame
        return await asyncio.wait_for(_pump(), timeout_s)

    async def send(self, cmd: str, **fields) -> int:
        self._id += 1
        self._writer.write(encode_command(self._id, cmd, **fields))
        await self._writer.drain()
        return self._id

    async def recv_raw(self, timeout_s: float = 3.0) -> bytes:
        """收一行原始字节,不解码成帧 —— 用来看仿真器到底吐了什么。"""
        return await asyncio.wait_for(self._reader.readline(), timeout_s)

    async def send_raw(self, text: str) -> None:
        self._writer.write((text + "\n").encode("utf-8"))
        await self._writer.drain()

    async def call(self, cmd: str, timeout_s: float = 3.0, **fields) -> Ack:
        """发一条命令,等到它自己那条回执(按 id 对号入座)。"""
        cmd_id = await self.send(cmd, **fields)

        async def _pump() -> Ack:
            while True:
                frame = await self.recv(timeout_s)
                if isinstance(frame, Ack) and frame.id == cmd_id:
                    return frame
        return await asyncio.wait_for(_pump(), timeout_s)

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


@contextlib.asynccontextmanager
async def _client(**kwargs) -> AsyncIterator[tuple[SimAgentServer, _RawClient]]:
    sim = SimAgentServer(port=0, **kwargs)
    await sim.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", sim.port)
    client = _RawClient(reader, writer)
    try:
        yield sim, client
    finally:
        await client.close()
        await sim.stop()


# ---------------------------------------------------------------- 握手


async def test_连上第一帧就是自我介绍():
    async with _client() as (_sim, client):
        hello = await client.recv()
        assert isinstance(hello, Hello)
        assert hello.proto == PROTO_VERSION
        assert hello.sdk == "0.1.1"
        assert hello.held is True


async def test_上装占着时hello就把held报成false():
    # 客户端在**下发任何动作之前**就该知道自己没方向盘,而不是发出去才被拒。
    async with _client(deny_control=True) as (_sim, client):
        hello = await client.recv()
        assert isinstance(hello, Hello)
        assert hello.held is False


async def test_端口在启动前读不到():
    sim = SimAgentServer(port=0)
    with pytest.raises(RuntimeError, match="还没启动"):
        _ = sim.port


async def test_可以当异步上下文管理器用():
    async with SimAgentServer(port=0) as sim:
        assert sim.port > 0


async def test_两个客户端各自收到自己的遥测():
    # 真机上旁路进程要同时服务巡检程序和人手工敲的调试连接,不能是单客户端。
    async with _client() as (sim, first):
        reader, writer = await asyncio.open_connection("127.0.0.1", sim.port)
        second = _RawClient(reader, writer)
        try:
            assert isinstance(await first.recv_of(StateFrame), StateFrame)
            assert isinstance(await second.recv_of(StateFrame), StateFrame)
        finally:
            await second.close()


# ---------------------------------------------------------------- 遥测


async def test_状态帧带全两块电池和两路急停():
    async with _client(battery=64.0) as (_sim, client):
        state = await client.recv_of(StateFrame)
        assert state.battery1 == 64.0
        assert state.battery2 == 64.0
        assert state.estop_software is EmergencyStatus.RECOVER
        assert state.estop_hardware is EmergencyStatus.RECOVER
        assert state.motion is MotionStatus.LIE_DOWN


async def test_里程帧一开始是原点():
    async with _client() as (_sim, client):
        odom = await client.recv_of(OdomFrame)
        assert (odom.x, odom.y, odom.yaw) == (0.0, 0.0, 0.0)


async def test_遥测是持续流不是一次性快照():
    async with _client() as (_sim, client):
        for _ in range(3):
            await client.recv_of(StateFrame)


async def test_到点自动丢控制权():
    # 模拟"跑着跑着上装把控制权收回去了"(#40/#46)。
    async with _client(drop_control_after=0.05) as (_sim, client):
        lost = await client.recv_of(ControlLostFrame)
        assert "收回" in lost.reason
        ack = await client.call("stand")
        assert ack.ok is False


# ---------------------------------------------------------------- 命令


async def test_看不懂的命令行只记日志不回执也不断链():
    # 回不了对号入座的 ack —— 那一行根本没解出 id。客户端靠自己的超时兜底。
    async with _client() as (_sim, client):
        await client.send_raw("这不是 JSON")
        await client.send_raw('{"没有 cmd 字段": 1}')
        ack = await client.call("hold")
        assert ack.ok is True


async def test_不认识的命令回一条失败回执():
    async with _client() as (_sim, client):
        ack = await client.call("somersault")
        assert ack.ok is False
        assert "不认识的命令" in ack.error


async def test_回执按id对号入座():
    async with _client() as (_sim, client):
        first = await client.send("light", which="front", on=True)
        second = await client.send("light", which="back", on=False)
        seen = []
        while len(seen) < 2:
            frame = await client.recv()
            if isinstance(frame, Ack):
                seen.append(frame.id)
        assert seen == [first, second]


async def test_命令按顺序记下来给测试断言用():
    async with _client() as (sim, client):
        await client.call("hold")
        await client.call("light", which="both", on=True)
        assert sim.commands == [("hold", {}),
                                ("light", {"which": "both", "on": True})]


async def test_未知灯位被拒():
    async with _client() as (_sim, client):
        ack = await client.call("light", which="上面那个")
        assert ack.ok is False
        assert "未知的灯位" in ack.error


# ---------------------------------------------------------------- 控制权


async def test_上装占着时hold被拒且错误原文照抄真机():
    async with _client(deny_control=True) as (_sim, client):
        ack = await client.call("hold")
        assert ack.ok is False
        # 这句是真机原样打出来的(#40/#47)。改了它,现场就对不上日志了。
        assert "Controlled denial of service" in ack.error


@pytest.mark.parametrize("cmd", ["stand", "lie", "head", "walk"])
async def test_没控制权时动作类命令一律被拒(cmd):
    async with _client(deny_control=True) as (_sim, client):
        ack = await client.call(cmd, seconds=1.0, fwd=0.4)
        assert ack.ok is False
        assert "Controlled denial of service" in ack.error


async def test_急停不需要控制权():
    """安全动作在任何状态下都得能发出去,包括控制权已经没了的时候。"""
    async with _client(deny_control=True) as (sim, client):
        ack = await client.call("estop", on=True)
        assert ack.ok is True
        assert sim.estop_software is EmergencyStatus.STOP


async def test_shutdown会广播控制权丢失():
    async with _client() as (_sim, client):
        await client.send("shutdown")
        lost = await client.recv_of(ControlLostFrame)
        assert "退出" in lost.reason


async def test_hold能把丢掉的控制权拿回来():
    # 仿真里 hold 是能成功的(只要没被 deny)。真机上不一定 —— 这一条刻意
    # 保持乐观,好让"客户端在丢控后重试"这条路径可测。
    async with _client() as (sim, client):
        sim.drop_control()
        assert (await client.call("hold")).ok is True
        assert (await client.call("stand")).ok is True


# ---------------------------------------------------------------- 站起 / 趴下


async def test_站起是有过程的不是瞬间():
    """真机实测约 6s(#36)。仿真缩短了时长,但**保留"要花时间"这个性质**。"""
    async with _client() as (sim, client):
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert (await client.call("stand")).ok is True
        assert loop.time() - started >= STAND_SECONDS
        assert sim.motion is MotionStatus.GENERAL


async def test_站起中途会经过STAND_UP再落到GENERAL():
    # 0.1.1 的实测时序(#45)。上层要是只认 STAND_UP 就会永远等不到"站稳"。
    async with _client() as (sim, client):
        task = asyncio.ensure_future(client.call("stand"))
        await asyncio.sleep(STAND_SECONDS * 1.5)
        assert sim.motion is MotionStatus.STAND_UP
        await task
        assert sim.motion is MotionStatus.GENERAL


async def test_趴下():
    async with _client() as (sim, client):
        await client.call("stand")
        assert (await client.call("lie")).ok is True
        assert sim.motion is MotionStatus.LIE_DOWN


async def test_急停生效时拒绝站起():
    async with _client() as (_sim, client):
        await client.call("estop", on=True)
        ack = await client.call("stand")
        assert ack.ok is False
        assert "软急停生效中" in ack.error


async def test_解除急停后又能动():
    async with _client() as (sim, client):
        await client.call("estop", on=True)
        await client.call("estop", on=False)
        assert sim.estop_software is EmergencyStatus.RECOVER
        assert (await client.call("stand")).ok is True


# ---------------------------------------------------------------- 行走


async def test_趴着走不了():
    async with _client() as (_sim, client):
        ack = await client.call("walk", seconds=1.0, fwd=0.4)
        assert ack.ok is False
        assert "先 stand" in ack.error


async def test_时长非正被拒():
    async with _client() as (_sim, client):
        await client.call("stand")
        ack = await client.call("walk", seconds=0.0, fwd=0.4)
        assert ack.ok is False
        assert "时长要为正" in ack.error


async def test_量给够了才真的走():
    async with _client() as (sim, client):
        await client.call("stand")
        assert (await client.call("walk", seconds=1.0, fwd=0.4)).ok is True
        assert sim.x > 0.4
        assert sim.distance > 0.4
        # 走完自己停下,速度回零 —— 一次 Move 只维持约 1s(#38)。
        assert (sim.vx, sim.vy, sim.vyaw) == (0.0, 0.0, 0.0)
        assert sim.motion is MotionStatus.GENERAL


async def test_量给太小就安静地不动这正是现场那次站起不走():
    """#37: ``fwd=0.11`` 几乎不动,当时被误判成"模式不对"。

    真机在这种情况下**回成功、然后不动**。仿真照抄这个沉默的失败 —— 上层只能
    靠里程发现"发了走的命令但没走",指望异常是指望不上的。
    """
    async with _client() as (sim, client):
        await client.call("stand")
        ack = await client.call("walk", seconds=1.0, fwd=WALK_DEADBAND - 0.01)
        assert ack.ok is True             # 回执说成功
        assert sim.distance == 0.0        # 但一步没挪
        assert sim.motion is MotionStatus.GENERAL   # 连步态都没进


async def test_原地转向只改朝向不动位置():
    async with _client() as (sim, client):
        await client.call("stand")
        await client.call("walk", seconds=1.0, fwd=0.0, lat=0.0, yaw=0.5)
        assert sim.yaw != 0.0
        assert (sim.x, sim.y) == (0.0, 0.0)
        assert sim.distance == 0.0


async def test_转向量太小也一样安静地不动():
    async with _client() as (sim, client):
        await client.call("stand")
        ack = await client.call("walk", seconds=1.0, yaw=WALK_DEADBAND - 0.01)
        assert ack.ok is True
        assert sim.yaw == 0.0


async def test_横移走的是lat这一路():
    async with _client() as (sim, client):
        await client.call("stand")
        await client.call("walk", seconds=1.0, fwd=0.0, lat=0.4)
        assert abs(sim.y) > 0.3
        assert abs(sim.x) < 1e-9


async def test_急停之后走的命令被拒():
    async with _client() as (sim, client):
        await client.call("stand")
        await client.call("estop", on=True)
        ack = await client.call("walk", seconds=1.0, fwd=0.4)
        assert ack.ok is False
        assert "软急停生效中" in ack.error
        assert sim.distance == 0.0


async def test_走过之后里程帧跟着变():
    async with _client() as (_sim, client):
        await client.call("stand")
        await client.call("walk", seconds=1.0, fwd=0.4)
        odom = await client.recv_of(OdomFrame)
        while odom.x == 0.0:
            odom = await client.recv_of(OdomFrame)
        assert odom.x > 0.4


# ---------------------------------------------------------------- 注入


async def test_注入故障():
    async with _client() as (sim, client):
        sim.push_fault(2, 17, "腿过热")
        fault = await client.recv_of(FaultFrame)
        assert (fault.level, fault.code, fault.message) == (2, 17, "腿过热")


async def test_注入电量():
    async with _client(battery=71.0) as (sim, client):
        sim.set_battery(17.0)
        state = await client.recv_of(StateFrame)
        while state.battery1 != 17.0:
            state = await client.recv_of(StateFrame)
        assert state.battery == 17.0


async def test_注入裸行给客户端练手():
    async with _client() as (sim, client):
        sim.push_raw('{"t":"quantum_flux"}')
        # 它就是原样下去的,仿真器不做任何校验 —— 校验是客户端的事。
        # 得从遥测流里把它捞出来,所以一行行读到为止。
        while True:
            raw = json.loads(await client.recv_raw())
            if raw.get("t") not in ("hello", "state", "odom", "ack"):
                break
        assert raw == {"t": "quantum_flux"}


async def test_客户端断开后仿真器还能收下一个():
    # 巡检程序重启不该弄死旁路进程,这是整个架构的立身之本。
    async with _client() as (sim, first):
        await first.call("stand")
        await first.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", sim.port)
        second = _RawClient(reader, writer)
        try:
            hello = await second.recv()
            assert isinstance(hello, Hello) and hello.held is True
            state = await second.recv_of(StateFrame)
            assert state.motion is MotionStatus.GENERAL
        finally:
            await second.close()
