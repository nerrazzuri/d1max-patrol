"""W00c5b:视频经站点。真 ffmpeg、真 SRT:狗(代理 + 仿真 HAL)用 ffmpeg 测试图顶相机,按需推到站点;
站点收流转 MJPEG,经 HTTPS API 给观众。"""

from __future__ import annotations

import http.client
import shutil
import socket
import time

import pytest
from test_site_api import PW, _等, 站

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="没有 ffmpeg")


def _端口段() -> tuple[int, int]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    return p, p + 10


def _站(tmp_path, **kw):
    video = {"ports": _端口段(), "ttl_ms": 2000, "idle_s": 1.0,
             "first_frame_timeout_s": 20.0} | kw.pop("video", {})
    return 站(tmp_path, video=video, **kw)


@pytest.fixture
def 站点(tmp_path):
    s = _站(tmp_path)
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name="alice"):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


class 观众:
    """一条 MJPEG 连接:读 multipart,数 JPEG。"""

    def __init__(self, s, tok, path="/api/robots/A/video/front") -> None:
        host, port = s.api.httpd.server_address[:2]
        self.conn = http.client.HTTPConnection(host, port, timeout=30)
        self.conn.request("GET", path, headers={"Authorization": f"Bearer {tok}"})
        self.resp = self.conn.getresponse()

    def jpegs(self, n: int) -> list[bytes]:
        got, buf = [], b""
        while len(got) < n:
            chunk = self.resp.read1(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or b < 0:
                    break
                got.append(buf[a:b + 2])
                buf = buf[b + 2:]
        return got

    def close(self) -> None:
        # 回包对象自己握着套接字:只关连接对象的话,观众其实没走。
        self.resp.close()
        self.conn.close()


def _新鲜(s, tok):
    _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))


