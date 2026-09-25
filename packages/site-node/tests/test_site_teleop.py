"""W00c5c:遥控经站点,端到端(真 WebSocket、内存 MQTT、仿真狗)。逐条对着决策 7 的条件验:
站点只转发、任一段断开狗都停、一个租约、先停车再移交、halt 不走遥控连接、审计、限速一半。"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time

import pytest
from test_site_api import PW, _等, target, 站

from d1max_site.ws import client_mask_frame


class 手机:
    """一条遥控 WebSocket(测试用的最小客户端)。"""

    def __init__(self, s, tok, robot="A", *, takeover: str = "") -> None:
        host, port = s.api.httpd.server_address[:2]
        self.sock = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        from urllib.parse import quote
        path = f"/api/robots/{robot}/teleop" + (f"?takeover={quote(takeover)}" if takeover
                                                 else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\nAuthorization: Bearer {tok}\r\n\r\n"
                           ).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                break
            head += chunk
        self.status = int(head.split(b" ")[1]) if head else 0
        self.error = ""
        if self.status != 101:
            n = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":")[1])
            body = b""
            while len(body) < n:
                body += self.sock.recv(n - len(body))
            self.error = json.loads(body or b"{}").get("error", "")
        self.buf = b""

    def send(self, obj) -> None:
        self.sock.sendall(client_mask_frame(json.dumps(obj).encode()))

    def recv(self, timeout: float = 5.0):
        """下一条文本消息(解析成 JSON);ping 自动回 pong;连接关了返回 None。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while len(self.buf) >= 2:
                op, n = self.buf[0] & 0x0F, self.buf[1] & 0x7F
                h = 2
                if n == 126:
                    if len(self.buf) < 4:
                        break
                    n, h = struct.unpack(">H", self.buf[2:4])[0], 4
                if len(self.buf) < h + n:
                    break
                data, self.buf = self.buf[h:h + n], self.buf[h + n:]
                if op == 9:
                    self.sock.sendall(client_mask_frame(data, opcode=10))
                elif op == 1:
                    return json.loads(data)
                elif op == 8:
                    return None
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self.buf += chunk
        raise AssertionError("等不到消息")

    def wait_kind(self, kind: str, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = self.recv(deadline - time.monotonic())
            if m is None:
                raise AssertionError(f"没等到 {kind},连接就关了")
            if m.get("kind") == kind:
                return m
        raise AssertionError(f"等不到 {kind}")

    def close(self) -> None:
        self.sock.close()


def _推(ph, vx, wz=0.0, seconds=1.0):
    """按住摇杆 ``seconds`` 秒(每 100 ms 一帧)。连接被站点关了就停手(返回 False)。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        try:
            ph.send({"vx": vx, "wz": wz})
        except OSError:
            return False
        time.sleep(0.1)
    return True


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, teleop={"renew_s": 0.5})
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("gus", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name="gina"):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _新鲜(s, tok):
    _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    _等(lambda: s.disp.clients["A"].capabilities is not None
        and "teleop" in s.disp.clients["A"].capabilities.tasks)


def _odom(s):
    return s.loop.call(s.dog.odometry)


def _停了(s):
    return s.loop.call(s.dog.stopped)


def test_保安遥控_狗动_限速一半_松手停_断开就放租_审计有起止(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    ph = 手机(s, tok)
    assert ph.status == 101, ph.error
    g = ph.wait_kind("granted")
    assert g["operator"] == "gina" and g["max_vx"] == pytest.approx(0.5)
    _推(ph, 3.0, seconds=1.2)                       # 要 3 m/s,只给一半能力
    o = _odom(s)
    assert 0 < o.vx <= 0.5 + 1e-6 and o.x > 0.1, o
    _推(ph, 0.0, seconds=0.5)
    assert _等(lambda: _停了(s), timeout=3)
    ph.close()                                       # 手机断了
    _等(lambda: s.teleop.active("A") is None, timeout=5)
    rows = s.db.query("SELECT * FROM teleop_leases WHERE robot_id='A'")
    assert [(r["operator"], r["end_reason"]) for r in rows] == [("gina", "disconnected")]
    _等(lambda: (s.disp.clients["A"].status.task is None), timeout=8)
    acts = [(r["actor"], r["action"]) for r in s.api.audit.list()]
    assert ("gina", "teleop_start") in acts and ("gina", "teleop_end") in acts


def test_站点到狗断了_狗在帧有效期里自己停(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    ph = 手机(s, tok)
    ph.wait_kind("granted")
    _推(ph, 0.4, seconds=0.8)
    assert _odom(s).vx > 0
    async def _断():
        s.broker.disconnect("dogA")                   # 站点↔狗那一段断了
    s.loop.call(_断)
    t0 = time.monotonic()
    _推(ph, 0.4, seconds=0.6)                         # 手机还在推,站点也照转 —— 狗收不到
    assert _等(lambda: _停了(s), timeout=3)
    assert time.monotonic() - t0 < 1.5, "帧有效期 300 ms,狗要自己停"
    ph.close()


def test_业主不能遥控_没登录401(站点):
    s = 站点
    olga = _登(s, "olga")
    _新鲜(s, olga)
    assert 手机(s, olga).status == 403
    assert 手机(s, "nope").status == 401


def test_一个租约_第二个保安409说是谁_管理员带理由接管(站点):
    s = 站点
    gina, gus, alice = _登(s), _登(s, "gus"), _登(s, "alice")
    _新鲜(s, gina)
    a = 手机(s, gina)
    a.wait_kind("granted")
    b = 手机(s, gus)
    assert b.status == 409 and "gina" in b.error
    assert 手机(s, gus, takeover="我来").status == 403, "只有管理员能强制接管"
    c = 手机(s, alice, takeover="gina 那台手机没电了")
    assert c.status == 101, c.error
    ended = a.wait_kind("ended")
    assert ended["reason"] == "taken_over"
    g = c.wait_kind("granted")
    assert g["lease_epoch"] == 2
    rows = s.db.query("SELECT epoch, operator, end_reason, takeover_by, takeover_reason "
                      "FROM teleop_leases ORDER BY epoch")
    assert rows[0]["end_reason"] == "taken_over" and rows[0]["takeover_by"] == "alice"
    assert rows[1]["takeover_reason"] == "gina 那台手机没电了"
    c.close()


def test_没画面不给租约_遥控中画面没了就不转发运动(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    s.video_live["site"] = False
    ph = 手机(s, tok)
    assert ph.status == 409 and "没有画面" in ph.error
    s.video_live["site"] = True
    ph = 手机(s, tok)
    ph.wait_kind("granted")
    _推(ph, 0.4, seconds=0.6)
    assert _odom(s).vx > 0
    s.video_live["site"] = False
    ph.send({"vx": 0.4, "wz": 0})
    assert ph.wait_kind("video")["ok"] is False
    _推(ph, 0.4, seconds=0.6)
    assert _等(lambda: _停了(s), timeout=3)
    ph.close()


def test_狗这头没在推流_帧到了也不动(站点):
    """站点那道门出错也兜得住:狗自己再查一遍本机有没有在推。"""
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    s.video_live["dog"] = False
    ph = 手机(s, tok)
    ph.wait_kind("granted")
    _推(ph, 0.4, seconds=1.0)
    assert _odom(s).x == 0.0
    ph.close()


def test_halt不走遥控连接_业主也能按_遥控跟着结束(站点):
    s = 站点
    tok, olga = _登(s), _登(s, "olga")
    _新鲜(s, tok)
    ph = 手机(s, tok)
    ph.wait_kind("granted")
    _推(ph, 0.4, seconds=0.6)
    code, d = s.req("POST", "/api/robots/A/halt", {}, token=olga)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    assert ph.wait_kind("ended")["reason"] == "halt"
    assert _等(lambda: _停了(s), timeout=3)
    ph.close()


def test_巡检中遥控_先抢占再接手_遥控期间派单被挑开(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(6.0), "max_speed_mps": 0.5},
                    token=tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _odom(s).vx > 0)
    ph = 手机(s, tok)
    assert ph.status == 101, ph.error
    ph.wait_kind("granted")
    _等(lambda: (s.disp.clients["A"].status.task or None) is not None
        and s.disp.clients["A"].status.task.kind == "teleop", timeout=8)
    evs = s.req("GET", "/api/robots/A", token=tok)[1]["events"]
    assert any(e["kind"] == "task_preempted" for e in evs), evs
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=tok)
    assert code == 409 and "正在遥控" in d["error"], d
    ph.close()


def test_遥控中账号被停用_连接结束说登录过期(站点):
    s = 站点
    s.api.sse_recheck_s = 0.5
    tok = _登(s)
    _新鲜(s, tok)
    ph = 手机(s, tok)
    ph.wait_kind("granted")
    s.accounts.set_disabled("gina", True)
    assert ph.wait_kind("ended")["reason"] == "logged_out"
    _等(lambda: s.teleop.active("A") is None, timeout=5)
    ph.close()


def test_帧两头都是QoS0_断线期间的帧不补投(站点):
    """决策 7:不许重放。站点发 QoS 0、不保留;狗订 QoS 0。两头各自都得是 0(另一头配错了也不补投)。"""
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    sent = []
    t = s.disp._t
    real = t.publish

    async def 记(topic, payload, *, qos=1, retain=False):
        if topic.endswith("/teleop"):
            sent.append((qos, retain))
        return await real(topic, payload, qos=qos, retain=retain)
    t.publish = 记
    ph = 手机(s, tok)
    ph.wait_kind("granted")
    _推(ph, 0.2, seconds=0.3)
    ph.close()
    assert sent and set(sent) == {(0, False)}, sent
    subs = [x.qos for x in s.broker._clients["dogA"].subs if x.topic_filter.endswith("/teleop")]
    assert subs == [0], subs


def test_急停按着不给租约(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    s.loop.call(lambda: s.dog.emergency_stop(True))
    _等(lambda: not s.disp.clients["A"].status.ready.estop_clear, timeout=5)
    ph = 手机(s, tok)
    assert ph.status == 409 and "estop_clear" in ph.error, (ph.status, ph.error)


def test_halt接到了狗的停车上(站点):
    s = 站点
    assert s.agent.processor.halt_hook == s.dog.stop
