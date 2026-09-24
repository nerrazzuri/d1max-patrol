"""``d1max-agent`` 入口(W00b 决定 3、4):装配 transport/HAL/引擎/运行时,可选托管老 AppServer
共用同一台引擎。这里在进程内调 ``build()``,transport 用 memory://(站点客户端挂同一个 broker)。"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

import pytest

from d1max_agent import main as agent_main
from d1max_contract.dispatch import DispatchClient
from d1max_contract.memory_broker import MemoryTransport
from d1max_contract.messages import AckResult, MapPose, TaskState
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics

REG = Registration(site_id="penang-1", robot_id="D1MAX-SIM", credential_fingerprint="sha256:x",
                   issued_at=0, expires_at=10**13)
T = Topics(site_id="penang-1", robot_id="D1MAX-SIM")


def _args(tmp_path, *extra):
    reg = tmp_path / "registration.json"
    REG.save(reg)
    return agent_main.parse_args([
        "--transport", "memory://", "--hal", "sim", "--registration", str(reg),
        "--store-dir", str(tmp_path / "store"), "--runs-root", str(tmp_path / "runs"),
        "--map", "estate-1:7", "--home", "0,0,0", "--period", "0.01", *extra])


def test_transport必须是mqtt或memory(tmp_path):
    with pytest.raises(SystemExit):
        _args(tmp_path, "--transport", "http://x")
    a = _args(tmp_path)
    assert a.transport == "memory://" and a.map == ("estate-1", "7")
    with pytest.raises(SystemExit):
        _args(tmp_path, "--map", "no-version")
    with pytest.raises(SystemExit):
        _args(tmp_path, "--home", "1,2")


def test_help子进程退0():
    got = subprocess.run([sys.executable, "-m", "d1max_agent.main", "--help"],
                         capture_output=True, text=True, timeout=60)
    assert got.returncode == 0 and "--transport" in got.stdout


def test_装配并跑通一条goto(tmp_path):
    args = _args(tmp_path)
    a = agent_main.build(args)
    try:
        a.start()
        site = DispatchClient(MemoryTransport(a.broker, "site"), T, now_ms=agent_main.wall_ms)
        a.bridge.call(site.start)
        target = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=0.6, y=0.0, yaw=0.0)
        cmd = site.new_command("goto", {"target": target.to_wire(), "max_speed_mps": 1.0},
                               ttl_ms=60_000, control_epoch=1)
        ack = a.bridge.call(lambda: site.send(cmd, timeout_s=5.0), timeout_s=10.0)
        assert ack.result is AckResult.ACCEPTED
        import time
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            st = site.status
            if st is not None and st.task is None and a.runtime.processor.finished:
                break
            time.sleep(0.05)
        fin = a.runtime.processor.finished[-1]
        assert fin.task_id == cmd.task_id and fin.state is TaskState.DONE, (fin.state, fin.detail)
        assert site.capabilities is not None and "goto" in site.capabilities.tasks
    finally:
        a.stop()


def test_legacy_http托管在同一进程_看得到MQTT派的那趟(tmp_path):
    args = _args(tmp_path, "--legacy-http", "127.0.0.1:0")
    a = agent_main.build(args)
    try:
        a.start()
        assert a.server is not None
        assert a.server.ctx.engine is a.runtime.parts.engine, "同一台引擎"
        site = DispatchClient(MemoryTransport(a.broker, "site"), T, now_ms=agent_main.wall_ms)
        a.bridge.call(site.start)
        target = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=3.0, y=0.0, yaw=0.0)
        cmd = site.new_command("goto", {"target": target.to_wire(), "max_speed_mps": 0.3},
                               ttl_ms=60_000, control_epoch=1)
        ack = a.bridge.call(lambda: site.send(cmd, timeout_s=5.0), timeout_s=10.0)
        assert ack.result is AckResult.ACCEPTED
        import time
        run = None
        for _ in range(100):
            with urllib.request.urlopen(f"{a.server.url}/api/state", timeout=5) as resp:
                run = json.loads(resp.read())["run"]
            if run.get("mission") == cmd.task_id and run.get("state") == "RUNNING":
                break
            time.sleep(0.05)
        assert run and run["mission"] == cmd.task_id, run
    finally:
        a.stop()


def _命令行(tmp_path, transport="memory://"):
    reg = tmp_path / "registration.json"
    REG.save(reg)
    return [sys.executable, "-m", "d1max_agent.main", "--transport", transport, "--hal", "sim",
            "--registration", str(reg), "--store-dir", str(tmp_path / "store"),
            "--runs-root", str(tmp_path / "runs"), "--map", "estate-1:7", "--home", "0,0,0"]


def test_SIGTERM好好收尾_退0(tmp_path):
    """systemd 停服务发的是 SIGTERM,不是 Ctrl+C:不接的话进程被直接杀掉,引擎归档不收口、
    HAL 不停、LWT 之外站点什么都收不到。"""
    import os
    import signal
    import threading
    import time
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    p = subprocess.Popen(_命令行(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=env)
    看门狗 = threading.Timer(90, p.kill)          # readline 会阻塞:超时由它来掐
    看门狗.start()
    try:
        deadline = time.monotonic() + 60
        line = ""
        while time.monotonic() < deadline:
            line = p.stdout.readline()
            if "起来了" in line or not line:
                break
        assert "起来了" in line, line
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
    finally:
        看门狗.cancel()
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, out
    assert "收尾" in out, out
    assert "HAL 已收尾" in out, "SIGTERM 要一路走到 HAL:停、放控制权、关链路"


def test_broker连不上_退1而不是挂着(tmp_path):
    """连不上就退出非零,让 systemd 的 Restart=always/RestartSec=5 接手重试;挂着不退的话
    systemd 以为它活着,一辈子不重试。"""
    got = subprocess.run(_命令行(tmp_path, "mqtt://127.0.0.1:1"), capture_output=True,
                         text=True, timeout=90)
    assert got.returncode == 1, (got.returncode, got.stdout[-2000:], got.stderr[-2000:])
    assert "起不来" in got.stderr + got.stdout


def test_hal_tick炸了下一拍照走(tmp_path):
    import time
    a = agent_main.build(_args(tmp_path))
    n = {"step": 0}
    real_step = a.runtime.step

    async def 数着(dt):
        n["step"] += 1
        await real_step(dt)

    def 炸(dt):
        raise RuntimeError("sim tick 炸了")

    a.runtime.step = 数着
    a.hal.tick = 炸
    try:
        a.start()
        time.sleep(0.5)
        assert n["step"] >= 5, n
    finally:
        a.stop()


def test_legacy_http暴露检查在起线程之前(tmp_path):
    import threading
    before = threading.active_count()
    with pytest.raises(SystemExit):
        agent_main.build(_args(tmp_path, "--legacy-http", "0.0.0.0:0"))
    assert threading.active_count() == before, "拒绝启动也不能留下事件循环线程"


def test_stop之后HAL的控制权放了_链路关了(tmp_path):
    a = agent_main.build(_args(tmp_path))
    a.start()
    hal = a.hal
    a.stop()
    import asyncio
    h = asyncio.run(hal.health())
    held = asyncio.run(hal.control_status()).held
    assert h.link_ok is False and held is False
