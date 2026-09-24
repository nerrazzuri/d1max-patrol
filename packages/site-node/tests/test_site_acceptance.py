"""W00c1 验收(设计 §6),全用真东西:``d1max-site`` 命令行建站点、签证书、加账号 → 真 Mosquitto
(mTLS + ACL)→ ``d1max-site serve`` 子进程 → ``d1max-agent`` 子进程(sim,带证书)→ 站点 API。

1. 登录 → 派 goto → 事件里看到 accepted 与 done;再派一趟远的,中途 abort → aborted、停下。
2. A 的证书发 B 的主题 → broker 丢弃。
3. 没证书 / 别的 CA → 连不上。
4. revoke A → 派单 409;重启 broker 后 A 的证书连不上。
5. 未登录派单 401;每条命令记着派单人。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import find_mosquitto, free_port

from d1max_contract.messages import MapPose
from d1max_contract.paho_transport import PahoTransport
from d1max_contract.topics import Topics

SITE = "estate-1"
PW = "correct-horse-battery"
ENV = {k: v for k, v in os.environ.items()
       if k not in ("PYTHONPATH", "AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH")}
ENV["PYTHONUNBUFFERED"] = "1"


def site(home: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "d1max_site.main", "--home", str(home), *args],
                          capture_output=True, text=True, timeout=120, env=ENV | kw.get("env", {}))


def target(x: float) -> dict:
    return MapPose(map_id="estate-1", map_version="7", frame_id="map", x=x, y=0.0,
                   yaw=0.0).to_wire()


class Proc:
    def __init__(self, name: str, argv: list[str], log: Path) -> None:
        self.name = name
        self.log = log
        self._f = open(log, "wb")
        self.p = subprocess.Popen(argv, stdout=self._f, stderr=subprocess.STDOUT, env=ENV)

    def wait_line(self, text: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.log.read_text(errors="replace"):
                return
            if self.p.poll() is not None:
                raise AssertionError(f"{self.name} 退了:\n{self.log.read_text()}")
            time.sleep(0.1)
        raise AssertionError(f"{self.name} {timeout}s 内没打出 {text!r}:\n{self.log.read_text()}")

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(15)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self._f.close()


@pytest.fixture
def 现场(tmp_path):
    exe = find_mosquitto()
    if exe is None:
        pytest.skip("没有 mosquitto,跳过端到端验收")
    home = tmp_path / "site"
    bport, aport = free_port(), free_port()
    r = site(home, "init", "--site-id", SITE, "--hostname", "localhost", "--hostname",
             "127.0.0.1", "--broker-port", str(bport))
    assert r.returncode == 0, r.stderr
    for rid in ("A", "B"):
        r = site(home, "enroll", rid, "--days", "30")
        assert r.returncode == 0, r.stderr
    r = site(home, "add-admin", "alice", env={"D1MAX_SITE_PASSWORD": PW})
    assert r.returncode == 0, r.stderr
    procs: list[Proc] = []

    def mosq() -> Proc:
        p = Proc("mosquitto", [exe, "-c", str(home / "broker" / "mosquitto.conf")],
                 tmp_path / f"mosq-{len(procs)}.log")
        procs.append(p)
        p.wait_line("mosquitto version", 10)
        time.sleep(0.3)
        return p

    broker = mosq()
    srv = Proc("site", [sys.executable, "-m", "d1max_site.main", "--home", str(home), "serve",
                        "--api-port", str(aport)], tmp_path / "site.log")
    procs.append(srv)
    srv.wait_line("d1max-site 起来了")
    a = home / "ca" / "issued" / "A"
    agent = Proc("agent", [sys.executable, "-m", "d1max_agent.main",
                           "--transport", f"mqtts://127.0.0.1:{bport}",
                           "--tls-ca", str(a / "ca.crt"), "--tls-cert", str(a / "robot.crt"),
                           "--tls-key", str(a / "robot.key"), "--hal", "sim",
                           "--registration", str(a / "registration.json"),
                           "--store-dir", str(tmp_path / "agent"),
                           "--runs-root", str(tmp_path / "runs"),
                           "--map", "estate-1:7", "--home", "0,0,0", "--period", "0.05"],
                 tmp_path / "agent.log")
    procs.append(agent)
    agent.wait_line("d1max-agent 起来了")
    ctx = {"home": home, "bport": bport, "api": f"http://127.0.0.1:{aport}", "broker": broker,
           "mosq": mosq, "procs": procs}
    yield ctx
    for p in reversed(procs):
        p.stop()


def req(ctx, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(ctx["api"] + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def 等(pred, timeout=40.0, what=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError(f"等不到: {what}")


def _task_event(ctx, tok, task_id, kind):
    v = req(ctx, "GET", "/api/robots/A", token=tok)[1]
    return any(e["kind"] == kind and e["data"].get("task_id") == task_id for e in v["events"])


def test_W00c1_端到端(现场):
    ctx = 现场
    # 5. 未登录 401
    assert req(ctx, "POST", "/api/robots/A/goto", {"target": target(0.5)})[0] == 401
    code, d = req(ctx, "POST", "/api/login", {"name": "alice", "password": PW})
    assert code == 200, d
    tok = d["token"]
    等(lambda: req(ctx, "GET", "/api/robots/A", token=tok)[1].get("fresh"), what="A 新鲜")

    # 1. goto → done
    code, d = req(ctx, "POST", "/api/robots/A/goto", {"target": target(0.5),
                                                      "max_speed_mps": 0.8}, token=tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    等(lambda: _task_event(ctx, tok, d["task_id"], "task_done"), what="task_done")

    # 1. 远的一趟,中途 abort
    code, far = req(ctx, "POST", "/api/robots/A/goto", {"target": target(8.0),
                                                        "max_speed_mps": 0.5}, token=tok)
    assert code == 200 and far["ack"]["result"] == "accepted", far
    time.sleep(1.0)
    code, ab = req(ctx, "POST", "/api/robots/A/abort", {"task_id": far["task_id"]}, token=tok)
    assert code == 200 and ab["ack"]["result"] == "accepted", ab
    等(lambda: _task_event(ctx, tok, far["task_id"], "task_aborted"), what="task_aborted")
    v = req(ctx, "GET", "/api/robots/A", token=tok)[1]
    assert {c["issued_by"] for c in v["commands"]} == {"alice"}
    assert [c["kind"] for c in v["commands"]][:3] == ["abort", "goto", "goto"]

    # W00c2a:导入任务包(命令行)→ 手动起一趟 patrol(带拍照动作,真 agent 进程用 sim 相机)
    from test_site_schedule import 任务, 打包
    带拍照 = json.loads(json.dumps(任务))
    带拍照["waypoints"][0]["actions"] = [{"type": "photo", "camera": "front"}]
    包 = 打包(ctx["home"].parent, 1, mission=带拍照)
    r = site(ctx["home"], "import-bundle", str(包))
    assert r.returncode == 0, r.stderr
    code, pt = req(ctx, "POST", "/api/robots/A/patrol", {"mission_id": "loop"}, token=tok)
    assert code == 200 and pt["ack"]["result"] == "accepted", pt
    等(lambda: _task_event(ctx, tok, pt["task_id"], "task_done"), what="patrol task_done")
    v = req(ctx, "GET", "/api/robots/A", token=tok)[1]
    点 = [e for e in v["events"] if e["kind"] == "patrol_waypoint"
         and e["data"].get("task_id") == pt["task_id"]]
    assert sorted(e["data"]["name"] for e in 点) == ["a", "b"] and all(e["data"]["ok"] for e in 点)

    # 2. A 冒充 B:站点看不到 B 的 status
    home, bport = ctx["home"], ctx["bport"]
    a = home / "ca" / "issued" / "A"

    async def 冒充():
        t = PahoTransport(f"mqtts://127.0.0.1:{bport}", "A-evil", tls_ca=str(a / "ca.crt"),
                          tls_cert=str(a / "robot.crt"), tls_key=str(a / "robot.key"))
        await t.connect()
        await t.publish(Topics(site_id=SITE, robot_id="B").status, b'{"forged":1}', qos=1,
                        retain=True)
        await t.close()

    asyncio.run(冒充())
    time.sleep(1.0)
    assert req(ctx, "GET", "/api/robots/B", token=tok)[1]["status"] is None

    # 3. 没证书连不上
    async def 匿名():
        t = PahoTransport(f"mqtts://127.0.0.1:{bport}", "anon", tls_ca=str(a / "ca.crt"))
        await t.connect()

    with pytest.raises((ConnectionError, TimeoutError)):
        asyncio.run(匿名())

    # 4. revoke → 409;重启 broker 后 A 连不上
    r = site(home, "revoke", "A")
    assert r.returncode == 0, r.stderr
    code, d = req(ctx, "POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert code == 409 and "吊销" in d["error"], d
    ctx["broker"].stop()
    ctx["mosq"]()

    async def 吊销后():
        t = PahoTransport(f"mqtts://127.0.0.1:{bport}", "A-again", tls_ca=str(a / "ca.crt"),
                          tls_cert=str(a / "robot.crt"), tls_key=str(a / "robot.key"))
        await t.connect()

    with pytest.raises((ConnectionError, TimeoutError)):
        asyncio.run(吊销后())
