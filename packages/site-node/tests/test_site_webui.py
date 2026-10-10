"""网页值班台(商业化 B1):静态文件与安全头、浏览器 cookie 登录、防跨站伪造。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from test_site_api import PW, 站

from d1max_site import webui

WEB = {"X-D1Max-Web": "1"}


def _http(s, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(s.api.url + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


@pytest.fixture
def 台(tmp_path):
    s = 站(tmp_path, alerts=True)
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>Duty</title>")
    (web / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("不该被读到")
    s.api.web_dir = web
    yield s
    s.close()


def test_静态_首页与资源_带安全头_资源长缓存_首页不缓存(台):
    code, h, body = _http(台, "GET", "/")
    assert code == 200 and b"Duty" in body and h["Content-Type"].startswith("text/html")
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "script-src 'self'" in h["Content-Security-Policy"]
    assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff"
    assert h["Cache-Control"] == "no-store"
    code, h, body = _http(台, "GET", "/assets/index-abc123.js")
    assert code == 200 and h["Content-Type"].startswith("text/javascript")
    assert "immutable" in h["Cache-Control"]
    code, _, body = _http(台, "GET", "/robots/A")                # 单页应用自己的路由
    assert code == 200 and b"Duty" in body


@pytest.mark.parametrize("path", ["/assets/../secret.txt", "/assets/..%2Fsecret.txt",
                                  "/assets/.hidden", "/assets/sub/x.js", "/assets/nope.js"])
def test_静态_走不出web目录(台, path):
    code, _, body = _http(台, "GET", path)
    assert code == 404 and "不该被读到".encode() not in body


def test_没构建过_静态404_接口照常(tmp_path):
    s = 站(tmp_path)
    try:
        s.api.web_dir = tmp_path / "empty"
        assert _http(s, "GET", "/")[0] == 404
        assert _http(s, "GET", "/healthz")[0] in (200, 503)
    finally:
        s.close()


def _cookie(h):
    return h["Set-Cookie"].split(";", 1)[0]


def test_网页登录_种HttpOnly_cookie_回复里没有令牌_cookie能看_改东西要防伪造头(台):
    code, h, body = _http(台, "POST", "/api/login", {"name": "alice", "password": PW, "web": True},
                          headers=WEB)
    d = json.loads(body)
    assert code == 200 and d["token"] == "" and d["name"] == "alice"
    sc = h["Set-Cookie"]
    for part in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
        assert part in sc
    ck = {"Cookie": _cookie(h)}
    assert _http(台, "GET", "/api/robots", headers=ck)[0] == 200
    code, _, body = _http(台, "POST", "/api/alerts/x/ack", {}, headers=ck)
    assert code == 403 and "防伪造" in json.loads(body)["error"]
    code, _, _ = _http(台, "POST", "/api/alerts/x/ack", {}, headers=ck | {"X-D1Max-Web": "1"})
    assert code != 403
    code, h2, _ = _http(台, "POST", "/api/logout", {}, headers=ck)
    assert code == 403, "退出也要防伪造头"
    code, h2, _ = _http(台, "POST", "/api/logout", {}, headers=ck | {"X-D1Max-Web": "1"})
    assert code == 200 and "Max-Age=0" in h2["Set-Cookie"]
    assert _http(台, "GET", "/api/robots", headers=ck)[0] == 401, "退出以后 cookie 作废"


def test_手机命令行照旧_带令牌的不要防伪造头_普通登录照样回令牌(台):
    code, _, body = _http(台, "POST", "/api/login", {"name": "alice", "password": PW})
    tok = json.loads(body)["token"]
    assert tok
    auth = {"Authorization": f"Bearer {tok}"}
    code, _, _ = _http(台, "POST", "/api/alerts/x/ack", {}, headers=auth)
    assert code != 403


def test_cookie_解析():
    assert webui.cookie_token("a=1; d1max_session=tok; b=2") == "tok"
    assert webui.cookie_token("d1max_session=") is None
    assert webui.cookie_token(None) is None
    assert webui.cookie_token('bad"cookie') is None


def test_我是谁_cookie刷新以后认得出人和角色(台):
    code, h, _ = _http(台, "POST", "/api/login", {"name": "alice", "password": PW, "web": True},
                       headers=WEB)
    台.api.site_name = "Lakeside Estate"
    code, _, body = _http(台, "GET", "/api/me", headers={"Cookie": _cookie(h)})
    d = json.loads(body)
    assert code == 200 and d["name"] == "alice" and d["role"] == "admin"
    assert d["site_name"] == "Lakeside Estate" and d["api_level"] >= 1
    assert _http(台, "GET", "/api/me")[0] == 401


def test_包里带的网页_构建过_没有内联脚本和内联样式():
    """站点的 CSP 不许内联脚本、内联样式:构建产物(web/console → d1max_site/web)里不能有。"""
    import re

    idx = webui.WEB_DIR / "index.html"
    assert idx.is_file(), "web/console 没构建:cd web/console && npm run build"
    html = idx.read_text(encoding="utf-8")
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html) == []
    assert "style=" not in html and "<style" not in html
    for src in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html):
        assert webui.resolve(src) is not None, src


def _raw(s, method, path, raw: bytes, headers):
    r = urllib.request.Request(s.api.url + path, data=raw, method=method)
    for k, v in headers.items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers


def test_外审I4_跨站表单换登录_拒在种cookie之前_正常网页登录和手机登录照常(台):
    """别的网站的 HTML 表单(text/plain、没有自定义头)能拼出合法 JSON:网页登录要拒,不种 cookie。"""
    form = b'{"name": "alice", "password": "' + PW.encode() + b'", "web": true, "x": "="}'
    for headers in ({"Content-Type": "text/plain", "Origin": "https://evil.example"},
                    {"Content-Type": "application/json"},                       # 没有防伪造头
                    {"Content-Type": "text/plain", "X-D1Max-Web": "1"}):         # 头有了但不是 JSON
        code, h = _raw(台, "POST", "/api/login", form, headers)
        assert code == 403 and "Set-Cookie" not in h, headers
    code, h = _raw(台, "POST", "/api/login", form,
                   {"Content-Type": "application/json; charset=utf-8", "X-D1Max-Web": "1"})
    assert code == 200 and "d1max_session=" in h["Set-Cookie"]
    code, _, body = _http(台, "POST", "/api/login", {"name": "alice", "password": PW})
    assert code == 200 and json.loads(body)["token"], "手机、命令行登录不受影响"


def test_外审M1_静态目录里的符号链接指到外面_不给(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<title>x</title>")
    (tmp_path / "secret.json").write_text("{}")
    (web / "assets" / "fixture.json").symlink_to(tmp_path / "secret.json")
    (web / "assets" / "ok.js").write_text("1")
    assert webui.resolve("/assets/fixture.json", web) is None
    assert webui.resolve("/assets/ok.js", web) is not None
    other = tmp_path / "web2"
    other.mkdir()
    (other / "index.html").symlink_to(tmp_path / "secret.json")
    assert webui.resolve("/", other) is None, "index.html 也查"
