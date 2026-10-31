"""W63 机器人这头的设备身份:设备钥匙、芯片编号、出厂申请、装出厂证书、出厂检验、答题。"""

from __future__ import annotations

import json
import stat

import pytest

from d1max_contract import device_id as di
from d1max_contract import ed25519
from d1max_patrol.bind import devid
from d1max_patrol.bind.paths import BindPaths, write_json

NOW = 1_800_000_000_000


@pytest.fixture
def 板(tmp_path):
    p = BindPaths(etc=tmp_path / "etc")
    p.etc.mkdir()
    write_json(p.device, {"serial": "D1M-0001", "model": "D1 Max", "code_hash": "x",
                          "hotspot_ssid": "s", "fingerprint": "f"})
    dt = tmp_path / "serial-number"
    dt.write_bytes(b"1424C21A0C3A\x00")
    return p, [dt]


def test_芯片编号_读设备树或cpuinfo_全零和读不到都不行(tmp_path):
    dt = tmp_path / "sn"
    dt.write_bytes(b"ABC123\x00")
    h = devid.read_hw_id([dt])
    assert len(h) == 64 and h == devid.read_hw_id([tmp_path / "none", dt])
    cpu = tmp_path / "cpuinfo"
    cpu.write_text("processor\t: 0\nSerial\t\t: 7c2f0a9b11223344\n")
    assert devid.read_hw_id([cpu]) != h
    zero = tmp_path / "z"
    zero.write_text("Serial\t\t: 0000000000000000\n")
    with pytest.raises(devid.DevIdError, match="芯片编号"):
        devid.read_hw_id([zero, tmp_path / "none"])


def test_出厂申请_生成设备钥匙只给root读_不覆盖(板):
    p, hw = 板
    req = devid.make_request(p, hw_sources=hw)
    key = devid.key_path(p)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    seed = ed25519.seed_from_pem(key.read_bytes())
    assert req["serial"] == "D1M-0001" and di._unb64(req["device_pub"]) == ed25519.public_key(seed)
    assert devid.make_request(p, hw_sources=hw)["device_pub"] == req["device_pub"], "不换钥匙"
    assert json.loads(devid.request_path(p).read_text()) == req


def test_装出厂证书_要是这块板的_要是认的出厂钥匙签的(板, tmp_path):
    p, hw = 板
    factory = ed25519.new_seed()
    pubs = [ed25519.public_key(factory)]
    req = devid.make_request(p, hw_sources=hw)
    cert = di.sign_device(factory, req, now_ms=NOW)
    for bad, why in ((dict(cert, serial="D1M-0002"), "序列号"),
                     (di.sign_device(factory, dict(req, hw_id="b" * 64), now_ms=NOW), "芯片编号"),
                     (di.sign_device(factory, dict(req, device_pub=di._b64(
                         ed25519.public_key(ed25519.new_seed()))), now_ms=NOW), "设备钥匙"),
                     (di.sign_device(ed25519.new_seed(), req, now_ms=NOW), "出厂")):
        with pytest.raises(devid.DevIdError, match=why):
            devid.install_cert(p, bad, factory_pubs=pubs, hw_sources=hw)
        assert not devid.cert_path(p).exists()
    devid.install_cert(p, cert, factory_pubs=pubs, hw_sources=hw)
    assert devid.problems(p, factory_pubs=pubs, hw_sources=hw) == []


def test_出厂检验_没证书_换了板子_都报出来(板, tmp_path):
    p, hw = 板
    factory = ed25519.new_seed()
    pubs = [ed25519.public_key(factory)]
    devid.make_request(p, hw_sources=hw)
    assert any("出厂证书" in x for x in devid.problems(p, factory_pubs=pubs, hw_sources=hw))
    devid.install_cert(p, di.sign_device(factory, json.loads(devid.request_path(p).read_text()),
                                         now_ms=NOW), factory_pubs=pubs, hw_sources=hw)
    other = tmp_path / "other-sn"
    other.write_bytes(b"FFFF0000AAAA\x00")
    assert any("芯片编号" in x for x in devid.problems(p, factory_pubs=pubs, hw_sources=[other]))


def test_答题_站点验得过(板):
    p, hw = 板
    factory = ed25519.new_seed()
    pubs = [ed25519.public_key(factory)]
    req = devid.make_request(p, hw_sources=hw)
    devid.install_cert(p, di.sign_device(factory, req, now_ms=NOW), factory_pubs=pubs,
                       hw_sources=hw)
    ans = devid.answer(p, purpose="claim", site_id="s1", nonce="n" * 32, hw_sources=hw)
    cert = di.verify_cert(ans["cert"], pubs)
    di.verify_attest(ans["cert"], di.attest_message(purpose="claim", site_id="s1", nonce="n" * 32,
                                                    serial=cert.serial), ans["sig"])
    assert ans["hw_id"] == cert.hw_id


def test_没烧写过设备身份_答不了题_说清楚(板):
    p, hw = 板
    assert devid.answer(p, purpose="bind", site_id="s1", nonce="n" * 32, hw_sources=hw) is None
