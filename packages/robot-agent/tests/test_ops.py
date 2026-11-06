"""W55 远程运维(机器人这边):日志、诊断、重启、空闲才重启整机、详细日志、清理、证书续签、
旁路程序随版本编。"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import json
import logging
import subprocess
from pathlib import Path

import pytest
from test_runtime_maps import REG, T, _cmd, 耳朵, 钟

from nexwar_adapter_sim.robot import SimRobot
from nexwar_agent import ops as O
from nexwar_agent.release_ops import build_sidecar
from nexwar_agent.runtime import AgentRuntime
from nexwar_contract.debuglog import DebugWatch, until_ms
from nexwar_contract.memory_broker import MemoryBroker, MemoryTransport
from nexwar_contract.privops import SimOps
from nexwar_patrol.protocol.nav_types import Pose

ROOT = Path(__file__).resolve().parents[3]


def _desk(tmp_path, *, priv=None, busy="", run=None, **kw):
    return O.OpsDesk(robot_id="A", priv=priv, data_root=tmp_path / "data",
                     busy=lambda: busy, run=run or (lambda argv, t: (0, "")), **kw)


def test_我们的服务名单_跟特权执行器一致():
    loader = importlib.machinery.SourceFileLoader("r", str(ROOT / "deploy" / "nexwar_ops_runner"))
    spec = importlib.util.spec_from_loader("r", loader)
    r = importlib.util.module_from_spec(spec)
    loader.exec_module(r)
    assert set(O.RESTART_UNITS) == set(r.ROBOT_UNITS) == set(O.OUR_UNITS)


def test_没装执行器_要root的不报_发了也说不支持(tmp_path):
    d = _desk(tmp_path)
    assert set(d.kinds()) == {"ops_journal", "ops_diag", "ops_debug", "ops_cleanup"}
    assert d.handle("ops_restart", {"unit": "nexwar_agent"}).startswith("unsupported")
    assert d.handle("ops_shell", {}) == "unsupported"


def test_日志_能看哪些_不在单子上的拒_打码_截尾从整行开始(tmp_path):
    lines = "".join(f"2026-10-31T10:00:{i % 60:02d} a token=SECRETVALUE{i} "
                    f"{'ab' * 20} line {i}\n" for i in range(20000))
    calls = []

    def run(argv, t):
        calls.append(argv)
        return 0, lines
    d = _desk(tmp_path, run=run)
    assert d.handle("ops_journal", {}) == ("", {"units": list(O.JOURNAL_UNITS)})
    assert d.handle("ops_journal", {"unit": "cron"}).startswith("payload")
    assert d.handle("ops_journal", {"unit": "nexwar_agent", "hours": 999}).startswith("payload")
    why, got = d.handle("ops_journal", {"unit": "nexwar_localizer", "hours": 2})
    assert why == "" and got["truncated"] and got["text"].startswith("2026-10-31T")
    assert "SECRETVALUE" not in got["text"] and "token=***" in got["text"]
    assert ("ab" * 20) not in got["text"], "长串十六进制只留前 8 位"
    assert len(got["text"].encode()) <= O.MAX_JOURNAL
    assert calls[-1][:4] == ["journalctl", "-u", "nexwar_localizer.service", "--since"]


def test_重启_只认我们的服务_走执行器(tmp_path):
    s = SimOps()
    d = _desk(tmp_path, priv=s)
    assert d.handle("ops_restart", {"unit": "ssh"}).startswith("payload")
    assert d.handle("ops_restart", {"unit": "nexwar_persons"}) == ("", {"unit": "nexwar_persons"})
    assert s.calls == [("restart", {"unit": "nexwar_persons"})]
    bad = _desk(tmp_path, priv=SimOps(fail={"restart": "systemctl 失败"}))
    assert bad.handle("ops_restart", {"unit": "nexwar_persons"}) == "priv_failed: systemctl 失败"


def test_重启整机_忙就拒_空闲才发(tmp_path):
    s = SimOps()
    assert _desk(tmp_path, priv=s, busy="正在执行任务 p1").handle("ops_reboot", {}) == \
        "busy: 正在执行任务 p1"
    assert s.calls == []
    assert _desk(tmp_path, priv=s).handle("ops_reboot", {}) == ("", {"delay_s": 5})
    assert s.calls == [("reboot", {"delay_s": 5})]


def test_详细日志_写到点文件_看门的跟着调级别_到点恢复(tmp_path):
    now = [1_000_000]
    d = _desk(tmp_path, now_ms=lambda: now[0])
    assert d.handle("ops_debug", {"minutes": 500}).startswith("payload")
    assert d.handle("ops_debug", {"minutes": 30}) == ("", {"until_ms": 1_000_000 + 30 * 60_000})
    lg = logging.getLogger("w55-test")
    lg.setLevel(logging.INFO)
    w = DebugWatch(d.debug_flag, logger=lg, now_ms=lambda: now[0])
    assert w.tick() and lg.level == logging.DEBUG
    now[0] += 31 * 60_000
    assert not w.tick() and lg.level == logging.INFO
    now[0] = 1_000_000
    d.handle("ops_debug", {"minutes": 0})
    assert until_ms(d.debug_flag) == 0 and not w.tick()


class _Rel:
    def __init__(self, root: Path) -> None:
        class L:
            releases = root
        self.layout = L()

    def current(self):
        return "2026-10-30-cccccc"

    def previous(self):
        return "2026-10-20-bbbbbb"


def test_清理_只清不是当前也不是上一版的_日志交给执行器(tmp_path):
    rel = tmp_path / "releases"
    for n in ("2026-10-30-cccccc", "2026-10-20-bbbbbb", "2026-10-01-aaaaaa", ".staging"):
        (rel / n).mkdir(parents=True)
        (rel / n / "f").write_text("x" * 100)
    s = SimOps()
    d = _desk(tmp_path, priv=s, releases=_Rel(rel),
              run=lambda argv, t: (0, "Archived and active journals take up 1.5G in the fs."))
    _, got = d.handle("ops_cleanup", {})
    assert got["items"]["releases"] == {"bytes": 100, "detail": ["2026-10-01-aaaaaa"]}
    assert got["items"]["journal"]["bytes"] == int(1.5 * 1024 ** 3)
    assert d.handle("ops_cleanup", {"do": ["rm"]}).startswith("payload")
    assert d.handle("ops_cleanup", {"do": ["releases", "journal"]}) == \
        ("", {"done": {"releases": ["2026-10-01-aaaaaa"], "journal": True}})
    assert sorted(p.name for p in rel.iterdir()) == [".staging", "2026-10-20-bbbbbb",
                                                     "2026-10-30-cccccc"]
    assert s.calls == [("journal_vacuum", {"max_mb": 200})]


def test_诊断快照_服务状态_环境里的PIN打码(tmp_path):
    env = tmp_path / "env"
    env.write_text("NEXWAR_HAL=d1max\nNEXWAR_DEVICE_PIN=123456\nNEXWAR_SITE_MQTT=mqtts://x:8883\n")

    def run(argv, t):
        if argv[0] == "systemctl":
            return 0, "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=3\n"
        return 1, ""
    d = _desk(tmp_path, run=run, env_file=env)
    _, got = d.handle("ops_diag", {})
    assert [s["unit"] for s in got["services"]] == list(O.OUR_UNITS)
    assert got["services"][0]["restarts"] == 3 and got["services"][0]["active"] == "active"
    assert got["env"] == {"NEXWAR_HAL": "d1max", "NEXWAR_DEVICE_PIN": "***",
                          "NEXWAR_SITE_MQTT": "mqtts://x:8883"}
    assert "memory" in got and "disks" in got and got["busy"] == ""


def _openssl(*args, cwd=None):
    subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=cwd)


def _ca(tmp: Path) -> tuple[Path, Path]:
    key, crt = tmp / "ca.key", tmp / "ca.crt"
    _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", str(key), "-out", str(crt), "-subj", "/CN=site-ca", "-days", "10")
    return key, crt


def _sign(tmp: Path, csr_pem: str, key: Path, crt: Path, name: str = "out.crt") -> str:
    (tmp / "in.csr").write_text(csr_pem)
    _openssl("x509", "-req", "-in", str(tmp / "in.csr"), "-CA", str(crt), "-CAkey", str(key),
             "-CAcreateserial", "-days", "5", "-out", str(tmp / name))
    return (tmp / name).read_text()


def test_证书续签_私钥留在机器人上_核过才交给执行器(tmp_path):
    key, crt = _ca(tmp_path)
    s = SimOps()
    d = _desk(tmp_path, priv=s, run=O.run_cmd, tls_ca=crt)
    why, got = d.handle("cert_csr", {})
    assert why == "" and "CERTIFICATE REQUEST" in got["csr"]
    assert oct((tmp_path / "data" / "cert-new" / "robot.key").stat().st_mode & 0o777) == "0o600"
    cert = _sign(tmp_path, got["csr"], key, crt)
    assert d.handle("cert_install", {"cert": cert, "registration": {"robot_id": "B"}}) \
        .startswith("payload")
    assert d.handle("cert_install", {"cert": cert, "registration": {"robot_id": "A"}}) == \
        ("", {"installed": True})
    assert s.calls == [("cert_install", {})]


def test_证书续签_对不上私钥_不是自己_不是站点签的_都拒(tmp_path):
    key, crt = _ca(tmp_path)
    d = _desk(tmp_path, priv=SimOps(), run=O.run_cmd, tls_ca=crt)
    _, got = d.handle("cert_csr", {})
    # 别的私钥的证书
    other = tmp_path / "other.key"
    _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-out", str(other))
    _openssl("req", "-new", "-key", str(other), "-subj", "/CN=A", "-out", str(tmp_path / "o.csr"))
    wrong = _sign(tmp_path, (tmp_path / "o.csr").read_text(), key, crt, "w.crt")
    assert d.handle("cert_install", {"cert": wrong, "registration": {"robot_id": "A"}}) \
        .startswith("cert_mismatch")
    # 别人签的
    (tmp_path / "x").mkdir()
    k2, c2 = _ca(tmp_path / "x")
    alien = _sign(tmp_path, got["csr"], k2, c2, "a.crt")
    assert d.handle("cert_install", {"cert": alien, "registration": {"robot_id": "A"}}) \
        .startswith("cert_untrusted")


def test_旁路程序_没有SDK不编_有就编到版本里_编不过算装失败(tmp_path):
    pkg, slot = tmp_path / "pkg", tmp_path / "slot"
    (pkg / "motion").mkdir(parents=True)
    (pkg / "motion" / "Makefile").write_text("x")
    (pkg / "motion" / "patrol_agent.cpp").write_text("x")
    slot.mkdir()
    assert build_sidecar(pkg, slot, sdk=None) == ""
    assert not (slot / "motion").exists()
    sdk = tmp_path / "sdk"
    sdk.mkdir()

    def ok(argv):
        assert argv[:3] == ["make", "-C", str(slot / "motion" / "build" / "src")]
        assert f"SDK_ROOT={sdk}" in argv
        (slot / "motion" / "build" / "src" / "patrol_agent").write_text("bin")
        return 0, ""
    assert build_sidecar(pkg, slot, sdk=str(sdk), arch="aarch64", run=ok) == ""
    assert (slot / "motion" / "build" / "patrol_agent").read_text() == "bin"
    assert not (slot / "motion" / "build" / "src").exists()
    assert "编译失败" in build_sidecar(pkg, slot, sdk=str(sdk), run=lambda a: (2, "g++: error"))


async def test_接进代理_能力里有_命令走得通(tmp_path):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path / "agent", now_ms=c,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      monotonic=lambda: c.mono)
    s = SimOps()
    rt.attach_ops(O.OpsDesk(robot_id=REG.robot_id, priv=s, data_root=tmp_path / "data",
                            busy=lambda: "x"))
    await rt.start()
    await broker.drain()
    tasks = ears.by["capabilities"][-1]["tasks"]
    assert {"ops_journal", "ops_diag", "ops_restart", "ops_reboot", "cert_csr"} <= set(tasks)
    await rt._on_cmd(_cmd("ops_restart", {"unit": "nexwar_localizer"}, "o1", c))
    await rt._on_cmd(_cmd("ops_reboot", {}, "o2", c))
    await broker.drain()
    acks = {a["command_id"]: a for a in ears.by["cmd/ack"]}
    assert acks["o1"]["result"] == "accepted" and acks["o1"]["data"] == {"unit": "nexwar_localizer"}
    # 接上以后问的是代理自己的「在不在忙」(空闲):重启整机照发
    assert acks["o2"]["result"] == "accepted"
    assert s.calls == [("restart", {"unit": "nexwar_localizer"}), ("reboot", {"delay_s": 5})]
    # 外审 W55-1 第 8 条:收下重启整机的那一刻起什么都不接,直到重启
    assert rt._restarting
    await rt._on_cmd(_cmd("ops_diag", {}, "o3", c))
    await broker.drain()
    acks = {a["command_id"]: a for a in ears.by["cmd/ack"]}
    assert acks["o3"]["result"] != "accepted" and "restarting" in str(acks["o3"].get("reason"))


async def test_外审W55_1_重启服务_忙就拒_做完放开(tmp_path):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path / "agent", now_ms=c,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      monotonic=lambda: c.mono)
    s = SimOps()
    rt.attach_ops(O.OpsDesk(robot_id=REG.robot_id, priv=s, data_root=tmp_path / "data",
                            busy=lambda: ""))
    await rt.start()
    rt._busy_reason = lambda: "正在执行任务 t1"
    await rt._on_cmd(_cmd("ops_restart", {"unit": "nexwar_localizer"}, "r1", c))
    await rt._on_cmd(_cmd("cert_install", {"cert": "x", "registration": {}}, "r2", c))
    await broker.drain()
    acks = {a["command_id"]: a for a in ears.by["cmd/ack"]}
    assert all("busy" in str(acks[k].get("reason")) for k in ("r1", "r2")), acks
    assert s.calls == [] and not rt._restarting
    rt._busy_reason = lambda: ""
    await rt._on_cmd(_cmd("ops_restart", {"unit": "nexwar_localizer"}, "r3", c))
    await broker.drain()
    assert s.calls == [("restart", {"unit": "nexwar_localizer"})]
    assert not rt._restarting, "重启的不是代理自己:做完放开"


async def test_W61_演示站点的仿真机器人_换证书重启以后不卡在维护状态(tmp_path):
    """演示站点(2026-11-01):站点一起来自动续证书,仿真机器人的模拟执行器回「成功」却没人重启代理,
    代理一直「重启中」、什么任务都不接。模拟执行器标明不会重启(restarts=False):做完就放开。"""
    broker, c = MemoryBroker(), 钟()
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path / "agent", now_ms=c,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      monotonic=lambda: c.mono)
    s = SimOps(restarts=False)
    rt.attach_ops(O.OpsDesk(robot_id=REG.robot_id, priv=s, data_root=tmp_path / "data",
                            busy=lambda: ""))
    await rt.start()
    for i, (kind, payload) in enumerate([("ops_reboot", {}),
                                         ("ops_restart", {"unit": "nexwar_agent"})]):
        await rt._on_cmd(_cmd(kind, payload, f"w{i}", c))
        await broker.drain()
        assert [a for a, _ in s.calls][-1] in ("reboot", "restart")
        assert not rt._restarting, kind
        assert not (tmp_path / "data" / "ops-hold.json").exists(), kind


def test_W61_模拟执行器默认跟真的一样_会重启():
    assert "no_restart" not in SimOps().call("reboot")
    assert SimOps(restarts=False).call("cert_install")["no_restart"] is True
    assert "no_restart" not in SimOps(restarts=False).call("timesync")


@pytest.fixture(autouse=True)
def _quiet():
    yield


def test_外审W55_1_钟不准时_对时和读诊断不判过期():
    from nexwar_agent.commands import NEVER_EXPIRE
    assert {"ops_timesync", "ops_diag", "ops_journal"} <= NEVER_EXPIRE
    assert "ops_reboot" not in NEVER_EXPIRE



async def test_外审W55_2_C3_执行器结果未知_维护状态不放_确定没做成才放(tmp_path):
    from nexwar_contract.privops import PrivTimeout

    class 慢执行器:
        def __init__(self):
            self.res = None

        def call(self, action, args=None, *, timeout_s=60.0, rid=None):
            raise PrivTimeout("执行器拿走了还没回", "rid42")

        def result(self, rid):
            return self.res

        def cancel(self, rid):
            return False
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path / "agent", now_ms=c,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      monotonic=lambda: c.mono)
    rt.OPS_WATCH_EVERY_S = 0.05
    priv = 慢执行器()
    rt.attach_ops(O.OpsDesk(robot_id=REG.robot_id, priv=priv, data_root=tmp_path / "data",
                            busy=lambda: ""))
    await rt.start()
    await rt._on_cmd(_cmd("ops_reboot", {}, "z1", c))
    await broker.drain()
    acks = {a["command_id"]: a for a in ears.by["cmd/ack"]}
    assert "priv_pending" in str(acks["z1"].get("reason"))
    await asyncio.sleep(0.2)
    assert rt._restarting, "结果未知:可能还要重启,不接新任务"
    priv.res = {"ok": False, "error": "reboot failed"}
    for _ in range(40):
        if not rt._restarting:
            break
        await asyncio.sleep(0.05)
    assert not rt._restarting, "确定没做成:放开"


class _卡住的执行器:
    """拿走了请求、一直没结果;撤不回。"""

    def __init__(self):
        self.res = {}
        self.cancelled = False

    def call(self, action, args=None, *, timeout_s=60.0, rid=None):
        from nexwar_contract.privops import PrivTimeout
        raise PrivTimeout("还没回", rid)

    def result(self, rid):
        return self.res.get(rid)

    def cancel(self, rid):
        return self.cancelled


async def test_外审W55_3_C3_代理重启以后_未决请求还在就先不接任务(tmp_path):
    broker, c = MemoryBroker(), 钟()
    priv = _卡住的执行器()

    def 起(name):
        rt = AgentRuntime(transport=MemoryTransport(broker, name), registration=REG,
                          hal=SimRobot(now_ms=c), store_dir=tmp_path / name, now_ms=c,
                          loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                          monotonic=lambda: c.mono)
        rt.OPS_WATCH_EVERY_S = 0.05
        rt.attach_ops(O.OpsDesk(robot_id=REG.robot_id, priv=priv, data_root=tmp_path / "data",
                                busy=lambda: ""))
        return rt
    rt = 起("dog1")
    await rt.start()
    await rt._on_cmd(_cmd("ops_reboot", {}, "h1", c))
    hold = json.loads((tmp_path / "data" / "ops-hold.json").read_text())
    assert hold["action"] == "reboot" and hold["rid"]
    await rt.close()
    rt2 = 起("dog2")                                   # 代理被重启了
    await rt2.start()
    assert rt2._restarting, "上次的重启请求还没结果:新代理先不接任务"
    priv.res[hold["rid"]] = {"ok": False, "error": "x"}
    for _ in range(40):
        if not rt2._restarting:
            break
        await asyncio.sleep(0.05)
    assert not rt2._restarting and not (tmp_path / "data" / "ops-hold.json").exists()
    await rt2.close()


def test_外审W55_3_C3_未决请求还没被执行器拿走_撤回_不用等(tmp_path):
    priv = _卡住的执行器()
    priv.cancelled = True
    d = _desk(tmp_path, priv=priv)
    d._hold_write("abcdef0123456789", "reboot", {})
    assert d.resume_hold() is None and not d.hold_file.exists()


def test_外审W55_3_I4_换版本后服务起不来_在途就退回上一版(tmp_path, monkeypatch):
    from nexwar_agent import main as M
    from nexwar_agent.engine import release as rel

    class 执行器:
        def call(self, action, args=None, *, timeout_s=60.0, rid=None):
            return {"ok": action != "restart_all", "error": "nexwar_localizer 起不来"}

    class 版本:
        layout = object()
        rolled = False

        def current(self):
            return "2026-10-31-abcdef12"

        def rollback(self):
            self.rolled = True
    ops = 版本()
    monkeypatch.setattr(rel, "read_pending", lambda layout: object())
    assert M.after_switch(执行器(), ops) is True and ops.rolled
    monkeypatch.setattr(rel, "read_pending", lambda layout: None)
    ops2 = 版本()
    assert M.after_switch(执行器(), ops2) is False and not ops2.rolled


def test_外审W55_4_I4_执行器不可用或结果未知_在途就退回(monkeypatch):
    from nexwar_agent import main as M
    from nexwar_agent.engine import release as rel
    from nexwar_contract.privops import PrivTimeout

    class 执行器:
        def call(self, action, args=None, *, timeout_s=60.0, rid=None):
            raise PrivTimeout("还没回", "r1")

    class 版本:
        layout = object()
        rolled = False

        def current(self):
            return "2026-10-31-abcdef12"

        def rollback(self):
            self.rolled = True
    ops = 版本()
    monkeypatch.setattr(rel, "read_pending", lambda layout: object())
    assert M.after_switch(执行器(), ops) is True and ops.rolled


def test_外审W55_4_N3_退回本身出错_不卡住_交回去让代理退出(monkeypatch):
    from nexwar_agent import main as M
    from nexwar_agent.engine import release as rel

    class 执行器:
        def call(self, action, args=None, *, timeout_s=60.0, rid=None):
            return {"ok": action != "restart_all", "error": "x"}

    class 版本:
        layout = object()

        def current(self):
            return "2026-10-31-abcdef12"

        def rollback(self):
            raise OSError("上一版的启动文件坏了")
    monkeypatch.setattr(rel, "read_pending", lambda layout: object())
    assert M.after_switch(执行器(), 版本()) is False, "回假:调用方照常退出,不悄悄卡住"


async def test_代理起来_上一趟建图暂停的服务恢复_出错不碍起来_工单21():
    import types

    from nexwar_agent.runtime import AgentRuntime
    calls = []

    async def leftover():
        calls.append(1)
        raise RuntimeError("执行器没回")
    rt = types.SimpleNamespace(mapper=types.SimpleNamespace(orch=types.SimpleNamespace(
        resume_leftover=leftover)))
    await AgentRuntime._resume_paused(rt)
    assert calls == [1]
    await AgentRuntime._resume_paused(types.SimpleNamespace(mapper=None))


def test_建图暂停_两个服务都经执行器_出错带回原因_工单21(monkeypatch):
    from nexwar_agent import main as M
    from nexwar_contract import privops
    sim = privops.SimOps(fail={"resume": "起不来"},
                         results={"pause": {"stopped": True}})
    monkeypatch.setattr(privops, "from_config", lambda: sim)
    pause = M._mapping_pauser()
    units = ["nexwar_lidar_merge", "nexwar_localizer"]
    assert pause(True, units) == (units, "")
    assert sim.calls == [("pause", {"unit": "nexwar_lidar_merge"}),
                         ("pause", {"unit": "nexwar_localizer"})]
    left, err = pause(False, ["nexwar_localizer"])
    assert left == ["nexwar_localizer"] and "nexwar_localizer:起不来" in err
    sim.fail["pause"] = "超时"                                  # 停到一半出错:当停了,之后恢复
    assert pause(True, ["nexwar_localizer"]) == (["nexwar_localizer"], "nexwar_localizer:超时")
    del sim.fail["pause"]
    sim.results["pause"] = {"stopped": False}                 # 建图前没在跑:没停,之后也不恢复
    assert pause(True, units) == ([], "")
    monkeypatch.setattr(privops, "from_config", lambda: None)
    assert M._mapping_pauser() is None


def test_W68_外审_远程升级也编Super_LIO_编不成算装失败(tmp_path):
    """codex r1 #8:远程升级原来只装 Python 依赖、编旁路程序,不编必装的 Super-LIO 就写哨兵。"""
    from nexwar_agent.release_ops import build_superlio
    pkg, slot = tmp_path / "pkg", tmp_path / "slot"
    (pkg / "deploy").mkdir(parents=True)
    slot.mkdir()
    assert build_superlio(pkg, slot) == ""                      # 老包:没源码、没脚本
    (pkg / "packages/localizer/native/superlio").mkdir(parents=True)
    assert "不全" in build_superlio(pkg, slot)                   # 带了源码没脚本
    script = pkg / "deploy" / "nexwar_superlio_build"
    script.write_text("x")
    script.chmod(0o644)                                          # 丢了执行位也照样编(用 bash 跑)
    seen = []

    def ok(argv, env):
        seen.append((argv, env))
        return 0, "Super-LIO 装好了"
    assert build_superlio(pkg, slot, prefix="/opt/x", run=ok) == ""
    assert seen[0][0] == ["bash", str(script)]
    assert seen[0][1]["NEXWAR_SUPERLIO_PREFIX"] == "/opt/x"
    why = build_superlio(pkg, slot, run=lambda a, e: (1, "缺命令: colcon"))
    assert "Super-LIO" in why and "colcon" in why


