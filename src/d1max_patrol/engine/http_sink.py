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
    """**非 200 不是异常,是"这次没成"。**

    抛出去会走 ``SinkError`` 那条退避,而 409(偏移对不上)要的是 rewind
    不是退避 —— 两条路不一样。所以状态码在这儿一律解析成 ``ok=False``,
    让 :meth:`Uploader.run_once` 按回执内容判。
    """
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # 中间挡了个网关,回了一页 HTML。这不该让上传器崩。
        return PutReceipt(ok=False, stored=0, sha256="", message="回执不是 JSON")
    if not isinstance(payload, dict):
        return PutReceipt(ok=False, stored=0, sha256="", message="回执不是 JSON")
    return PutReceipt(
        ok=bool(payload.get("ok")) and status == 200,
        stored=int(payload.get("stored", 0)),
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

    def put(self, req: PutRequest) -> PutReceipt:
        request = urllib.request.Request(
            self._base + WIRE_PATH,
            data=build_body(req),
            headers=_headers(req, self._token),
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_S) as resp:
                return parse_receipt(getattr(resp, "status", 200), resp.read())
        except urllib.error.HTTPError as err:
            # urllib 把 4xx/5xx 抛成 HTTPError,但它身上带着 body —— 409 的
            # body 里有服务器说的 stored,那个数正是我们要的。当回执读,不当异常。
            return parse_receipt(err.code, err.read() or b"")
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
