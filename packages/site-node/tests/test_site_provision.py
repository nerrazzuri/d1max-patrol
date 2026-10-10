"""狗的开通码(商业化 A4):站点出一次性开通码、狗钉住站点证书指纹领证书包、放好文件、填好 env、出厂
自检。端到端用真的站点证书起一个 HTTPS 服务。"""

from __future__ import annotations

import json
import os
import ssl
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from d1max_patrol import provision as dog
from d1max_site import main as site_main
from d1max_site import provision as site
from d1max_site.db import SiteDB
from d1max_site.registry import Registry

NOW = 1_800_000_000_000


@pytest.fixture
def 站(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    monkeypatch.setattr(site_main, "wall_ms", lambda: NOW)
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "e",
                           "--hostname", "localhost"]) == 0
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 0
    return home


def _码(home, rid="A", hours=24.0):
    return site_main.cmd_enroll_code(home, rid, "localhost", hours)


def _领(home, rid, token, now=NOW):
    db = SiteDB(home / "site.db")
    try:
        reg = Registry(db, site_id="e")
        return site.claim(db, home, rid, token, now_ms=now,
                          revoked=lambda r: reg.get(r) is None or reg.get(r).revoked)
    finally:
        db.close()


def test_开通码_一次性_对不上一律同一句_过期作废_重出旧的作废_吊销了不给(站):
    home = 站
    c = site.decode(_码(home))
    assert c["id"] == "A" and c["mqtt"] == "mqtts://localhost:8883" and len(c["fp"]) == 64
    with pytest.raises(site.ProvisionError, match="不对、过期了或者已经用过了"):
        _领(home, "A", "猜的")
    files = _领(home, "A", c["t"])
    assert set(files) == {"ca.crt", "robot.crt", "robot.key", "registration.json"}
    with pytest.raises(site.ProvisionError):
        _领(home, "A", c["t"]), "用过就作废"
    c2 = site.decode(_码(home, hours=1))
    with pytest.raises(site.ProvisionError):
        _领(home, "A", c2["t"], now=NOW + 2 * 3600_000), "过期"
    old = site.decode(_码(home))
    new = site.decode(_码(home))
    with pytest.raises(site.ProvisionError):
        _领(home, "A", old["t"]), "重出一个,旧的作废"
    site_main.main(["--home", str(home), "revoke", "A"])
    with pytest.raises(site.ProvisionError):
        _领(home, "A", new["t"]), "吊销了不给"


def test_命令行_enroll带code打出开通码_没登记的狗不给码(站, capsys, tmp_path):
    home = 站
    assert site_main.main(["--home", str(home), "enroll", "B", "--code"]) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.startswith("D1MAX1."))
    assert site.decode(line)["id"] == "B"
    assert site_main.main(["--home", str(home), "enroll-code", "Z"]) != 0


class _站点:
    """真的站点服务证书起的 HTTPS 服务,只有领证书包这一个口。"""

    def __init__(self, home: Path, cert: Path | None = None, key: Path | None = None):
        self.hits = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.hits.append(d)
                try:
                    body, code = {"files": _领(home, d["robot_id"], d["token"])}, 200
                except site.ProvisionError as exc:
                    body, code = {"error": str(exc)}, 403
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert or home / "ca/server/server.crt",
                            key or home / "ca/server/server.key")
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def code(self, text):
        d = dog.decode(text)
        d["api"] = f"https://127.0.0.1:{self.port}"
        return site.encode(d)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_端到端_狗钉住指纹领证书包_放好文件_私钥0600_env只改站点那一行(站, tmp_path):
    home = 站
    srv = _站点(home)
    try:
        etc = tmp_path / "etc-d1max"
        etc.mkdir()
        (etc / "env").write_text("# 注释\nD1MAX_SITE_MQTT=\nD1MAX_MAP=m:1\nD1MAX_HAL=sim\n")
        rid = dog.provision(srv.code(_码(home)), etc=etc, user=None)
        assert rid == "A"
        assert stat.S_IMODE((etc / "tls/robot.key").stat().st_mode) == 0o600
        assert (etc / "tls/robot.crt").read_bytes() == (home / "ca/issued/A/robot.crt").read_bytes()
        assert json.loads((etc / "registration.json").read_text())["robot_id"] == "A"
        env = (etc / "env").read_text()
        assert "D1MAX_SITE_MQTT=mqtts://localhost:8883" in env
        assert "# 注释" in env and "D1MAX_MAP=m:1" in env and "D1MAX_HAL=sim" in env
        with pytest.raises(dog.ProvisionError, match="不对、过期了或者已经用过了"):
            dog.provision(srv.code(site.encode(dict(dog.decode(_码(home)), t="错的"))),
                          etc=etc, user=None)
    finally:
        srv.close()


