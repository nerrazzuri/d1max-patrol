"""W63 站点核设备身份:出题、核答题(出厂证书、设备私钥、芯片编号、序列号没被别的机器占)、
核不过悄悄拒(中性的一句话、不报警)、记给公司。"""

from __future__ import annotations

import json

import pytest

from d1max_contract import device_id as di
from d1max_contract import ed25519
from d1max_site.attest import NEUTRAL, AttestDesk, AttestRefused
from d1max_site.db import SiteDB

HW = "c" * 64


class 钟:
    def __init__(self):
        self.t = 1_800_000_000_000

    def __call__(self):
        return self.t


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    factory = ed25519.new_seed()
    c = 钟()
    desk = AttestDesk(db, site_id="estate-1", now_ms=c,
                      factory_pubs=lambda: [ed25519.public_key(factory)], revoked=lambda: set())
    yield desk, db, factory, c
    db.close()


def _机器人(factory, serial="D1M-0001", hw=HW):
    dev = ed25519.new_seed()
    cert = di.sign_device(factory, di.make_request(serial=serial, model="d1max",
                                                   device_pub=ed25519.public_key(dev), hw_id=hw),
                          now_ms=1)
    return dev, cert


def _答(desk, dev, cert, *, purpose="claim", hw=HW, site="estate-1", nonce=None):
    n = nonce or desk.challenge()["nonce"]
    msg = di.attest_message(purpose=purpose, site_id=site, nonce=n, serial=cert["serial"],
                            hw_id=hw)
    return {"cert": cert, "hw_id": hw, "nonce": n, "sig": di.sign_attest(dev, msg)}


def _记录(db):
    return [dict(r) for r in db.query("SELECT * FROM company_reports ORDER BY id")]


def test_原厂机器人_答对了就过_记下设备公钥(台):
    desk, db, factory, _ = 台
    dev, cert = _机器人(factory)
    got = desk.verify(_答(desk, dev, cert), purpose="claim", serial="D1M-0001", source="10.0.0.5")
    assert got.serial == "D1M-0001"
    assert db.query("SELECT serial FROM device_ids") == [], "核过不等于领到:领到证书以后才记"
    desk.remember(got)
    assert db.query("SELECT serial FROM device_ids")[0]["serial"] == "D1M-0001"
    assert _记录(db) == []


@pytest.mark.parametrize("what", ["没答", "别家出厂钥匙", "别的私钥", "芯片编号不对", "序列号不对",
                                  "用途不对", "别的站点", "题是编的"])
def test_核不过_一律同一句中性的话_记给公司(台, what):
    desk, db, factory, _ = 台
    dev, cert = _机器人(factory)
    ans = _答(desk, dev, cert)
    serial = "D1M-0001"
    if what == "没答":
        ans = None
    elif what == "别家出厂钥匙":
        dev, cert = _机器人(ed25519.new_seed())
        ans = _答(desk, dev, cert)
    elif what == "别的私钥":
        ans = _答(desk, ed25519.new_seed(), cert)
    elif what == "芯片编号不对":
        ans = dict(ans, hw_id="d" * 64)
    elif what == "序列号不对":
        serial = "D1M-0002"
    elif what == "用途不对":
        ans = _答(desk, dev, cert, purpose="bind")
    elif what == "别的站点":
        ans = _答(desk, dev, cert, site="other-site")
    elif what == "题是编的":
        ans = _答(desk, dev, cert, nonce="1." + "a" * 32 + "." + "b" * 64)
    with pytest.raises(AttestRefused) as e:
        desk.verify(ans, purpose="claim", serial=serial, source="10.0.0.9")
    assert str(e.value) == NEUTRAL
    rows = _记录(db)
    assert len(rows) == 1 and rows[0]["kind"] == "device_rejected"
    assert rows[0]["source"] == "10.0.0.9"
    assert rows[0]["serial"] == serial and json.loads(rows[0]["detail"])["why"]


