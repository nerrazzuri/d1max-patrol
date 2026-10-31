"""W63 机器人领证书、挂配网申请:先要题,用设备钥匙答,一起交;老站点(没有出题的口)照旧。"""

from __future__ import annotations

import base64
import json

from d1max_patrol import provision as pv
from d1max_patrol.bind.sitelink import LinkError, SiteLink

CODE = {"api": "https://10.0.0.2:8443", "fp": "a" * 64, "mqtt": "mqtts://10.0.0.2:8883",
        "id": "D1M-1", "t": "tok"}


def _files():
    return {k: base64.b64encode(b"x").decode() for k in pv.REQUIRED}


def test_领证书_先要题_答了一起交():
    sent = []

    def call(code, method, path, body, timeout_s):
        sent.append((method, path, json.loads(body) if body else None))
        if path == "/api/attest/challenge":
            return 200, json.dumps({"nonce": "N", "site_id": "S"}).encode()
        return 200, json.dumps({"files": _files()}).encode()
    got = pv.claim(CODE, attest=lambda p, s, n: {"purpose": p, "site": s, "nonce": n}, call=call)
    assert set(got) >= set(pv.REQUIRED)
    assert sent[0][:2] == ("GET", "/api/attest/challenge")
    assert sent[1][2]["attest"] == {"purpose": "claim", "site": "S", "nonce": "N"}


def test_领证书_老站点没有出题的口_照旧领_没有设备身份就不答():
    def old(code, method, path, body, timeout_s):
        if path == "/api/attest/challenge":
            return 404, b"{}"
        return 200, json.dumps({"files": _files()}).encode()
    assert pv.claim(CODE, attest=lambda *a: {"x": 1}, call=old)
    seen = []

    def new(code, method, path, body, timeout_s):
        seen.append(json.loads(body) if body else None)
        if path == "/api/attest/challenge":
            return 200, b'{"nonce": "N", "site_id": "S"}'
        return 403, json.dumps({"error": "此设备无法加入站点,请联系供应商"}).encode()
    try:
        pv.claim(CODE, attest=lambda *a: None, call=new)
    except pv.ProvisionError as exc:
        assert "供应商" in str(exc)
    assert "attest" not in seen[-1]


def test_配网申请_先要题_老站点404就不带():
    calls = []

    class L(SiteLink):
        def _call(self, site, method, path, body=None):
            calls.append((method, path, body))
            if path == "/api/attest/challenge":
                if site.get("old"):
                    raise LinkError("没有", 404)
                return {"nonce": "N", "site_id": "S"}
            return {"request_id": "r", "poll_secret": "p"}
    link = L(attest=lambda p, s, n: {"p": p, "s": s, "n": n})
    link.request({"url": "https://x"}, {"serial": "D1M-1"})
    assert calls[-1][2]["attest"] == {"p": "bind", "s": "S", "n": "N"}
    link.request({"url": "https://x", "old": True}, {"serial": "D1M-1"})
    assert "attest" not in calls[-1][2]
