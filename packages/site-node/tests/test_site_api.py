"""站点 API(W00c1 Task 5):真 HTTP、真线程、真钟;broker 用 MemoryBroker,狗是
AgentRuntime + SimRobot,在同一个事件循环线程里每 20 ms 推一拍。"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import MapPose
from d1max_contract.registration import Registration
from d1max_patrol.protocol.nav_types import Pose
from d1max_site.accounts import Accounts
from d1max_site.api import SiteApi
from d1max_site.db import SiteDB
from d1max_site.dispatcher import Dispatcher
from d1max_site.loop import LoopThread
from d1max_site.registry import Registry

SITE = "estate-1"
MAP = ("estate-1", "7")
PW = "correct-horse-battery"


def wall() -> int:
    return int(time.time() * 1000)


def target(x: float) -> dict:
    return MapPose(map_id=MAP[0], map_version=MAP[1], frame_id="map", x=x, y=0.0,
                   yaw=0.0).to_wire()


class 站:
    def __init__(self, tmp_path, *, alerts: bool = False, video: dict | None = None,
                 agent_video: bool = True) -> None:
        self.loop = LoopThread()
        self.loop.start()
        self.db = SiteDB(tmp_path / "site.db")
        self.reg = Registry(self.db, site_id=SITE)
        self.reg.enroll("A", fingerprint="sha256:a", issued_at=wall() - 1,
                        expires_at=wall() + 10**9)
        self.accounts = Accounts(self.db, now_ms=wall)
        self.accounts.add("alice", PW, role="admin")
        self._stop = False

        async def build():
            self.broker = MemoryBroker()
            self.disp = Dispatcher(MemoryTransport(self.broker, "site"), self.db, self.reg,
                                   now_ms=wall, ack_timeout_s=5.0)
            await self.disp.start()
            self.dog = SimRobot(now_ms=wall, max_vx=1.0, max_wz=1.5, stop_latency_s=0.1)
            reg = Registration(site_id=SITE, robot_id="A", credential_fingerprint="sha256:a",
                               issued_at=0, expires_at=10**14)
            self.pusher = None
            if video is not None and agent_video:      # W00c5b:狗这头用 ffmpeg 测试图顶相机
                from d1max_agent.video_push import VideoPusher, lavfi_source
                self.pusher = VideoPusher(source=lavfi_source)
            self.agent = AgentRuntime(transport=MemoryTransport(self.broker, "dogA"),
                                      registration=reg, hal=self.dog,
                                      store_dir=tmp_path / "agent", now_ms=wall,
                                      loaded_map=MAP, home=Pose.from_xy_yaw(0.0, 0.0),
                                      video=self.pusher)
            await self.agent.start()
            self.desk = self.sources = None
            if alerts:                      # W00c5a:告警台挂上派遣器
                from d1max_site.alert_sources import SiteAlertSources
                from d1max_site.alert_store import AlertDesk
                self.desk = AlertDesk(self.db, now_ms=wall, publish=self.disp.feed.publish)
                self.sources = SiteAlertSources(self.desk, now_ms=wall)
                self.sources.attach(self.disp)
            self.driver = asyncio.ensure_future(self._drive())

        self.loop.call(build)
        self.hub = None
        if video is not None:                          # W00c5b:站点的视频扇出
            from d1max_site.video import VideoHub, dispatcher_sender
            self.hub = VideoHub(send=dispatcher_sender(self.disp, self.loop),
                                known=lambda rid: rid in self.disp.clients,
                                srt_host="127.0.0.1", bind_host="127.0.0.1", **video)
            self.disp.on_event(self.hub.on_event)
        self.api = SiteApi(host="127.0.0.1", port=0, loop=self.loop, dispatcher=self.disp,
                           accounts=self.accounts, alerts=self.desk, video=self.hub)
        self.api.start()

    async def _drive(self) -> None:
        while not self._stop:
            self.dog.tick(0.02)
            await self.agent.step(0.02)
            await self.broker.drain()
            await asyncio.sleep(0.02)

    def close(self) -> None:
        self._stop = True
        self.api.stop()
        if self.hub is not None:
            self.hub.close()

        async def down():
            await self.agent.close()
            await self.disp.close()

        self.loop.call(down)
        self.loop.stop()
        self.db.close()

    # ------------------------------------------------------------ HTTP

    def req(self, method: str, path: str, body=None, token: str | None = None,
            raw: bytes | None = None) -> tuple[int, dict]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None
                                            else None)
        r = urllib.request.Request(self.api.url + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def login(self) -> str:
        code, d = self.req("POST", "/api/login", {"name": "alice", "password": PW})
        assert code == 200, d
        return d["token"]


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    yield s
    s.close()


def _等(pred, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("等不到")


def test_没登录一律401(站点):
    for m, p in (("GET", "/api/robots"), ("GET", "/api/robots/A"),
                 ("POST", "/api/robots/A/goto"), ("GET", "/api/events")):
        code, d = 站点.req(m, p, {} if m == "POST" else None)
        assert code == 401, (m, p, code)
    code, _ = 站点.req("GET", "/api/robots", token="made-up")
    assert code == 401


def test_错口令401_连错五次锁429(站点):
    for _ in range(5):
        code, _ = 站点.req("POST", "/api/login", {"name": "alice", "password": "wrong-wrong!"})
        assert code == 401
    code, d = 站点.req("POST", "/api/login", {"name": "alice", "password": PW})
    assert code == 429, d


def test_登录后派goto走到_命令记着alice(站点):
    tok = 站点.login()
    _等(lambda: 站点.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    code, d = 站点.req("POST", "/api/robots/A/goto",
                     {"target": target(0.5), "max_speed_mps": 0.8}, token=tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    task_id = d["task_id"]

    def done():
        v = 站点.req("GET", "/api/robots/A", token=tok)[1]
        return v if any(e["data"].get("task_id") == task_id and e["kind"] == "task_done"
                        for e in v["events"]) else None

    v = _等(done)
    assert v["commands"][0]["issued_by"] == "alice"


def test_派遣条件不满足回409带理由(站点):
    tok = 站点.login()
    code, d = 站点.req("POST", "/api/robots/ghost/goto", {"target": target(1.0)}, token=tok)
    assert code == 409 and "没有登记" in d["error"]


def test_请求体坏_超限_参数不对(站点):
    tok = 站点.login()
    code, _ = 站点.req("POST", "/api/robots/A/goto", raw=b"{nope", token=tok)
    assert code == 400
    code, _ = 站点.req("POST", "/api/robots/A/goto", raw=b"x" * (70 * 1024), token=tok)
    assert code == 413
    code, _ = 站点.req("POST", "/api/robots/A/goto",
                     {"target": target(1.0), "max_speed_mps": -1}, token=tok)
    assert code == 400
    code, _ = 站点.req("POST", "/api/robots/A/abort", {}, token=tok)
    assert code == 400


def test_注销之后令牌作废(站点):
    tok = 站点.login()
    assert 站点.req("POST", "/api/logout", {}, token=tok)[0] == 200
    assert 站点.req("GET", "/api/robots", token=tok)[0] == 401


def test_SSE第一帧是快照_之后看得到派单的回执(站点):
    import http.client
    tok = 站点.login()
    _等(lambda: 站点.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    host, port = 站点.api.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/api/events", headers={"Authorization": f"Bearer {tok}"})
    resp = conn.getresponse()
    assert resp.status == 200 and "text/event-stream" in resp.getheader("Content-Type")

    def frame():
        while True:
            line = resp.fp.readline().decode()
            if line.startswith("data: "):
                return json.loads(line[6:])

    first = frame()
    assert first["kind"] == "snapshot" and first["robots"][0]["robot_id"] == "A"
    code, d = 站点.req("POST", "/api/robots/A/goto", {"target": target(0.3)}, token=tok)
    assert code == 200
    seen = []
    for _ in range(200):
        f = frame()
        seen.append(f["kind"])
        if f["kind"] == "ack":
            assert f["issued_by"] == "alice" and f["ack"]["command_id"] == d["command_id"]
            break
    assert "ack" in seen
    conn.close()


def test_绑非本机地址不带TLS拒绝启动(站点):
    with pytest.raises(SystemExit):
        SiteApi(host="0.0.0.0", port=0, loop=站点.loop, dispatcher=站点.disp,
                accounts=站点.accounts)


def _raw(api, data: bytes, read_timeout: float = 5.0) -> bytes:
    import socket
    host, port = api.httpd.server_address[:2]
    s = socket.create_connection((host, port), timeout=read_timeout)
    try:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                return out + b"<timeout>"
            if not chunk:
                return out
            out += chunk
            if b"\r\n\r\n" in out and b"Content-Length" in out:
                head, _, body = out.partition(b"\r\n\r\n")
                n = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
                if len(body) >= n:
                    return out
    finally:
        s.close()


def test_负的Content_Length_立刻400_不读到EOF(站点):
    out = _raw(站点.api, b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n"
               + b"x" * 100_000, read_timeout=5.0)
    assert out.startswith(b"HTTP/1.1 400"), out[:200]


def test_慢客户端到点被断开(站点):
    api = SiteApi(host="127.0.0.1", port=0, loop=站点.loop, dispatcher=站点.disp,
                  accounts=站点.accounts, request_timeout_s=1.0)
    api.start()
    try:
        t0 = time.monotonic()
        out = _raw(api, b"POST /api/login HTTP/1.1\r\nHost: x\r\n", read_timeout=10.0)
        assert b"<timeout>" not in out and time.monotonic() - t0 < 5.0
    finally:
        api.stop()


def test_限速NaN或无穷_400(站点):
    tok = 站点.login()
    for bad in (b"NaN", b"Infinity"):
        body = b'{"target": ' + json.dumps(target(1.0)).encode() + b', "max_speed_mps": ' \
            + bad + b"}"
        code, _ = 站点.req("POST", "/api/robots/A/goto", raw=body, token=tok)
        assert code == 400, bad


def test_URL里的robot_id要解码_怪字符404(站点):
    tok = 站点.login()
    assert 站点.req("GET", "/api/robots/%41", token=tok)[0] == 200      # %41 = A
    assert 站点.req("GET", "/api/robots/a%09b", token=tok)[0] == 404


def test_注销之后SSE也断(站点):
    import http.client
    api = SiteApi(host="127.0.0.1", port=0, loop=站点.loop, dispatcher=站点.disp,
                  accounts=站点.accounts, sse_recheck_s=0.5)
    api.start()
    try:
        tok = 站点.login()
        host, port = api.httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("GET", "/api/events", headers={"Authorization": f"Bearer {tok}"})
        resp = conn.getresponse()
        assert resp.fp.readline().startswith(b"data: ")
        站点.req("POST", "/api/logout", {}, token=tok)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 8:
            line = resp.fp.readline()
            if not line:
                break
        assert not line, "注销之后 SSE 还开着"
        conn.close()
    finally:
        api.stop()


def _任务包(tmp_path, version=1):
    from test_site_schedule import 打包
    return 打包(tmp_path, version)


def test_导入任务包_手动起一趟patrol走完(站点, tmp_path):
    tok = 站点.login()
    _等(lambda: 站点.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    code, d = 站点.req("POST", "/api/bundles", {"path": str(_任务包(tmp_path))}, token=tok)
    assert code == 200 and d["missions"] == ["loop"], d
    code, d = 站点.req("POST", "/api/robots/A/patrol", {"mission_id": "loop"}, token=tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    tid = d["task_id"]

    def done():
        v = 站点.req("GET", "/api/robots/A", token=tok)[1]
        return any(e["kind"] == "task_done" and e["data"].get("task_id") == tid
                   for e in v["events"])

    _等(done, timeout=40)
    v = 站点.req("GET", "/api/robots/A", token=tok)[1]
    assert v["commands"][0]["kind"] == "patrol" and v["commands"][0]["issued_by"] == "alice"


def test_任务包路由的错误(站点, tmp_path):
    tok = 站点.login()
    assert 站点.req("POST", "/api/bundles", {}, token=tok)[0] == 400
    code, d = 站点.req("POST", "/api/bundles", {"path": str(tmp_path / "nope")}, token=tok)
    assert code == 409 and "校验没过" in d["error"]
    assert 站点.req("POST", "/api/robots/A/patrol", {"mission_id": "loop"}, token=tok)[0] == 404
    assert 站点.req("POST", "/api/robots/A/patrol", {}, token=tok)[0] == 400
    assert 站点.req("GET", "/api/schedule", token=tok)[0] == 404, "没开排程的站点"
    assert 站点.req("POST", "/api/bundles", {"path": "x"})[0] == 401


def test_排程视图(站点, tmp_path):
    from d1max_site.scheduler import SiteScheduler
    tok = 站点.login()
    站点.req("POST", "/api/bundles", {"path": str(_任务包(tmp_path))}, token=tok)
    站点.api.scheduler = SiteScheduler(站点.db, 站点.disp, now_ms=wall)
    code, v = 站点.req("GET", "/api/schedule", token=tok)
    assert code == 200 and v["bundle"]["bundle_id"] == "estate-kl"
    assert v["entries"][0]["id"] == "nightly" and v["entries"][0]["next_run"]


def test_请求体里的priority不起作用_手动派单一律按MANUAL(站点):
    from d1max_site.priorities import MANUAL
    tok = 站点.login()
    _等(lambda: 站点.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    code, d = 站点.req("POST", "/api/robots/A/goto", {"target": target(0.3), "priority": 1000},
                     token=tok)
    assert code == 200, d
    row = 站点.db.query("SELECT priority FROM commands WHERE command_id=?", (d["command_id"],))
    assert row[0]["priority"] == MANUAL


def test_待命点API(站点):
    from d1max_site.standby import StandbyManager
    tok = 站点.login()
    assert 站点.req("GET", "/api/robots/A/standby", token=tok)[0] == 404, "没开待命点的站点"
    站点.api.standby = StandbyManager(站点.db, 站点.disp, now_ms=wall)
    code, d = 站点.req("POST", "/api/robots/A/standby",
                     {"name": "dock", "map_id": "estate-1", "map_version": "7", "x": 0,
                      "y": 0, "yaw": 0, "default": True}, token=tok)
    assert code == 200, d
    code, d = 站点.req("GET", "/api/robots/A/standby", token=tok)
    assert code == 200 and d["points"][0]["name"] == "dock" and d["points"][0]["default"]
    code, d = 站点.req("POST", "/api/robots/A/standby", {"name": "x"}, token=tok)
    assert code == 400
    for bad in ({"x": 10**400}, {"default": "false"}):
        body = {"name": "y", "map_id": "estate-1", "map_version": "7", "x": 0, "y": 0,
                "yaw": 0} | bad
        assert 站点.req("POST", "/api/robots/A/standby", body, token=tok)[0] == 400, bad

    _等(lambda: 站点.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    code, d = 站点.req("POST", "/api/robots/A/standby/return", {}, token=tok)
    assert code == 200 and d["task_id"].startswith("standby-"), d


def test_事件回调要签名_不要登录_管理路由要登录(站点):
    import hashlib
    import hmac
    import urllib.error
    import urllib.request

    from d1max_site.incidents import IncidentDesk
    tok = 站点.login()
    assert 站点.req("GET", "/api/incidents", token=tok)[0] == 404, "没开事件派遣的站点"
    desk = IncidentDesk(站点.db, 站点.disp, now_ms=wall)
    站点.api.incidents = desk
    secret = desk.add_source("nvr-1")
    assert 站点.req("POST", "/api/intercepts", {"name": "gate", "map_id": "estate-1",
                                              "map_version": "7", "x": 0.5, "y": 0, "yaw": 0},
                  token=tok)[0] == 200
    assert 站点.req("POST", "/api/zones", {"zone": "yard", "intercept": "gate"}, token=tok)[0] \
        == 200
    assert 站点.req("POST", "/api/zones", {"zone": "yard", "intercept": "nope"}, token=tok)[0] \
        == 400
    assert 站点.req("GET", "/api/incidents")[0] == 401
    _等(lambda: 站点.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))

    def 报(body: bytes, sig: str | None = None, ts: int | None = None):
        ts = wall() if ts is None else ts
        sig = sig or hmac.new(bytes.fromhex(secret), str(ts).encode() + b"." + body,
                              hashlib.sha256).hexdigest()
        r = urllib.request.Request(站点.api.url + "/api/incidents", data=body, method="POST")
        r.add_header("X-D1MAX-Source", "nvr-1")
        r.add_header("X-D1MAX-Timestamp", str(ts))
        r.add_header("X-D1MAX-Signature", sig)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    body = json.dumps({"event_id": "e1", "type": "intrusion", "zone": "yard"}).encode()
    bad = 报(body, sig="00" * 32)
    expired = 报(body, ts=wall() - 10 * 60_000)
    assert bad[0] == expired[0] == 401
    assert bad[1] == expired[1] == {"error": "验签没过"}, "401 不说是哪一步没过(源名不可探测)"
    assert 报(b'{"event_id": ""}')[0] == 400
    code, d = 报(body)
    assert code == 200 and d["outcome"] == "dispatched" and d["robot_id"] == "A", d
    code, d = 站点.req("GET", "/api/incidents", token=tok)
    assert code == 200 and d["incidents"][0]["event_id"] == "e1"
