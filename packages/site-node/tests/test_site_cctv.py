"""CCTV 接入(W19,决策 33、34):摄像头自带的入侵检测走 ONVIF 事件 → 入侵派遣。

假摄像头是一个本机 HTTP 服务:设备能力、订阅、拉消息、续订、退订,按 WS-Security 的口令摘要核口令。
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import re
import subprocess
import sys
import threading
import time

import pytest

from d1max_site import cctv
from d1max_site.cctv import (
    Camera,
    CameraWatch,
    CctvManager,
    CctvView,
    OnvifAuthError,
    OnvifClient,
    OnvifEvent,
    _rtsp_with_auth,
    add_camera,
    incident_body,
    load_cameras,
    parse_event,
    remove_camera,
)
from d1max_site.db import SiteDB

FIELD = "tns1:RuleEngine/FieldDetector/ObjectsInside"
MOTION = "tns1:RuleEngine/CellMotionDetector/Motion"


def _msg(topic: str, value: str, op: str = "Changed", rule: str = "R1") -> str:
    return (f'<wsnt:NotificationMessage><wsnt:Topic Dialect="x">{topic}</wsnt:Topic>'
            f'<wsnt:Message><tt:Message UtcTime="2026-10-05T01:00:00Z" PropertyOperation="{op}">'
            f'<tt:Source><tt:SimpleItem Name="Rule" Value="{rule}"/></tt:Source>'
            f'<tt:Data><tt:SimpleItem Name="IsInside" Value="{value}"/></tt:Data>'
            "</tt:Message></wsnt:Message></wsnt:NotificationMessage>")


ENV = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
       'xmlns:tt="http://www.onvif.org/ver10/schema" '
       'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
       'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
       'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
       'xmlns:wsa="http://www.w3.org/2005/08/addressing"><s:Body>{}</s:Body></s:Envelope>')


class 假摄像头:
    def __init__(self, user="admin", password="cam-pass") -> None:
        self.user, self.password = user, password
        self.queue: list[str] = []
        self.calls: list[str] = []
        self.lock = threading.Lock()
        cam = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                if not cam._ok(body):
                    fault = ('<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>'
                             '<s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>'
                             '<s:Reason><s:Text>NotAuthorized</s:Text></s:Reason></s:Fault>')
                    return self._send(400, ENV.format(fault))
                op = re.search(r"<s:Body><(?:\w+:)?(\w+)", body).group(1)
                with cam.lock:
                    cam.calls.append(op)
                base = f"http://127.0.0.1:{cam.port}"
                if op == "GetCapabilities":
                    out = (f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Events>"
                           f"<tt:XAddr>{base}/onvif/events</tt:XAddr></tt:Events>"
                           "</tds:Capabilities></tds:GetCapabilitiesResponse>")
                elif op == "CreatePullPointSubscription":
                    out = ("<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
                           f"<wsa:Address>{base}/onvif/sub/1</wsa:Address>"
                           "</tev:SubscriptionReference></tev:CreatePullPointSubscriptionResponse>")
                elif op == "PullMessages":
                    deadline = time.monotonic() + 0.3
                    while time.monotonic() < deadline:
                        with cam.lock:
                            msgs, cam.queue = cam.queue, []
                        if msgs:
                            break
                        time.sleep(0.02)
                    out = "<tev:PullMessagesResponse>" + "".join(msgs) + \
                          "</tev:PullMessagesResponse>"
                else:
                    out = f"<x:{op}Response xmlns:x='x'/>"
                self._send(200, ENV.format(out))

            def _send(self, code, text):
                data = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _ok(self, body: str) -> bool:
        """核 WS-Security 的口令摘要:SHA1(nonce + created + 口令)。"""
        try:
            user = re.search(r"<wsse:Username>(.*?)</wsse:Username>", body).group(1)
            pw = re.search(r"<wsse:Password[^>]*>(.*?)</wsse:Password>", body).group(1)
            nonce = base64.b64decode(re.search(r"<wsse:Nonce[^>]*>(.*?)</wsse:Nonce>", body)
                                     .group(1))
            created = re.search(r"<wsu:Created>(.*?)</wsu:Created>", body).group(1)
        except AttributeError:
            return False
        want = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode())
                                .digest()).decode()
        return user == self.user and pw == want

    def push(self, *msgs: str) -> None:
        with self.lock:
            self.queue.extend(msgs)

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def 摄像头(monkeypatch):
    monkeypatch.setattr(cctv, "PULL_WAIT_S", 1)
    c = 假摄像头()
    yield c
    c.close()


def _cam(port=1, **kw) -> Camera:
    return Camera(name="gate-cam", onvif_url=f"http://127.0.0.1:{port}", username="admin",
                  password=kw.pop("password", "cam-pass"), zone="front-yard", **kw)


def _等(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError("等不到")


# ------------------------------------------------------------ 规则


def _ev(topic=FIELD, value="true", op="Changed", rule="R1") -> OnvifEvent:
    import xml.etree.ElementTree as ET
    return parse_event(ET.fromstring(ENV.format(_msg(topic, value, op, rule))))


def test_通知解析_主题_现状还是变化_来源_数据():
    e = _ev()
    assert e.topic == FIELD and e.operation == "Changed" and e.active
    assert e.source == {"Rule": "R1"} and e.data == {"IsInside": "true"}
    assert not _ev(value="false").active


def test_只认由无到有_开头报的现状不算_一直有不重报_30秒内不重报_动了默认不算():
    t = [100.0]
    fired = []
    w = CameraWatch(_cam(), on_intrusion=lambda c, e: fired.append(e.topic), now_ms=lambda: 1,
                    client=OnvifClient("http://x", "", ""), monotonic=lambda: t[0])
    assert not w.handle(_ev(op="Initialized")), "订阅开头报的现状(正好有人)不算"
    assert not w.handle(_ev()), "开头就是有:这一下不是由无到有"
    w.handle(_ev(value="false"))
    assert w.handle(_ev()) and fired == [FIELD]
    assert not w.handle(_ev()), "一直有:不重报"
    w.handle(_ev(value="false"))
    t[0] += 5
    assert not w.handle(_ev()), "30 秒内不重报"
    w.handle(_ev(value="false"))
    t[0] += 40
    assert w.handle(_ev()) and len(fired) == 2
    t[0] += 40                                             # 过了防抖:下面不报只能是因为「动了不算」
    assert not w.handle(_ev(MOTION, value="false")) and not w.handle(_ev(MOTION)), "动了默认不算"
    t[0] += 40
    w.cam.motion = True
    w.handle(_ev(MOTION, value="false"))
    assert w.handle(_ev(MOTION)), "开了 motion 才算"
    assert w.status.last_topic == MOTION and w.status.last_event_ms == 1


def test_各家的入侵规则名都认():
    for topic in ("tns1:RuleEngine/LineDetector/Crossed", "tns1:VideoAnalytics/CrossRegion",
                  "tns1:RuleEngine/PeopleDetector/People", "tns1:Ext/HumanDetect",
                  "tns1:RuleEngine/MyRule/Intrusion"):
        assert _cam().wants(topic), topic
    assert not _cam().wants("tns1:VideoSource/ImageTooBlurry")
    assert not _cam().wants("tns1:Device/Trigger/DigitalInput")


# ------------------------------------------------------------ 跟假摄像头连


def test_连上订阅_入侵来了报一条_状态连着(摄像头):
    got = []
    w = CameraWatch(_cam(摄像头.port), on_intrusion=lambda c, e: got.append((c.zone, e.topic)),
                    now_ms=lambda: 5)
    w.start()
    try:
        _等(lambda: w.status.connected)
        摄像头.push(_msg(FIELD, "true", "Initialized"))
        摄像头.push(_msg(FIELD, "false"), _msg(FIELD, "true"))
        _等(lambda: got)
        assert got == [("front-yard", FIELD)]
        assert {"GetCapabilities", "CreatePullPointSubscription", "PullMessages"} <= set(
            摄像头.calls)
    finally:
        w.stop()
        w.join()
    _等(lambda: "Unsubscribe" in 摄像头.calls)


def test_口令不对_连不上_说是口令(摄像头, monkeypatch):
    monkeypatch.setattr(cctv, "RETRY_S", 0.05)
    w = CameraWatch(_cam(摄像头.port, password="wrong"), on_intrusion=lambda c, e: None,
                    now_ms=lambda: 5)
    w.start()
    try:
        _等(lambda: w.status.error)
        assert not w.status.connected and "NotAuthorized" in w.status.error
    finally:
        w.stop()
        w.join()
    with pytest.raises(OnvifAuthError):
        OnvifClient(f"http://127.0.0.1:{摄像头.port}", "admin", "wrong").events_url()


def test_摄像头没了_状态变断开_说连不上(monkeypatch):
    monkeypatch.setattr(cctv, "PULL_WAIT_S", 1)
    monkeypatch.setattr(cctv, "RETRY_S", 0.05)
    c = 假摄像头()
    port = c.port
    w = CameraWatch(_cam(port), on_intrusion=lambda cam, e: None, now_ms=lambda: 5)
    w.start()
    try:
        _等(lambda: w.status.connected)
        c.close()
        c.httpd.server_close()
        _等(lambda: not w.status.connected, timeout=10)
        assert "连不上" in w.status.error
    finally:
        w.stop()
        w.join()


# ------------------------------------------------------------ 管


class _假订阅:
    def __init__(self, cam, *, on_intrusion, now_ms):
        self.cam = cam
        self.status = cctv.CamStatus(since_ms=now_ms())
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True
        return True


class _告警:
    """真的告警台(落库),同步调;``fail`` 里的那几样这一下抛错(测重试)。"""

    def __init__(self, db, now):
        from d1max_site.alert_store import AlertDesk
        self.desk = AlertDesk(db, now_ms=lambda: now[0])
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def _go(self, what, fn, *a, **kw):
        self.calls.append(what)
        if what in self.fail:
            raise RuntimeError(f"{what} 炸了")
        return fn(*a, **kw)

    def raise_alert(self, **kw):
        return self._go("raise", self.desk.raise_alert, **kw)

    def resolve_all(self, robot, kind, *, who):
        return self._go("resolve", self.desk.resolve_all, robot, kind, who=who)

    def resolve_except(self, kind, prefix, keep, *, who):
        return self._go("resolve_except", self.desk.resolve_except, kind, prefix, keep, who=who)

    def has_open(self, robot, kind):
        return self._go("has_open", self.desk.has_open, robot, kind)

    def offline(self):
        return sorted(a["robot"] for a in self.desk.open() if a["kind"] == "cctv_offline")


def test_照库起停_改了重起_断五分钟报一次P2_口令不出站(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    now = [1_000_000]
    al = _告警(db, now)
    m = CctvManager(db, report=lambda c, e: None, now_ms=lambda: now[0], alerts=al,
                    watch_factory=_假订阅)
    add_camera(db, _cam(80), now_ms=now[0])
    m.sync()
    w = m.watches["gate-cam"]
    assert w.started
    view = m.view()
    assert view[0]["name"] == "gate-cam" and "password" not in view[0] and not view[0]["live"]
    assert "cam-pass" not in str(view)
    add_camera(db, _cam(80, rtsp_url="rtsp://10.0.0.9/1"), now_ms=now[0])
    m.sync()
    assert w.stopped and m.watches["gate-cam"] is not w, "改了:换一条订阅"
    now[0] += 299_000
    m.sync()
    assert al.offline() == []
    now[0] += 2_000
    m.sync()
    m.sync()
    [a] = [x for x in al.desk.open() if x["kind"] == "cctv_offline"]
    assert a["robot"] == "cctv:gate-cam" and "gate-cam" in a["title"] and a["level"] == "P2"
    assert a["count"] == 1, "不重报"
    remove_camera(db, "gate-cam")
    m.sync()
    assert m.watches == {}
    with pytest.raises(ValueError):
        add_camera(db, Camera("a b", "http://x", "", "", "z"), now_ms=1)
    with pytest.raises(ValueError):
        add_camera(db, Camera("cam", "http://x", "", "", ""), now_ms=1)
    assert load_cameras(db) == []


def test_交给事件派遣的那一条():
    b = incident_body(_cam(), OnvifEvent(FIELD, "Changed", {}, {"IsInside": "true"}), 123)
    assert b == {"event_id": "gate-cam-123", "type": "intrusion", "zone": "front-yard",
                 "occurred_at": 123, "detail": {"camera": "gate-cam", "topic": FIELD}}
    from d1max_site.incidents import IncidentDesk
    assert IncidentDesk.parse(b)["zone"] == "front-yard"


# ------------------------------------------------------------ 实时画面


def test_rtsp地址里没账号就塞进去_有就不动():
    assert _rtsp_with_auth("rtsp://10.0.0.9:554/s1", "admin", "p@ss") == \
        "rtsp://admin:p%40ss@10.0.0.9:554/s1"
    assert _rtsp_with_auth("rtsp://u:p@10.0.0.9/s1", "admin", "x") == "rtsp://u:p@10.0.0.9/s1"
    assert _rtsp_with_auth("rtsp://10.0.0.9/s1", "", "") == "rtsp://10.0.0.9/s1"


def test_实时画面_观众共用一条_都走了收掉():
    procs = []

    def popen(argv, **kw):
        p = subprocess.Popen([sys.executable, "-c",
                              "import sys,time\n"
                              "for i in range(400):\n"
                              "    sys.stdout.buffer.write(b'\\xff\\xd8%d\\xff\\xd9' % i)\n"
                              "    sys.stdout.flush(); time.sleep(0.01)"],
                             stdout=subprocess.PIPE, stderr=kw["stderr"])
        procs.append((argv, p))
        return p
    v = CctvView(_cam(rtsp_url="rtsp://10.0.0.9/s1"), popen=popen, idle_s=0.1)
    a, b = v.frames(), v.frames()
    fa, fb = next(a), next(b)
    assert fa.startswith(b"\xff\xd8") and fb.startswith(b"\xff\xd8")
    assert len(procs) == 1, "两个观众共用一条 ffmpeg"
    argv = procs[0][0]
    assert not any("cam-pass" in a for a in argv), "口令不进命令行(ps、/proc 看得到)"
    a.close()
    b.close()
    _等(lambda: procs[0][1].poll() is not None, timeout=5)


# ------------------------------------------------------------ 接口、命令行


@pytest.fixture
def 站点(tmp_path):
    from test_site_api import PW, 站
    s = 站(tmp_path)
    s.cctv = CctvManager(s.db, report=lambda c, e: None, now_ms=lambda: 1,
                         watch_factory=_假订阅)
    s.api.cctv = s.cctv
    s.cctv.on_changed = s.api.drop_cctv_view              # 同站点主程序的接线
    s.accounts.add("olga", PW, role="owner")
    s.PW = PW
    yield s
    s.close()


def test_接口_谁都能看状态_不带口令_只收GET_画面没登记地址404(站点):
    s = 站点
    add_camera(s.db, _cam(80), now_ms=1)
    s.cctv.sync()
    tok = s.req("POST", "/api/login", {"name": "olga", "password": s.PW})[1]["token"]
    code, d = s.req("GET", "/api/cameras", token=tok)
    assert code == 200 and d["cameras"][0]["name"] == "gate-cam" and "cam-pass" not in str(d)
    assert s.req("POST", "/api/cameras", {}, token=tok)[0] == 405
    assert s.req("GET", "/api/cameras")[0] == 401
    code, d = s.req("GET", "/api/cameras/gate-cam/live", token=tok)
    assert code == 404 and "--rtsp" in d["error"]
    assert s.req("GET", "/api/cameras/nope/live", token=tok)[0] == 404


def test_接口_实时画面是MJPEG(站点):
    import urllib.request
    s = 站点
    add_camera(s.db, _cam(80, rtsp_url="rtsp://10.0.0.9/s1"), now_ms=1)
    s.cctv.sync()
    cam = s.cctv.camera("gate-cam")

    def popen(argv, **kw):
        return subprocess.Popen([sys.executable, "-c",
                                 "import sys,time\n"
                                 "for i in range(200):\n"
                                 "    sys.stdout.buffer.write(b'\\xff\\xd8x\\xff\\xd9')\n"
                                 "    sys.stdout.flush(); time.sleep(0.02)"],
                                stdout=subprocess.PIPE, stderr=kw["stderr"])
    s.api._cctv_views["gate-cam"] = CctvView(cam, popen=popen, idle_s=0.1)
    tok = s.req("POST", "/api/login", {"name": "olga", "password": s.PW})[1]["token"]
    host, port = s.api.httpd.server_address[:2]
    req = urllib.request.Request(f"http://{host}:{port}/api/cameras/gate-cam/live",
                                 headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        head = resp.read(200)
    assert b"image/jpeg" in head and b"\xff\xd8" in head


def test_没开摄像头的站点404(tmp_path):
    from test_site_api import PW, 站
    s = 站(tmp_path)
    try:
        tok = s.req("POST", "/api/login", {"name": "alice", "password": PW})[1]["token"]
        assert s.req("GET", "/api/cameras", token=tok)[0] == 404
    finally:
        s.close()


def test_命令行_加_列_删_口令不显示(tmp_path, monkeypatch, capsys):
    from d1max_site import main as site_main
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "estate-1",
                           "--hostname", "127.0.0.1"]) == 0
    monkeypatch.setenv("D1MAX_CAMERA_PASSWORD", "cam-secret")
    capsys.readouterr()
    assert site_main.main(["--home", str(home), "camera-add", "gate-cam", "--onvif",
                           "http://10.0.0.9", "--user", "admin", "--zone", "front-yard",
                           "--rtsp", "rtsp://10.0.0.9/s1"]) == 0
    assert site_main.main(["--home", str(home), "camera-add", "bad name", "--onvif", "x",
                           "--zone", "z"]) == 2
    capsys.readouterr()
    assert site_main.main(["--home", str(home), "camera-list"]) == 0
    out = capsys.readouterr().out
    assert "gate-cam" in out and "front-yard" in out and "有画面" in out
    assert "cam-secret" not in out
    db = SiteDB(home / "site.db")
    assert load_cameras(db)[0].password == "cam-secret"
    db.close()
    assert site_main.main(["--home", str(home), "camera-rm", "gate-cam"]) == 0
    assert site_main.main(["--home", str(home), "camera-rm", "gate-cam"]) == 2



# ------------------------------------------------------------ W19 外审:告警、线程、进程、口令


def test_W19外审_两台同时断各一条_只恢复一台解决一台_删掉断着的也解决(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    now = [1_000_000]
    al = _告警(db, now)
    m = CctvManager(db, report=lambda c, e: None, now_ms=lambda: now[0], alerts=al,
                    watch_factory=_假订阅)
    for n in ("a", "b", "c"):
        add_camera(db, Camera(n, "http://x", "", "", "z"), now_ms=1)
    m.sync()
    now[0] += 301_000
    m.sync()
    assert al.offline() == ["cctv:a", "cctv:b", "cctv:c"], "各一条,不合并、不互相盖"
    m.sync()
    assert al.calls.count("raise") == 3, "不重报"
    m.watches["a"].status.connected = True
    m.sync()
    assert al.offline() == ["cctv:b", "cctv:c"], "只解决恢复的那一台"
    m.watches["b"].status.connected = True
    m.sync()
    assert al.offline() == ["cctv:c"]
    remove_camera(db, "c")
    m.sync()
    assert al.offline() == [], "删掉断着的:它的告警不再挂着"
    m.watches["a"].status.connected = False
    m.watches["a"].status.since_ms = now[0]
    now[0] += 301_000
    m.sync()
    assert al.offline() == ["cctv:a"], "恢复后再断:再报"


def test_W19复查_重启之后_已经连上的解决旧告警_还断着的不重报_停机时删的也解决(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    now = [1_000_000]
    al = _告警(db, now)
    m = CctvManager(db, report=lambda c, e: None, now_ms=lambda: now[0], alerts=al,
                    watch_factory=_假订阅)
    for n in ("a", "b", "c"):
        add_camera(db, Camera(n, "http://x", "", "", "z"), now_ms=1)
    m.sync()
    now[0] += 301_000
    m.sync()
    assert al.offline() == ["cctv:a", "cctv:b", "cctv:c"]
    m.close()                                             # 站点停了
    remove_camera(db, "c")                                # 停机期间删了 c
    al2 = _告警(db, now)                                  # 重启:告警台从库里读回来,内存是空的
    m2 = CctvManager(db, report=lambda c, e: None, now_ms=lambda: now[0], alerts=al2,
                     watch_factory=_假订阅)
    m2.sync()
    m2.watches["a"].status.connected = True               # a 恢复了;b 还断着
    m2.sync()
    assert al2.offline() == ["cctv:b"], "a 的旧告警解决了;停机时删的 c 也解决了"
    now[0] += 301_000
    m2.sync()
    assert al2.calls.count("raise") == 0, "b 还断着:库里已经挂着,不重报"
    assert [a["count"] for a in al2.desk.open() if a["robot"] == "cctv:b"] == [1]


def test_W19复查_报不出去_解决不了_下一拍重试(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    now = [1_000_000]
    al = _告警(db, now)
    m = CctvManager(db, report=lambda c, e: None, now_ms=lambda: now[0], alerts=al,
                    watch_factory=_假订阅)
    add_camera(db, Camera("a", "http://x", "", "", "z"), now_ms=1)
    m.sync()
    now[0] += 301_000
    al.fail = {"raise"}
    m.sync()
    assert al.offline() == []
    al.fail = set()
    m.sync()
    assert al.offline() == ["cctv:a"], "上一拍没报成:这一拍报"
    m.watches["a"].status.connected = True
    al.fail = {"resolve"}
    m.sync()
    assert al.offline() == ["cctv:a"]
    al.fail = set()
    m.sync()
    assert al.offline() == [], "上一拍没解决成:这一拍解决"
    n = al.calls.count("resolve")
    m.sync()
    assert al.calls.count("resolve") == n, "确认解决了:不每拍都去问"


class _卡住的客户端:
    """PullMessages 卡着,测试放行时回一条入侵。"""

    def __init__(self):
        self.release = threading.Event()
        self.pulling = threading.Event()

    def events_url(self):
        return "e"

    def subscribe(self, url):
        return "s"

    def pull(self, sub):
        import xml.etree.ElementTree as ET
        self.pulling.set()
        self.release.wait(10)
        root = ET.fromstring(ENV.format(_msg(FIELD, "false") + _msg(FIELD, "true")))
        return [m for m in root.iter() if m.tag.endswith("NotificationMessage")]

    def renew(self, sub):
        pass

    def unsubscribe(self, sub):
        pass


def test_W19外审_删除时正卡在拉消息里_回来的入侵不派_删除等旧线程退干净(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    reported = []
    client = _卡住的客户端()

    def factory(cam, *, on_intrusion, now_ms):
        return CameraWatch(cam, on_intrusion=on_intrusion, now_ms=now_ms, client=client)
    m = CctvManager(db, report=lambda c, e: reported.append(c.name), now_ms=lambda: 1,
                    watch_factory=factory)
    add_camera(db, _cam(80), now_ms=1)
    m.sync()
    assert client.pulling.wait(5)
    old = m.watches["gate-cam"]
    remove_camera(db, "gate-cam")
    t = threading.Thread(target=m.sync)
    t.start()
    time.sleep(0.2)
    assert t.is_alive(), "删除要等旧订阅退干净"
    client.release.set()                                  # 这时候一条入侵回来了
    t.join(10)
    assert not t.is_alive() and not old._thread.is_alive()
    assert reported == [], "已经删了的摄像头:不派"


def test_W19外审_站点收尾等订阅线程退出(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    client = _卡住的客户端()
    m = CctvManager(db, report=lambda c, e: None, now_ms=lambda: 1,
                    watch_factory=lambda cam, **kw: CameraWatch(cam, client=client, **kw))
    add_camera(db, _cam(80), now_ms=1)
    m.sync()
    assert client.pulling.wait(5)
    w = m.watches["gate-cam"]
    threading.Timer(0.3, client.release.set).start()
    m.close()
    assert not w._thread.is_alive()


def _ffmpeg(frames: bytes, then_sleep: float = 0.0):
    """假 ffmpeg:吐几帧(可选再挂着)。"""
    code = ("import sys,time\n"
            f"sys.stdout.buffer.write({frames!r}); sys.stdout.flush()\n"
            f"time.sleep({then_sleep})\n")
    procs = []

    def popen(argv, **kw):
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                             stderr=kw["stderr"])
        procs.append((argv, p, kw["stderr"]))
        return p
    return popen, procs


def test_W19外审_ffmpeg自己退了_回收进程_关文件_删清单(tmp_path):
    import os
    popen, procs = _ffmpeg(b"\xff\xd8A\xff\xd9")
    v = CctvView(_cam(rtsp_url="rtsp://10.0.0.9/s1"), popen=popen, major=lambda f: 4)
    it = v.frames()
    assert next(it) == b"\xff\xd8A\xff\xd9"
    assert list(it) == [], "断了:流结束"
    argv, p, err = procs[0]
    listing = argv[argv.index("-i") + 1]
    _等(lambda: p.returncode is not None)
    assert err.closed and not os.path.exists(listing)
    assert v._proc is None and v._frame is None


def test_W19外审_重起不给上一轮的旧画面(tmp_path):
    popen, procs = _ffmpeg(b"\xff\xd8OLD\xff\xd9")
    v = CctvView(_cam(rtsp_url="rtsp://10.0.0.9/s1"), popen=popen, major=lambda f: 4)
    assert next(v.frames()) == b"\xff\xd8OLD\xff\xd9"
    _等(lambda: v._proc is None)
    popen2, procs2 = _ffmpeg(b"", then_sleep=5)            # 摄像头断了:一帧都不出
    v._popen = popen2
    with pytest.raises(TimeoutError):
        next(v.frames(first_timeout_s=0.5))
    v.close()


def test_W19外审_站点收尾收掉画面进程_幂等(站点):
    s = 站点
    add_camera(s.db, _cam(80, rtsp_url="rtsp://10.0.0.9/s1"), now_ms=1)
    s.cctv.sync()
    popen, procs = _ffmpeg(b"\xff\xd8x\xff\xd9", then_sleep=30)
    v = CctvView(s.cctv.camera("gate-cam"), popen=popen, idle_s=60, major=lambda f: 4)
    s.api._cctv_views["gate-cam"] = v
    it = v.frames()
    next(it)
    s.api.close_cctv_views()
    _等(lambda: procs[0][1].returncode is not None)
    v.close()                                             # 再关一次:没事
    with pytest.raises(TimeoutError):
        next(v.frames())


def test_W19外审_口令不进命令行_清单只许本用户读_出画面就删_v5带tcp(tmp_path):
    import os
    import stat
    seen = {}

    def popen(argv, **kw):
        listing = argv[argv.index("-i") + 1]
        seen["mode"] = stat.S_IMODE(os.stat(listing).st_mode)
        seen["text"] = open(listing).read()
        return subprocess.Popen([sys.executable, "-c",
                                 "import sys,time;sys.stdout.buffer.write(b'\\xff\\xd8z\\xff\\xd9')"
                                 ";sys.stdout.flush();time.sleep(3)"],
                                stdout=subprocess.PIPE, stderr=kw["stderr"])
    v = CctvView(_cam(rtsp_url="rtsp://10.0.0.9/s1"), popen=popen, major=lambda f: 6)
    it = v.frames()
    next(it)
    assert seen["mode"] == 0o600
    assert "rtsp://admin:cam-pass@10.0.0.9/s1" in seen["text"]
    assert "option rtsp_transport tcp" in seen["text"], "ffmpeg 5 起走 TCP"
    assert v._list is None, "画面出来了:清单删掉"
    it.close()
    v.close()
    v4 = CctvView(_cam(rtsp_url="rtsp://10.0.0.9/s1"), popen=popen, major=lambda f: 4)
    next(v4.frames())
    assert "option" not in seen["text"], "4.x 不认这一句"
    v4.close()


def test_W19外审_报错里的口令抹掉_日志和接口都不出现(站点, caplog):
    from d1max_site.cctv import scrub
    assert scrub("rtsp://admin:cam-pass@10.0.0.9/s1: 401") == "rtsp://***@10.0.0.9/s1: 401"
    assert scrub("http://u:p@h x rtsp://a:b@c") == "http://***@h x rtsp://***@c"
    code = ("import sys;sys.stderr.write('rtsp://admin:cam-pass@10.0.0.9/s1: 401 Unauthorized')")

    def popen(argv, **kw):
        return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                stderr=kw["stderr"])
    v = CctvView(_cam(rtsp_url="rtsp://10.0.0.9/s1"), popen=popen, major=lambda f: 4)
    with caplog.at_level("WARNING"), pytest.raises((TimeoutError, StopIteration)):
        next(v.frames(first_timeout_s=3))
    _等(lambda: "401" in caplog.text)
    assert "cam-pass" not in caplog.text and "***@" in caplog.text
    s = 站点
    add_camera(s.db, _cam(80), now_ms=1)
    s.cctv.sync()
    s.cctv.watches["gate-cam"].status.error = "rtsp://admin:cam-pass@x 连不上"
    assert "cam-pass" not in str(s.cctv.view())



def test_W19外审_告警台按这一位这一种解决_别的摄像头的不动(tmp_path):
    from d1max_site.alert_store import AlertDesk
    desk = AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1)
    for n in ("a", "b"):
        desk.raise_alert(kind="cctv_offline", robot=f"cctv:{n}", title=f"{n} 连不上")
    desk.raise_alert(kind="robot_offline_idle", robot="cctv:a", title="别的种类")
    assert desk.resolve_all("cctv:a", "cctv_offline", who="站点") == 1
    left = {(a["robot"], a["kind"]) for a in desk.open()}
    assert left == {("cctv:b", "cctv_offline"), ("cctv:a", "robot_offline_idle")}



def test_W19复查_有人在看时删掉摄像头_画面当场结束_ffmpeg收掉(站点):
    s = 站点
    add_camera(s.db, _cam(80, rtsp_url="rtsp://10.0.0.9/s1"), now_ms=1)
    s.cctv.sync()
    popen, procs = _ffmpeg(b"\xff\xd8x\xff\xd9" * 3, then_sleep=30)
    v = CctvView(s.cctv.camera("gate-cam"), popen=popen, idle_s=60, major=lambda f: 4)
    s.api._cctv_views["gate-cam"] = v
    it = v.frames()
    next(it)
    remove_camera(s.db, "gate-cam")
    s.cctv.sync()                                         # 不等谁再打开画面
    assert list(it) == [], "正在看的流当场结束"
    _等(lambda: procs[0][1].returncode is not None)
    assert "gate-cam" not in s.api._cctv_views
    with pytest.raises(TimeoutError):
        next(v.frames())


def test_W19复查_有人在看时改了地址_旧流关掉_再打开按新配置(站点):
    s = 站点
    add_camera(s.db, _cam(80, rtsp_url="rtsp://10.0.0.9/old"), now_ms=1)
    s.cctv.sync()
    popen, procs = _ffmpeg(b"\xff\xd8x\xff\xd9", then_sleep=30)
    v = CctvView(s.cctv.camera("gate-cam"), popen=popen, idle_s=60, major=lambda f: 4)
    s.api._cctv_views["gate-cam"] = v
    it = v.frames()
    next(it)
    add_camera(s.db, _cam(80, rtsp_url="rtsp://10.0.0.9/new"), now_ms=2)
    s.cctv.sync()
    _等(lambda: procs[0][1].returncode is not None)
    assert "gate-cam" not in s.api._cctv_views, "旧的摘掉了"
    assert s.cctv.camera("gate-cam").rtsp_url == "rtsp://10.0.0.9/new"
    import urllib.request
    procs2 = []

    def popen2(argv, **kw):                               # 一直出帧(观众只拿最新那张)
        p = subprocess.Popen([sys.executable, "-c",
                              "import sys,time\n"
                              "for i in range(1500):\n"
                              "    sys.stdout.buffer.write(b'\\xff\\xd8n\\xff\\xd9')\n"
                              "    sys.stdout.flush(); time.sleep(0.02)"],
                             stdout=subprocess.PIPE, stderr=kw["stderr"])
        procs2.append((argv, p))
        return p
    s.api.cctv_view_factory = lambda cam: CctvView(cam, popen=popen2, idle_s=60,
                                                   major=lambda f: 4)
    tok = s.req("POST", "/api/login", {"name": "olga", "password": s.PW})[1]["token"]
    host, port = s.api.httpd.server_address[:2]
    req = urllib.request.Request(f"http://{host}:{port}/api/cameras/gate-cam/live",
                                 headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=10) as resp:     # 再打开:按新配置起
        assert b"\xff\xd8n" in resp.read(200)
    assert s.api._cctv_views["gate-cam"].cam.rtsp_url == "rtsp://10.0.0.9/new"
    s.api.close_cctv_views()
    _等(lambda: procs2[0][1].returncode is not None)
