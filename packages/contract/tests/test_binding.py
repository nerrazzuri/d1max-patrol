"""W42 绑定格式:绑定码、贴纸二维码、核对码、解绑令、WiFi。"""

import pytest

from d1max_contract import binding as b
from d1max_contract import ed25519


def test_绑定码_手输宽容_大小写横杠和易混字母都认():
    code = b.new_code()
    assert len(code) == 16 and all(ch in b.ALPHABET for ch in code)
    pretty = b.pretty_code(code)
    assert pretty.count("-") == 3
    assert b.normalize_code(pretty.lower()) == code
    assert b.normalize_code("ilo0-1111-2222-3333") == "1100111122223333"
    assert b.code_hash(pretty) == b.code_hash(code)


@pytest.mark.parametrize("bad", ["", "123", "U" * 16, "1234567890ABCDEFG", None])
def test_绑定码_不对的拒(bad):
    with pytest.raises(b.BindingError):
        b.normalize_code(bad)


def test_贴纸_来回一致_前缀不对或改坏了拒():
    s = b.Sticker("D1M-000123", b.new_code(), "NW-000123", "abcdefgh12345678", "0" * 32)
    text = s.encode()
    assert text.startswith("D1MAXBOT1.")
    assert b.decode_sticker(text) == s
    with pytest.raises(b.BindingError):
        b.decode_sticker("D1MAXSITE1." + text.split(".", 1)[1])
    with pytest.raises(b.BindingError):
        b.decode_sticker(text[:-6])


def test_贴纸_序列号和指纹形状不对拒():
    good = b.Sticker("D1M-1", b.new_code(), "NW-1", "abcdefgh", "a" * 32)
    for bad in (b.Sticker("有空格 1", good.code, "NW", "abcdefgh", "a" * 32),
                b.Sticker("D1M-1", good.code, "NW", "abcdefgh", "A" * 32)):
        with pytest.raises(b.BindingError):
            b.decode_sticker(bad.encode())


def test_核对码_六位_同一随机数同一结果():
    n = "0123456789abcdef"
    assert b.check_digits(n) == b.check_digits(n)
    assert len(b.check_digits(n)) == 6 and b.check_digits(n).isdigit()
    assert b.check_digits(n) != b.check_digits("fedcba9876543210")


def test_WiFi_名字和密码的边界():
    assert b.check_wifi("Estate", "") == ("Estate", "")
    assert b.check_wifi("庄园", "12345678")[0] == "庄园"
    assert b.check_wifi("x" * 32, "a" * 63)
    assert b.check_wifi("x", "A1" * 32)              # 64 位十六进制
    for ssid, psk in (("", "12345678"), ("x" * 33, "12345678"), ("x", "1234567"),
                      ("x", "g" * 64), ("x", "密码密码密码密码"), ("a\nb", "12345678")):
        with pytest.raises(b.BindingError):
            b.check_wifi(ssid, psk)


def test_解绑令_签了能验_序列号过期签名都要对():
    seed = ed25519.new_seed()
    pub = ed25519.public_key(seed)
    tok = b.sign_unbind(seed, "D1M-7", now_ms=1_000, hours=24)
    got = b.verify_unbind(tok, pub, serial="D1M-7", now_ms=2_000)
    assert got.serial == "D1M-7" and got.expires_ms == 1_000 + 24 * 3600_000
    with pytest.raises(b.BindingError, match="不是给这台"):
        b.verify_unbind(tok, pub, serial="D1M-8", now_ms=2_000)
    with pytest.raises(b.BindingError, match="过期"):
        b.verify_unbind(tok, pub, serial="D1M-7", now_ms=1_000 + 24 * 3600_000 + 1)
    other = ed25519.public_key(ed25519.new_seed())
    with pytest.raises(b.BindingError, match="签名"):
        b.verify_unbind(tok, other, serial="D1M-7", now_ms=2_000)
    head, body, sig = tok.split(".")
    with pytest.raises(b.BindingError):
        b.verify_unbind(f"{head}.{body[:-2]}xx.{sig}", pub, serial="D1M-7", now_ms=2_000)


def test_解绑令_有效期太长不签_机器人钟慢太多也拒():
    seed = ed25519.new_seed()
    with pytest.raises(b.BindingError):
        b.sign_unbind(seed, "D1M-7", now_ms=0, hours=24 * 8)
    tok = b.sign_unbind(seed, "D1M-7", now_ms=10 * 24 * 3600_000, hours=24)
    with pytest.raises(b.BindingError, match="钟"):
        b.verify_unbind(tok, ed25519.public_key(seed), serial="D1M-7", now_ms=0)


def test_WiFi状态_形状不对的丢掉():
    assert b.wifi_view({}) is None and b.wifi_view(None) is None
    assert b.wifi_view({"wifi": {"ssid": "E", "link": "4g"}}) == \
        {"ssid": "E", "link": "4g", "change": None}
    v = b.wifi_view({"wifi": {"ssid": "E", "link": "wifi", "change": {
        "id": "0123abcd", "ssid": "New", "mode": "trial", "expires_ms": 5}}})
    assert v["change"] == {"id": "0123abcd", "ssid": "New", "mode": "trial", "expires_ms": 5}
    v = b.wifi_view({"wifi": {"ssid": 3, "link": "lte", "change": {
        "id": "x", "ssid": "New", "mode": "trial"}}})
    assert v == {"ssid": "", "link": "none", "change": None}