def test_W68_外审r2_远程编Super_LIO_不动全局current_按版本记_目录不归机器人说重跑装机(tmp_path):
    """codex r2 #6、#7:准备升级时就把全局 current 指到新编的,回滚也不回;老装机脚本 root 建的目录
    机器人账号写不了。"""
    import os as _os

    from nexwar_agent.release_ops import build_superlio
    pkg, slot = tmp_path / "pkg", tmp_path / "slot"
    (pkg / "deploy").mkdir(parents=True)
    (pkg / "deploy" / "nexwar_superlio_build").write_text("x")
    (slot / "venv").mkdir(parents=True)
    prefix = tmp_path / "superlio"
    prefix.mkdir()
    seen = []
    assert build_superlio(pkg, slot, prefix=str(prefix),
                          run=lambda a, e: (seen.append(e), (0, ""))[1]) == ""
    assert seen[0]["NEXWAR_SUPERLIO_LINK"] == "0"
    assert seen[0]["NEXWAR_SUPERLIO_STAMP"] == str(slot / "venv" / "superlio-root")
    prefix.chmod(0o555)
    try:
        if not _os.access(prefix, _os.W_OK):              # root 跑测试时跳过这一段
            why = build_superlio(pkg, slot, prefix=str(prefix), run=lambda a, e: (0, ""))
            assert "install.sh" in why
    finally:
        prefix.chmod(0o755)


