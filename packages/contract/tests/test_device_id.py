"""W63 设备身份:出厂证书(公司出厂钥匙签「序列号 + 设备公钥 + 芯片编号」)、答题签名。"""

from __future__ import annotations

import pytest

from d1max_contract import device_id as di
from d1max_contract import ed25519

NOW = 1_800_000_000_000


@pytest.fixture(scope="module")
def keys():
    factory, device, other = ed25519.new_seed(), ed25519.new_seed(), ed25519.new_seed()
    return factory, device, other


HW = "a" * 64


def _req(device, serial="D1M-0001", hw=HW):
    return di.make_request(serial=serial, model="d1max-v2", device_pub=ed25519.public_key(device),
                           hw_id=hw)


def test_出厂签了_验得过_内容一样(keys):
    factory, device, _ = keys
    cert = di.sign_device(factory, _req(device), now_ms=NOW)
    got = di.verify_cert(cert, [ed25519.public_key(factory)])
    assert got.serial == "D1M-0001" and got.hw_id == "a" * 64
    assert got.device_pub == ed25519.public_key(device)
    assert got.key_id == di.key_id(ed25519.public_key(factory))


@pytest.mark.parametrize("field, value", [("serial", "D1M-0002"), ("hw_id", "b" * 64),
                                          ("model", "fake"), ("issued_ms", NOW + 1)])
def test_改了任何一项_验不过(keys, field, value):
    factory, device, _ = keys
    cert = dict(di.sign_device(factory, _req(device), now_ms=NOW))
    cert[field] = value
    with pytest.raises(di.DeviceIdError):
        di.verify_cert(cert, [ed25519.public_key(factory)])


def test_换了设备公钥_验不过(keys):
    factory, device, other = keys
    cert = dict(di.sign_device(factory, _req(device), now_ms=NOW))
    cert["device_pub"] = di._b64(ed25519.public_key(other))
    with pytest.raises(di.DeviceIdError):
        di.verify_cert(cert, [ed25519.public_key(factory)])


def test_不是我们的出厂钥匙签的_验不过_作废了的也不认(keys):
    factory, device, other = keys
    fake = di.sign_device(other, _req(device), now_ms=NOW)
    with pytest.raises(di.DeviceIdError, match="出厂钥匙"):
        di.verify_cert(fake, [ed25519.public_key(factory)])
    good = di.sign_device(factory, _req(device), now_ms=NOW)
    with pytest.raises(di.DeviceIdError, match="作废"):
        di.verify_cert(good, [ed25519.public_key(factory)],
                       revoked={di.key_id(ed25519.public_key(factory))})


def test_证书不成形_都是同一种错(keys):
    factory, _, _ = keys
    for bad in (None, "x", {}, {"v": 1}, {"v": 1, "serial": "D1M-1", "sig": "!!"}):
        with pytest.raises(di.DeviceIdError):
            di.verify_cert(bad, [ed25519.public_key(factory)])


def test_申请里的芯片编号_要是64位十六进制_序列号要合规矩(keys):
    _, device, _ = keys
    with pytest.raises(di.DeviceIdError):
        _req(device, hw="xyz")
    with pytest.raises(di.DeviceIdError):
        _req(device, serial="bad serial")


def test_答题_只认这道题这个用途(keys):
    factory, device, other = keys
    cert = di.sign_device(factory, _req(device), now_ms=NOW)
    msg = di.attest_message(purpose="claim", site_id="s1", nonce="n" * 32, serial="D1M-0001",
                            hw_id=HW)
    sig = di.sign_attest(device, msg)
    di.verify_attest(cert, msg, sig)                           # 不抛
    kw = {"serial": "D1M-0001", "hw_id": HW}
    for m in (di.attest_message(purpose="bind", site_id="s1", nonce="n" * 32, **kw),
              di.attest_message(purpose="claim", site_id="s2", nonce="n" * 32, **kw),
              di.attest_message(purpose="claim", site_id="s1", nonce="m" * 32, **kw)):
        with pytest.raises(di.DeviceIdError):
            di.verify_attest(cert, m, sig)
    with pytest.raises(di.DeviceIdError):
        di.verify_attest(cert, msg, di.sign_attest(other, msg))   # 不是这台的私钥
    with pytest.raises(di.DeviceIdError):                          # 现场芯片编号也签在里面
        di.verify_attest(cert, di.attest_message(purpose="claim", site_id="s1", nonce="n" * 32,
                                                 serial="D1M-0001", hw_id="f" * 64), sig)
