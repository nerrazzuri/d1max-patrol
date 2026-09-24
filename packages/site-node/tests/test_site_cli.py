"""``d1max-site`` 命令行(不需要 Mosquitto 的那部分)。"""

from __future__ import annotations

import json
import stat

import pytest
from conftest import free_port

from d1max_contract.registration import Registration
from d1max_site import main as site_main
from d1max_site.db import SiteDB
from d1max_site.registry import Registry


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "site"
    assert site_main.main(["--home", str(h), "init", "--site-id", "estate-1",
                           "--hostname", "localhost", "--broker-port", "18883"]) == 0
    return h


def test_init产物齐全_再init拒绝(home, capsys):
    cfg = json.loads((home / "site.json").read_text())
    assert cfg["site_id"] == "estate-1" and cfg["broker_port"] == 18883
    conf = (home / "broker" / "mosquitto.conf").read_text()
    assert "listener 18883" in conf and "require_certificate true" in conf
    assert "pattern read site/estate-1/robot/%u/cmd" in (home / "broker" / "acl").read_text()
    assert stat.S_IMODE((home / "ca" / "server" / "server.key").stat().st_mode) == 0o600
    assert site_main.main(["--home", str(home), "init", "--site-id", "x",
                           "--hostname", "h"]) == 2
    assert "init 过了" in capsys.readouterr().err


def test_enroll出证书包并登记_重复拒绝_吊销后可重签(home, capsys):
    assert site_main.main(["--home", str(home), "enroll", "A", "--days", "10"]) == 0
    d = home / "ca" / "issued" / "A"
    reg = Registration.load(d / "registration.json")
    db = SiteDB(home / "site.db")
    rec = Registry(db, site_id="estate-1").get("A")
    assert rec.fingerprint == reg.credential_fingerprint and not rec.revoked
    db.close()
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 2
    assert site_main.main(["--home", str(home), "revoke", "A"]) == 0
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 0
    db = SiteDB(home / "site.db")
    rec2 = Registry(db, site_id="estate-1").get("A")
    assert not rec2.revoked and rec2.fingerprint != rec.fingerprint
    db.close()


def test_没init的目录_各子命令都说清楚(tmp_path, capsys):
    for args in (["enroll", "A"], ["revoke", "A"], ["serve"]):
        assert site_main.main(["--home", str(tmp_path / "none"), *args]) == 2, args
    assert capsys.readouterr().err.count("还没 init") == 3


def test_add_admin口令太短退2(home, monkeypatch, capsys):
    monkeypatch.setenv("D1MAX_SITE_PASSWORD", "short")
    assert site_main.main(["--home", str(home), "add-admin", "alice"]) == 2
    monkeypatch.setenv("D1MAX_SITE_PASSWORD", "long-enough-pass")
    assert site_main.main(["--home", str(home), "add-admin", "alice"]) == 0


def test_serve连不上broker退1(home):
    port = free_port()
    rc = site_main.cmd_serve(home, "127.0.0.1", free_port(), f"mqtts://127.0.0.1:{port}")
    assert rc == 1


def test_API没start就stop不挂(home):
    import threading

    from d1max_site.api import SiteApi
    db = SiteDB(home / "site.db")
    from d1max_site.accounts import Accounts
    api = SiteApi(host="127.0.0.1", port=0, loop=None, dispatcher=None,
                  accounts=Accounts(db, now_ms=lambda: 0))
    t = threading.Thread(target=api.stop)
    t.start()
    t.join(5)
    assert not t.is_alive()
    db.close()