def test_W70_站点带了站点时刻_按收到时的钟差交给特权执行器_能力里说认(tmp_path):
    """用户 11-03 现场选「代理向站点对时」:站点在笔记本上、经隧道接入,机器人慢 28 s。"""
    s = SimOps(results={"timesync": {"how": "site", "stepped": True, "offset_ms": 28_000}})
    d = _desk(tmp_path, priv=s, now_ms=lambda: 1_000_000)
    assert d.kinds()["ops_timesync"] == {"site_time": True, "probe": True}
    reason, data = d.handle("ops_timesync", {"site_ms": 1_028_000})
    assert reason == "" and s.calls[-1] == ("timesync", {"offset_ms": 28_000})
    assert data["stepped"] is True and data["offset_ms"] == 28_000
    assert d.handle("ops_timesync", {"site_ms": "x"}).startswith("payload")
    d.handle("ops_timesync", {})
    assert s.calls[-1] == ("timesync", {})


def test_W70_对时先量往返_probe只回自己的钟不拨_step按相对量拨(tmp_path):
    """11-03 热点被地图下载占满,往返 3–7 s:站点估的钟差 −28 s 是积压出来的,按站点时刻拨会拨错。"""
    s = SimOps(results={"timesync": {"how": "site", "stepped": True, "offset_ms": 1500}})
    d = _desk(tmp_path, priv=s, now_ms=lambda: 1_000_000)
    n = len(s.calls)
    reason, data = d.handle("ops_timesync", {"probe": True, "site_ms": 5})
    assert reason == "" and data == {"robot_ms": 1_000_000} and len(s.calls) == n, "不拨"
    reason, data = d.handle("ops_timesync", {"step_ms": 1500})
    assert reason == "" and s.calls[-1] == ("timesync", {"offset_ms": 1500})
    assert d.handle("ops_timesync", {"step_ms": 1.5}).startswith("payload")


