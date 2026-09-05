"""app 的鉴权:一个 PIN 换一个 token,之后每个请求带着 token 走。

**为什么必须有。** 机器狗热点的密码是 12345678,而且印在我们自己的手册里
—— 射程之内谁都能上这个网。而这些接口能让狗走起来,也能把变电站里拍的照片
整包拉走;后者是客户的资产,比机器本身更敏感。

**为什么 token 走请求头而不是 cookie。** cookie 是浏览器自动带上的:操作员
一边连着狗热点一边刷网页,随便哪个页面里的 JS 往 ``http://192.168.168.100``
发个请求,浏览器就替他把 cookie 附上了 —— 等于替他开狗。家用路由器就是这么
被攻破的。自定义请求头跨域要先过 CORS 预检,而我们一个来源都不放行,于是这
一整类攻击天然打不进来。

**为什么有几个接口破例认 ``?token=``。** ``<img src=...>``、``<a href=...>``
和 ``EventSource`` 是浏览器给的三个 API,它们都没有"设请求头"的地方。要么让
实时画面、照片、报告和事件流裸奔,要么让 token 走一次查询串。选后者,并且卡
死两条:**只认 GET,只认下面列出的那几条只读路径**。别处带 ``?token=`` 一律
不认 —— 这个口子一旦被当成通用后门用开,前面那套 CSRF 免疫就白做了。手机
app 那边没有这个限制,一律走请求头。

**为什么失败要限速。** 六位 PIN 只有一百万种。不限速的话几分钟就爆破完了,
那样的 PIN 是装饰,不是门。

**为什么 token 只在内存里。** 不落盘就不会泄漏。代价是狗一重启手机要重输一
次 PIN —— 五秒钟的事,换的是"这台机器上没有任何一处存着能开狗的凭证"。
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass

#: 换 token 的接口。它自己不能要 token,不然没人换得到。
AUTH_PATH = "/api/auth"

#: 只有这几条路径额外认 ``?token=``,而且只在 GET 上认。
#:
#: 每一条都对应一个"浏览器原生加载、设不了请求头"的地方:事件流是
#: ``EventSource``,实时画面和照片是 ``<img>``,报告是 ``<a href>``。
#: **想往这个表里加东西之前先问一句:那个接口会改东西吗?** 会的话不能加 ——
#: 查询串会进浏览器历史、进 Referer,而这个表里的东西最坏也只是被读到。
QUERY_TOKEN_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/events$"),
    re.compile(r"^/api/video/[^/]+$"),
    re.compile(r"^/api/runs/[^/]+/photos/.+$"),
    re.compile(r"^/api/runs/[^/]+/report\.[A-Za-z0-9]+$"),
)

#: PIN 最短几位。四位一万种,配上下面的锁定已经不好猜了;再短就没意义了。
MIN_PIN_LEN = 4

#: 连错几次开始锁。
MAX_FAILS = 5

#: 第一次锁多久。之后每错一次翻倍,封顶 ``MAX_LOCKOUT_S``。
LOCKOUT_S = 30.0
MAX_LOCKOUT_S = 900.0

#: token 有多少字节的熵。32 字节 = 256 位,猜不着。
TOKEN_BYTES = 32

#: 多久没用就作废。**是"闲置"不是"签发"** —— 正在干活的人不该被踢下线,
#: 而一台被顺走的手机上的 token 不该永远有效。
TOKEN_IDLE_S = 12 * 3600.0

#: 同时最多留几个 token。防的是"有人反复解锁把内存撑爆"。
MAX_TOKENS = 64

#: Host 头里允许出现的名字。别的一律只收 IP 字面量。
_LOCAL_NAMES = frozenset({"localhost"})


class Denied(Exception):
    """没放行。调用方负责把它变成 HTTP 响应。"""

    def __init__(self, status: int, error: str, detail: str = "") -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.detail = detail


# ------------------------------------------------------------------ token 库


@dataclass
class _Live:
    """一个还活着的 token。``last`` 是最近一次用它的时刻。"""

    last: float


class TokenStore:
    """内存里的 token 表。线程安全 —— HTTP 是一请求一线程的。"""

    def __init__(self, *, idle_s: float = TOKEN_IDLE_S,
                 cap: int = MAX_TOKENS) -> None:
        self._idle_s = idle_s
        self._cap = cap
        self._lock = threading.Lock()
        #: 插入序就是签发序,满了从头上淘汰。
        self._live: dict[str, _Live] = {}

    def issue(self, *, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            self._sweep(now)
            while len(self._live) >= self._cap:
                self._live.pop(next(iter(self._live)))
            self._live[token] = _Live(last=now)
        return token

    def valid(self, token: str, *, now: float | None = None) -> bool:
        """认一个 token,顺带续期。

        直接查字典而不是逐个 ``compare_digest``:token 是 256 位随机数,猜不
        中的东西不存在计时侧信道可利用的余地,而线性扫描会让 token 一多就慢。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(now)
            rec = self._live.get(token)
            if rec is None:
                return False
            rec.last = now
            return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._live.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._live.clear()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._live)

    def _sweep(self, now: float) -> None:
        """清掉闲置超时的。调用方已经拿着锁了。"""
        dead = [t for t, r in self._live.items() if now - r.last > self._idle_s]
        for t in dead:
            del self._live[t]