def test_画面经站点来了_健康里看得到(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    v = 观众(s, tok)
    try:
        assert v.resp.status == 200, v.resp.read()
        assert v.resp.getheader("Content-Type").startswith("multipart/x-mixed-replace")
        assert len(v.jpegs(3)) == 3
        code, h = s.req("GET", "/api/robots/A/video/health", token=tok)
        assert code == 200 and h["cameras"]["front"]["online"] is True
        assert h["cameras"]["front"]["viewers"] == 1 and h["cameras"]["back"]["online"] is False
    finally:
        v.close()


def test_两个观众只起一路_都走了之后站点收掉_狗也在有效期内停(站点):
    s = 站点
    tok, olga = _登(s), _登(s, "olga")
    _新鲜(s, tok)
    a, b = 观众(s, tok), 观众(s, olga)                   # 业主也能看(view)
    try:
        assert len(a.jpegs(2)) == 2 and len(b.jpegs(2)) == 2
        assert s.hub.feed("A", "front").starts == 1
        assert s.pusher.starts == 1, "狗那头只推一份"
    finally:
        a.close()
        b.close()
    _等(lambda: s.req("GET", "/api/robots/A/video/health", token=tok)[1]["cameras"]["front"]
        ["viewers"] == 0)
    _等(lambda: not s.hub.feed("A", "front")._running, timeout=10)
    _等(lambda: s.pusher.running() == set(), timeout=10)


def test_没登录401_相机名不认404(站点):
    s = 站点
    tok = _登(s)
    assert s.req("GET", "/api/robots/A/video/front")[0] == 401
    assert s.req("GET", "/api/robots/A/video/top", token=tok)[0] == 404
    assert s.req("GET", "/api/robots/A/video/health")[0] == 401


def test_没登记的狗_回404(站点):
    s = 站点
    tok = _登(s)
    code, d = s.req("GET", "/api/robots/ghost/video/front", token=tok)
    assert code == 404 and "没有登记" in d["error"], d


def test_狗不推视频_回502带狗的理由(tmp_path):
    s = _站(tmp_path, agent_video=False)
    try:
        tok = _登(s)
        _新鲜(s, tok)
        code, d = s.req("GET", "/api/robots/A/video/front", token=tok)
        assert code == 502 and "unsupported" in d["error"], d
    finally:
        s.close()


def test_第一帧等不到_回504(tmp_path):
    s = _站(tmp_path, video={"first_frame_timeout_s": 0.3})
    try:
        tok = _登(s)
        _新鲜(s, tok)
        code, d = s.req("GET", "/api/robots/A/video/front", token=tok)
        assert code == 504 and "第一帧" in d["error"], d
    finally:
        s.close()


def test_狗报推流失败_观众立刻拿到原因(tmp_path):
    s = _站(tmp_path)
    try:
        s.pusher._ffmpeg = "/bin/false"               # 狗上推流进程一起来就退
        tok = _登(s)
        _新鲜(s, tok)
        t0 = time.monotonic()
        code, d = s.req("GET", "/api/robots/A/video/front", token=tok)
        assert code == 502 and "狗推流失败" in d["error"], d
        assert time.monotonic() - t0 < 15, "不用干等第一帧超时"
    finally:
        s.close()


def test_看着的时候注销_画面跟着断(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    s.api.sse_recheck_s = 0.5
    v = 观众(s, tok)
    try:
        assert len(v.jpegs(2)) == 2
        assert s.req("POST", "/api/logout", {}, token=tok)[0] == 200
        t0 = time.monotonic()
        while v.resp.read1(65536):
            assert time.monotonic() - t0 < 10, "注销之后画面还在流"
    finally:
        v.close()


def test_登记过但不在线的狗_站点当场说不在线_不去干等回执(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    s.loop.call(s.agent.close)
    _等(lambda: not s.req("GET", "/api/robots/A", token=tok)[1].get("fresh")
        or s.disp.clients["A"].status.online is False)
    t0 = time.monotonic()
    code, d = s.req("GET", "/api/robots/A/video/front", token=tok)
    assert code == 502 and "不在线" in d["error"], d
    assert time.monotonic() - t0 < 4, "不许等到回执超时才说"


def test_画面冻住了_站点不等SRT自己超时_先断给观众(tmp_path):
    """推流卡住但连接没断(狗那头进程挂起):冻着的画面比没画面更危险 —— 人以为前面没障碍。"""
    import os
    import signal
    s = _站(tmp_path, video={"stale_s": 0.5})
    try:
        tok = _登(s)
        _新鲜(s, tok)
        v = 观众(s, tok)
        try:
            assert len(v.jpegs(2)) == 2
            proc = s.pusher._push["front"].proc
            os.kill(proc.pid, signal.SIGSTOP)
            try:
                t0 = time.monotonic()
                while v.resp.read1(65536):
                    pass
                assert time.monotonic() - t0 < 3.5, "冻住了还在一直等"
            finally:
                os.kill(proc.pid, signal.SIGCONT)
        finally:
            v.close()
    finally:
        s.close()


# ------------------------------------------------------------ 内部评审补的(W00c5b)

def test_续期按有效期的一半_不按帧率(站点):
    """评审阻断:续期线程跟收流线程共用条件变量,每来一帧就发一条命令。"""
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    n = {"req": 0}
    real = s.pusher.request

    def 数(req):
        n["req"] += 1
        return real(req)
    s.pusher.request = 数
    v = 观众(s, tok)
    try:
        assert len(v.jpegs(3)) == 3
        t0, n0 = time.monotonic(), n["req"]
        while time.monotonic() - t0 < 4.0:
            v.jpegs(1)
        # ttl 2 s → 每 1 s 续一次;4 s 里最多 5 条,不是 5 fps × 4 s = 20 条
        assert n["req"] - n0 <= 6, n["req"] - n0
    finally:
        v.close()


def test_正常收流不报推流失败_事件里也没有口令(tmp_path):
    """有效期(10 s)比 SRT 对端空闲超时(约 5 s)长:站点收流时不先让狗停的话,狗那头推流进程会先因为
    对端没了自己退、报一条假的推流失败。"""
    s = _站(tmp_path, video={"ttl_ms": 10_000})
    try:
        tok = _登(s)
        _新鲜(s, tok)
        v = 观众(s, tok)
        try:
            assert len(v.jpegs(2)) == 2
        finally:
            v.close()
        _等(lambda: s.pusher.running() == set(), timeout=15)
        time.sleep(1)
        evs = s.req("GET", "/api/robots/A", token=tok)[1]["events"]
        assert not [e for e in evs if e["kind"] == "video_failed"], evs
    finally:
        s.close()


def test_第一帧超时_这一路收掉_下一个观众重新起(tmp_path):
    s = _站(tmp_path, video={"first_frame_timeout_s": 0.3})
    try:
        tok = _登(s)
        _新鲜(s, tok)
        assert s.req("GET", "/api/robots/A/video/front", token=tok)[0] == 504
        f = s.hub.feed("A", "front")
        assert not f._running, "起不来的那一路不许留着,狗上的推流也不许一直被续"
        assert s.req("GET", "/api/robots/A/video/front", token=tok)[0] == 504
        assert f.starts == 2
    finally:
        s.close()


def test_没登记的狗_404_不建路不起进程(站点):
    s = 站点
    tok = _登(s)
    assert s.req("GET", "/api/robots/nobody/video/front", token=tok)[0] == 404
    assert s.req("GET", "/api/robots/nobody/video/health", token=tok)[0] == 404
    assert ("nobody", "front") not in s.hub._feeds


def test_续期偶尔失败一次不断画面(站点):
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    v = 观众(s, tok)
    try:
        assert len(v.jpegs(2)) == 2
        real, once = s.hub._send, {"left": 1}

        def 抖一下(rid, req, **kw):
            if once["left"]:
                once["left"] -= 1
                return "等狗的回执超时"
            return real(rid, req, **kw)
        s.hub._send = 抖一下
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            assert v.jpegs(1), "续期失败一次就把正在出画面的流收掉了"
        assert once["left"] == 0
    finally:
        v.close()


def test_迟到的旧一代失败事件_不杀新一代(站点):
    from d1max_contract.messages import Event
    s = 站点
    tok = _登(s)
    _新鲜(s, tok)
    v = 观众(s, tok)
    try:
        assert len(v.jpegs(2)) == 2
        s.hub.on_event("A", Event(event_id="x", seq=999, boot_id="b", stamp=1, kind="video_failed",
                                  data={"camera": "front", "url": "srt://127.0.0.1:1",
                                        "reason": "旧的"}))
        assert len(v.jpegs(3)) == 3, "别的端口(旧一代)的失败事件不许收掉当前这一路"
    finally:
        v.close()