# ---------------------------------------------------------------- 不靠 SSH(11-06,用户定)


def test_不靠SSH_设置名单跟特权执行器一致():
    from nexwar_contract.robot_settings import SETTINGS
    loader = importlib.machinery.SourceFileLoader("r2", str(ROOT / "deploy" / "nexwar_ops_runner"))
    spec = importlib.util.spec_from_loader("r2", loader)
    r = importlib.util.module_from_spec(spec)
    loader.exec_module(r)
    assert r.ENV_SETTINGS == SETTINGS and set(r.ENV_UNITS) == set(SETTINGS)


def test_不靠SSH_读设置_只回这几项_老写法也认_PIN不出去(tmp_path):
    env = tmp_path / "env"
    env.write_text("NEXWAR_PIN=123456\nNEXWAR_PERSONS=bridge\nNEXWAR_OBS_SIDECAR=127.0.0.1:8090\n"
                   "NEXWAR_AGENT_ARGS=--max-fraction 0.7 --x\n")
    d = _desk(tmp_path, priv=SimOps(), env_file=env)
    assert "ops_settings" in d.kinds()
    err, out = d.handle("ops_settings", {})
    assert err == "" and out["settings"] == {
        "NEXWAR_PERSONS": "bridge", "NEXWAR_OBS_PERMIT": "1", "NEXWAR_MAX_FRACTION": "0.7",
        "NEXWAR_VENDOR_PAUSE": "0", "NEXWAR_VENDOR_AUTOSTART": "0"}
    assert "123456" not in json.dumps(out)
    assert out["choices"]["NEXWAR_MAX_FRACTION"] == ["0.5", "0.7", "1.0"]


