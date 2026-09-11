"""故障注入矩阵:**``HttpSink.put()`` 的出口只有两种 —— 一张回执,或者一个 ``SinkError``。**

这个文件不是"又一条回归用例"。前面已经有过四轮"清点出一条漏的异常 -> 补一支
except",每一轮都只堵住被点名的那一条,下一轮又找出新的:

1. ``HTTPException`` 不是 ``OSError`` 的子类 —— ``BadStatusLine`` 穿出去。
2. ``parse_receipt`` 里 ``int(...)`` 的 ``ValueError``/``TypeError`` 穿出去。
3. ``err.read()`` 在 ``except HTTPError`` 块**内部**抛,同级的 except 接不住。
4. ``Request(...)``/header 编码在 ``try`` **外面**抛 ``ValueError``/``UnicodeEncodeError``。

缺的不是第五个补丁,是一个**能把这条契约变成会红的测试**的东西。这张表就是。

**它靠什么守住"绝不会是第三种"?** 靠**不去 catch**。每一行只写两种断言:
该抛的用 ``pytest.raises(SinkError)``,该回执的直接 ``isinstance(..., PutReceipt)``。
漏出来的异常要是 ``SinkError`` 之外的任何东西(``IncompleteRead``、
``UnicodeEncodeError``、``MemoryError``、我们自己写错的 ``TypeError``……),
``pytest.raises`` 罩不住它,它会原样穿出这一行,那一行当场变红。写一个
``except Exception`` 去分类反而会漏 —— 那样只挡得住 ``Exception`` 那一支,
而且撞 ruff 的 BLE。

**加一种故障模式的成本是往 ``矩阵`` 里加一行**,不是再写一个测试函数。
每一行必须带一句 ``现场`` —— 说清它模拟的是客户那边的哪件事。
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from d1max_patrol.engine.http_sink import HttpSink
from d1max_patrol.engine.uploader import PutReceipt, PutRequest, SinkError

_地址 = "http://console.example:8095"

样例 = PutRequest(
    sn="D1MAX-TEST-01",
    run="巡检一/20260911T101500Z",
    rel="photos/P1__front__20260911T101500Z.jpg",
    offset=7,
    data=b"\xff\xd8hello",
    total=99,
)

中文sn的样例 = PutRequest(
    sn="狗一号",
    run=样例.run,
    rel=样例.rel,
    offset=样例.offset,
    data=样例.data,
    total=样例.total,
)


# ---------------------------------------------------------------- 注故障的家伙什


class _抛的opener:
    """``open()`` 当场抛 —— 连不上/超时/DNS/状态行不合法那一批走这条。"""

    def __init__(self, 抛: BaseException) -> None:
        self._抛 = 抛

    def open(self, request: Any, timeout: Any = None) -> Any:
        raise self._抛


class _响应:
    """``open()`` 顺利返回的响应。``读时抛`` 非空就是"读 body 读到一半断了"。

    ``read`` 照 ``http.client.HTTPResponse.read(amt=None)`` 的签名收参数:生产
    代码现在**带着长度上限读**(裁决二十五),``read()`` 一定会被塞一个字节数
    进去,而且要一次次读到空为止。替身不照这个契约办事,就不是在模拟真响应,
    而是在替生产代码规定一条真机上不存在的契约 —— 那种绿是假的。
    """

    def __init__(self, status: int = 200, body: bytes = b"{}", 读时抛: BaseException | None = None):
        self.status, self._body, self._读时抛 = status, body, 读时抛
        self._给到 = 0

    def read(self, 上限: int | None = None) -> bytes:
        if self._读时抛 is not None:
            raise self._读时抛
        剩 = self._body[self._给到 :]
        片 = 剩 if 上限 is None else 剩[:上限]
        self._给到 += len(片)
        return 片

    def __enter__(self) -> _响应:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _回响应的opener:
    def __init__(self, resp: Any) -> None:
        self._resp = resp

    def open(self, request: Any, timeout: Any = None) -> Any:
        return self._resp


class _照真机编header的opener(_回响应的opener):
    """照 ``http.client`` 的做法把每个 header 值 latin-1 编一遍再放行。

    真机上 header 值编不出来是在 ``open()`` 深处炸的(``UnicodeEncodeError``,
    **不是 OSError**)。假 opener 默认压根不编码,不模拟这一步的话"中文口令"
    那几行会永远绿 —— 绿得毫无意义。
    """

    def open(self, request: Any, timeout: Any = None) -> Any:
        for _, 值 in request.header_items():
            str(值).encode("latin-1")
        return super().open(request, timeout)


def _HTTPError(
    code: int, body: bytes | None = None, 读时抛: BaseException | None = None
) -> urllib.error.HTTPError:
    """造一个 urllib 那样的 4xx/5xx 异常。

    ``读时抛`` 是第 3 条洞的形状:``err.read()`` 是在 ``except HTTPError``
    块**内部**调的,它抛出来的东西同级的 except 接不住。
    """
    err = urllib.error.HTTPError(_地址 + "/api/intake/put", code, "x", {}, None)
    if 读时抛 is not None:

        def _读(上限: int | None = None) -> bytes:
            raise 读时抛

        err.read = _读
    elif body is not None:
        给到 = [0]

        def _按上限给(上限: int | None = None, _b: bytes = body) -> bytes:
            起 = 给到[0]
            片 = _b[起:] if 上限 is None else _b[起 : 起 + 上限]
            给到[0] += len(片)
            return 片

        err.read = _按上限给
    return err


def _没有尽头地给(上限: int | None = None) -> bytes:
    """要多少给多少,永远给得出来 —— 服务器 ``Content-Length`` 报 100 GB 的形状。

    真机上不带上限地读这种响应,是在底层 ``fp.read(那个数)`` 那一步抛
    ``MemoryError``(``10**11``/``2**40`` 档)或 ``OverflowError``(``2**63`` 档)。
    这个替身把同一件事做成"你要多少给多少",好让**上限那一道本身**可测:生产
    代码只肯要 ``上限 + 1`` 个字节,拿到 ``上限 + 1`` 就该判"这不是一张回执"。
    不带上限地读它(``上限 is None``)才走 ``MemoryError`` 那条真机形状。
    """
    if 上限 is None:
        raise MemoryError
    return b"a" * 上限


class _没有尽头的响应:
    """``open()`` 顺利返回,但 body 没有尽头。"""

    status = 200
    read = staticmethod(_没有尽头地给)

    def __enter__(self) -> _没有尽头的响应:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _没有尽头的HTTPError(code: int) -> urllib.error.HTTPError:
    """4xx/5xx 身上那份 body 也没有尽头 —— ``err.read()`` 走的是同一道上限。"""
    err = urllib.error.HTTPError(_地址 + "/api/intake/put", code, "x", {}, None)
    err.read = _没有尽头地给
    return err


def _json(payload: Any) -> bytes:
    return json.dumps(payload).encode()


def _正常body(stored: int = 12) -> bytes:
    return _json({"ok": True, "stored": stored, "sha256": "d" * 64, "message": ""})


# ---------------------------------------------------------------------- 矩阵本体


@dataclass(frozen=True)
class 故障:
    """一行 = 一种现场会发生的故障 + 它该从哪个出口出来。"""

    名字: str
    现场: str
    """这一行模拟客户那边的哪件事。**不许空** —— 说不出现场的故障模式不该进表。"""
    装opener: Callable[[], Any]
    预期: str
    """``"回执"`` 或 ``"SinkError"``。没有第三种,第三种就是这张表要抓的东西。"""
    信息含: str = ""
    """非空时断言 ``SinkError`` 的话术里有这一段 —— 钉住"哪一支翻的"。"""
    期望: PutReceipt | None = None
    """非空时断言回执逐字段相等。"""
    base: str = _地址
    token: str = ""
    请求: PutRequest = field(default=样例)


矩阵: tuple[故障, ...] = (
    # ---- 一、连不上:open() 当场抛 ------------------------------------------
    故障(
        名字="连接被拒",
        现场="控制台那台机器上的服务没起来,或者端口没放行",
        装opener=lambda: _抛的opener(
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        ),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="超时",
        现场="狗在地下车库,4G 信号弱,30 秒内没等到回应",
        装opener=lambda: _抛的opener(TimeoutError("timed out")),
        预期="SinkError",
    ),
    故障(
        名字="DNS解不出来",
        现场="客户内网换了 DNS,控制台的域名解不出来了",
        装opener=lambda: _抛的opener(
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        ),
        预期="SinkError",
    ),
    故障(
        名字="连接被RST",
        现场="服务器进程被运维 kill,或者防火墙把这条连接掐了",
        装opener=lambda: _抛的opener(ConnectionResetError(104, "Connection reset by peer")),
        预期="SinkError",
    ),
    故障(
        名字="状态行不是HTTP",
        现场="客户网络里挡了个透明代理/认证门户,回了一段不是 HTTP 的东西",
        装opener=lambda: _抛的opener(http.client.BadStatusLine("\x16\x03\x01garbage")),
        预期="SinkError",
    ),
    故障(
        名字="响应头超长",
        现场="中间设备往回塞了一堆超长的头,读头这一步就崩了",
        装opener=lambda: _抛的opener(http.client.LineTooLong("header line")),
        预期="SinkError",
    ),
    故障(
        名字="TLS握手失败",
        现场="https 的口对着 http 的服务发,或者中间设备劫持了 TLS",
        装opener=lambda: _抛的opener(ssl.SSLError(1, "wrong version number")),
        预期="SinkError",
    ),
    故障(
        名字="证书验不过",
        现场="Task 14 钉扎上之后证书换过了没同步,或者客户装了中间人根证书",
        装opener=lambda: _抛的opener(ssl.SSLCertVerificationError(1, "certificate verify failed")),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="TLS没说再见就断了",
        现场="中间设备把 TLS 连接直接掐了,没走 close_notify —— SSLEOFError 是 SSLError 的子类",
        装opener=lambda: _抛的opener(ssl.SSLEOFError("EOF occurred in violation of protocol")),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="服务器没回话就断开",
        现场="keep-alive 的连接被服务器那边先回收了,urllib 复用时读到空 —— RemoteDisconnected",
        装opener=lambda: _抛的opener(http.client.RemoteDisconnected("Remote end closed")),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="系统调用被信号打断",
        现场="狗上有别的进程发了信号,socket 的系统调用被 EINTR 打断",
        装opener=lambda: _抛的opener(InterruptedError(4, "Interrupted system call")),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="跳转目标是畸形地址",
        现场=(
            "服务器回 302 且 Location 写成了 http://[ —— urllib 自己的 HTTPRedirectHandler "
            "里 urlsplit 抛 ValueError(Invalid IPv6 URL),它既不是 OSError 也不是 HTTPException"
        ),
        装opener=lambda: _抛的opener(ValueError("Invalid IPv6 URL")),
        预期="SinkError",
        信息含="跳转目标",
    ),
    # ---- 二、请求根本拼不出来 / header 编不出来 -----------------------------
    故障(
        名字="base_url漏了scheme",
        现场="配置里控制台地址写成了 console.example:8095 那样,少了 http://",
        装opener=lambda: _回响应的opener(_响应(body=_正常body())),
        预期="SinkError",
        信息含="请求拼不出来",
        base="console.example",
    ),
    故障(
        名字="口令是中文编不成latin1",
        现场="配置里的口令粘成了中文,header 值编不出 latin-1",
        装opener=lambda: _照真机编header的opener(_响应(body=_正常body())),
        预期="SinkError",
        信息含="latin-1",
        token="口令中文",
    ),
    故障(
        名字="sn是中文编不成latin1",
        现场="装机脚本把中文设备名写进了 sn,X-D1Max-SN 这个头不转义",
        装opener=lambda: _照真机编header的opener(_响应(body=_正常body())),
        预期="SinkError",
        信息含="latin-1",
        请求=中文sn的样例,
    ),
    # ---- 三、连上了,读 body 读到一半断了 -----------------------------------
    故障(
        名字="读body时被截断",
        现场="CPE 重拨,响应读到一半连接断了(收到的字节比 Content-Length 少)",
        装opener=lambda: _回响应的opener(_响应(读时抛=http.client.IncompleteRead(b"ab", 3))),
        预期="SinkError",
    ),
    故障(
        名字="读body时被RST",
        现场="服务器回完头之后崩了,body 还没发完连接就被 RST",
        装opener=lambda: _回响应的opener(_响应(读时抛=ConnectionResetError(104, "reset"))),
        预期="SinkError",
    ),
    故障(
        名字="读body时超时",
        现场="头回来了,body 卡住不发,读到超时",
        装opener=lambda: _回响应的opener(_响应(读时抛=TimeoutError("read timed out"))),
        预期="SinkError",
    ),
    # ---- 四、4xx/5xx:body 当回执读,读 body 那一步也会炸 --------------------
    故障(
        名字="409偏移对不上_body完好",
        现场="服务器换过盘/回滚过备份,它那边比我们以为的少 —— 这是服务器明确说了话",
        装opener=lambda: _抛的opener(
            _HTTPError(
                409,
                _json({"ok": False, "stored": 3, "sha256": "", "message": "偏移对不上"}),
            )
        ),
        预期="回执",
        期望=PutReceipt(ok=False, stored=3, sha256="", message="偏移对不上"),
    ),
    故障(
        名字="401口令过期_body完好",
        现场="口令轮换过没同步到狗上,服务器回 401 并带一句人话",
        装opener=lambda: _抛的opener(
            _HTTPError(401, _json({"ok": False, "stored": 0, "sha256": "", "message": "口令不对"}))
        ),
        预期="回执",
        期望=PutReceipt(ok=False, stored=0, sha256="", message="口令不对"),
    ),
    故障(
        名字="502网关回HTML",
        现场="反向代理挡在前面,后端没起来,回了一页 HTML 错误页",
        装opener=lambda: _抛的opener(_HTTPError(502, b"<html>502 Bad Gateway</html>")),
        预期="SinkError",
        信息含="回执不是 JSON",
    ),
    故障(
        名字="500且读body时被截断",
        现场="服务器一边回 500 一边被掐断连接 —— err.read() 在 except 块内部抛,同级 except 接不住",
        装opener=lambda: _抛的opener(_HTTPError(500, 读时抛=http.client.IncompleteRead(b"ab", 3))),
        预期="SinkError",
    ),
    故障(
        名字="503且读body时被RST",
        现场="服务器在回 503 的过程中进程没了",
        装opener=lambda: _抛的opener(_HTTPError(503, 读时抛=ConnectionResetError(104, "reset"))),
        预期="SinkError",
    ),
    故障(
        名字="504且读body时超时",
        现场="网关回了 504,body 却一直不来",
        装opener=lambda: _抛的opener(_HTTPError(504, 读时抛=TimeoutError("read timed out"))),
        预期="SinkError",
    ),
    故障(
        名字="502空body",
        现场="网关只回了个状态码,body 一个字节都没有",
        装opener=lambda: _抛的opener(_HTTPError(502, b"")),
        预期="SinkError",
        信息含="回执不是 JSON",
    ),
    # ---- 五、200 回来了,但回执读不懂 ---------------------------------------
    故障(
        名字="回执是空body",
        现场="服务器回完 200 的头就退了,body 是空的",
        装opener=lambda: _回响应的opener(_响应(body=b"")),
        预期="SinkError",
        信息含="回执不是 JSON",
    ),
    故障(
        名字="回执是HTML",
        现场="客户网络里的认证门户回了一页登录页,状态码还是 200",
        装opener=lambda: _回响应的opener(_响应(body=b"<html>login</html>")),
        预期="SinkError",
        信息含="回执不是 JSON",
    ),
    故障(
        名字="回执不是UTF8",
        现场="老网关的错误页是 GBK 编的,decode 就炸",
        装opener=lambda: _回响应的opener(_响应(body=b"\xff\xfe\x00\xd6\xd0")),
        预期="SinkError",
        信息含="回执不是 JSON",
    ),
    故障(
        名字="回执JSON嵌套太深",
        现场="压坏的缓存回了一坨畸形的深嵌套 JSON,json 扫描器抛的是 RecursionError 不是 ValueError",
        装opener=lambda: _回响应的opener(_响应(body=b"[" * 2000)),
        预期="SinkError",
        信息含="回执不是 JSON",
    ),
    故障(
        名字="回执是JSON数组",
        现场="服务器侧改接口时把回执从对象改成了数组,两边形状漂了",
        装opener=lambda: _回响应的opener(_响应(body=b"[1, 2, 3]")),
        预期="SinkError",
        信息含="回执不是 JSON 对象",
    ),
    故障(
        名字="回执是JSON的null",
        现场="服务器那边某个分支忘了填回执,序列化出来是 null",
        装opener=lambda: _回响应的opener(_响应(body=b"null")),
        预期="SinkError",
        信息含="回执不是 JSON 对象",
    ),
    故障(
        名字="回执是裸字符串",
        现场="服务器直接回了一句话并且 JSON 编了一遍",
        装opener=lambda: _回响应的opener(_响应(body=b'"ok"')),
        预期="SinkError",
        信息含="回执不是 JSON 对象",
    ),
    故障(
        名字="回执是裸数字",
        现场="服务器只回了个数字(比如落盘字节数),没包成对象",
        装opener=lambda: _回响应的opener(_响应(body=b"3")),
        预期="SinkError",
        信息含="回执不是 JSON 对象",
    ),
    故障(
        名字="stored是一句人话",
        现场="服务器出错时把 stored 字段填成了错误描述",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": "abc", "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是列表",
        现场="服务器把每一块的落盘长度攒成了列表回过来",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": [1, 2], "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是null",
        现场="服务器这次压根没落盘,stored 填成了 null",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": None, "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是对象",
        现场="服务器把 stored 写成了一个带单位的结构体",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": {"bytes": 3}, "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是NaN",
        现场="服务器用了个会把 NaN 写进 JSON 的序列化器,int(nan) 抛 ValueError",
        装opener=lambda: _回响应的opener(
            _响应(body=b'{"ok": true, "stored": NaN, "sha256": "x", "message": ""}')
        ),
        预期="SinkError",
        信息含="stored",
    ),
    # ---- 五之二、stored 是 inf:NaN 那行的亲兄弟,int(inf) 抛的是 OverflowError -
    #
    # ``OverflowError`` 的 MRO 是 ``ArithmeticError -> Exception``,老代码那张
    # ``except (ValueError, TypeError)`` 的清单**两个都接不住**。服务器侧同样是
    # Python,``json.dumps(float("inf"))`` 默认就吐 ``Infinity``,而
    # ``json.loads`` 默认就认 ``Infinity``,``1e400``/``1e309`` 也解成 ``inf`` ——
    # 这三个字面量是三条独立的进门路径,一行一条,不合并。
    故障(
        名字="stored是Infinity",
        现场="服务器侧也是 Python,某个分支算出了 inf,json.dumps 默认就吐 Infinity",
        装opener=lambda: _回响应的opener(
            _响应(body=b'{"ok": true, "stored": Infinity, "sha256": "x", "message": ""}')
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是负Infinity",
        现场="服务器拿两个数相减算 stored,结果溢出成了 -inf",
        装opener=lambda: _回响应的opener(
            _响应(body=b'{"ok": true, "stored": -Infinity, "sha256": "x", "message": ""}')
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是1e400",
        现场="回执里是个十进制字面量,json.loads 自己解成 inf —— 不写 Infinity 也能进门",
        装opener=lambda: _回响应的opener(
            _响应(body=b'{"ok": true, "stored": 1e400, "sha256": "x", "message": ""}')
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="409的body里stored是Infinity",
        现场="同一个洞在 err.read() 那条路径上也得堵住 —— 4xx 的 body 一样要过 parse_receipt",
        装opener=lambda: _抛的opener(_HTTPError(409, b'{"ok": false, "stored": 1e400}')),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是小数",
        现场="服务器把落盘字节数除了个 1024 才回过来,变成了 3.5",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": 3.5, "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是bool的true",
        现场="服务器把 stored 写成了存没存下的布尔 —— bool 是 int 的子类,不拦就悄悄变成 stored=1",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": True, "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="stored",
    ),
    故障(
        名字="stored是超长数字串",
        现场="服务器把 stored 序列化成了五千位的数字字符串,int() 在 py3.11+ 还会撞整数转换上限",
        装opener=lambda: _回响应的opener(
            _响应(body=('{"ok": true, "stored": "' + "1" * 5000 + '"}').encode())
        ),
        预期="SinkError",
        信息含="stored",
    ),
    # ---- 五之三、另外三个字段的放行条件 --------------------------------------
    #
    # ``stored`` 之外的三个字段过去走 ``bool(...)``/``str(...)``,对什么都不抛 ——
    # 于是"服务器发来一坨我们不认识的东西"被悄悄塞进回执当真话用:``{"ok": "no"}``
    # 会变成 ``ok=True``(非空字符串为真),``{"sha256": 123}`` 会变成
    # ``sha256="123"`` 去跟真哈希比。跟 stored 一个口径:认得的形状才用。
    故障(
        名字="ok是字符串yes",
        现场="服务器侧把 ok 写成了字符串 —— bool 判非空串为真,不拦就把没传成读成传成了",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": "yes", "stored": 4, "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="ok",
    ),
    故障(
        名字="ok是null",
        现场="服务器某个分支忘了填 ok,序列化出来是 null —— 那是没表态,不是表态说不行",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": None, "stored": 4, "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="ok",
    ),
    故障(
        名字="sha256是数字",
        现场="服务器把哈希当数字回了过来,str() 罩得住但拿去跟真哈希比一定不等,白跑一轮",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": 4, "sha256": 123, "message": ""}))
        ),
        预期="SinkError",
        信息含="sha256",
    ),
    故障(
        名字="message是对象",
        现场="服务器把 message 写成了结构化的错误对象,str() 出来是一坨 Python 字面量进日志",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": 4, "sha256": "x", "message": {"a": 1}}))
        ),
        预期="SinkError",
        信息含="message",
    ),
    故障(
        名字="回执是超深嵌套的JSON对象",
        现场="压坏的缓存回了两千层嵌套的对象 —— 顶层是 dict,深度在 json 扫描器里就崩了",
        装opener=lambda: _回响应的opener(_响应(body=(b'{"a":' * 2000) + b"1" + (b"}" * 2000))),
        预期="SinkError",
        信息含="回执不是 JSON",
    ),
    # ---- 五之四、回执大得离谱:上限是第一道,翻译是第二道(裁决二十五)-------
    #
    # 复评在**真 http.server + 真默认 opener** 上实测到这几条:不带上限地读,
    # 就是照着服务器说的那个数去要内存。``2**63`` 档在底层 ``fp.read()`` 里抛
    # ``OverflowError``(MRO 是 ArithmeticError -> Exception),``10**11``(100 GB)
    # 和 ``2**40`` 档抛 ``MemoryError``(``Exception`` 的直接子类)—— 老代码那三
    # 支 except 一个都接不住。而 100 GB 这个数**不需要有人存心**,一个算错长度
    # 的服务器就发得出来。
    故障(
        名字="回执长得没有尽头",
        现场=(
            "服务器 Content-Length 报了 100 GB(算错长度的服务器就干得出来)—— "
            "正确的做法不是接住 MemoryError,是根本不去分配那 100 GB"
        ),
        装opener=lambda: _回响应的opener(_没有尽头的响应()),
        预期="SinkError",
        信息含="回执太长",
    ),
    故障(
        名字="500的body也长得没有尽头",
        现场="4xx/5xx 身上那份 body 走的是 err.read(),报 100 GB 的 500 跟报 100 GB 的 200 是一回事",
        装opener=lambda: _抛的opener(_没有尽头的HTTPError(500)),
        预期="SinkError",
        信息含="回执太长",
    ),
    故障(
        名字="回执ContentLength报2的63次方",
        现场=(
            "Content-Length >= 2**63,读 body 时底层 fp.read() 抛 OverflowError —— "
            "它在分配之前就抛,够不着长度上限那一道,必须翻"
        ),
        装opener=lambda: _回响应的opener(
            _响应(读时抛=OverflowError("cannot fit 'int' into an index-sized integer"))
        ),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="回执ContentLength报100GB时读body抛MemoryError",
        现场="Content-Length 是 10**11,读 body 那一步抛 MemoryError —— 它是 Exception 的直接子类",
        装opener=lambda: _回响应的opener(_响应(读时抛=MemoryError())),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="500的body读到一半抛MemoryError",
        现场=(
            "500 的 Content-Length 也报了 100 GB —— err.read() 是在 except 块**内部**"
            "调的,同级的 except 接不住"
        ),
        装opener=lambda: _抛的opener(_HTTPError(500, 读时抛=MemoryError())),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="chunked分块长度是畸形的超大十六进制",
        现场="Transfer-Encoding: chunked 且分块长度写成二十个 f,读 body 时抛 OverflowError",
        装opener=lambda: _回响应的opener(
            _响应(读时抛=OverflowError("cannot fit 'int' into an index-sized integer"))
        ),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="跳转目标的端口大得装不进C的long",
        现场=(
            "服务器回 302 且 Location 是 http://127.0.0.1:99999999999999999999/x —— "
            "getaddrinfo 抛 OverflowError,这条走不到读 body,上限那一道够不着"
        ),
        装opener=lambda: _抛的opener(OverflowError("Python int too large to convert to C long")),
        预期="SinkError",
        信息含="发不出去",
    ),
    故障(
        名字="base_url的端口大得装不进C的long",
        现场=(
            "配置里把控制台端口写成了 99999999999999999999 —— getaddrinfo 抛 OverflowError,"
            "而且**每一次调用都抛**:配错这一个数,上传从第一秒起就是死的"
        ),
        装opener=lambda: _抛的opener(OverflowError("Python int too large to convert to C long")),
        预期="SinkError",
        信息含="发不出去",
        base="http://127.0.0.1:99999999999999999999",
    ),
    # ---- 五之五、ok 没有缺省了(裁决二十四)----------------------------------
    #
    # ``ok`` 曾经缺省 ``False``,等于把"它什么都没说"翻译成"它说了 false" ——
    # 而 ``ok=False`` 是会让上传器 rewind 的。复评端到端量过:一个 502 配上 API
    # 网关的标准错误体,能把 2 MiB 已确认进度抹回 offset 0。
    故障(
        名字="502网关标准错误体里没有ok字段",
        现场="API 网关回的标准错误体只有 message —— 那是它什么都没说,不是它说了 ok=false",
        装opener=lambda: _抛的opener(_HTTPError(502, _json({"message": "Internal server error"}))),
        预期="SinkError",
        信息含="ok",
    ),
    故障(
        名字="回执是空的JSON对象",
        现场="服务器某个分支回了个 {} —— 结构是完好的,但它一个字都没说",
        装opener=lambda: _回响应的opener(_响应(body=b"{}")),
        预期="SinkError",
        信息含="ok",
    ),
    # ---- 五之六、stored 是负数:形状对、值荒唐 -------------------------------
    #
    # 复评端到端量过:``{"stored": -5}`` 写进队列,下一轮 ``fh.seek(-5)`` 抛
    # ``OSError``,此后永久 defer。backlog 保持 1 不丢证据,但 defer 的文案说的是
    # "run 目录还在但这个文件不见了" —— 文件明明在,会把现场排查带偏。
    故障(
        名字="stored是负数",
        现场="服务器拿两个数相减算 stored,结果成了负的 —— isinstance 是 int,值没有任何讲得通的意思",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": True, "stored": -5, "sha256": "x", "message": ""}))
        ),
        预期="SinkError",
        信息含="stored",
    ),
    # ---- 六、对照组:该给回执的就得给回执 -----------------------------------
    故障(
        名字="一切正常",
        现场="网络好、服务器正常收下这一块 —— 少了这一行,矩阵全绿也可能只是因为每一行都抛",
        装opener=lambda: _回响应的opener(_响应(body=_正常body(12))),
        预期="回执",
        期望=PutReceipt(ok=True, stored=12, sha256="d" * 64, message=""),
    ),
    故障(
        名字="200但服务器明确说ok为假",
        现场="服务器盘满了,结构完好地回了一句 ok=false —— 这是明确表态,该让上传器 rewind",
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": False, "stored": 5, "sha256": "e" * 64, "message": "盘满了"}))
        ),
        预期="回执",
        期望=PutReceipt(ok=False, stored=5, sha256="e" * 64, message="盘满了"),
    ),
    故障(
        名字="ok和sha256和message类型歪但stored是对的",
        现场=(
            "服务器侧字段类型漂了,stored 还是个数 —— 修复轮 5 之前这一行是放行的"
            "(bool()/str() 什么都罩得住),于是 ok=yes 被读成传成了、sha256=None 被读成 "
            "字符串 None 拿去跟真哈希比。现在四个字段一个口径:认不得就退避"
        ),
        装opener=lambda: _回响应的opener(
            _响应(body=_json({"ok": "yes", "stored": 4, "sha256": None, "message": 123}))
        ),
        预期="SinkError",
        信息含="ok",
    ),
    故障(
        名字="回执里没有stored字段",
        现场="老版本服务器不回 stored —— 缺字段按 0 算,交给上传器拿哈希去判",
        装opener=lambda: _回响应的opener(_响应(body=_json({"ok": True, "sha256": "f" * 64}))),
        预期="回执",
        期望=PutReceipt(ok=True, stored=0, sha256="f" * 64, message=""),
    ),
)


# ------------------------------------------------------------------------ 断言


@pytest.mark.parametrize("案例", 矩阵, ids=[c.名字 for c in 矩阵])
def test_故障注入矩阵_put的出口只有回执和SinkError(案例: 故障) -> None:
    """每一种故障都跑一遍**完整的** ``HttpSink.put()``,断言出口只有那两种。

    **第三种是靠"不 catch"抓的**:``SinkError`` 之外的任何异常
    (``IncompleteRead``/``UnicodeEncodeError``/``MemoryError``/我们自己写错的
    ``TypeError``……)``pytest.raises(SinkError)`` 都罩不住,会原样穿出这一行,
    这一行当场变红。这正是 ``Uploader.run_once``(只 ``except SinkError``)在
    真机上会遇到的事 —— 穿出去的不是"这一块没传成",是上传线程带着异常往上走,
    规格 §4.3 的"传不上去就一直排着,绝不悄悄丢证据"当场破掉。
    """
    sink = HttpSink(案例.base, token=案例.token, opener=案例.装opener())
    if 案例.预期 == "回执":
        回执 = sink.put(案例.请求)
        assert isinstance(回执, PutReceipt)
        if 案例.期望 is not None:
            assert 回执 == 案例.期望
        return
    with pytest.raises(SinkError) as excinfo:
        sink.put(案例.请求)
    if 案例.信息含:
        assert 案例.信息含 in str(excinfo.value)


def test_矩阵本身是齐的() -> None:
    """守表的规矩,免得以后加行的人少填一格就悄悄退化成一行空跑的用例。"""
    assert len(矩阵) >= 65, "故障模式只许加不许减"
    名字 = [c.名字 for c in 矩阵]
    assert len(set(名字)) == len(名字), "行名重了,报错时分不清是哪一行"
    for c in 矩阵:
        assert c.预期 in ("回执", "SinkError"), f"{c.名字}: 出口只有这两种"
        assert c.现场.strip(), f"{c.名字}: 说不出现场的故障模式不该进表"
    assert any(c.预期 == "回执" for c in 矩阵), "得有对照组,否则全抛也算全绿"
    assert sum(c.预期 == "SinkError" for c in 矩阵) >= 60
