"""W42 绑定申请(站点这头):机器人挂申请 → 人核对核对码、批准 → 机器人轮询拿开通码 → 领证书包。"""

from __future__ import annotations

import os

import pytest

from d1max_contract.binding import check_digits
from d1max_site import binding as bd
from d1max_site import main as site_main
from d1max_site import provision as site
from d1max_site.db import SiteDB

NOW = 1_800_000_000_000
NONCE = "0123456789abcdef0123456789abcdef"


class 告警:
    def __init__(self):
        self.raised, self.resolved = [], []

    def raise_alert(self, **kw):
        self.raised.append(kw)

    def resolve_all(self, robot, kind, *, who):
        self.resolved.append((robot, kind, who))
        return 1


@pytest.fixture
def 台(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    monkeypatch.setattr(site_main, "wall_ms", lambda: NOW)
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "e",
                           "--hostname", "localhost"]) == 0
    db = SiteDB(home / "site.db")
    clock = {"t": NOW}
    names = {}
    a = 告警()
    desk = bd.BindDesk(db, now_ms=lambda: clock["t"], alerts=a,
                       enroll=lambda s: site_main.cmd_enroll(home, s, 365),
                       issue=lambda s, h, fp: site_main.cmd_enroll_code(home, s, h or None, 1.0,
                                                                        expect_fp=fp),
                       set_name=lambda s, n: names.__setitem__(s, n))
    yield desk, home, db, clock, a, names
    db.close()


def _挂(desk, serial="D1M-1", nonce=NONCE, source="10.0.0.5"):
    return desk.request({"serial": serial, "nonce": nonce, "model": "D1 Max", "version": "1.2"},
                        source=source, host="192.168.1.10")


def test_挂申请_批准_拿开通码_领证书包_回复丢了能再领(台):
    desk, home, db, clock, a, names = 台
    r = _挂(desk)
    assert r["check"] == check_digits(NONCE)
    assert a.raised and a.raised[0]["kind"] == "bind_request"
    assert r["check"] in a.raised[0]["detail"]
    assert desk.poll(r["request_id"], r["poll_secret"]) == {"state": "pending"}
    [p] = desk.pending()
    assert p["serial"] == "D1M-1" and p["check"] == r["check"] and p["source"] == "10.0.0.5"
    desk.approve(r["request_id"], by="owner", name="南区一号")
    assert names == {"D1M-1": "南区一号"}
    assert a.resolved == [("D1M-1", "bind_request", "owner")]
    got = desk.poll(r["request_id"], r["poll_secret"])
    assert got["state"] == "approved"
    code = site.decode(got["code"])
    assert code["id"] == "D1M-1" and code["api"] == "https://192.168.1.10:8443"
    assert code["mqtt"].startswith("mqtts://192.168.1.10:")
    files = site.claim(db, home, "D1M-1", code["t"], now_ms=NOW)
    assert "robot.key" in files
    # 外审 I7:领证的回复丢了 / 写到一半断电:再问一次给一个新码(同一代证书),旧码作废
    again = site.decode(desk.poll(r["request_id"], r["poll_secret"])["code"])
    assert again["t"] != code["t"]
    assert site.claim(db, home, "D1M-1", again["t"], now_ms=NOW)["robot.crt"] == files["robot.crt"]
    clock["t"] += bd.CLAIM_WINDOW_MS + 1
    assert desk.poll(r["request_id"], r["poll_secret"]) == {"state": "expired"}
    assert desk.pending() == []


def test_外审C1_解绑重新绑定以后_旧申请的口令领不到新私钥(台):
    desk, home, db, *_ = 台
    old = _挂(desk)
    desk.approve(old["request_id"], by="owner")
    site_main.cmd_revoke(home, "D1M-1")
    assert desk.poll(old["request_id"], old["poll_secret"]) == {"state": "replaced"}
    new = _挂(desk, nonce="c" * 32, source="10.0.0.9")
    desk.approve(new["request_id"], by="owner")
    assert desk.poll(old["request_id"], old["poll_secret"]) == {"state": "replaced"}
    assert desk.poll(new["request_id"], new["poll_secret"])["state"] == "approved"


