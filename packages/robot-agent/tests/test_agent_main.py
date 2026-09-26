"""``d1max-agent`` 入口(W00b 决定 3、4):装配 transport/HAL/引擎/运行时(W00c5e 起不再托管老的
HTTP 面)。这里在进程内调 ``build()``,transport 用 memory://(站点客户端挂同一个 broker)。"""

from __future__ import annotations

import subprocess
import sys

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


def test_hal_d1max的参数(tmp_path):
    a = _args(tmp_path, "--hal", "d1max", "--sidecar", "127.0.0.1:9001", "--mps-per-unit", "0.42",
              "--radps-per-unit", "0.9", "--deadband", "0.06", "--max-fraction", "0.3",
              "--invert-yaw", "--stopped-eps", "0.05")
    assert a.hal == "d1max" and a.sidecar == ("127.0.0.1", 9001)
    assert (a.mps_per_unit, a.radps_per_unit, a.deadband, a.max_fraction, a.invert_yaw) == \
        (0.42, 0.9, 0.06, 0.3, True)
    assert a.stopped_eps == 0.05
    d = _args(tmp_path, "--hal", "d1max")
    assert d.sidecar == ("127.0.0.1", 8090) and d.mps_per_unit == 0.4 and not d.invert_yaw
    assert d.stopped_eps == 0.02
    with pytest.raises(SystemExit):
        _args(tmp_path, "--hal", "d1max", "--sidecar", "nope")
    with pytest.raises(SystemExit):
        _args(tmp_path, "--sidecar", "127.0.0.1:9001"), "sim 不收 --sidecar"


def test_hal_d1max_经仿真旁路跑通一条goto(tmp_path):
    """W00d 验收:代理 ``--hal d1max`` 接旁路进程(这里是仿真旁路)跑完一趟 goto。仿真旁路里
    比例 1.0 折 1.2 m/s、转向折 1.5 rad/s,低于比例 0.2 原地蹭不动,参数照这个配。"""
    import time

    from d1max_patrol.app.bridge import LoopBridge
    from d1max_patrol.protocol.agent_frames import MotionStatus as SdkMotion
    from d1max_sim.agent_server import SimAgentServer

    sim_loop = LoopBridge()
    sim_loop.start()
    sim = SimAgentServer(port=0)

    async def _起():
        await sim.start()
        sim.motion = SdkMotion.GENERAL                # 站着待命:代理不替人站起
    sim_loop.call(_起)
    a = None
    try:
        # 这条测的是旁路进程的运动链路;d1max 默认要人监护(W00c6i),
        # 监护本身在 test_supervision.py 测。
        args = _args(tmp_path, "--hal", "d1max", "--sidecar", f"127.0.0.1:{sim.port}",
                     "--mps-per-unit", "1.2", "--radps-per-unit", "1.5", "--deadband", "0.25",
                     "--autonomy", "autonomous")
        a = agent_main.build(args)
        a.start()
        site = DispatchClient(MemoryTransport(a.broker, "site"), T, now_ms=agent_main.wall_ms)
        a.bridge.call(site.start)
        # W00c6e:真狗开机要人给一次位置(里程锚定)。仿真旁路的狗在原点。
        reloc = site.new_command("relocalize", {"x": 0.0, "y": 0.0, "yaw": 0.0}, ttl_ms=60_000,
                                 control_epoch=1)
        ack = a.bridge.call(lambda: site.send(reloc, timeout_s=5.0), timeout_s=10.0)
        assert ack.result is AckResult.ACCEPTED, ack
        target = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=0.8, y=0.4,
                         yaw=0.0)
        cmd = site.new_command("goto", {"target": target.to_wire(), "max_speed_mps": 1.0},
                               ttl_ms=60_000, control_epoch=1)
        ack = a.bridge.call(lambda: site.send(cmd, timeout_s=5.0), timeout_s=10.0)
        assert ack.result is AckResult.ACCEPTED, ack
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not a.runtime.processor.finished:
            time.sleep(0.05)
        fin = a.runtime.processor.finished[-1]
        assert fin.task_id == cmd.task_id and fin.state is TaskState.DONE, (fin.state, fin.detail)
        assert abs(sim.x - 0.8) < 0.2 and abs(sim.y - 0.4) < 0.2, (sim.x, sim.y)
        assert "vel" in [c for c, _ in sim.commands]
        caps = site.capabilities
        assert caps is not None and caps.adapter == "d1max/0.1.0"
    finally:
        if a is not None:
            a.stop()
        assert "shutdown" not in [c for c, _ in sim.commands], "代理退出不许让旁路放控制权"
        sim_loop.call(sim.stop)
        sim_loop.stop()


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