def test_站点证书指纹对不上_令牌一个字节都不发(站, tmp_path):
    home = 站
    other = tmp_path / "other"
    site_main.main(["--home", str(other), "init", "--site-id", "x", "--hostname", "localhost"])
    srv = _站点(home, cert=other / "ca/server/server.crt", key=other / "ca/server/server.key")
    try:
        with pytest.raises(dog.ProvisionError, match="指纹对不上"):
            dog.provision(srv.code(_码(home)), etc=tmp_path / "etc", user=None)
        assert srv.hits == [], "冒充的站点拿不到令牌"
    finally:
        srv.close()


def test_已经开通成别的编号_不换_force才换(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "registration.json").write_text(json.dumps({"robot_id": "A"}))
    code = site.encode({"api": "https://h:1", "fp": "ab", "mqtt": "mqtts://h:8883", "id": "B",
                        "t": "x"})
    with pytest.raises(dog.ProvisionError, match="已经开通成 A"):
        dog.provision(code, etc=etc, user=None, claimer=lambda c: {})
    files = {n: b"x" for n in dog.REQUIRED}
    assert dog.provision(code, etc=etc, user=None, force=True, claimer=lambda c: files) == "B"


def test_出厂自检_缺什么说什么(tmp_path):
    etc = tmp_path / "etc"
    rows = {c.name: c for c in dog.selfcheck(etc=etc, user=None, run=lambda c: (1, "inactive"))}
    assert rows["注册文件"].level == "FAIL" and rows["env D1MAX_SITE_MQTT"].level == "FAIL"
    (etc / "tls").mkdir(parents=True)
    for n in ("ca.crt", "robot.crt", "robot.key"):
        (etc / "tls" / n).write_text("x")
    (etc / "tls/robot.key").chmod(0o644)
    (etc / "registration.json").write_text(json.dumps({"robot_id": "A"}))
    (etc / "env").write_text("D1MAX_SITE_MQTT=mqtts://s:8883\nD1MAX_MAP=m:1\nD1MAX_HOME=0,0,0\n")
    (etc / "release-pub.pem").write_text("x")

    def 跑(cmd):
        return (0, "yes") if cmd[0] == "timedatectl" else (0, "active")
    rows = {c.name: c for c in dog.selfcheck(etc=etc, user=None, run=跑,
                                              mqtt_probe=lambda u, e: "")}
    assert rows["私钥权限"].level == "FAIL"
    (etc / "tls/robot.key").chmod(0o600)
    rows = {c.name: c for c in dog.selfcheck(etc=etc, user=None, run=跑,
                                              mqtt_probe=lambda u, e: "")}
    assert all(c.level == "PASS" for k, c in rows.items() if k != "证据公钥"), rows
    assert rows["证据公钥"].level == "WARN"
    rows = {c.name: c for c in dog.selfcheck(etc=etc, user=None, run=跑,
                                              mqtt_probe=lambda u, e: "握手失败")}
    assert rows["连站点"].level == "FAIL" and "握手失败" in rows["连站点"].detail


def test_接口_领证书包不用登录_用过一次403(tmp_path, 站):
    import json as _j

    from test_site_api import 站 as 台
    home = 站
    s = 台(tmp_path / "api", alerts=True)
    try:
        s.api.home = home
        if s.reg.get("A") is None:
            s.reg.enroll("A", fingerprint="f", issued_at=0, expires_at=NOW * 2, now_ms=0)
        cfg = _j.loads((home / "site.json").read_text())
        c = site.decode(site.issue_code(s.db, cfg, home, "A", fingerprint="ab", now_ms=NOW))
        code, d = s.req("POST", "/api/enroll/claim", {"robot_id": "A", "token": c["t"]})
        assert code == 200 and "robot.key" in d["files"]
        assert s.req("POST", "/api/enroll/claim", {"robot_id": "A", "token": c["t"]})[0] == 403
        assert s.req("POST", "/api/enroll/claim", {"robot_id": "A", "token": "猜的"})[0] == 403
    finally:
        s.close()