def test_同一道题只能用一次_过期了也不认(台):
    desk, _, factory, c = 台
    dev, cert = _机器人(factory)
    ans = _答(desk, dev, cert)
    desk.verify(ans, purpose="claim", serial="D1M-0001", source="x")
    with pytest.raises(AttestRefused):
        desk.verify(ans, purpose="claim", serial="D1M-0001", source="x")
    old = _答(desk, dev, cert)
    c.t += 6 * 60_000
    with pytest.raises(AttestRefused):
        desk.verify(old, purpose="claim", serial="D1M-0001", source="x")


def test_同一个序列号换了一把设备钥匙_判仿冒(台):
    desk, db, factory, _ = 台
    dev, cert = _机器人(factory)
    desk.remember(desk.verify(_答(desk, dev, cert), purpose="claim", serial="D1M-0001", source="a"))
    dev2, cert2 = _机器人(factory)                       # 同一序列号、另一块板(另一把钥匙)
    with pytest.raises(AttestRefused):
        desk.verify(_答(desk, dev2, cert2), purpose="claim", serial="D1M-0001", source="b")
    assert "设备钥匙" in json.loads(_记录(db)[0]["detail"])["why"]


def test_作废的出厂钥匙签的_不认(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    factory = ed25519.new_seed()
    pub = ed25519.public_key(factory)
    desk = AttestDesk(db, site_id="estate-1", now_ms=钟(), factory_pubs=lambda: [pub],
                      revoked=lambda: {di.key_id(pub)})
    dev, cert = _机器人(factory)
    with pytest.raises(AttestRefused):
        desk.verify(_答(desk, dev, cert), purpose="claim", serial="D1M-0001", source="a")
    db.close()


def test_同一来源一分钟只记一条_防刷满(台):
    desk, db, _, c = 台
    for _ in range(5):
        with pytest.raises(AttestRefused):
            desk.verify(None, purpose="bind", serial="D1M-0001", source="10.0.0.9")
    assert len(_记录(db)) == 1
    c.t += 61_000
    with pytest.raises(AttestRefused):
        desk.verify(None, purpose="bind", serial="D1M-0001", source="10.0.0.9")
    assert len(_记录(db)) == 2


def test_没装出厂公钥的站点_谁都进不来_也记(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    desk = AttestDesk(db, site_id="estate-1", now_ms=钟(), factory_pubs=lambda: [],
                      revoked=lambda: set())
    dev, cert = _机器人(ed25519.new_seed())
    with pytest.raises(AttestRefused):
        desk.verify(_答(desk, dev, cert), purpose="claim", serial="D1M-0001", source="a")
    assert len(_记录(db)) == 1
    db.close()


def test_解绑以后_同一序列号换了板子可以重新加入(台):
    from d1max_site.registry import Registry
    desk, db, factory, _ = 台
    reg = Registry(db, site_id="estate-1")
    reg.enroll("D1M-0001", fingerprint="sha256:" + "a" * 64, issued_at=0, expires_at=1)
    dev, cert = _机器人(factory)
    desk.remember(desk.verify(_答(desk, dev, cert), purpose="claim", serial="D1M-0001", source="a"))
    assert db.query("SELECT serial FROM device_ids")
    reg.revoke("D1M-0001")                               # 换主板:先在站点上解绑
    dev2, cert2 = _机器人(factory, hw="d" * 64)
    assert desk.verify(_答(desk, dev2, cert2, hw="d" * 64), purpose="claim", serial="D1M-0001",
                       source="b").hw_id == "d" * 64


def test_外审W63_3_现场芯片编号签在答题里_中间改了这个字段就不认(台):
    """整盘拷到另一块板:那块板如实读到自己的芯片编号、签进答题;转发的人把 hw_id 字段改成证书里的,
    签名就对不上了。"""
    desk, db, factory, _ = 台
    dev, cert = _机器人(factory)
    real = "d" * 64                                       # 这块板真实的芯片编号(跟证书不一样)
    ans = _答(desk, dev, cert, hw=real)
    forged = dict(ans, hw_id=cert["hw_id"])               # 中间把字段改成证书里的
    with pytest.raises(AttestRefused):
        desk.verify(forged, purpose="claim", serial="D1M-0001", source="10.0.0.9")
