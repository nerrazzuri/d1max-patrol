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

from d1max_agent.engine.uploader import HTTP_TIMEOUT_S, PutReceipt, PutRequest, SinkError
from d1max_contract.intake import REFUSED_STATUS

WIRE_PATH = "/api/intake/put"

#: **回执的长度上限。读到比这还长还没完,就判它不是一张回执。**
#:
#: 为什么是这个数:回执按设计只有四个字段 —— ``ok`` 一个布尔、``stored`` 一个
#: 字节数、``sha256`` 是 64 个十六进制字符、``message`` 是一句给人看的话。装满
#: 的一张回执几百字节,连 1 KiB 都够不着。64 KiB 给"网关往 message 里塞了一大段
#: 人话"留了上百倍余量,同时比"服务器报 Content-Length 是 100 GB"小一千六百万
#: 倍 —— 宽到不会误伤真回执,窄到绝不会照着服务器说的那个数去要内存。
#:
#: **这是字节上限,不是时间常量**(挂账 98 管的是时间常量),不进真机待验证清单。
回执上限字节 = 64 * 1024

#: ``_认字段`` 的"这个键必须有"标记。**不是缺省值** —— 它要是漏进了回执里,
#: 说明代码写错了,所以单独立一个谁都造不出来的对象当哨兵。
_必填 = object()


def build_body(req: PutRequest) -> bytes:
    """**原始字节,不做 base64。** base64 会把每一块涨三分之一,而上行带宽
    是这套系统最紧的东西(spec §4.3 整节讲的就是带宽不够时怎么让路)。
    """
    return req.data


