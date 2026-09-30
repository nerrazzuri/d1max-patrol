"""真 Mosquitto 的测试夹具(W00c1)。找 broker 的顺序:环境变量 ``D1MAX_MOSQUITTO`` → PATH 里的
``mosquitto`` → ``/usr/sbin/mosquitto``;都没有就跳过真 broker 测试。起在临时目录与空闲端口上,
站点 CA、A/B 两台狗的证书包、站点服务证书都现签。"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from d1max_site.broker_conf import BrokerPaths, render_acl, render_conf
from d1max_site.ca import RobotBundle, SiteCA

SITE = "estate-1"
NOW = 1_800_000_000_000


def find_mosquitto() -> str | None:
    env = os.environ.get("D1MAX_MOSQUITTO")
    if env:
        return env if Path(env).is_file() else None
    return shutil.which("mosquitto") or (
        "/usr/sbin/mosquitto" if Path("/usr/sbin/mosquitto").is_file() else None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Broker:
    exe: str
    root: Path
    port: int
    ca: SiteCA
    server_crt: Path
    server_key: Path
    robots: dict[str, RobotBundle]
    proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"mqtts://127.0.0.1:{self.port}"

    @property
    def conf(self) -> Path:
        return self.root / "mosquitto.conf"

    @property
    def log(self) -> Path:
        return self.root / "mosquitto.log"

    def start(self) -> None:
        with open(self.log, "ab") as out:
            self.proc = subprocess.Popen([self.exe, "-c", str(self.conf)], stdout=out,
                                         stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("mosquitto 起不来:\n" + self.log.read_text())
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mosquitto 10 s 内没开端口:\n" + self.log.read_text())

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def site_tls(self) -> dict[str, str]:
        return {"tls_ca": str(self.ca.ca_cert), "tls_cert": str(self.server_crt),
                "tls_key": str(self.server_key)}

    def robot_tls(self, robot_id: str) -> dict[str, str]:
        d = self.robots[robot_id].dir
        return {"tls_ca": str(d / "ca.crt"), "tls_cert": str(d / "robot.crt"),
                "tls_key": str(d / "robot.key")}


def make_broker(root: Path, robots=("A", "B")) -> Broker:
    exe = find_mosquitto()
    if exe is None:
        pytest.skip("没有 mosquitto(设 D1MAX_MOSQUITTO 或 apt install mosquitto),"
                    "跳过真 broker 测试")
    ca = SiteCA(root / "ca")
    ca.init(SITE)
    crt, key = ca.issue_server(["localhost", "127.0.0.1"])
    bundles = {r: ca.issue_robot(r, days=30, now_ms=NOW) for r in robots}
    port = free_port()
    acl = root / "acl"
    acl.write_text(render_acl(SITE))
    b = Broker(exe=exe, root=root, port=port, ca=ca, server_crt=crt, server_key=key,
               robots=bundles)
    b.conf.write_text(render_conf(port=port, bind="127.0.0.1", paths=BrokerPaths(
        cafile=ca.ca_cert, certfile=crt, keyfile=key, crlfile=ca.crl, aclfile=acl)))
    return b


@pytest.fixture
def broker(tmp_path):
    b = make_broker(tmp_path)
    b.start()
    yield b
    b.stop()


class 同钟钟差:
    """进程内测试台子用的钟差估计(W09h):台子里狗跟站点用**同一个钟**(假钟或墙钟),钟差本来就是 0。
    真的估计器在这种台子里要攒几条遥测(狗慢的方向 10 条)才算「知道」,而 W09h 起不知道就不派 —— 每条
    派单的测试都得先等 10 s。这里:估计器说不知道就当 0,**有样本照真的算**(钟差大的测试照样拒)。
    钟差本身怎么判(零样本、样本不够、样本过期)在 ``test_site_temporal.py`` 里用真的估计器测。"""

    def __init__(self, real) -> None:
        self.real = real

    def note(self, robot_id, dog_ms, received_ms) -> None:
        self.real.note(robot_id, dog_ms, received_ms)

    def skew_s(self, robot_id):
        got = self.real.skew_s(robot_id)
        return 0.0 if got is None else got

    @property
    def _samples(self):
        return self.real._samples


def 同钟(dispatcher) -> None:
    """把派遣器的钟差估计换成 :class:`同钟钟差`。"""
    dispatcher._skew = 同钟钟差(dispatcher._skew)


def 真钟差(dispatcher) -> None:
    """换回真的估计器(测时间权威本身用)。"""
    s = dispatcher._skew
    if isinstance(s, 同钟钟差):
        dispatcher._skew = s.real
