"""站点健康与诊断(商业化 A1):体检各项、探活不用登录只回好不好、体检要管理员、证书快到期/盘紧/
后台道卡住报 P2 好了自动解决(报失败下一拍再来)、指标历史、诊断包不带密钥和令牌。"""

from __future__ import annotations

import io
import json
import os
import tarfile
from types import SimpleNamespace

import pytest
from test_site_api import PW, 站

from d1max_site import main as site_main
from d1max_site.alert_store import AlertDesk
from d1max_site.db import SiteDB
from d1max_site.health import HealthDesk, redact_cfg, redact_text, support_bundle

NOW = 1_800_000_000_000
DAY = 86_400_000


def _盘(free):
    return lambda p: SimpleNamespace(total=100, used=100 - free, free=free)


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    home = tmp_path / "site"
    (home / "ca" / "server").mkdir(parents=True)
    (home / "ca" / "server" / "server.crt").write_text("x")
    (home / "ca" / "issued" / "A").mkdir(parents=True)
    (home / "ca" / "issued" / "A" / "robot.crt").write_text("x")
    clock, ends, free, lag, lanes = [NOW], {"server.crt": NOW + 400 * DAY,
                                            "robot.crt": NOW + 400 * DAY}, [50], [0.2], {}
    h = HealthDesk(db, home=home, now_ms=lambda: clock[0], disk_usage=lambda p: _盘(free[0])(p),
                   cert_end=lambda p: ends[p.name], loop_lag=lambda: lag[0],
                   lanes=lambda: dict(lanes), monotonic=lambda: 1000.0)
    desk = AlertDesk(db, now_ms=lambda: clock[0])
    h.alerts = desk
    yield SimpleNamespace(h=h, db=db, home=home, clock=clock, ends=ends, free=free, lag=lag,
                          lanes=lanes, desk=desk)
    db.close()


def test_体检_都好_各项说人话(台):
    t = 台
    c = t.h.checks()
    assert all(c[k]["ok"] for k in ("db", "loop", "disk", "certs", "lanes")), c
    assert c["backup"]["ok"] is False and "没配备份" in c["backup"]["detail"]
    assert "400 天" in c["certs"]["detail"]
    assert t.h.healthz() == (True, {"ok": True, "version": t.h.healthz()[1]["version"]})


def test_体检_盘紧_事件循环卡住_后台道卡住_证书快到期_都查得出(台):
    t = 台
    t.free[0] = 5
    t.lag[0] = 9.0
    t.lanes["推送"] = 1000.0 - 400
    t.ends["robot.crt"] = NOW + 10 * DAY
    c = t.h.checks()
    assert not c["disk"]["ok"] and "5%" in c["disk"]["detail"]
    assert not c["loop"]["ok"] and "卡住" in c["loop"]["detail"]
    assert not c["lanes"]["ok"] and "推送" in c["lanes"]["detail"]
    assert not c["certs"]["ok"] and "狗 A 的证书还有 10 天到期" in c["certs"]["detail"]
    ok, body = t.h.healthz()
    assert not ok and set(body) == {"ok", "version"}, "探活不给细节"


def test_吊销了的狗的证书不算(台):
    t = 台
    with t.db.tx() as c:
        c.execute("INSERT INTO robots(robot_id, fingerprint, issued_at, expires_at, revoked, "
                  "enrolled_at) VALUES ('A','f',0,0,1,0)")
    t.ends["robot.crt"] = NOW - DAY
    assert t.h.checks()["certs"]["ok"]


def test_自己盯着_证书快到期盘紧卡住报P2_好了自动解决_报失败下一拍再来(台):
    t = 台
    t.ends["server.crt"] = NOW + 5 * DAY
    real = t.desk.raise_alert
    t.desk.raise_alert = lambda **kw: (_ for _ in ()).throw(OSError("库锁住了"))
    t.h.tick()
    assert not t.desk.book.open()
    t.desk.raise_alert = real
    t.clock[0] += 60_000
    t.h.tick()
    assert [a.kind for a in t.desk.book.open()] == ["cert_expiring"], "报失败了下一拍补上"
    t.h.tick()
    assert len(t.desk.book.open()) == 1, "一小时对一次账"
    t.ends["server.crt"] = NOW + 400 * DAY
    t.free[0] = 3
    t.clock[0] += 3600_000
    t.h.tick()
    assert [a.kind for a in t.desk.book.open()] == ["site_disk_low"]
    assert all(a.level.name == "P2" for a in t.desk.book.open())