def test_stop之后HAL的控制权放了_链路关了(tmp_path):
    a = agent_main.build(_args(tmp_path))
    a.start()
    hal = a.hal
    a.stop()
    import asyncio
    h = asyncio.run(hal.health())
    held = asyncio.run(hal.control_status()).held
    assert h.link_ok is False and held is False


def test_mqtts必须带齐三件证书_其他transport不许带(tmp_path):
    tls = ["--tls-ca", "/e/ca.crt", "--tls-cert", "/e/r.crt", "--tls-key", "/e/r.key"]
    with pytest.raises(SystemExit):
        _args(tmp_path, "--transport", "mqtts://site:8883")
    with pytest.raises(SystemExit):
        _args(tmp_path, "--transport", "mqtts://site:8883", *tls[:4])
    with pytest.raises(SystemExit):
        _args(tmp_path, *tls)                                 # memory:// 带证书
    a = _args(tmp_path, "--transport", "mqtts://site:8883", *tls)
    assert (a.tls_ca, a.tls_cert, a.tls_key) == ("/e/ca.crt", "/e/r.crt", "/e/r.key")


def test_证书交给PahoTransport(monkeypatch):
    seen = {}

    class 假:
        def __init__(self, url, client_id, **kw):
            seen.update(kw, url=url, client_id=client_id)

    import d1max_contract.paho_transport as pt
    monkeypatch.setattr(pt, "PahoTransport", 假)
    agent_main._make_transport("mqtts://site:8883", "D1MAX-SIM", None,
                               tls=("/e/ca.crt", "/e/r.crt", "/e/r.key"))
    assert seen == {"url": "mqtts://site:8883", "client_id": "D1MAX-SIM", "tls_ca": "/e/ca.crt",
                    "tls_cert": "/e/r.crt", "tls_key": "/e/r.key"}


def test_视频参数_仿真用测试图_真狗拉相机RTSP(tmp_path):
    from d1max_agent.video_push import lavfi_source
    a = agent_main.build(_args(tmp_path))
    try:
        assert a.runtime.video is not None and a.runtime.video._source is lavfi_source
    finally:
        a.stop()
    d = _args(tmp_path, "--hal", "d1max", "--camera-host", "10.1.2.3", "--video-transcode")
    assert d.camera_host == "10.1.2.3" and d.video_transcode is True
    assert _args(tmp_path).camera_host == "192.168.234.1"


# ------------------------------------------------------------ W00c5d:发件箱

def _outbox_args(tmp_path, *extra):
    reg = tmp_path / "registration.json"
    REG.save(reg)
    return agent_main.parse_args([
        "--transport", "memory://", "--hal", "sim", "--registration", str(reg),
        "--store-dir", str(tmp_path / "store"), "--outbox", str(tmp_path / "outbox"),
        "--map", "estate-1:7", "--home", "0,0,0", "--period", "0.01", *extra])


def test_发件箱参数_运行记录落发件箱_接收口默认跟着broker走(tmp_path):
    a = _outbox_args(tmp_path)
    assert a.runs_root == tmp_path / "outbox" / "runs" and a.outbox_max_gb == 20.0
    assert a.intake is None, "memory:// 没有接收口:只攒不传"
    tls = ["--tls-ca", "/e/ca.crt", "--tls-cert", "/e/r.crt", "--tls-key", "/e/r.key"]
    m = _outbox_args(tmp_path, "--transport", "mqtts://site.lan:8883", *tls)
    assert m.intake == "https://site.lan:8444"
    m = _outbox_args(tmp_path, "--transport", "mqtts://site.lan:8883", *tls,
                     "--intake", "https://10.0.0.2:9444")
    assert m.intake == "https://10.0.0.2:9444"
    with pytest.raises(SystemExit):                         # 两个都给:runs 到底落哪?
        _outbox_args(tmp_path, "--runs-root", str(tmp_path / "runs"))
    with pytest.raises(SystemExit):                         # 两个都不给
        agent_main.parse_args([
            "--transport", "memory://", "--registration", str(tmp_path / "registration.json"),
            "--store-dir", str(tmp_path / "s"), "--map", "estate-1:7"])
    with pytest.raises(SystemExit):                         # 只认 https(mTLS)
        _outbox_args(tmp_path, "--transport", "mqtts://site.lan:8883", *tls,
                     "--intake", "http://site:8444")


