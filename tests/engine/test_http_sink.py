"""HTTP 上传后端。上线形状定在这儿,服务器侧(Task 8)照着实现。"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from urllib.parse import unquote

import pytest

from d1max_patrol.engine.http_sink import HttpSink, build_body, parse_receipt
from d1max_patrol.engine.uploader import PutReceipt, PutRequest, SinkError

样例 = PutRequest(
    sn="D1MAX-TEST-01",
    run="巡检一/20260911T101500Z",
    rel="photos/P1__front__20260911T101500Z.jpg",
    offset=7,
    data=b"\xff\xd8hello",
    total=99,
)


class 假opener:
    def __init__(self, status: int = 200, body: bytes = b"{}", 抛=None) -> None:
        self.status, self.body, self.抛 = status, body, 抛
        self.收到: list = []

    def open(self, request, timeout=None):
        if self.抛 is not None:
            raise self.抛
        self.收到.append((request, timeout))
        return _假响应(self.status, self.body)


class _假响应:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _假响应:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _读body时抛的假响应:
    """``open()`` 顺利返回,但读 body 的时候才炸 —— 模拟连接读到一半被截断。"""

    def __init__(self, 抛: BaseException) -> None:
        self._抛 = 抛

    def read(self) -> bytes:
        raise self._抛

    def __enter__(self) -> _读body时抛的假响应:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _固定响应的假opener:
    """``open()`` 不抛,直接把一个现成的响应对象原样交出去。"""

    def __init__(self, resp) -> None:
        self._resp = resp

    def open(self, request, timeout=None):
        return self._resp


def test_body就是原始字节_不做base64() -> None:
    """base64 会把每一块涨三分之一。上行带宽是这套系统最紧的东西。"""
    assert build_body(样例) == b"\xff\xd8hello"


def test_元数据走header不走url() -> None:
    opener = 假opener(body=b'{"ok": true, "stored": 12, "sha256": "ab", "message": ""}')
    sink = HttpSink("http://console.example:8095", opener=opener)
    sink.put(样例)
    request, timeout = opener.收到[0]
    assert request.full_url == "http://console.example:8095/api/intake/put"
    assert request.get_header("X-d1max-sn") == "D1MAX-TEST-01"
    # run/rel 里有中文,header 值必须 latin-1 编得出来 —— 所以转义,不裸传。
    assert unquote(request.get_header("X-d1max-run")) == "巡检一/20260911T101500Z"
    assert unquote(request.get_header("X-d1max-rel")) == "photos/P1__front__20260911T101500Z.jpg"
    assert request.get_header("X-d1max-offset") == "7"
    assert request.get_header("X-d1max-total") == "99"
    assert timeout == 30


def test_有token就带上() -> None:
    opener = 假opener(body=b'{"ok": true, "stored": 1, "sha256": "ab", "message": ""}')
    # 口令必须是 latin-1 编得出来的 —— header 值的硬限制,中文口令会当场炸。
    HttpSink("http://x:8095", token="PLACEHOLDER123", opener=opener).put(样例)
    assert opener.收到[0][0].get_header("Authorization") == "Bearer PLACEHOLDER123"


def test_没token就不带Authorization头() -> None:
    opener = 假opener(body=b'{"ok": true, "stored": 1, "sha256": "ab", "message": ""}')
    HttpSink("http://x:8095", opener=opener).put(样例)
    assert opener.收到[0][0].get_header("Authorization") is None


def test_回执解析() -> None:
    body = json.dumps({"ok": True, "stored": 42, "sha256": "d" * 64, "message": ""}).encode()
    assert parse_receipt(200, body) == PutReceipt(ok=True, stored=42, sha256="d" * 64, message="")


def test_非200一律当没传成功_但不抛() -> None:
    """**503 不是异常,是"这次没成"。** 抛出去会走 SinkError 那条退避,
    而 409(偏移对不上)要的是 rewind 不是退避 —— 两条路不一样,所以
    状态码在这儿解析成 ok=False,交给 Uploader 判。
    """
    got = parse_receipt(
        409, '{"ok": false, "stored": 3, "sha256": "", "message": "偏移对不上"}'.encode()
    )
    assert got.ok is False
    assert got.stored == 3
    assert got.message == "偏移对不上"


def test_回执不是json就抛SinkError_不清进度() -> None:
    """中间挡了个网关,回了一页 HTML。我们读不懂,只能退避,不能 rewind ——
    所以这是 ``SinkError``,不是 ``ok=False``(``ok=False`` 会触发
    rewind,把之前已经确认过哈希对上的进度一起作废)。
    """
    with pytest.raises(SinkError, match="回执不是 JSON"):
        parse_receipt(200, b"<html>502 Bad Gateway</html>")


def test_网络错误翻成SinkError() -> None:
    opener = 假opener(抛=urllib.error.URLError("断了"))
    with pytest.raises(SinkError):
        HttpSink("http://x:8095", opener=opener).put(样例)


def test_超时翻成SinkError() -> None:
    opener = 假opener(抛=TimeoutError("超时"))
    with pytest.raises(SinkError):
        HttpSink("http://x:8095", opener=opener).put(样例)


def test_HTTPError当回执读_不当异常() -> None:
    """urllib 把 4xx/5xx 抛成 HTTPError,但它身上带着 body。
    409 的 body 里有服务器说的 stored,那个数正是我们要的。
    """
    err = urllib.error.HTTPError(
        "http://x:8095/api/intake/put",
        409,
        "Conflict",
        {},
        None,
    )
    err.read = lambda: '{"ok": false, "stored": 3, "sha256": "", "message": "偏移对不上"}'.encode()
    got = HttpSink("http://x:8095", opener=假opener(抛=err)).put(样例)
    assert (got.ok, got.stored) == (False, 3)


def test_回执是合法json但不是对象就抛SinkError_不清进度() -> None:
    """``json.loads`` 成功,但顶层不是 dict(列表/null/裸字符串/裸数字)——
    这不该走到 ``.get(...)`` 那一步去炸 ``AttributeError``,应该翻成
    ``SinkError``(不是 ``ok=False``:我们没读懂服务器想说什么,不能
    rewind 作废之前已确认的进度)。
    """
    for body in (b"[1, 2, 3]", b"null", b'"ok"', b"3"):
        with pytest.raises(SinkError, match="回执不是 JSON 对象"):
            parse_receipt(200, body)


def test_HTTPException翻成SinkError() -> None:
    """``http.client.HTTPException`` 不是 ``OSError`` 的子类 —— 网关回一段
    不是合法 HTTP 状态行的东西时,urllib 在 ``open()`` 就会抛这个,必须翻成
    ``SinkError``,否则会带着原始异常把 ``Uploader.run_once`` 炸穿。
    """
    opener = 假opener(抛=http.client.BadStatusLine("garbage-not-http"))
    with pytest.raises(SinkError):
        HttpSink("http://x:8095", opener=opener).put(样例)


def test_读body时的HTTPException也翻成SinkError() -> None:
    """``IncompleteRead`` 是在 ``resp.read()`` 那一步才抛的 —— 连接在
    ``open()`` 顺利返回之后、读 body 的中途被截断(CPE 重拨截断响应是真实
    会发生的场景)。这条不在 ``open()`` 那条路径上,单独补一条覆盖。
    """
    抛 = http.client.IncompleteRead(b"ab", 3)
    opener = _固定响应的假opener(_读body时抛的假响应(抛))
    with pytest.raises(SinkError):
        HttpSink("http://x:8095", opener=opener).put(样例)


def _装个记录参数的HTTPSHandler(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 ``urllib.request.HTTPSHandler`` 换成一个记参数的子类。

    **不能换成普通函数** —— ``build_opener`` 自己会对传进去的 handler
    实例做 ``isinstance(check, klass)``/``issubclass`` 判断(判断要不要用
    默认的 ``HTTPSHandler``),换成函数会让那段标准库代码直接
    ``TypeError: isinstance() arg 2 must be a type``。子类化才既能让
    ``build_opener`` 内部的类型检查照常工作,又能拦截住构造参数。
    """
    收到: dict = {}
    真HTTPSHandler = urllib.request.HTTPSHandler

    class 假HTTPSHandler(真HTTPSHandler):
        def __init__(self, *args, **kwargs):
            收到.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(urllib.request, "HTTPSHandler", 假HTTPSHandler)
    return 收到