def test_指标历史_每5分钟一笔_留30天(台):
    t = 台
    t.h.tick()
    t.h.tick()
    keys = {r["key"] for r in t.h.metrics()}
    assert {"site_disk_free", "loop_lag_s", "alerts_open_P1"} <= keys
    n = len(t.h.metrics())
    t.clock[0] += 31 * DAY
    t.h.tick()
    assert len(t.h.metrics()) < n + len(keys) and min(r["ts"] for r in t.h.metrics()) \
        > NOW, "30 天前的删了"
    assert all(r["key"] == "site_disk_free" for r in t.h.metrics(key="site_disk_free"))


def test_真证书_openssl读得出到期时间(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "e",
                           "--hostname", "localhost"]) == 0
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 0
    db = SiteDB(home / "site.db")
    try:
        import time
        c = HealthDesk(db, home=home, now_ms=lambda: int(time.time() * 1000)).checks()
        assert c["certs"]["ok"] and c["certs"]["value"] > 300, c["certs"]
    finally:
        db.close()


def test_诊断包_不带密钥令牌库文件_日志打码(台, tmp_path):
    t = 台
    (t.home / "site.json").write_text(json.dumps({
        "site_id": "e", "push": {"app_key": "AK-123", "secret_file": "/etc/x"},
        "backup_key": "/etc/d1max-site/backup.key", "camera_password": "pw!"}))
    t.desk.raise_alert(kind="dog_sees_person", robot="A", title="狗看见人了")
    hexs = "ab" * 32

    def 日志(unit, hours):
        return f"登录 Bearer tok-SECRET-123 指纹 {hexs}\n"
    out = support_bundle(tmp_path / "b.tar.gz", home=t.home, db=t.db, health=t.h, now_ms=NOW,
                         journal=日志)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        files = {n.split("/", 1)[1]: tar.extractfile(n).read() for n in names}
    assert {"README.txt", "versions.json", "health.json", "site.json", "db_counts.json",
            "alerts_7d.json", "metrics.csv", "logs/d1max-site.log"} <= set(files)
    blob = b"".join(files.values())
    for bad in (b"AK-123", b"pw!", b"tok-SECRET-123", hexs.encode()):
        assert bad not in blob, bad
    cfg = json.loads(files["site.json"])
    assert cfg["push"]["app_key"] == "***" and cfg["push"]["secret_file"] == "/etc/x"
    assert not any(n.endswith((".db", ".key")) for n in names)
    assert "狗看见人了" in files["alerts_7d.json"].decode()


def test_打码规矩():
    assert redact_cfg({"a_key": "x", "key_file": "/p", "token": "", "n": 1}) == \
        {"a_key": "***", "key_file": "/p", "token": "", "n": 1}
    assert redact_text("Bearer abc.def ok") == "Bearer *** ok"


def test_接口_探活不用登录_体检和诊断包要管理员_指标能看就能看(tmp_path):
    s = 站(tmp_path, alerts=True)
    try:
        s.accounts.add("gina", PW, role="guard")
        free = [50]
        s.api.health = HealthDesk(s.db, home=tmp_path, now_ms=lambda: NOW,
                                  disk_usage=lambda p: _盘(free[0])(p),
                                  cert_end=lambda p: NOW + 400 * DAY)
        code, d = s.req("GET", "/healthz")
        assert code == 200 and set(d) == {"ok", "version"} and d["ok"]
        free[0] = 2
        assert s.req("GET", "/healthz")[0] == 503
        admin = s.login()
        code, gina = s.req("POST", "/api/login", {"name": "gina", "password": PW})
        g = gina["token"]
        assert s.req("GET", "/api/health", token=g)[0] == 403
        code, v = s.req("GET", "/api/health", token=admin)
        assert code == 200 and v["checks"]["disk"]["ok"] is False
        assert s.req("GET", "/api/metrics", token=g)[0] == 200
        import urllib.request
        r = urllib.request.Request(s.api.url + "/api/support-bundle",
                                   headers={"Authorization": f"Bearer {admin}"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            assert any(n.endswith("health.json") for n in tar.getnames())
        assert s.req("GET", "/api/support-bundle", token=g)[0] == 403
    finally:
        s.close()


def test_命令行导出诊断包(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    home = tmp_path / "site"
    site_main.main(["--home", str(home), "init", "--site-id", "e", "--hostname", "localhost"])
    out = tmp_path / "x.tar.gz"
    assert site_main.main(["--home", str(home), "support-bundle", "--out", str(out)]) == 0
    with tarfile.open(out) as tar:
        assert any(n.endswith("site.json") for n in tar.getnames())