@pytest.mark.parametrize("serial", ["..", ".", ".x", "-x"])
def test_外审C3_序列号不许点打头(台, serial):
    with pytest.raises(bd.BindRefused) as e:
        _挂(台[0], serial=serial)
    assert e.value.status == 400


def test_外审C3_CA不收全是点的编号(tmp_path):
    from d1max_site.ca import CAError, check_robot_id
    for bad in ("..", "."):
        with pytest.raises(CAError):
            check_robot_id("e", bad)


def test_口令不对_没这条_一律404(台):
    desk = 台[0]
    r = _挂(desk)
    for rid, sec in ((r["request_id"], "猜的"), ("ffff", r["poll_secret"]),
                     (r["request_id"], None)):
        with pytest.raises(bd.BindRefused) as e:
            desk.poll(rid, sec)
        assert e.value.status == 404


def test_拒绝_过期_同序列号再来顶掉旧的(台):
    desk, _, _, clock, a, _ = 台
    r1 = _挂(desk)
    r2 = _挂(desk, nonce="f" * 32)
    assert desk.poll(r1["request_id"], r1["poll_secret"]) == {"state": "replaced"}
    assert [p["id"] for p in desk.pending()] == [r2["request_id"]]
    desk.reject(r2["request_id"], by="owner")
    assert desk.poll(r2["request_id"], r2["poll_secret"]) == {"state": "rejected"}
    with pytest.raises(bd.BindRefused):
        desk.approve(r2["request_id"], by="owner")
    r3 = _挂(desk, serial="D1M-3")
    clock["t"] += bd.TTL_MS + 1
    assert desk.poll(r3["request_id"], r3["poll_secret"]) == {"state": "expired"}
    assert ("D1M-3", "bind_request", "system") in a.resolved
    assert desk.pending() == []


def test_已经在这个站点_不给挂_解绑后可以(台):
    desk, home, *_ = 台
    r = _挂(desk)
    desk.approve(r["request_id"], by="owner")
    with pytest.raises(bd.BindRefused) as e:
        _挂(desk, nonce="a" * 32, source="10.0.0.6")
    assert e.value.status == 409
    site_main.cmd_revoke(home, "D1M-1")
    r2 = _挂(desk, nonce="b" * 32, source="10.0.0.6")
    desk.approve(r2["request_id"], by="owner")          # 吊销过的同序列号重新登记
    assert desk.poll(r2["request_id"], r2["poll_secret"])["state"] == "approved"


def test_最多挂十条_同一地址一分钟五次(台):
    desk, _, _, clock, _, _ = 台
    for i in range(bd.MAX_PENDING):
        _挂(desk, serial=f"S{i}", source=f"10.0.1.{i}")
    with pytest.raises(bd.BindRefused) as e:
        _挂(desk, serial="S99", source="10.0.2.1")
    assert e.value.status == 429 and "10 台" in e.value.message
    for i in range(bd.RATE_PER_MIN - 1):
        desk.reject(desk.pending()[0]["id"], by="o")
        _挂(desk, serial=f"T{i}", source="10.0.3.1")
    desk.reject(desk.pending()[0]["id"], by="o")
    _挂(desk, serial="T9", source="10.0.3.1")
    desk.reject(desk.pending()[0]["id"], by="o")
    with pytest.raises(bd.BindRefused, match="太频繁"):
        _挂(desk, serial="T10", source="10.0.3.1")
    clock["t"] += 61_000
    _挂(desk, serial="T10", source="10.0.3.1")


@pytest.mark.parametrize("body", [{"serial": "有空格 1", "nonce": NONCE},
                                  {"serial": "D1M-1", "nonce": "短"}, [], None])
def test_形状不对400(台, body):
    with pytest.raises(bd.BindRefused) as e:
        台[0].request(body, source="x", host="h")
    assert e.value.status == 400


