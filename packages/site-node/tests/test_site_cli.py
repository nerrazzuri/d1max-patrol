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


def test_只给局域网名字init_站点也连得上本机broker(tmp_path):
    """serve 默认连 mqtts://127.0.0.1:<端口>;站点证书不带 127.0.0.1 的话主机名校验不过,
    d1max-site.service 会一直重启。"""
    import asyncio
    import subprocess
    import time

    from conftest import find_mosquitto

    from d1max_contract.paho_transport import PahoTransport
    exe = find_mosquitto()
    if exe is None:
        pytest.skip("没有 mosquitto")
    home = tmp_path / "s"
    port = free_port()
    assert site_main.main(["--home", str(home), "init", "--site-id", "estate-1",
                           "--hostname", "site.local", "--broker-port", str(port)]) == 0
    p = subprocess.Popen([exe, "-c", str(home / "broker" / "mosquitto.conf")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        srv = home / "ca" / "server"

        async def go():
            t = PahoTransport(f"mqtts://127.0.0.1:{port}", "site:estate-1",
                              tls_ca=str(home / "ca" / "ca.crt"),
                              tls_cert=str(srv / "server.crt"), tls_key=str(srv / "server.key"))
            await t.connect()
            await t.close()

        asyncio.run(go())
    finally:
        p.terminate()
        p.wait(5)


def test_注册表里没有但CA里有效_revoke照样吊销证书(home):
    """enroll 签完证书、写注册表那一步失败,会留下一张有效证书而注册表没行:
    重签被 CA 拒、吊销被注册表拒,命令行上就修不了了。"""
    from d1max_site.ca import SiteCA
    SiteCA(home / "ca").issue_robot("Z", days=5, now_ms=1)          # 只签不登记
    assert site_main.main(["--home", str(home), "revoke", "Z"]) == 0
    assert site_main.main(["--home", str(home), "enroll", "Z"]) == 0


def test_不是站点目录的属主就拒绝跑(home, monkeypatch, capsys):
    """root 跑命令行会把 CRL 重写成 root 的 0600,d1max-mosquitto(以 d1max-site 跑)重启后读不了。"""
    monkeypatch.setattr(site_main.os, "geteuid", lambda: home.stat().st_uid + 12345)
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 2
    assert "属主" in capsys.readouterr().err


def test_init到一半失败_再init说清楚怎么办(tmp_path, capsys):
    h = tmp_path / "half"
    assert site_main.main(["--home", str(h), "init", "--site-id", "estate-1",
                           "--hostname", "bad,host"]) == 2
    assert site_main.main(["--home", str(h), "init", "--site-id", "estate-1",
                           "--hostname", "ok.local"]) == 2
    err = capsys.readouterr().err
    assert "上一次 init 没做完" in err
