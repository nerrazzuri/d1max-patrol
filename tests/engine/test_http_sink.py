"""HTTP 上传后端。上线形状定在这儿,服务器侧(Task 8)照着实现。"""

from __future__ import annotations

import json
import urllib.error
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


def test_回执不是json就当失败_不抛() -> None:
    """中间挡了个网关,回了一页 HTML。这不该让上传器崩。"""
    got = parse_receipt(200, b"<html>502 Bad Gateway</html>")
    assert got.ok is False
    assert "回执不是 JSON" in got.message


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
