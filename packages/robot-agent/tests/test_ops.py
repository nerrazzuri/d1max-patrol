"""W55 远程运维(机器人这边):日志、诊断、重启、空闲才重启整机、详细日志、清理、证书续签、
旁路程序随版本编。"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import subprocess
from pathlib import Path

import pytest
from test_runtime_maps import REG, T, _cmd, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent import ops as O
from d1max_agent.release_ops import build_sidecar
from d1max_agent.runtime import AgentRuntime
from d1max_contract.debuglog import DebugWatch, until_ms
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.privops import SimOps
from d1max_patrol.protocol.nav_types import Pose

ROOT = Path(__file__).resolve().parents[3]


def _desk(tmp_path, *, priv=None, busy="", run=None, **kw):
    return O.OpsDesk(robot_id="A", priv=priv, data_root=tmp_path / "data",
                     busy=lambda: busy, run=run or (lambda argv, t: (0, "")), **kw)


def test_我们的服务名单_跟特权执行器一致():
    loader = importlib.machinery.SourceFileLoader("r", str(ROOT / "deploy" / "d1max-ops-runner"))
    spec = importlib.util.spec_from_loader("r", loader)
    r = importlib.util.module_from_spec(spec)
    loader.exec_module(r)
    assert set(O.OUR_UNITS) == set(r.ROBOT_UNITS)


def test_没装执行器_要root的不报_发了也说不支持(tmp_path):
    d = _desk(tmp_path)
    assert set(d.kinds()) == {"ops_journal", "ops_diag", "ops_debug", "ops_cleanup"}
    assert d.handle("ops_restart", {"unit": "d1max-agent"}).startswith("unsupported")
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
    assert d.handle("ops_journal", {"unit": "d1max-agent", "hours": 999}).startswith("payload")
    why, got = d.handle("ops_journal", {"unit": "d1max-localizer", "hours": 2})
    assert why == "" and got["truncated"] and got["text"].startswith("2026-10-31T")
    assert "SECRETVALUE" not in got["text"] and "token=***" in got["text"]
    assert ("ab" * 20) not in got["text"], "长串十六进制只留前 8 位"
    assert len(got["text"].encode()) <= O.MAX_JOURNAL
    assert calls[-1][:4] == ["journalctl", "-u", "d1max-localizer.service", "--since"]


def test_重启_只认我们的服务_走执行器(tmp_path):
    s = SimOps()
    d = _desk(tmp_path, priv=s)
    assert d.handle("ops_restart", {"unit": "ssh"}).startswith("payload")
    assert d.handle("ops_restart", {"unit": "d1max-persons"}) == ("", {"unit": "d1max-persons"})
    assert s.calls == [("restart", {"unit": "d1max-persons"})]
    bad = _desk(tmp_path, priv=SimOps(fail={"restart": "systemctl 失败"}))
    assert bad.handle("ops_restart", {"unit": "d1max-persons"}) == "priv_failed: systemctl 失败"


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
    env.write_text("D1MAX_HAL=d1max\nD1MAX_DEVICE_PIN=123456\nD1MAX_SITE_MQTT=mqtts://x:8883\n")

    def run(argv, t):
        if argv[0] == "systemctl":
            return 0, "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=3\n"
        return 1, ""
    d = _desk(tmp_path, run=run, env_file=env)
    _, got = d.handle("ops_diag", {})
    assert [s["unit"] for s in got["services"]] == list(O.OUR_UNITS)
    assert got["services"][0]["restarts"] == 3 and got["services"][0]["active"] == "active"
    assert got["env"] == {"D1MAX_HAL": "d1max", "D1MAX_DEVICE_PIN": "***",
                          "D1MAX_SITE_MQTT": "mqtts://x:8883"}
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
    await rt._on_cmd(_cmd("ops_restart", {"unit": "d1max-localizer"}, "o1", c))
    await rt._on_cmd(_cmd("ops_reboot", {}, "o2", c))
    await broker.drain()
    acks = {a["command_id"]: a for a in ears.by["cmd/ack"]}
    assert acks["o1"]["result"] == "accepted" and acks["o1"]["data"] == {"unit": "d1max-localizer"}
    # 接上以后问的是代理自己的「在不在忙」(空闲):重启整机照发
    assert acks["o2"]["result"] == "accepted"
    assert s.calls == [("restart", {"unit": "d1max-localizer"}), ("reboot", {"delay_s": 5})]


@pytest.fixture(autouse=True)
def _quiet():
    yield
