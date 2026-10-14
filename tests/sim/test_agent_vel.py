"""仿真旁路的 ``vel``(协议 v3,W00d 设计决定一 A):带有效期的持续速度。立刻回执;有效期内
一直走,到期自停;新的 ``vel`` 覆盖旧的并续期;``halt``/``estop`` 插队作废;趴着、急停、没控制权、
超限都拒。跟 ``motion/patrol_agent.cpp`` 的 ``DoVel`` 同一套语义。"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from d1max_patrol.protocol.agent_frames import PROTO_VERSION, Ack
from d1max_sim.agent_server import SimAgentServer
from tests.sim.test_agent_server import _client

ROOT = Path(__file__).resolve().parents[2]


def test_协议是v9_跟C加加旁路进程的常量一致():
    src = (ROOT / "motion" / "patrol_agent.cpp").read_text(encoding="utf-8")
    m = re.search(r"static const int kProtoVersion = (\d+);", src)
    # W12:state 带 speed_level、hello 带 speed_level_want;W26:imu 帧;S11:auth
    assert m and int(m.group(1)) == PROTO_VERSION == 9


def test_CPP只收代理的运动命令_行为测试编译并通过(tmp_path):
    """S11:``motion/cmd_auth.hpp`` 的令牌、命令分类、FIFO 权限、令牌文件(0600、改名落盘、不跟链接)用
    ``motion/test_cmd_auth.cpp`` 跑,不链接厂商 SDK。"""
    import shutil
    import subprocess

    cxx = shutil.which("g++")
    if cxx is None:
        import pytest
        pytest.skip("没有 g++")
    exe = tmp_path / "test_cmd_auth"
    subprocess.run([cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-o", str(exe), str(ROOT / "motion" / "test_cmd_auth.cpp")],
                   check=True, capture_output=True, timeout=180)
    got = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "cmd_auth: ok" in got.stdout


def test_旁路进程每条TCP命令先过认证门_FIFO看权限_令牌先于开门():
    """S11 的接线(真二进制要连厂商 SDK,开发机上跑不起来,这里核源码):读线程里 auth 先处理、
    每条命令先过 ``ConnAuth::Check`` 才插队或进队;FIFO 按打开的那个 fd 判可不可信、不可信的只收
    停车;令牌在开门之前写好;hello 报 ``auth``。"""
    src = (ROOT / "motion" / "patrol_agent.cpp").read_text(encoding="utf-8")
    code = "\n".join(ln.split("//")[0] for ln in src.splitlines())

    def body(sig: str) -> str:
        i = code.index(sig)
        return code[i:code.index("\n}\n", i)]
    serve = body("static void ServeClient(")
    gate = serve.index("auth.Check(cmd, JsonBoolField(line, \"on\", true))")
    assert serve.index('if (cmd == "auth")') < gate < serve.index("if (IsUrgent(cmd))") \
        < serve.index("conn.queue.push_back"), "先认证、再过门,才插队或进队"
    assert "cmdauth::ConnAuth auth(&g_token);" in serve, "每条连接自己的认证状态"
    between = serve[gate:serve.index("if (IsUrgent(cmd))")]
    assert re.search(r"if \(!deny\.empty\(\)\) \{.*SendTo\(fd, AckFrame\(id, Reject\(deny\)\)\);"
                     r"\s*continue;\s*\}", between, re.S), "过不了门就回拒、不往下走"
    auth = serve[serve.index('if (cmd == "auth")'):gate]
    assert "auth.Auth(got)" in auth and "continue;" in auth, "auth 自己回执,不往下走"
    fifo = body("static void FifoLoop(")
    assert "::fstat(fd, &st)" in fifo and "cmdauth::FifoTrusted(st, ::geteuid())" in fifo
    line = body("static void HandleFifoLine(")
    assert line.index("!trusted && !cmdauth::FifoOpen(cmd)") < line.index('if (cmd == "stand")')
    assert '",\\"auth\\":true"' in body("static std::string HelloFrame(")
    main = code[code.index("int main("):]
    assert main.index("cmdauth::WriteTokenFile(token_file, g_token)") \
        < main.index("client.Connect(") < main.index("Listen(listen_host, listen_port)")
    assert "g_token.clear();" in main, "没写成令牌文件就谁都认证不了(不留一个没人知道的令牌)"


async def _站起(sim: SimAgentServer, client) -> None:
    await client.recv()                                   # hello
    ack = await client.call("stand", timeout_s=10)
    assert ack.ok


async def test_vel立刻回执_有效期内一直走_到期自停():
    async with _client() as (sim, client):
        await _站起(sim, client)
        t0 = time.monotonic()
        ack = await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)
        assert ack.ok and time.monotonic() - t0 < 0.2, "vel 是立刻回执的,不等走完"
        await asyncio.sleep(0.15)
        assert sim.vx > 0
        await asyncio.sleep(0.4)
        assert sim.vx == 0.0, "有效期到了自己停"
        assert sim.x > 0.05


async def test_续期就一直走_halt插队立刻停():
    async with _client() as (sim, client):
        await _站起(sim, client)
        for _ in range(6):
            assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
            await asyncio.sleep(0.1)
        assert sim.vx > 0, "每拍续一次,中间不该停"
        assert (await client.call("halt")).ok
        await asyncio.sleep(0.08)
        assert sim.vx == 0.0


async def test_趴着_急停_没控制权_超限_ttl不对_都拒():
    async with _client() as (sim, client):
        await client.recv()
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "趴着" in ack.error
        assert (await client.call("stand", timeout_s=10)).ok
        for bad in ({"fwd": 0.9}, {"yaw": -0.6}, {"ttl_ms": 0}, {"ttl_ms": 5000},
                    {"fwd": float("nan")}):
            args = {"fwd": 0.3, "lat": 0.0, "yaw": 0.0, "ttl_ms": 300} | bad
            ack = await client.call("vel", **args)
            assert not ack.ok, bad
        assert (await client.call("estop", on=True)).ok
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "急停" in ack.error
    async with _client(held=False) as (sim, client):
        await client.recv()
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "Controlled denial" in ack.error


async def test_低于死区的比例_回执ok但不动():
    """真机的样子:量太小就只是原地蹭,不报错(清单 #37)。适配器那层自己拦死区。"""
    async with _client() as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.05, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx == 0.0 and sim.x == 0.0


async def test_vel不排在walk后面():
    async with _client() as (sim, client):
        await _站起(sim, client)
        await client.send("walk", seconds=5.0, fwd=0.3, lat=0.0, yaw=0.0)
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        ack = await client.call("vel", fwd=0.2, lat=0.0, yaw=0.0, ttl_ms=300)
        assert isinstance(ack, Ack) and time.monotonic() - t0 < 0.3


async def test_锁死与硬急停也拒_跟CPP的DoVel一致():
    from d1max_patrol.protocol.agent_frames import EmergencyStatus, MotionStatus

    async with _client() as (sim, client):
        await _站起(sim, client)
        sim.motion = MotionStatus.LOCKED
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "锁死" in ack.error
        sim.motion = MotionStatus.GENERAL
        sim.estop_hardware = EmergencyStatus.STOP
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "急停" in ack.error


def test_CPP运动安全门_行为测试编译并通过(tmp_path):
    """W00d 外审阻断 1:``motion/vel_gate.hpp`` 用假 SDK 跑行为测试(``motion/test_vel_gate.cpp``):
    halt/急停与 vel 登记同一把锁、急停闩锁、状态变坏作废且不复活、拿锁后与 Gait 后现查、
    并发压测。"""
    import shutil
    import subprocess

    cxx = shutil.which("g++")
    if cxx is None:
        import pytest
        pytest.skip("没有 g++")
    exe = tmp_path / "test_vel_gate"
    subprocess.run([cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-o", str(exe), str(ROOT / "motion" / "test_vel_gate.cpp")],
                   check=True, capture_output=True, timeout=180)
    got = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "全部通过" in got.stdout


def test_CPP抢控制权看回执_行为测试编译并通过_旁路四处都走它(tmp_path):
    """W35(2026-10-13 C40221):SDK 的 ``TakeControl`` 回没出错只说明收到了回执,回执是 FAILURE
    (遥控器还拿着)旁路也记成抢到了、把 held 置真。成没成只看回执(``motion/take_ack.hpp``)。"""
    import re
    import shutil
    import subprocess

    src = (ROOT / "motion" / "patrol_agent.cpp").read_text(encoding="utf-8")
    code = "\n".join(ln.split("//")[0] for ln in src.splitlines())
    # 带超时的 TakeControl(等回执的那种)只许出现在 TakeConfirmed 里;续期用的 TakeControl(0) 不算
    waits = re.findall(r"TakeControl\((?!0\)|\))[^)]*\)", code)
    assert waits == ["TakeControl(timeout_ms)"], waits
    assert code.count("TakeConfirmed(") == 5, "定义 1 处 + 开机、hold、SDK 说可用、每 6 秒重抢"
    assert "g_take.OnAck(a.error_code == 0, a.reason)" in code

    cxx = shutil.which("g++")
    if cxx is None:
        import pytest
        pytest.skip("没有 g++")
    exe = tmp_path / "test_take_ack"
    subprocess.run([cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-o", str(exe), str(ROOT / "motion" / "test_take_ack.cpp")],
                   check=True, capture_output=True, timeout=180)
    got = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "take_ack: ok" in got.stdout


def test_旁路进程的vel全走运动安全门_常量两边一致():
    from d1max_patrol.backends.sidecar_device import MAX_WALK_SPEED, VEL_TTL_MAX_MS, VEL_TTL_MIN_MS

    hdr = (ROOT / "motion" / "vel_gate.hpp").read_text(encoding="utf-8")
    for name, want in (("kMaxFraction", MAX_WALK_SPEED), ("kTtlMinMs", VEL_TTL_MIN_MS),
                       ("kTtlMaxMs", VEL_TTL_MAX_MS)):
        m = re.search(rf"constexpr \w+ {name} = ([0-9.]+);", hdr)
        assert m and float(m.group(1)) == want, name
    src = (ROOT / "motion" / "patrol_agent.cpp").read_text(encoding="utf-8")

    def body(sig: str) -> str:
        i = src.index(sig)
        return src[i:src.index("\n}\n", i)]
    assert "g_gate.Register(" in body("static Outcome DoVel(")
    assert "g_gate.Cancel()" in body("static Outcome DoHalt(")
    estop = body("static Outcome DoEstop(")
    assert estop.index("g_gate.LatchEstop(true)") < estop.index("SoftEmergencyStop"), \
        "先闩本地急停,再跟 SDK 说"
    assert estop.index("g_gate.LatchEstop(false)") > estop.index("SoftEmergencyStop")
    assert "g_gate.OnState(" in body("void OnRobotStateData(")
    lost = body("void OnControlLost(")
    assert "g_gate.OnControlLost()" in lost and "g_cancel_gen.fetch_add(1)" in lost, \
        "控制权丢了要作废速度目标与排队的 walk"
    assert "velgate::Driver<" in body("static void VelLoop(")
    assert "g_vel_mtx" not in src and "g_client->Move" not in body("static void VelLoop(")


async def test_走着的时候硬急停或趴下_目标作废_恢复了也不接着走():
    from d1max_patrol.protocol.agent_frames import EmergencyStatus, MotionStatus

    async with _client() as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)).ok
        await asyncio.sleep(0.12)
        assert sim.vx > 0
        sim.estop_hardware = EmergencyStatus.STOP
        await asyncio.sleep(0.12)
        assert sim.vx == 0.0
        sim.estop_hardware = EmergencyStatus.RECOVER
        await asyncio.sleep(0.2)
        assert sim.vx == 0.0, "急停前收下的速度不许在解除后复活"
    async with _client() as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)).ok
        await asyncio.sleep(0.12)
        sim.motion = MotionStatus.LIE_DOWN
        await asyncio.sleep(0.12)
        assert sim.vx == 0.0
        sim.motion = MotionStatus.GENERAL
        await asyncio.sleep(0.2)
        assert sim.vx == 0.0, "趴下前收下的速度不许在站起来后复活"