# ------------------------------------------------------------------ 限速


@dataclass
class _Fails:
    count: int = 0
    #: 锁到什么时候(monotonic)。0 表示没锁。
    until: float = 0.0


class Throttle:
    """按来源 IP 记 PIN 试错。连错就退避,退避时间翻倍。"""

    def __init__(self, *, max_fails: int = MAX_FAILS,
                 lockout_s: float = LOCKOUT_S,
                 max_lockout_s: float = MAX_LOCKOUT_S) -> None:
        self._max_fails = max_fails
        self._lockout_s = lockout_s
        self._max_lockout_s = max_lockout_s
        self._lock = threading.Lock()
        self._by_client: dict[str, _Fails] = {}

    def locked_for(self, client: str, *, now: float | None = None) -> float:
        """还要锁多少秒。0 表示没锁。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            rec = self._by_client.get(client)
            if rec is None or rec.until <= now:
                return 0.0
            return rec.until - now

    def fail(self, client: str, *, now: float | None = None) -> float:
        """记一次失败,返回接下来要锁多少秒(0 表示还没到锁的份上)。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            rec = self._by_client.setdefault(client, _Fails())
            rec.count += 1
            if rec.count < self._max_fails:
                return 0.0
            grow = 2.0 ** (rec.count - self._max_fails)
            wait = min(self._lockout_s * grow, self._max_lockout_s)
            rec.until = now + wait
            return wait

    def succeed(self, client: str) -> None:
        with self._lock:
            self._by_client.pop(client, None)


# ------------------------------------------------------------------ 闸门


def bearer(headers: Mapping[str, str]) -> str | None:
    """从 ``Authorization: Bearer xxx`` 里取出 token。

    **键名一律小写。** HTTP 头名是大小写不敏感的,而 curl 发 ``authorization``、
    浏览器发 ``Authorization`` 都合法 —— 在边界上统一成小写,比在每个取值的地
    方各写一遍"两种都试试"可靠。``server.py`` 负责做这次归一。
    """
    raw = headers.get("authorization", "")
    kind, _, value = raw.partition(" ")
    if kind.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def host_is_literal(raw: str) -> bool:
    """``Host`` 头是不是一个 IP 字面量(或 localhost)。

    防的是 DNS rebinding:把 ``evil.com`` 解析到 192.168.168.100,操作员的浏
    览器一访问,同源策略就把攻击者的脚本当成自己人了。我们期望的 Host 本来就
    只有 IP 和 localhost 两种,别的一律不收。
    """
    host = raw.strip()
    if not host:
        return True                     # HTTP/1.0 客户端可以不带 Host
    if host.startswith("["):            # IPv6 字面量写成 [::1]:8095
        end = host.find("]")
        if end < 0:
            return False
        host = host[1:end]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host.lower() in _LOCAL_NAMES:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def query_token_ok(method: str, path: str) -> bool:
    """这条路径能不能用 ``?token=``。只读 GET 才行。"""
    if method != "GET":
        return False
    return any(p.match(path) for p in QUERY_TOKEN_PATHS)


