"""网页上登记 / 出开通码 / 吊销机器狗(商业化 B1b):只有管理员;跟命令行同一套(证书、注册表、CRL)。"""

from __future__ import annotations

import os

import pytest
from test_site_api import PW, 站

from d1max_site import main as site_main
from d1max_site import provision


@pytest.fixture
def 台(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "e",
                           "--hostname", "localhost"]) == 0
    s = 站(tmp_path / "api", alerts=True)
    s.api.home = home
    s.accounts.add("gina", PW, role="guard")
    yield s, home
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200
    return d["token"]


def test_管理员登记新狗_拿到开通码_证书包在_只许手动派(台):
    s, home = 台
    tok = s.login()
    code, d = s.req("POST", "/api/admin/robots", {"robot_id": "D1-07"}, token=tok)
    assert code == 200, d
    assert provision.decode(d["code"])["id"] == "D1-07" and d["manual_only"] is True
    assert (home / "ca/issued/D1-07/robot.key").is_file()
    code, again = s.req("POST", "/api/admin/robots", {"robot_id": "D1-07"}, token=tok)
    assert code == 409 and "已登记" in again["error"]
    code, d2 = s.req("POST", "/api/admin/robots/D1-07/code", {"hours": 2}, token=tok)
    assert code == 200
    assert provision.decode(d2["code"])["t"] != provision.decode(d["code"])["t"], "重出的是新码"


def test_重出开通码_旧码作废_吊销以后码领不到_也不能再出码(台):
    s, home = 台
    tok = s.login()
    first = s.req("POST", "/api/admin/robots", {"robot_id": "D1-08"}, token=tok)[1]["code"]
    second = s.req("POST", "/api/admin/robots/D1-08/code", {}, token=tok)[1]["code"]
    from d1max_site.db import SiteDB
    db = SiteDB(home / "site.db")
    try:
        with pytest.raises(provision.ProvisionError):
            provision.claim(db, home, "D1-08", provision.decode(first)["t"],
                            now_ms=site_main.wall_ms())
    finally:
        db.close()
    code, d = s.req("POST", "/api/admin/robots/D1-08/revoke", {}, token=tok)
    assert code == 200 and "CRL" in d["summary"] and "mosquitto" in d["broker_restart"]
    db = SiteDB(home / "site.db")
    try:
        with pytest.raises(provision.ProvisionError):
            provision.claim(db, home, "D1-08", provision.decode(second)["t"],
                            now_ms=site_main.wall_ms())
    finally:
        db.close()
    assert s.req("POST", "/api/admin/robots/D1-08/code", {}, token=tok)[0] == 409
    assert s.req("POST", "/api/admin/robots/nope/revoke", {}, token=tok)[0] == 409


def test_只有管理员_坏参数400(台):
    s, _ = 台
    g = _登(s, "gina")
    assert s.req("POST", "/api/admin/robots", {"robot_id": "X"}, token=g)[0] == 403
    assert s.req("POST", "/api/admin/robots/X/revoke", {}, token=g)[0] == 403
    tok = s.login()
    assert s.req("POST", "/api/admin/robots", {"robot_id": "../x"}, token=tok)[0] == 400
    assert s.req("POST", "/api/admin/robots", {"robot_id": "Y", "days": 0}, token=tok)[0] == 400
    assert s.req("GET", "/api/admin/robots", token=tok)[0] == 405
