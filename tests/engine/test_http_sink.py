"""HTTP 上传后端。上线形状定在这儿,服务器侧(Task 8)照着实现。"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from urllib.parse import unquote

import pytest

from d1max_agent.engine.http_sink import HttpSink, build_body, parse_receipt
from d1max_agent.engine.uploader import PutReceipt, PutRequest, SinkError

样例 = PutRequest(
    sn="D1MAX-TEST-01",
    run="巡检一/20260911T101500Z",
    rel="photos/P1__front__20260911T101500Z.jpg",
    offset=7,
    data=b"\xff\xd8hello",
    total=99,
)


def _按上限给(body: bytes):
    """造一个照 ``read(amt)`` 契约办事的读函数:要多少给多少,给完了给 ``b""``。

    真机上 ``read(n)`` 只保证**至多** n 字节,读完了给空 —— 替身写成"每次都把
    整个 body 再给一遍"会让生产代码的上限循环永远吃不到结尾,那不是模拟真机。
    """
    位置 = [0]

    def read(上限: int | None = None) -> bytes:
        起 = 位置[0]
        片 = body[起:] if 上限 is None else body[起 : 起 + 上限]
        位置[0] += len(片)
        return 片

    return read


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
    """``read`` 照 ``http.client.HTTPResponse.read(amt=None)`` 的签名收参数。

    生产代码现在**带着长度上限读**(``_读回执``),``read()`` 一定会被塞一个
    字节数进去。替身不收这个参数就不是在模拟真响应,而是在替生产代码规定一条
    真机上不存在的契约 —— 那种绿是假的。
    """

    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body
        self._给到 = 0

    def read(self, 上限: int | None = None) -> bytes:
        剩 = self._body[self._给到 :]
        片 = 剩 if 上限 is None else 剩[:上限]
        self._给到 += len(片)
        return 片

    def __enter__(self) -> _假响应:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _读body时抛的假响应:
    """``open()`` 顺利返回,但读 body 的时候才炸 —— 模拟连接读到一半被截断。"""

    def __init__(self, 抛: BaseException) -> None:
        self._抛 = 抛

    def read(self, 上限: int | None = None) -> bytes:
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
    err.read = _按上限给(
        '{"ok": false, "stored": 3, "sha256": "", "message": "偏移对不上"}'.encode()
    )
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


def test_畸形跳转地址翻成SinkError() -> None:
    """服务器回 302 且 ``Location`` 写成 ``http://[`` 时,``urllib`` 自己的
    ``HTTPRedirectHandler.http_error_302`` 里 ``urlsplit`` 抛
    ``ValueError: Invalid IPv6 URL``,从 ``opener.open()`` 冒出来。

    它既不是 ``OSError`` 也不是 ``HTTPException``,原来那张清单接不住。
    """
    opener = 假opener(抛=ValueError("Invalid IPv6 URL"))
    with pytest.raises(SinkError, match="跳转目标"):
        HttpSink("http://x:8095", opener=opener).put(样例)


def test_三支except的顺序_专用话术不会被ValueError那支吞掉() -> None:
    """``put()`` 那三支 except 的**顺序本身**是行为,得有测试钉住。

    - ``UnicodeEncodeError`` **是** ``ValueError`` 的子类 —— 排到 ``ValueError``
      后面,"header 编不出 latin-1"就会被说成"地址解不开"。
    - ``ssl.SSLCertVerificationError`` **同时是** ``ValueError`` 和 ``OSError``
      的子类 —— ``ValueError`` 那支排到网络那支前面,"证书验不过"就会被说成
      "地址解不开",现场会去查跳转而不是查证书。
    """

    class _编header的opener(_固定响应的假opener):
        def open(self, request, timeout=None):
            for _, 值 in request.header_items():
                str(值).encode("latin-1")
            return super().open(request, timeout)

    resp = _假响应(200, b'{"ok": true, "stored": 1, "sha256": "a", "message": ""}')
    with pytest.raises(SinkError, match="latin-1"):
        HttpSink("http://x:8095", token="口令中文", opener=_编header的opener(resp)).put(样例)

    抛 = ssl.SSLCertVerificationError(1, "certificate verify failed")
    with pytest.raises(SinkError, match="发不出去"):
        HttpSink("http://x:8095", opener=假opener(抛=抛)).put(样例)


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
    err.read = _按上限给(b'{"ok": false, "stored": [1, 2], "sha256": "", "message": "m"}')
    with pytest.raises(SinkError):
        HttpSink("http://x:8095", opener=假opener(抛=err)).put(样例)


def test_ok和sha256和message字段形状不对时抛SinkError() -> None:
    """另外三个字段跟 ``stored`` 一个口径:**认得的形状才用,认不得就退避。**

    这三行过去走 ``bool(...)``/``str(...)``,对什么都不抛 —— 于是服务器发来
    一坨我们不认识的东西会被悄悄当真话用:``{"ok": "no"}`` 变成 ``ok=True``
    (非空字符串为真),``{"sha256": None}`` 变成字符串 ``"None"`` 拿去跟真
    哈希比。宽容转换在这儿不是优点:回执读错一个字段,后果跟读不懂是一样的,
    但读错了没人知道。

    ``SinkError`` 只退避不 rewind,所以判严的代价是多退一轮,不丢证据。
    """
    for payload, 字段 in (
        ({"ok": "text", "stored": 1, "sha256": "a", "message": "m"}, "ok"),
        ({"ok": [1, 2], "stored": 1, "sha256": "a", "message": "m"}, "ok"),
        ({"ok": None, "stored": 1, "sha256": "a", "message": "m"}, "ok"),
        ({"ok": True, "stored": 1, "sha256": [1, 2], "message": "m"}, "sha256"),
        ({"ok": True, "stored": 1, "sha256": None, "message": "m"}, "sha256"),
        ({"ok": True, "stored": 1, "sha256": "a", "message": 123}, "message"),
        ({"ok": True, "stored": 1, "sha256": "a", "message": {}}, "message"),
    ):
        body = json.dumps(payload).encode()
        with pytest.raises(SinkError) as excinfo:
            parse_receipt(200, body)
        assert 字段 in str(excinfo.value)


def test_四个字段形状都对时照常出回执() -> None:
    """判严了也不能把正常回执判死 —— 这条钉住放行的那一侧。"""
    body = json.dumps({"ok": True, "stored": 7, "sha256": "a" * 64, "message": "好了"}).encode()
    assert parse_receipt(200, body) == PutReceipt(
        ok=True, stored=7, sha256="a" * 64, message="好了"
    )


def test_stored是bool时抛SinkError_不当成1() -> None:
    """``bool`` 是 ``int`` 的子类 —— ``isinstance(True, int)`` 为真。不单挡一下,
    ``{"stored": true}`` 会悄悄变成 ``stored=1``,上传器拿着这个 1 去对偏移。
    """
    with pytest.raises(SinkError) as excinfo:
        parse_receipt(200, b'{"ok": true, "stored": true, "sha256": "a", "message": ""}')
    assert "stored" in str(excinfo.value)


def test_stored是inf时抛SinkError_不穿OverflowError() -> None:
    """``json.loads`` **默认就认** ``Infinity``,``1e400``/``1e309`` 也解成 ``inf``;
    服务器侧同样是 Python,``json.dumps(float("inf"))`` **默认就吐** ``Infinity``。

    ``int(inf)`` 抛的是 ``OverflowError``,MRO 是 ``ArithmeticError -> Exception``——
    老代码那张 ``except (ValueError, TypeError)`` 的清单两个都接不住,原样穿出
    ``put()`` 把上传线程炸掉。同一行的 ``NaN`` 一直是绿的(``int(nan)`` 抛
    ``ValueError``),两者只差一个字面量。
    """
    for 字面量 in (b"Infinity", b"-Infinity", b"1e400", b"-1e400", b"1e309"):
        body = b'{"ok": true, "stored": ' + 字面量 + b', "sha256": "a", "message": ""}'
        with pytest.raises(SinkError) as excinfo:
            parse_receipt(200, body)
        assert "stored" in str(excinfo.value)
def test_ok缺了就是读不懂_不当成服务器说了false() -> None:
    """裁决二十四:``ok`` 没有缺省值了。

    它曾经缺省 ``False``,于是"回执里压根没有 ok 这个键"被翻译成了"服务器说了
    ok 为假" —— 而 ``ok=False`` 会让 ``Uploader`` rewind,把已经确认过哈希的进度
    整个作废。一个 502 配上 API 网关的标准错误体就走得通这条路。

    这是**裁决十八的贯彻,不是新规矩**:rewind 只能由服务器**明确说了话**触发,
    我们读不懂的一切只能退避。
    """
    for body in (
        b"{}",
        b'{"message": "Internal server error"}',
        b'{"stored": 2097152, "sha256": "a"}',
    ):
        with pytest.raises(SinkError) as excinfo:
            parse_receipt(200, body)
        assert "ok" in str(excinfo.value)


def test_服务器明确说ok为假那条路还活着() -> None:
    """把 ``ok`` 判严之后要确认没把 ``PutReceipt(ok=False)`` 变成死路。

    ``ok=False`` 是**服务器明确表态**的那条路,``Uploader`` 的 rewind 靠它。
    服务器结构完好地回 ``{"ok": false, ...}``(200 也好,409 也好)必须照旧
    给回执,不能变成 ``SinkError``。
    """
    body = json.dumps(
        {"ok": False, "stored": 5, "sha256": "e" * 64, "message": "盘满了"}
    ).encode()
    assert parse_receipt(200, body) == PutReceipt(
        ok=False, stored=5, sha256="e" * 64, message="盘满了"
    )
    assert parse_receipt(409, body).ok is False
    opener = 假opener(body=body)
    assert HttpSink("http://x:8095", opener=opener).put(样例).ok is False


def test_stored是负数时抛SinkError() -> None:
    """形状对、值荒唐,一样是读不懂。

    ``{"stored": -5}`` 过得了 ``isinstance(值, int)``,写进队列之后下一轮
    ``fh.seek(-5)`` 抛 ``OSError``,此后永久 defer —— 证据不丢(backlog 保持
    住了),但 defer 的文案说的是"run 目录还在但这个文件不见了",文件明明在,
    会把现场排查带去完全错的方向。在这儿判掉,错就报在离根子最近的地方。
    """
    for stored in (-1, -5, -2**62):
        body = json.dumps({"ok": True, "stored": stored, "sha256": "a", "message": ""}).encode()
        with pytest.raises(SinkError) as excinfo:
            parse_receipt(200, body)
        assert "stored" in str(excinfo.value)


def test_必须正好200才算ok_201和204都判没传成() -> None:
    """上线形状:服务器收下一块之后**必须回 200**。

    ``201``/``204``/``206`` 哪怕 body 完美,在这儿也一律判成"这次没传成"。
    这条规则以前只活在 ``ok=... and status == 200`` 那半行里,没写在任何地方。
    要放宽成 ``200 <= status < 300`` 是**改协议**,得先改规格。
    """
    body = json.dumps({"ok": True, "stored": 9, "sha256": "a" * 64, "message": ""}).encode()
    assert parse_receipt(200, body).ok is True
    for status in (201, 202, 204, 206):
        assert parse_receipt(status, body).ok is False


class _没有尽头的假响应:
    """要多少给多少,永远给得出来 —— 服务器 ``Content-Length`` 报 100 GB 的形状。"""

    status = 200

    def read(self, 上限: int | None = None) -> bytes:
        if 上限 is None:
            raise MemoryError
        return b"a" * 上限

    def __enter__(self) -> _没有尽头的假响应:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_回执超过上限就判读不懂_压根不去分配那100GB() -> None:
    """裁决二十五:**上限是第一道,翻译异常是第二道。**

    服务器报 ``Content-Length: 100000000000`` 的时候,正确的做法不是接住
    ``MemoryError``,是根本不去分配那 100 GB。这个替身把"不带上限地读"做成
    抛 ``MemoryError``(真机上就是那样炸的)—— 生产代码要是没带上限,这条会
    直接红在 ``MemoryError`` 上;带了上限,它只会要 ``上限 + 1`` 个字节,拿到
    ``上限 + 1`` 就判"这不是一张回执"。
    """
    opener = _固定响应的假opener(_没有尽头的假响应())
    with pytest.raises(SinkError, match="回执太长"):
        HttpSink("http://x:8095", opener=opener).put(样例)


def test_读body时抛OverflowError和MemoryError也翻成SinkError() -> None:
    """够不着上限的那两档:``Content-Length >= 2**63`` 在分配**之前**就抛
    ``OverflowError``,``getaddrinfo`` 遇上大得装不进 C 的 long 的端口也抛它。

    ``OverflowError`` 的 MRO 是 ``ArithmeticError -> Exception``,``MemoryError``
    直接挂在 ``Exception`` 底下 —— **两个都不是 OSError**,原来那张清单一个都
    接不住,会带着原始异常把 ``Uploader.run_once`` 炸穿。
    """
    for 抛 in (
        OverflowError("cannot fit 'int' into an index-sized integer"),
        MemoryError(),
    ):
        opener = _固定响应的假opener(_读body时抛的假响应(抛))
        with pytest.raises(SinkError, match="发不出去"):
            HttpSink("http://x:8095", opener=opener).put(样例)
        with pytest.raises(SinkError, match="发不出去"):
            HttpSink("http://x:8095", opener=假opener(抛=抛)).put(样例)


def test_地址解不开的话术先说本机配置_不把人往查服务器上带() -> None:
    """这一支罩的大多数其实是**我们这边**的问题:``base_url`` 的主机名过不了
    IDNA 编码、``sn`` 里塞了 CRLF —— 两条都跟服务器和跳转毫无关系。

    行为是对的(都翻成 ``SinkError`` 只退避),但话术以前只写"跳转目标?",
    现场照着去查服务器和跳转,查的是没坏的那一头。
    """
    opener = 假opener(抛=ValueError("Invalid IPv6 URL"))
    with pytest.raises(SinkError) as excinfo:
        HttpSink("http://x:8095", opener=opener).put(样例)
    话 = str(excinfo.value)
    assert "base_url" in 话 and "sn" in 话
    assert 话.index("base_url") < 话.index("跳转目标")
