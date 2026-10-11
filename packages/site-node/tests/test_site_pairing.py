"""手机扫码添加站点的配对码(App V2):站点名 + 地址 + 证书指纹,没有秘密;地址只收 https://主机[:端口]。"""

from __future__ import annotations

import base64
import json
import os

import pytest
from test_site_api import PW, 站

from d1max_site import main as site_main
from d1max_site import pairing

FP = "ab" * 32


@pytest.fixture
def 台(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "estate-1",
                           "--hostname", "localhost"]) == 0
    s = 站(tmp_path / "api", alerts=True)
    s.api.home = home
    s.api.site_name = "庄园一号"
    s.accounts.add("gina", PW, role="guard")
    yield s, home
    s.close()


def test_拼出来读回去一样_中文站点名也行_里面只有三样():
    code = pairing.encode(" 庄园一号 ", "https://site.example:8443/", "sha256:" + FP.upper())
    assert code.startswith("D1MAXSITE1.") and "=" not in code
    assert pairing.decode(code) == {"name": "庄园一号", "url": "https://site.example:8443", "fingerprint": FP}
    body = code.split(".", 1)[1]
    raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert set(raw) == {"n", "u", "f"}, "没有账号、口令、令牌"


@pytest.mark.parametrize("url", [
    "", "http://site:8443", "https://", "site:8443", "https://u:p@site:8443", "https://u@site:8443",
    "https://site:8443/api", "https://site:8443?x=1", "https://site:8443#f", "https://site:99999",
    "https://si te:8443", "https://si\nte:8443", "https://" + "a" * 200, "javascript:alert(1)",
])
def test_地址只收https主机端口(url):
    with pytest.raises(pairing.PairingError):
        pairing.check_url(url)


def test_地址规范化_IPv6带方括号_没写端口就不带():
    assert pairing.check_url("https://Site.Example/") == "https://site.example"
    assert pairing.check_url(" https://[fd00::1]:8443 ") == "https://[fd00::1]:8443"
    assert pairing.check_url("https://192.168.1.5:8443") == "https://192.168.1.5:8443"


@pytest.mark.parametrize("name,fp", [("", FP), ("x" * 65, FP), ("a", "ab" * 31), ("a", "zz" * 32), ("a", "")])
def test_站点名和指纹不对就不出码(name, fp):
    with pytest.raises(pairing.PairingError):
        pairing.encode(name, "https://s:8443", fp)


@pytest.mark.parametrize("code", [
    "", "D1MAX1.abc", "D1MAXSITE1.", "D1MAXSITE1.!!!!", "D1MAXSITE1." + base64.urlsafe_b64encode(b"[1]").decode(),
    "D1MAXSITE1." + base64.urlsafe_b64encode(json.dumps({"n": "a", "u": "http://s", "f": FP}).encode()).decode(),
    "D1MAXSITE1." + base64.urlsafe_b64encode(json.dumps({"n": "a", "u": "https://s", "f": "12"}).encode()).decode(),
    "D1MAXSITE1." + base64.urlsafe_b64encode(json.dumps({"n": 1, "u": "https://s", "f": FP}).encode()).decode(),
    "D1MAXSITE1." + base64.urlsafe_b64encode(b"\xff\xfe").decode(),
])
def test_坏码读不出来_狗的开通码也不认(code):
    with pytest.raises(pairing.PairingError):
        pairing.decode(code)


def test_接口_登录了谁都能拿_指纹就是站点证书的_没登录不给(台):
    s, home = 台
    tok = s.req("POST", "/api/login", {"name": "gina", "password": PW})[1]["token"]
    code, d = s.req("GET", "/api/pairing?url=https%3A%2F%2F192.168.1.5%3A8443", token=tok)
    assert code == 200, d
    fp = site_main.cmd_fingerprint(home)
    assert d["fingerprint"] == fp and d["url"] == "https://192.168.1.5:8443" and d["name"] == "庄园一号"
    assert pairing.decode(d["code"]) == {"name": "庄园一号", "url": "https://192.168.1.5:8443", "fingerprint": fp}
    assert s.req("GET", "/api/pairing?url=https%3A%2F%2Fx%3A8443")[0] == 401


@pytest.mark.parametrize("q", ["", "?url=", "?url=http%3A%2F%2Fx", "?url=https%3A%2F%2Fx%2Fpath"])
def test_接口_地址不对回400_说原因(台, q):
    s, _ = 台
    code, d = s.req("GET", "/api/pairing" + q, token=s.login())
    assert code == 400 and "地址" in d["error"]


def test_接口_没接站点目录回404(台):
    s, _ = 台
    s.api.home = None
    assert s.req("GET", "/api/pairing?url=https%3A%2F%2Fx%3A8443", token=s.login())[0] == 404


def test_命令行出码_地址不对退出码非零(台, capsys):
    _, home = 台
    assert site_main.main(["--home", str(home), "pair-code", "--url", "https://site.lan:8443"]) == 0
    out = capsys.readouterr().out.strip()
    d = pairing.decode(out)
    assert d["url"] == "https://site.lan:8443" and d["name"] == "estate-1", "缺省用 site.json 的站点号"
    assert d["fingerprint"] == site_main.cmd_fingerprint(home)
    assert site_main.main(["--home", str(home), "pair-code", "--url", "https://x:8443", "--name", "East"]) == 0
    assert pairing.decode(capsys.readouterr().out.strip())["name"] == "East"
    assert site_main.main(["--home", str(home), "pair-code", "--url", "http://site.lan"]) != 0
