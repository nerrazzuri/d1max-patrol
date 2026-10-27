"""S11(地图与导航能力对账 2026-10-14):旁路进程只收代理的运动命令。

以前旁路进程的本机端口(127.0.0.1:8090)谁连上都能发 ``vel``/``walk`` —— 现场脚本、标定脚本、老命令行
全都绕过代理,也就绕过禁行区、限速区、避障守卫。协议 9:旁路进程起来时生成令牌写进 ``--token-file``,
每条连接先 ``auth`` 才能开车;停车、按下急停谁都能发(停车永远不挡)。仿真旁路进程(``SimAgentServer``)
是 C++ 那头的可执行契约,两边的命令分类由 ``test_仿真跟CPP的不要令牌的命令一致`` 对账。
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path

import pytest

from d1max_patrol.backends.base import DeviceBackendError
from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend, read_token
from d1max_patrol.protocol.agent_frames import NOT_AUTHORIZED, PROTO_VERSION
from d1max_sim import agent_server
from d1max_sim.agent_server import SimAgentServer, open_command

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "ab" * 32


@contextlib.asynccontextmanager
async def _sim(token: str | None = TOKEN):
    sim = SimAgentServer(port=0, token=token)
    await sim.start()
    try:
        yield sim
    finally:
        await sim.stop()


def _token_file(tmp_path, token: str = TOKEN) -> Path:
    p = tmp_path / "sidecar.token"
    p.write_text(token + "\n", encoding="utf-8")
    p.chmod(0o600)
    return p


async def _站起(sim: SimAgentServer, backend: SidecarDeviceBackend) -> None:
    await backend.stand()
    assert sim.motion.value in ("StandUp", "General")


async def test_协议9_hello说要令牌():
    assert PROTO_VERSION == 9
    async with _sim() as sim:
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0)
        await b.connect()
        try:
            assert b.hello.auth is True and b.hello.proto == 9
            assert not b.authorized, "没给令牌:只读"
        finally:
            await b.close()


async def test_没认证的连接_开不了车_停车急停照收(tmp_path):
    """现场脚本、老命令行这类没令牌的连接:运动命令一律 not_authorized;停车、按急停不挡。"""
    async with _sim() as sim:
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0)
        await b.connect()
        try:
            for call in (b.stand(), b.lie(), b.vel(0.3, 0.0, 0.0, 300),
                         b.walk(1.0, 0.4), b.set_gimbal(0.1, 0.0), b.set_light(True),
                         b.emergency_stop(False)):
                with pytest.raises(DeviceBackendError, match=NOT_AUTHORIZED):
                    await call
            with pytest.raises(DeviceBackendError, match=NOT_AUTHORIZED):
                await b.acquire_control()                # hold 也要令牌:会从遥控器手里抢控制权
            await b.halt()
            await b.emergency_stop(True)
            assert sim.estop_software.value == "Stop", "急停按下去了"
            assert sim.distance == 0.0 and sim.motion.value == "LieDown", "机器人一下没动"
        finally:
            await b.close()


async def test_代理带令牌_连上就认证_能开车(tmp_path):
    async with _sim() as sim:
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=5.0,
                                 token_file=_token_file(tmp_path))
        await b.connect()
        try:
            assert b.authorized
            assert ("auth", {"token": TOKEN}) in sim.commands
            await b.acquire_control()
            await _站起(sim, b)
            await b.vel(0.3, 0.0, 0.0, 300)
            await b.emergency_stop(True)
            await b.emergency_stop(False)                # 松开急停:认证过的才行
        finally:
            await b.close()


async def test_令牌不对_连接就抛_不留半条链路(tmp_path):
    async with _sim() as sim:
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0,
                                 token_file=_token_file(tmp_path, "cd" * 32))
        with pytest.raises(DeviceBackendError, match="认证没过") as ei:
            await b.connect()
        assert TOKEN not in str(ei.value), "报错里不带旁路进程的令牌"
        assert not b.connected and not b.authorized


async def test_令牌文件读不了_说清楚在哪读的(tmp_path):
    async with _sim() as sim:
        missing = tmp_path / "nope" / "sidecar.token"
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0, token_file=missing)
        with pytest.raises(DeviceBackendError, match=re.escape(str(missing))):
            await b.connect()
        assert not b.connected


def test_令牌文件空的或太大_不当令牌(tmp_path):
    (tmp_path / "empty").write_text("\n", encoding="utf-8")
    (tmp_path / "big").write_text("a" * 300, encoding="utf-8")
    for name in ("empty", "big"):
        with pytest.raises(DeviceBackendError, match="不像令牌"):
            read_token(tmp_path / name)
    assert read_token(_token_file(tmp_path)) == TOKEN


async def test_每次连接现读令牌_旁路进程重启换了令牌照样连得上(tmp_path):
    p = _token_file(tmp_path)
    async with _sim() as sim:
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0, token_file=p)
        await b.connect()
        await b.close()
        sim.token = "ef" * 32
        p.write_text(sim.token, encoding="utf-8")
        await b.connect()
        try:
            assert b.authorized
        finally:
            await b.close()


async def test_认证按连接算_一条认证过不放行别的连接(tmp_path):
    async with _sim() as sim:
        a = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=5.0,
                                 token_file=_token_file(tmp_path))
        other = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0)
        await a.connect()
        await other.connect()
        try:
            await _站起(sim, a)
            with pytest.raises(DeviceBackendError, match=NOT_AUTHORIZED):
                await other.vel(0.3, 0.0, 0.0, 300)
            await a.vel(0.3, 0.0, 0.0, 300)
        finally:
            await a.close()
            await other.close()


async def test_老旁路进程不核令牌_代理照样能开车_记一条警告(tmp_path, monkeypatch, caplog):
    """新代理配 8 号及更老的旁路进程(先升了代理、旁路进程还没重编):能开(不然整机不能动),
    但要说出来。"""
    monkeypatch.setattr(agent_server, "PROTO_VERSION", 8)
    async with _sim(token=None) as sim:
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=5.0,
                                 token_file=_token_file(tmp_path))
        with caplog.at_level(logging.WARNING):
            await b.connect()
        try:
            assert b.hello.auth is False and b.authorized
            assert "auth" not in [c for c, _ in sim.commands], "老旁路进程不认 auth,不发"
            assert any("不核令牌" in r.message for r in caplog.records)
            await _站起(sim, b)
        finally:
            await b.close()


async def test_老代理认3到8_连9号旁路进程握手就拒(monkeypatch):
    """老代理不发 auth,连上了也开不了车 —— 握手时就拒(版本规矩不变:改了协议一起加号)。"""
    from d1max_patrol.backends import sidecar_device
    monkeypatch.setattr(sidecar_device, "PROTO_VERSION", 8)
    async with _sim() as sim:
        b = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0)
        with pytest.raises(DeviceBackendError, match="协议版本对不上"):
            await b.connect()


async def test_HAL带令牌能开车_没令牌速度命令被拒(tmp_path):
    from d1max_adapter_d1max.hal import D1MaxHal
    from d1max_contract.hal import VelocityCommand

    def _v():
        return VelocityCommand(seq=1, ttl_ms=300, frame="base", vx=0.3, vy=0.0, wz=0.0)
    async with _sim() as sim:
        hal = D1MaxHal("127.0.0.1", sim.port, token_file=str(_token_file(tmp_path)))
        await hal.connect()
        try:
            await hal.acquire_control()
            await hal.set_motion_mode("stand")
            r = await hal.set_velocity(_v())
            assert not r.rejected, r
        finally:
            await hal.close()
        bare = D1MaxHal("127.0.0.1", sim.port)
        await bare.connect()
        try:
            with pytest.raises(DeviceBackendError, match=NOT_AUTHORIZED):
                await bare.acquire_control()
            r = await bare.set_velocity(_v())
            assert r.rejected
        finally:
            await bare.close()


def test_仿真跟CPP的不要令牌的命令一致():
    """仿真旁路进程是 C++ 的可执行契约:不要令牌的命令两边一字不差(cmd_auth.hpp 的 IsOpen)。"""
    hdr = (ROOT / "motion" / "cmd_auth.hpp").read_text(encoding="utf-8")
    body = hdr[hdr.index("inline bool IsOpen("):]
    body = body[:body.index("\n}\n")]
    cpp_open = set(re.findall(r'cmd == "(\w+)"', body.split('if (cmd == "estop")')[0]))
    assert cpp_open == {"halt", "auth", "clear"}
    assert 'if (cmd == "estop") return estop_on;' in body
    for cmd in ("halt", "auth", "clear"):
        assert open_command(cmd, {})
    assert open_command("estop", {}) and open_command("estop", {"on": True})
    for args in ({"on": False}, {"on": 0}):
        assert not open_command("estop", args)
    for cmd in ("vel", "walk", "stand", "lie", "head", "hold", "shutdown", "light", "x"):
        assert not open_command(cmd, {})