async def test_急停Unknown也拒_走着时变Unknown就停_恢复也不接着走():
    """复审阻断 1:急停三态,两路都明确 Recover 才能走(同 vel_gate.hpp)。"""
    from d1max_patrol.protocol.agent_frames import EmergencyStatus

    async with _client() as (sim, client):
        await _站起(sim, client)
        sim.estop_software = EmergencyStatus.UNKNOWN
        ack = await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)
        assert not ack.ok and "急停" in ack.error
        sim.estop_software = EmergencyStatus.RECOVER
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)).ok
        await asyncio.sleep(0.12)
        assert sim.vx > 0
        sim.estop_hardware = EmergencyStatus.UNKNOWN
        await asyncio.sleep(0.12)
        assert sim.vx == 0.0
        sim.estop_hardware = EmergencyStatus.RECOVER
        await asyncio.sleep(0.2)
        assert sim.vx == 0.0


async def test_控制权丢了_速度作废_拿回来也不接着走_新的vel照常():
    """复审阻断 2:控制权丢失是安全状态转换,不是暂停。"""
    async with _client() as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)).ok
        await asyncio.sleep(0.12)
        assert sim.vx > 0
        sim.drop_control()
        await asyncio.sleep(0.12)
        assert sim.vx == 0.0
        assert (await client.call("hold")).ok             # 重新拿到控制权
        await asyncio.sleep(0.2)
        assert sim.vx == 0.0, "丢控制权之前的速度不许复活"
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)).ok
        await asyncio.sleep(0.12)
        assert sim.vx > 0