def test_证书还算不算(台):
    desk, home, db, *_ = 台
    assert desk.cert_state("D1M-1", "ab") == "unknown"
    r = _挂(desk)
    desk.approve(r["request_id"], by="owner")
    fp = db.query("SELECT fingerprint FROM robots WHERE robot_id='D1M-1'")[0]["fingerprint"]
    assert desk.cert_state("D1M-1", fp) == "active"
    assert desk.cert_state("D1M-1", fp.removeprefix("sha256:").upper()) == "active"
    # 外审 C2:登记的是另一张(续签了、机器人还没换上)不算吊销,机器人别清机
    assert desk.cert_state("D1M-1", "sha256:" + "0" * 64) == "unknown"
    site_main.cmd_revoke(home, "D1M-1")
    assert desk.cert_state("D1M-1", fp) == "revoked"
    assert desk.cert_state("../x", fp) == "unknown"


def test_接口_机器人不用登录_批准要manage和密码(tmp_path, 台):
    from test_site_api import PW
    from test_site_api import 站 as 起站
    _, home, *_ = 台
    s = 起站(tmp_path / "api", alerts=True)
    try:
        s.api.home = home          # 测试台的库跟站点目录的库是两个:登记进站点目录那个
        from attest_kit import 出厂
        fac = 出厂(home)           # W63:原厂机器人才进得来
        tok = s.login()
        code, d = s.req("POST", "/api/bind/request", {"serial": "D1M-9", "nonce": NONCE,
                                                      "attest": fac.答(s, "D1M-9", "bind")})
        assert code == 200, d
        rid, sec = d["request_id"], d["poll_secret"]
        assert s.req("GET", f"/api/bind/request/{rid}?secret={sec}")[1] == {"state": "pending"}
        assert s.req("GET", f"/api/bind/request/{rid}?secret=x")[0] == 404
        assert s.req("GET", "/api/admin/bind")[0] == 401
        code, d = s.req("GET", "/api/admin/bind", token=tok)
        assert code == 200 and d["requests"][0]["serial"] == "D1M-9"
        assert s.req("POST", f"/api/admin/bind/{rid}/approve", {}, token=tok)[0] == 403
        code, d = s.req("POST", f"/api/admin/bind/{rid}/approve",
                        {"confirm_password": PW, "name": "西区"}, token=tok)
        assert code == 200 and d["state"] == "approved", d
        # 测试台的库跟站点目录的库是两个:站点目录里登记的这一代,抄进测试台的库(真站点是同一个库)
        hd = SiteDB(home / "site.db")
        fp = hd.query("SELECT fingerprint FROM robots WHERE robot_id='D1M-9'")[0]["fingerprint"]
        hd.close()
        s.reg.enroll("D1M-9", fingerprint=fp, issued_at=0, expires_at=NOW * 2, now_ms=0)
        with s.db.tx() as c:
            c.execute("UPDATE bind_requests SET cert_fp=? WHERE id=?", (fp, rid))
        code, d = s.req("GET", f"/api/bind/request/{rid}?secret={sec}")
        assert d["state"] == "approved" and d["code"].startswith("D1MAX1."), d
        port = s.api.httpd.server_address[1]          # 开通码里的接口端口是站点实际听的那个
        assert site.decode(d["code"])["api"].endswith(f":{port}"), site.decode(d["code"])["api"]
        assert s.req("GET", "/api/bind/state?serial=A&cert=sha256:a")[1] == {"state": "active"}
        assert s.req("GET", "/api/bind/state?serial=A&cert=sha256:b")[1] == {"state": "unknown"}
        assert s.req("GET", "/api/bind/state?serial=NOPE&cert=x")[1] == {"state": "unknown"}
        assert s.req("POST", "/api/bind/request", {"serial": "A", "nonce": NONCE,
                                                   "attest": fac.答(s, "A", "bind")})[0] == 409
    finally:
        s.close()