def _headers(req: PutRequest, token: str) -> dict[str, str]:
    """元数据走 header,**不走 query string**。

    两个理由:``run``/``rel`` 里有中文(``巡检一``),塞进 URL 要转义两遍;
    而且 URL 会原样进服务器的访问日志,run 名和点位名不该躺在那儿。

    header 的值必须 latin-1 编得出来,所以 ``run``/``rel`` 用 ``quote`` 转义。
    ``sn`` **不转义**,原样进 header:它是装机时定死的设备编号(``D1MAX-01``
    那种 ASCII 串),服务器侧拿它当键直接比对,转义了两边就对不上。真被人填
    成中文时不会在这儿炸,而是在 ``open()`` 深处编 latin-1 时抛
    ``UnicodeEncodeError``,由 ``put()`` 翻成 ``SinkError`` 走退避重排 ——
    "装机时挡住中文 sn"是配置自检那一层的事(挂账 142),不在这个函数里。
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


def _读回执(读: Any) -> bytes:
    """**带上限地把 body 读回来。** 裁决二十五。

    服务器报 ``Content-Length: 100000000000`` 的时候,正确的做法不是接住
    ``MemoryError``,是**根本不去分配那 100 GB**。不带上限的 ``read()`` 会照着
    服务器说的那个数去要内存:``2**63`` 那一档在底层 ``fp.read()`` 里抛
    ``OverflowError``,``10**11``(100 GB)和 ``2**40`` 那一档抛 ``MemoryError``
    —— 后者是 ``Exception`` 的直接子类,``put()`` 那几支 except 一个都接不住。
    而 100 GB 这个数**不需要有人存心**,一个算错长度的服务器就发得出来。

    这是**裁决二十的同一个动作用在网络分支上**:不去枚举"它会抛什么",先判
    "它是不是我们认的形状"。读超过上限的那一段,本身就是"这不是我们认的东西"。

    ``read(n)`` 只保证**至多** n 字节(chunked 时一次往往只给一个分块),所以
    这儿循环着读,**总共最多要 ``上限 + 1`` 个字节**。多要的那一个字节就是
    "正好读满上限"和"读完了"的分界:读回来的长度 ``> 上限`` 说明后面还有东西,
    ``<= 上限`` 才可能是一张完整回执。少了这一个字节,一张正好 ``上限`` 字节的
    回执跟一坨没有尽头的东西长得一模一样,分不出来。

    **这一道是第一道,``put()`` 里翻 ``OverflowError``/``MemoryError`` 是第二道。**
    两道都要:``Content-Length >= 2**63`` 是在分配之前就抛的,跳转目标端口大得
    装不进 C 的 long 那条走的是 ``getaddrinfo``,两条都够不着这个上限。
    """
    片段: list[bytes] = []
    还能读 = 回执上限字节 + 1
    while 还能读 > 0:
        片 = 读(还能读)
        if not 片:
            break
        片段.append(片)
        还能读 -= len(片)
    body = b"".join(片段)
    if len(body) > 回执上限字节:
        raise SinkError(f"回执太长: 读到超过 {回执上限字节} 字节还没完, 这不是一张回执")
    return body


def _认字段(
    payload: dict[str, Any], 键: str, 形状: type, 缺省: Any, *, 非负: bool = False
) -> Any:
    """**放行条件,不是异常清单。**

    前四轮在这个文件里找出的泄漏,有四条是同一个形状:有人列了一张
    ``except (A, B)`` 的类型清单,清单漏了一个类型。最后一条尤其说明问题 ——
    ``int(payload.get("stored", 0))`` 补过一次 ``ValueError``/``TypeError``,
    可 ``json.loads`` 默认就认 ``Infinity``(``1e400``/``1e309`` 也解成 ``inf``),
    而 ``int(inf)`` 抛的是 ``OverflowError``,它的 MRO 是
    ``ArithmeticError -> Exception``,**那两个都接不住**。同一行 ``NaN``
    是绿的(``int(nan)`` 抛 ``ValueError``),写那行的人离 ``Infinity``
    只差一个字面量。

    只要判据是"我能想到几种异常",下一个人就总能想出第 N+1 种。所以这儿把
    判据**反过来**:先问"它是不是我们认的东西",是才用,不是就 ``SinkError``。
    漏判的方向从"炸穿上传线程"变成"多退避一轮" —— 规格 §4.2 本来就是
    "对不上就是没传成功",多退避一轮不丢证据,炸穿上传线程会丢。

    代价是认得窄:服务器要是发 ``{"stored": "123"}``,本来 ``int()`` 宽容转得出来,
    现在判成读不懂去退避。**这是故意选的**,宁可多退一轮,不留一条能炸穿的路。

    字段缺省时给 ``缺省``(老版本服务器不回 ``stored`` 是已知的兼容情况);
    字段在但形状不对,一律 ``SinkError``。

    ``缺省`` 传 ``_必填`` 表示**这个键必须有**:缺了就是读不懂,走 ``SinkError``。
    ``ok`` 走的是这条(裁决二十四)—— 给 ``ok`` 一个 ``False`` 缺省,等于把
    "它什么都没说"翻译成"它说了 false",而 ``ok=False`` 是会触发 rewind 的。

    ``非负`` 为真时再多问一句"值 >= 0"。形状对、值荒唐,一样是读不懂:``stored``
    是"服务器那边现在有多少字节",负数没有任何一种讲得通的意思,放进去下一轮会
    变成 ``fh.seek(-5)``,在离这儿很远的地方炸出一条指向别处的错。
    """
    值 = payload.get(键, 缺省)
    if 值 is _必填:
        raise SinkError(f"回执里没有 {键} 字段: 服务器没表态, 我们不替它把话说了")
    if isinstance(值, 形状) and not (形状 is int and isinstance(值, bool)):
        # ``bool`` 是 ``int`` 的子类 —— ``{"stored": true}`` 不该被当成 stored=1。
        if 非负 and 值 < 0:
            raise SinkError(f"回执里的 {键} 字段是负数: {值!r:.80}")
        return 值
    raise SinkError(f"回执里的 {键} 字段不是 {形状.__name__}: {type(值).__name__} {值!r:.80}")


def parse_receipt(status: int, body: bytes) -> PutReceipt:
    """读不懂 = ``SinkError``,读懂了且服务器说不行 = ``ok=False``。

    ``rewind`` 只能由"服务器明确说了话"触发 —— 那是"之前传的那些全不算数,
    从头再来"的信号,会把之前**已经确认过哈希对上**的进度一起作废。我们读
    不懂的一切,都只能退避(保留 ``item.offset``),不能 rewind。

    所以 ``PutReceipt(ok=False, ...)`` 只留给一种情况:回执是个结构完好的
    对象,里面服务器**明确**说了 ``ok`` 为假(包括 409 偏移对不上那种 ——
    那也是"结构完好、服务器明确表态"的一种)。

    剩下每一种"解不出服务器到底想说什么"的情况,一律 ``SinkError``:
    JSON 解不开、顶层不是对象、四个字段里**任何一个**形状不对。这些情况我们
    连"服务器那边到底存了多少/说了什么"都没有可信信息,跟"服务器明确说了话"
    是两件不同的事,不该走同一条会作废历史进度的路。

    **四个字段一个口径,都走 ``_认字段`` 的放行条件**(见那个函数的说明:
    枚举放行条件,不枚举异常类型):

    - ``ok``:必须是 ``bool``,**必须有这个键**(缺了就是读不懂)
    - ``stored``:必须是 ``int``(``bool`` 不算)且 ``>= 0``,缺省 ``0``
    - ``sha256``:必须是 ``str``,缺省 ``""``
    - ``message``:必须是 ``str``,缺省 ``""``

    ``ok`` 没有缺省(裁决二十四)。它曾经缺省 ``False``,于是一个 ``502`` 配上
    API 网关的标准错误体 ``{"message": "Internal server error"}`` 就能把已经确认
    的进度抹回 ``offset 0`` —— 那是把"它什么都没说"翻译成了"它说了 false",跟
    上面"只有服务器明确说了话才能 rewind"那条直接打架。``{}``、状态行是 999、
    畸形分帧同样走得通这条路。缺 ``ok`` 现在一律 ``SinkError``,只退避。

    **``ok=True`` 还额外要求 ``status`` 正好是 200 —— 这是上线形状的一部分。**
    服务器侧(``d1max_console.intake``)收下一块之后**必须回 200**:回 ``201``
    / ``204`` / ``206`` 哪怕 body 完美,在这儿也一律判成"这次没传成"。要放宽成
    ``200 <= status < 300`` 是**改协议**,得先改规格再改这儿,不许顺手放宽 ——
    已经装在客户那儿的狗跟服务器是照这条对齐的。
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
    ok = _认字段(payload, "ok", bool, _必填)
    return PutReceipt(
        ok=ok and status == 200,
        stored=_认字段(payload, "stored", int, 0, 非负=True),
        sha256=_认字段(payload, "sha256", str, ""),
        message=_认字段(payload, "message", str, ""),
        # W00c5d:站点明确说「永远不收」(422 且 ok 为假):隔离这个文件,不再重试。
        refused=(status == REFUSED_STATUS and not ok),
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
        都不解析 —— 但**带着长度上限读**(``_读回执``,裁决二十五):读多少是
        "认不认这个形状"的事,得在拿到字节之前就定下来,不能等分配完再后悔。

        这个方法**除了那一道上限之外不翻译任何异常**。它的 ``except HTTPError``
        块内部还要
        再读一次 body(``err.read()``,4xx/5xx 身上那份),而**同级的 except
        接不住在 except 块内部抛出来的东西** —— 读错误 body 被截断时的
        ``IncompleteRead``/``ConnectionResetError`` 曾经就是从这儿原样穿出
        ``put()`` 的。把翻译边界整个挪到调用方,这一次读跟 ``open()``、
        ``resp.read()`` 才走同一条出口,少一处"看着被罩住其实没有"的地方。
        """
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_S) as resp:
                return getattr(resp, "status", 200), _读回执(resp.read)
        except urllib.error.HTTPError as err:
            # urllib 把 4xx/5xx 抛成 HTTPError,但它身上带着 body —— 409 的
            # body 里有服务器说的 stored,那个数正是我们要的。当回执读,不当异常。
            #
            # 这一次读**也走同一道上限**:``Content-Length`` 报 100 GB 的 500
            # 跟报 100 GB 的 200 是一样的东西,没理由只护住其中一条。
            return err.code, _读回执(err.read)

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
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            HTTPException,
            OverflowError,
            MemoryError,
        ) as err:
            # **只翻这几种。** catch Exception 会把编码错误、路径错误一起
            # 吞成"网络不好",而 ruff 的 BLE 也不许那么写。
            #
            # OverflowError / MemoryError 是第二道(第一道是 ``_读回执`` 的长度
            # 上限,见那个函数)。**两个都不是 OSError**:OverflowError 的 MRO 是
            # ArithmeticError -> Exception,MemoryError 直接挂在 Exception 底下,
            # 上面那三种一个都接不住。够不着上限的两条真实入口:服务器回 302 且
            # Location 里的端口大得装不进 C 的 long(getaddrinfo 抛 OverflowError),
            # 以及 base_url 自己的端口就 >= 2**31 —— 后者**每一次调用都会抛**,
            # 配置一写错,上传从第一秒起就是死的,而值守屏还一直显示正常。
            #
            # HTTPException 单独列一支:BadStatusLine(网关回了一段不是合法
            # HTTP 状态行的东西)、IncompleteRead(读到的字节数比 Content-Length
            # 少)都是它的子类,**不是** OSError 的子类,不加这一支的话,服务器
            # 半路掐断连接、CPE 重拨截断响应这两种真实场景会带着原始异常把
            # Uploader.run_once 炸穿,而不是走退避重排。
            raise SinkError(f"发不出去: {type(err).__name__}: {err}") from err
        except ValueError as err:
            # 这一支罩的是"某个地址字符串解不开",**大多数时候根子在我们这边**:
            #
            # - ``base_url`` 的主机名过不了 IDNA 编码(配置里粘进了怪字符)
            # - ``sn`` 里塞了 CRLF,拼请求头时被判成非法
            # - 服务器回 302 且 ``Location`` 是畸形的(``http://[``),
            #   HTTPRedirectHandler 里 urlsplit 抛 ``ValueError: Invalid IPv6 URL``
            #
            # 前两条跟服务器、跟跳转都没关系。话术**先说本机配置**:这支以前只
            # 写"跳转目标?",现场照着去查服务器和跳转,查的是没坏的那一头。
            # 它既不是 OSError 也不是 HTTPException,上面那支接不住。
            #
            # **这一支必须排在 UnicodeEncodeError 后面**(那个是 ValueError 的
            # 子类,排前面会把"header 编不出 latin-1"的专用话术吞掉),也必须
            # 排在网络那支后面(``ssl.SSLCertVerificationError`` 同时是
            # ValueError 和 OSError 的子类,排前面会把"证书验不过"说成"地址不合法")。
            raise SinkError(
                f"地址解不开: 先查本机配置的 base_url/sn, 再看服务器回的跳转目标: {err}"
            ) from err
        return parse_receipt(status, body)