async def test_净空许可_开了没许可只转不走_有许可才往前_后来的旧许可不截短():
    """W11 第二层(``--require-clearance``):同 ``vel_gate.hpp`` 的 ``Live``。"""
    async with _client(require_clearance=True) as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.3, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx == 0.0 and sim.vyaw != 0.0, "没许可:前进置零、转向照常"
        assert (await client.call("clear", ms=500, dist=2.0)).ok
        assert (await client.call("clear", ms=50, dist=2.0)).ok      # 晚到的旧许可
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx > 0, "有许可:往前"
        for bad in ({"ms": 0}, {"ms": 5000}, {"ms": "x"}):
            assert not (await client.call("clear", **bad)).ok, bad
    async with _client() as (sim, client):                      # 没开:照旧
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx > 0


def test_CPP的clear_插队_开关参数():
    src = (ROOT / "motion" / "patrol_agent.cpp").read_text(encoding="utf-8")
    assert '|| cmd == "clear"' in src and "g_gate.SetClearance(" in src
    assert '"--require-clearance"' in src and "g_gate.RequireClearance(true)" in src


async def test_净空许可门开着_调过头尾就不放行前进():
    """W11a:狗头的许可只管狗头那头;狗尾为前时往前走是往狗尾那头走。"""
    async with _client(require_clearance=True) as (sim, client):
        await _站起(sim, client)
        assert (await client.call("clear", ms=800, dist=2.0)).ok
        sim.head = 2
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx == 0.0
        sim.head = 1
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx > 0