def test_不靠SSH_改设置_忙着不改_值不对拒_上限超过一半要开许可_交给执行器(tmp_path):
    env = tmp_path / "env"
    env.write_text("NEXWAR_PERSONS=none\n")
    s = SimOps()
    d = _desk(tmp_path, priv=s, env_file=env, busy="在跑巡逻")
    assert d.handle("ops_settings", {"set": {"NEXWAR_PERSONS": "bridge"}}).startswith("busy")
    d = _desk(tmp_path, priv=s, env_file=env)
    assert d.handle("ops_settings", {"set": {"NEXWAR_PIN": "1"}}).startswith("payload")
    assert d.handle("ops_settings", {"set": {"NEXWAR_MAX_FRACTION": "1.0"}}).startswith("payload")
    assert s.calls == []
    err, out = d.handle("ops_settings", {"set": {"NEXWAR_OBS_PERMIT": "1",
                                                "NEXWAR_MAX_FRACTION": "1.0"}})
    assert err == "" and out["changed"] == {"NEXWAR_OBS_PERMIT": "1", "NEXWAR_MAX_FRACTION": "1.0"}
    assert s.calls[-1][0] == "env_set"
    assert s.calls[-1][1] == {"changes": {"NEXWAR_OBS_PERMIT": "1", "NEXWAR_MAX_FRACTION": "1.0"}}


def test_不靠SSH_关掉开机暂停_先把停着的厂家进程恢复(tmp_path):
    env = tmp_path / "env"
    env.write_text("NEXWAR_VENDOR_PAUSE=1\n")
    s = SimOps()
    d = _desk(tmp_path, priv=s, env_file=env)
    d.handle("ops_settings", {"set": {"NEXWAR_VENDOR_PAUSE": "0"}})
    assert [c[0] for c in s.calls] == ["vendor_resume", "env_set"]


async def test_不靠SSH_开机暂停厂家进程_开着才做_前十分钟每分钟补一次(tmp_path):
    s = SimOps()
    t = [0.0]
    d = _desk(tmp_path, priv=s)

    async def sleep(x):
        t[0] += x
    await d.boot_vendor_pause({"NEXWAR_VENDOR_PAUSE": "0"}, sleep=sleep)
    assert s.calls == []
    await d.boot_vendor_pause({"NEXWAR_VENDOR_PAUSE": "1"}, sleep=sleep)
    assert [c[0] for c in s.calls] == ["vendor_pause"] * 10 and t[0] == 600