def test_W63_没有出厂证书的_悄悄拒_不进待批准_不报警_记给公司(tmp_path, 台):
    from attest_kit import 出厂
    from test_site_api import 站 as 起站

    from d1max_site.attest import NEUTRAL
    _, home, *_ = 台
    s = 起站(tmp_path / "api", alerts=True)
    try:
        s.api.home = home
        fac = 出厂(home)
        tok = s.login()
        code, d = s.req("POST", "/api/bind/request", {"serial": "D1M-7", "nonce": NONCE})
        assert code == 403 and d["error"] == NEUTRAL
        from d1max_contract import device_id as di
        from d1max_contract import ed25519
        dev, alien = ed25519.new_seed(), ed25519.new_seed()   # 别家「出厂钥匙」签的
        cert = di.sign_device(alien, di.make_request(serial="D1M-8", model="x",
                                                     device_pub=ed25519.public_key(dev),
                                                     hw_id="e" * 64), now_ms=1)
        ch = s.req("GET", "/api/attest/challenge")[1]
        fake = {"cert": cert, "hw_id": "e" * 64, "nonce": ch["nonce"], "sig": di.sign_attest(
            dev, di.attest_message(purpose="bind", site_id=ch["site_id"], nonce=ch["nonce"],
                                   serial="D1M-8", hw_id="e" * 64))}
        code, d = s.req("POST", "/api/bind/request", {"serial": "D1M-8", "nonce": NONCE,
                                                      "attest": fake})
        assert code == 403 and d["error"] == NEUTRAL
        assert s.req("GET", "/api/admin/bind", token=tok)[1]["requests"] == []
        assert not any(a.get("kind") == "bind_request"
                       for a in s.req("GET", "/api/alerts", token=tok)[1]["alerts"])
        # 外审 W63-1 第 2 条:被拒的不进站点的操作日志(管理员在网页上看得到;也不受一分钟一条的限)
        assert not [r for r in s.db.query("SELECT action, status FROM audit")
                    if "bind/request" in r["action"] and r["status"] == 403]
        rows = s.db.query("SELECT kind, serial, detail FROM company_reports")
        assert [r["serial"] for r in rows] == ["D1M-7"], "同一来源一分钟只记一条"
        assert rows[0]["kind"] == "device_rejected" and "出厂证书" in rows[0]["detail"]
        del fac
    finally:
        s.close()


def test_解绑后重启broker_查到进程号就发SIGTERM_查不到算没做到():
    import signal as sg
    import subprocess as sp
    killed = []

    def 跑(out):
        return lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=out, stderr="")
    assert site_main.restart_broker(run=跑("4321\n"), kill=lambda p, s: killed.append((p, s)))
    assert killed == [(4321, sg.SIGTERM)]
    assert not site_main.restart_broker(run=跑("0\n"), kill=lambda p, s: killed.append(p))
    assert not site_main.restart_broker(run=跑(""), kill=lambda p, s: killed.append(p))

    def 没有systemctl(cmd, **kw):
        raise FileNotFoundError("systemctl")
    assert not site_main.restart_broker(run=没有systemctl)
    assert len(killed) == 1