async def test_净空许可分两头_往前走向哪头要哪头的许可():
    """W09i:同 vel_gate.hpp 的 LeadEndLocked。"""
    async with _client(require_clearance=True) as (sim, client):
        await _站起(sim, client)
        sim.head = 2
        assert (await client.call("clear", ms=800, dist=2.0, end="tail")).ok
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx > 0, "狗尾为前、有狗尾的许可:放"
        sim.head = 1
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx == 0.0, "狗头为前:狗尾的许可不算"
        assert not (await client.call("clear", ms=800, dist=2.0, end="side")).ok
    async with _client(require_clearance=True, follows_head=False) as (sim, client):
        await _站起(sim, client)
        sim.head = 2
        assert (await client.call("clear", ms=800, dist=2.0, end="tail")).ok
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx == 0.0, "SDK 不跟着调头:往前还是往狗头走,要狗头的许可"


async def test_速度档位_被换掉就不走_过一会儿自己设回去():
    """W12:同 vel_gate.hpp 的档位门 + patrol_agent 的档位线程。"""
    async with _client() as (sim, client):
        await _站起(sim, client)
        sim.speed_level = 2                                   # 有人在厂家 App 上换成中速
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx == 0.0, "档位不对:不走(按低速换算的比例会快一倍)"
        await asyncio.sleep(1.3)
        assert sim.speed_level == 1, "过一会儿自己设回去"
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx > 0
    async with _client(speed_level_want=0) as (sim, client):  # 不管档位
        await _站起(sim, client)
        sim.speed_level = 3
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx > 0 and sim.speed_level == 3
