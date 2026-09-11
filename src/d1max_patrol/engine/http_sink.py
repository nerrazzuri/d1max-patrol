"""把一块字节发到服务器上去。**上线形状定在这个文件里。**

服务器侧(``d1max_console.intake``)必须照着这儿实现,两边对不上就是传不成。
改这个文件等于改协议 —— 改之前先想清楚已经装在客户那儿的狗怎么办。

**只用标准库。** 不引 requests、不引 httpx:这台狗上的依赖每多一个,装机
脚本就多一次联网、uninstall.sh 的删除范围就多一块要盯的地方。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from http.client import HTTPException
from typing import Any
from urllib.parse import quote

from d1max_patrol.engine.uploader import HTTP_TIMEOUT_S, PutReceipt, PutRequest, SinkError

WIRE_PATH = "/api/intake/put"


def build_body(req: PutRequest) -> bytes:
    """**原始字节,不做 base64。** base64 会把每一块涨三分之一,而上行带宽
    是这套系统最紧的东西(spec §4.3 整节讲的就是带宽不够时怎么让路)。
    """
    return req.data


def _headers(req: PutRequest, token: str) -> dict[str, str]:
    """元数据走 header,**不走 query string**。

    两个理由:``sn``/``run``/``rel`` 里有中文(``巡检一``),塞进 URL 要转义
    两遍;而且 URL 会原样进服务器的访问日志,run 名和点位名不该躺在那儿。

    header 的值必须 latin-1 编得出来,所以 run/rel 用 ``quote`` 转义。
    """
    headers = {
        "Content-Type": "application/octet-stream",
        "X-D1Max-SN": req.sn,
        "X-D1Max-Run": quote(req.run),
        "X-D1Max-Rel": quote(req.rel),
        "X-D1Max-Offset": str(req.offset),
        "X-D1Max-Total": str(req.total),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_receipt(status: int, body: bytes) -> PutReceipt:
    """读不懂 = ``SinkError``,读懂了且服务器说不行 = ``ok=False``。

    ``rewind`` 只能由"服务器明确说了话"触发 —— 那是"之前传的那些全不算数,
    从头再来"的信号,会把之前**已经确认过哈希对上**的进度一起作废。我们读
    不懂的一切,都只能退避(保留 ``item.offset``),不能 rewind。

    所以 ``PutReceipt(ok=False, ...)`` 只留给一种情况:回执是个结构完好的
    对象,里面服务器**明确**说了 ``ok`` 为假(包括 409 偏移对不上那种 ——
    那也是"结构完好、服务器明确表态"的一种)。

    剩下每一种"解不出服务器到底想说什么"的情况,一律 ``SinkError``:
    JSON 解不开、顶层不是对象、``stored`` 字段类型不对(字符串、列表、
    ``None``……)。这些情况我们连"服务器那边到底存了多少/说了什么"都没有
    可信信息,跟"服务器明确说了话"是两件不同的事,不该走同一条会作废历史
    进度的路。
    """
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        # 中间挡了个网关,回了一页 HTML。我们读不懂,只能退避,不能 rewind。
        #
        # ``RecursionError`` 单列一支:回执要是一坨深度嵌套的 JSON(几千个
        # 连着的 ``[``,烂网关和压坏的缓存都干得出来),``json`` 的扫描器抛的
        # 是 ``RecursionError``,**它不是 ``ValueError``**,不列就原样穿出去。
        # 这一支只罩住 ``json.loads`` 这一句,罩不到我们自己写的递归。
        raise SinkError(f"回执不是 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        # json.loads 成功,但顶层不是对象(列表/null/裸字符串/裸数字)。
        # 同样是"读不懂服务器想说什么",不是"服务器明确说了话"。
        raise SinkError(f"回执不是 JSON 对象: {payload!r}")
    try:
        stored = int(payload.get("stored", 0))
    except (ValueError, TypeError) as exc:
        # 字段存在但类型不对(字符串、列表、``None``……)。同上,不能 rewind。
        raise SinkError(f"回执里的 stored 字段解析不出数字: {exc}") from exc
    return PutReceipt(
        ok=bool(payload.get("ok")) and status == 200,
        stored=stored,
        sha256=str(payload.get("sha256", "")),
        message=str(payload.get("message", "")),
    )


class HttpSink:
    """``UploadSink`` 的 HTTP 实现。

    ``ssl_context`` 是给证书钉扎(Task 14 / 挂账 67b)留的口子:钉扎版的
    ``SSLContext`` 从那儿传进来,这个类不需要知道钉扎这回事。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        ssl_context: ssl.SSLContext | None = None,
        opener: Any = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_context)
            if ssl_context is not None
            else urllib.request.HTTPSHandler()
        )

    def _build_request(self, req: PutRequest) -> urllib.request.Request:
        """拼出这一次要发的东西。**这一步不碰网络。**

        ``Request(...)`` 会在 base_url 压根没有 scheme(配置里写成
        ``console.example/api`` 少了 ``http://``)时抛 ``ValueError``。这三句
        以前摆在 ``try`` **外面**,抛出来是直接穿过 ``put()`` 的。
        """
        try:
            return urllib.request.Request(
                self._base + WIRE_PATH,
                data=build_body(req),
                headers=_headers(req, self._token),
                method="POST",
            )
        except ValueError as err:
            # 只罩住"URL 拼不出来"这一件事,话术也跟"网络不好"分开 —— 配错了
            # 地址跟线路抖是两码事,现场看日志得能一眼分出来。
            raise SinkError(f"请求拼不出来(base_url 配错了?): {err}") from err

    def _exchange(self, request: urllib.request.Request) -> tuple[int, bytes]:
        """**唯一一处真的碰网络的地方。** 把状态码和 body 原样带回来,一个字节
        都不解析。

        这个方法**故意不翻译任何异常**。它的 ``except HTTPError`` 块内部还要
        再读一次 body(``err.read()``,4xx/5xx 身上那份),而**同级的 except
        接不住在 except 块内部抛出来的东西** —— 读错误 body 被截断时的
        ``IncompleteRead``/``ConnectionResetError`` 曾经就是从这儿原样穿出
        ``put()`` 的。把翻译边界整个挪到调用方,这一次读跟 ``open()``、
        ``resp.read()`` 才走同一条出口,少一处"看着被罩住其实没有"的地方。
        """
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_S) as resp:
                return getattr(resp, "status", 200), resp.read()
        except urllib.error.HTTPError as err:
            # urllib 把 4xx/5xx 抛成 HTTPError,但它身上带着 body —— 409 的
            # body 里有服务器说的 stored,那个数正是我们要的。当回执读,不当异常。
            return err.code, err.read() or b""

    def put(self, req: PutRequest) -> PutReceipt:
        """出口只有两种:一张 ``PutReceipt``,或者一个 ``SinkError``。

        第三种都是 bug —— ``Uploader.run_once`` 只 ``except SinkError``,别的
        异常会带着上传线程往上走,规格 §4.3 的"传不上去就一直排着"当场破掉。
        ``tests/engine/test_http_sink_matrix.py`` 那张故障注入矩阵守的就是这条。

        ``parse_receipt`` 摆在 ``try`` **外面**是故意的:它只抛 ``SinkError``,
        摆进去会被下面那支重新包一遍,把"回执读不懂"说成"发不出去"。
        """
        try:
            status, body = self._exchange(self._build_request(req))
        except UnicodeEncodeError as err:
            # header 的值必须 latin-1 编得出来,编不出来是在 ``open()`` 里面
            # 炸的,而 ``UnicodeEncodeError`` **不是 OSError** —— 配置里 sn 或
            # 口令填了中文时,这条以前原样穿出去。翻成 SinkError:证据继续排
            # 着队重试,不会把上传线程炸掉。单独一支单独的话术,免得跟"网络
            # 不好"混成一句让人去查线路。
            raise SinkError(f"请求头里有 latin-1 编不出来的字符(sn/口令?): {err}") from err
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as err:
            # **只翻这几种。** catch Exception 会把编码错误、路径错误一起
            # 吞成"网络不好",而 ruff 的 BLE 也不许那么写。
            #
            # HTTPException 单独列一支:BadStatusLine(网关回了一段不是合法
            # HTTP 状态行的东西)、IncompleteRead(读到的字节数比 Content-Length
            # 少)都是它的子类,**不是** OSError 的子类,不加这一支的话,服务器
            # 半路掐断连接、CPE 重拨截断响应这两种真实场景会带着原始异常把
            # Uploader.run_once 炸穿,而不是走退避重排。
            raise SinkError(f"发不出去: {err}") from err
        return parse_receipt(status, body)