def test_端到端_换WiFi站点确认_解绑先叫机器人清机(tmp_path, 台):
    """站点 + 真代理 + 真配网服务状态机(假路由器)经本机套接字:WiFi 改动进遥测、报告警、站点确认后
    机器人才换;站点解绑先发 unbind,机器人证书对得上就开始清机,然后吊销。"""
    import base64
    import threading
    import time as _t

    from test_site_api import PW
    from test_site_api import 站 as 起站

    from d1max_agent.bindlink import BindLink
    from d1max_contract import binding as cb
    from d1max_patrol.bind import server as sv
    from d1max_patrol.bind.core import BindCore, cert_fp
    from d1max_patrol.bind.paths import BindPaths, write_json
    from d1max_patrol.bind.router import FakeRouter

    _, home, *_ = 台
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 0
    code = cb.new_code()
    paths = BindPaths(etc=tmp_path / "etc", data_root=tmp_path / "data")
    write_json(paths.device, {"serial": "A", "code_hash": cb.code_hash(code)})
    pem = (b"-----BEGIN CERTIFICATE-----\n" + base64.b64encode(b"der-A") +
           b"\n-----END CERTIFICATE-----\n")
    paths.robot_cert.parent.mkdir(parents=True)
    paths.robot_cert.write_bytes(pem)
    now = int(_t.time() * 1000)
    write_json(paths.state, {"state": "bound", "wifi": {"ssid": "Estate", "psk": "12345678"},
                             "site": {"url": "https://x", "fp": "ab" * 32}, "site_ok_ms": now})
    router = FakeRouter()
    wiped = []
    core = BindCore(paths, router=router, site=None, provision=str,
                    wipe=lambda: wiped.append(1) or [], start_services=lambda: None,
                    now_ms=lambda: int(_t.time() * 1000))
    sock = tmp_path / "bind.sock"
    unix = sv.make_unix(core, sock)
    threading.Thread(target=unix.serve_forever, daemon=True).start()
    s = 起站(tmp_path / "api", alerts=True, bind=BindLink(sock))
    try:
        s.api.home = home
        with s.db.tx() as c:                       # 站点登记的这台的证书 = 机器人上这张
            c.execute("UPDATE robots SET fingerprint=? WHERE robot_id='A'", (cert_fp(pem),))
        tok = s.login()
        ch = core.wifi(code, {"ssid": "New", "psk": "abcdefgh"})["change"]
        assert ch["mode"] == "online"

        def 等(fn, t=10.0):
            end = _t.time() + t
            while _t.time() < end:
                v = fn()
                if v:
                    return v
                _t.sleep(0.1)
            raise AssertionError("等不到")
        v = 等(lambda: (s.req("GET", "/api/robots/A", token=tok)[1].get("wifi") or {})
              .get("change"))
        assert v["id"] == ch["id"] and v["ssid"] == "New"
        ov = s.req("GET", "/api/robots/overview", token=tok)[1]["robots"]
        assert [r["wifi"]["change"]["id"] for r in ov if r["robot_id"] == "A"] == [ch["id"]]
        a = 等(lambda: [x for x in s.desk.book.open() if x.kind == "wifi_change"])
        assert "New" in a[0].detail
        assert router.ssid == "", "没确认,路由器没动"
        code_, d = s.req("POST", "/api/robots/A/wifi", {"id": ch["id"], "approve": True},
                         token=tok)
        assert code_ == 200 and d["ack"]["result"] == "accepted", d
        assert router.ssid == "New"
        assert s.req("POST", "/api/robots/A/wifi", {"id": "zz", "approve": True},
                     token=tok)[0] == 400
        code_, d = s.req("POST", "/api/admin/robots/A/revoke", {"confirm_password": PW},
                         token=tok)
        assert code_ == 200 and d["robot_wipe"] == "sent", d
        assert core.s["state"] == "wiping"
        assert d["broker_restart"]                 # 测试台没接重启:叫人去重启
    finally:
        s.close()
        unix.shutdown()


def test_外审2A_批准后被解绑重新登记_旧申请出码那一刻也领不到新一代(台):
    """检查和出码之间有人解绑、重新登记(并发交错):出码时在同一个事务里再核一次批准时那一代。"""
    desk, home, db, *_ = 台
    r = _挂(desk)
    desk.approve(r["request_id"], by="owner")
    real = desk._issue

    def 交错(serial, host, fp):
        site_main.cmd_revoke(home, serial)                 # 刚核完那一代,另一边解绑、重新登记
        site_main.cmd_enroll(home, serial, 365)
        return real(serial, host, fp)
    desk._issue = 交错
    with pytest.raises(bd.BindRefused):
        desk.poll(r["request_id"], r["poll_secret"])


def test_外审2B_老的绑定申请表没有cert_fp列_升上来补上(tmp_path):
    import sqlite3

    from d1max_site.db import SiteDB
    p = tmp_path / "s.db"
    SiteDB(p).close()
    con = sqlite3.connect(p)
    con.execute("DROP TABLE bind_requests")
    con.execute("CREATE TABLE bind_requests (id TEXT PRIMARY KEY, serial TEXT NOT NULL, "
                "nonce TEXT NOT NULL, secret_hash TEXT NOT NULL, created_ms INTEGER NOT NULL, "
                "expires_ms INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
                "decided_by TEXT NOT NULL DEFAULT '', decided_ms INTEGER, model TEXT NOT NULL "
                "DEFAULT '', version TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
                "host TEXT NOT NULL DEFAULT '')")
    con.commit()
    con.close()
    db = SiteDB(p)
    cols = {r[1] for r in db.query("PRAGMA table_info(bind_requests)")}
    db.close()
    assert "cert_fp" in cols