def normalize_pin(pin: str) -> str:
    """把 PIN 收拾干净,不合规就抛 ``ValueError``。

    两头的空白吃掉:从环境变量或文件里读出来的值常常带个换行,而"我明明输对了"
    这种问题在现场最难查。中间的空白反过来一律拒 —— 人自己都记不清输没输那一下。
    """
    pin = pin.strip()
    if len(pin) < MIN_PIN_LEN:
        raise ValueError(f"PIN 至少要 {MIN_PIN_LEN} 位,收到的是 {len(pin)} 位")
    if not pin.isprintable() or any(c.isspace() for c in pin):
        raise ValueError("PIN 里不能有空白或不可打印字符")
    return pin


class Guard:
    """鉴权闸门。``pin`` 给 ``None`` 就是不鉴权(只在本机听时才允许)。"""

    def __init__(self, pin: str | None = None, *,
                 tokens: TokenStore | None = None,
                 throttle: Throttle | None = None) -> None:
        self._pin = normalize_pin(pin) if pin is not None else None
        self.tokens = tokens if tokens is not None else TokenStore()
        self.throttle = throttle if throttle is not None else Throttle()

    @property
    def enabled(self) -> bool:
        return self._pin is not None

    # ---------------------------------------------------------------- 解锁

    def unlock(self, pin: object, client: str) -> str:
        """PIN 换 token。不对就抛 ``Denied``。"""
        if self._pin is None:
            raise Denied(400, "这台没设 PIN", "启动时没给 --pin,不需要解锁。")
        wait = self.throttle.locked_for(client)
        if wait > 0:
            raise Denied(429, "试得太多了,先等等",
                         f"还要等 {wait:.0f} 秒才能再试。")
        # 先比长度再比内容会漏长度;``compare_digest`` 对 str 要求两边都是
        # ASCII,所以统一编成 bytes 再比。
        want = self._pin.encode("utf-8")
        got = pin.encode("utf-8") if isinstance(pin, str) else b""
        if not secrets.compare_digest(want, got):
            wait = self.throttle.fail(client)
            detail = (f"连错太多次,锁 {wait:.0f} 秒。" if wait > 0
                      else "再试一次。")
            raise Denied(401, "PIN 不对", detail)
        self.throttle.succeed(client)
        return self.tokens.issue()

    # ---------------------------------------------------------------- 放行

    def gate(self, method: str, path: str, headers: Mapping[str, str],
             query: Mapping[str, str]) -> None:
        """放行就什么都不做,不放行就抛 ``Denied``。"""
        if not host_is_literal(headers.get("host", "")):
            raise Denied(403, "Host 头不对",
                         "只收 IP 地址,不收域名 —— 防的是 DNS rebinding。")
        if not self.enabled:
            return
        # 页面和静态资源不拦:人得先能把页面打开,才有地方输 PIN。
        # 里面没有任何秘密 —— 数据全在 /api/ 后面。
        if not path.startswith("/api/"):
            return
        if path == AUTH_PATH:
            return
        token = bearer(headers)
        if token is None and query_token_ok(method, path):
            token = query.get("token")
        if not token:
            raise Denied(401, "要先解锁", "在页面上输一次 PIN,或者用 app 连。")
        if not self.tokens.valid(token):
            raise Denied(401, "登录过期了,重新输一次 PIN",
                         "token 无效或太久没用。")