def test_发件箱装上了_盘况进遥测(tmp_path):
    a = agent_main.build(_outbox_args(tmp_path))
    try:
        assert a.pump is not None and a.pump.box.runs_root == tmp_path / "outbox" / "runs"
        a.start()
        import time
        deadline = time.monotonic() + 10
        while a.pump.facts() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        f = a.runtime._storage()
        assert f is not None and f.outbox_cap_bytes == 20 * 2**30
    finally:
        a.stop()
    assert not a.pump._thread.is_alive(), "收尾要停发件箱线程"


def test_发件箱的路径要绝对_要求的挂载点没挂就不起_老HTTP面的参数不认了(tmp_path):
    reg = tmp_path / "registration.json"
    REG.save(reg)
    base = ["--transport", "memory://", "--registration", str(reg), "--store-dir",
            str(tmp_path / "s"), "--map", "estate-1:7"]
    with pytest.raises(SystemExit):
        agent_main.parse_args([*base, "--outbox", "relative/outbox"])
    for 老 in (["--legacy-http", "127.0.0.1:0"], ["--pin", "x"], ["--sn", "x"]):
        with pytest.raises(SystemExit):                      # W00c5e:老 HTTP 面退役
            agent_main.parse_args([*base, "--outbox", str(tmp_path / "o"), *老])
    with pytest.raises(SystemExit):                          # tmp_path 不是挂载点
        agent_main.parse_args([*base, "--outbox", str(tmp_path / "o"), "--outbox-mount",
                               str(tmp_path)])
    ok = agent_main.parse_args([*base, "--outbox", "/o", "--outbox-mount", "/"])
    assert ok.outbox_mount == "/"


def test_引擎跑完之后_最后一趟不再算正在写(tmp_path):
    from types import SimpleNamespace
    a = agent_main.build(_outbox_args(tmp_path))
    try:
        eng = a.parts.engine
        box = a.pump.box
        eng._live = SimpleNamespace(archive=SimpleNamespace(path=tmp_path / "x"))
        assert box.active() == set(), "引擎没在跑:上一趟可以删了"
        import asyncio
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        eng._task = fut
        assert box.active() == {tmp_path / "x"}
        loop.close()
        assert eng.run_suffix().isdigit() and len(eng.run_suffix()) == 6
    finally:
        a.parts.engine._task = None
        a.stop()


def test_仿真定位器要配仿真狗和本机定位桥(tmp_path):
    """W09a:``--sim-localizer`` 只在 ``--hal sim --localizer bridge`` 时有意义。"""
    assert _args(tmp_path).localizer == "anchor", "默认照旧里程锚定"
    with pytest.raises(SystemExit):
        _args(tmp_path, "--sim-localizer")
    with pytest.raises(SystemExit):
        _args(tmp_path, "--hal", "d1max", "--localizer", "bridge", "--sim-localizer")
    with pytest.raises(SystemExit):
        _args(tmp_path, "--localizer", "amcl")


def test_仿真定位器经本机定位桥_跑通一条goto(tmp_path):
    import shutil
    import tempfile
    import time
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix="lm", dir="/tmp"))      # Unix 套接字路径要短
    a = agent_main.build(_args(tmp_path, "--localizer", "bridge", "--sim-localizer",
                               "--loc-socket", str(d / "loc.sock")))
    try:
        a.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not a.bridge.call(
                lambda: _ok(a), timeout_s=5.0):
            time.sleep(0.05)
        assert a.bridge.call(lambda: _ok(a), timeout_s=5.0), "仿真定位器连上、定到位了"
        site = DispatchClient(MemoryTransport(a.broker, "site"), T, now_ms=agent_main.wall_ms)
        a.bridge.call(site.start)
        target = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=0.6, y=0.0, yaw=0.0)
        cmd = site.new_command("goto", {"target": target.to_wire(), "max_speed_mps": 1.0},
                               ttl_ms=60_000, control_epoch=1)
        ack = a.bridge.call(lambda: site.send(cmd, timeout_s=5.0), timeout_s=10.0)
        assert ack.result is AckResult.ACCEPTED, ack
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not a.runtime.processor.finished:
            time.sleep(0.05)
        fin = a.runtime.processor.finished[-1]
        assert fin.state is TaskState.DONE, (fin.state, fin.detail)
    finally:
        a.stop()
        shutil.rmtree(d, ignore_errors=True)


async def _ok(a) -> bool:
    return a.runtime.parts.nav.anchor.ok(True)