def test_不传opener时真的调用build_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 ``opener`` 是真机上唯一会跑的路径 —— 生产代码不会注入假 opener。
    这条分支里 ``ssl_context is not None`` 判断决定要不要把它挂进
    ``HTTPSHandler``,不是纯转发给标准库,值得单独验证构造出来的参数对不对。
    """
    收到 = _装个记录参数的HTTPSHandler(monkeypatch)
    ctx = ssl.create_default_context()
    sink = HttpSink("https://x:8095", ssl_context=ctx)
    assert 收到.get("context") is ctx
    assert sink._opener is not None


def test_不传ssl_context时HTTPSHandler不带context(monkeypatch: pytest.MonkeyPatch) -> None:
    """没给 ``ssl_context`` 就不该往 ``HTTPSHandler`` 里塞任何 ``context`` 参数——
    这一步是 Task 14 证书钉扎留的口子,这一步先不用,得先确认"不用"这件事
    真的成立,不是嘴上说说。
    """
    收到 = _装个记录参数的HTTPSHandler(monkeypatch)
    HttpSink("https://x:8095")
    assert "context" not in 收到


def test_stored字段类型不对时parse_receipt抛SinkError_不是ok为false() -> None:
    """``stored`` 字段存在但不是数字(服务器发来字符串、列表……)——
    ``int(...)`` 会抛 ``ValueError``/``TypeError``。这不该走 ``ok=False``
    (那条路在 ``Uploader`` 里会触发 rewind,把之前已经验证过哈希的进度
    一起作废),该走 ``SinkError``(只退避重排,不动 offset)。
    """
    for stored, exc_word in (
        ('"abc"', "abc"),
        ("[1, 2]", "list"),
    ):
        body = f'{{"ok": true, "stored": {stored}, "sha256": "x", "message": "m"}}'.encode()
        with pytest.raises(SinkError) as excinfo:
            parse_receipt(200, body)
        assert exc_word in str(excinfo.value)


def test_stored字段类型不对时put也抛SinkError_不穿透() -> None:
    """走完整 ``HttpSink.put()``(不只是单独调 ``parse_receipt``)确认
    同一个洞在真正的调用路径上也堵住了。
    """
    opener = 假opener(body=b'{"ok": true, "stored": "abc", "sha256": "x", "message": "m"}')
    with pytest.raises(SinkError):
        HttpSink("http://x:8095", opener=opener).put(样例)


def test_HTTPError分支里stored字段类型不对也抛SinkError_不穿透() -> None:
    """``err.read()`` 那条路径(HTTPError 当回执读)一样会调 ``parse_receipt``,
    同一个洞,单独补一条覆盖,别漏了这条调用点。
    """
    err = urllib.error.HTTPError(
        "http://x:8095/api/intake/put",
        409,
        "Conflict",
        {},
        None,
    )
    err.read = lambda: b'{"ok": false, "stored": [1, 2], "sha256": "", "message": "m"}'
    with pytest.raises(SinkError):
        HttpSink("http://x:8095", opener=假opener(抛=err)).put(样例)


def test_ok和sha256和message字段任意类型都不抛() -> None:
    """异常出口清点表里另外三个字段(``ok``/``sha256``/``message``)—— 分别
    经过 ``bool(...)``/``str(...)``,对 JSON 能解出来的任何标量、``list``、
    ``dict``、``None`` 都不会抛,这里用一批"歪的"值钉住这个结论,防止以后
    有人在这三行加新逻辑时不小心引入第四条泄漏。
    """
    for payload in (
        {"ok": "text", "stored": 1, "sha256": "a", "message": "m"},
        {"ok": [1, 2], "stored": 1, "sha256": "a", "message": "m"},
        {"ok": None, "stored": 1, "sha256": [1, 2], "message": {}},
        {"ok": True, "stored": 1, "sha256": None, "message": 123},
    ):
        body = json.dumps(payload).encode()
        got = parse_receipt(200, body)
        assert isinstance(got, PutReceipt)
