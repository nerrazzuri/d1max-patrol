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

import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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

#: ``Throttle`` 最多同时记多少个来源 IP。内存兜底 —— 见 ``Throttle`` 的
#: docstring:淘汰分两档,第一档优先扔没锁着的,扔完还超才动锁着的兜底。
MAX_THROTTLE_CLIENTS = 512

#: token 有多少字节的熵。32 字节 = 256 位,猜不着。
TOKEN_BYTES = 32

#: 多久没用就作废。**是"闲置"不是"签发"** —— 正在干活的人不该被踢下线,
#: 而一台被顺走的手机上的 token 不该永远有效。
TOKEN_IDLE_S = 12 * 3600.0

#: 同时最多留几个 token。防的是"有人反复解锁把内存撑爆"。
MAX_TOKENS = 64

#: §3.6:一只狗同时最多几个非本机 app 会话。**多的直接拒,不排队。**
#: 依据是物理的:CPE 的无线上行是共享的,每多一路视频所有人都变卡。这条对
#: 回环流量不成立 —— 回环不占那条无线上行,所以 ``CHANNEL_LOCAL`` 不占用
#: 这三个名额、也不受这道闸拦(见 ``Guard._issue``)。排队更糟 —— "等着
#: 等着突然就连上了"意味着操作员在不确定的时刻拿到一只会动的狗。
#:
#: **满了之后,远端的人拿不回座位。** 这一卷里没有远程"踢人"的接口 ——
#: ``logout()`` 只退自己手里的 token,``revoke_ref()`` 没有对外的 HTTP 路由。
#: 三个非本机名额都被占、又没人主动退出、也没人能同名换座时,唯一还能进来
#: 的路是本机(SSH/控制台到场),或者重启整个服务(代价是打断正在跑的巡检)。
#: **这不是"已解决"**,是一把物理到场才能用的钥匙,不是远程钥匙。完整的
#: 威胁模型见 ``Guard._issue``。
MAX_SESSIONS = 3

#: 本机(``CHANNEL_LOCAL``)自己的会话上限。**本机免于 MAX_SESSIONS 这道
#: 闸(见 ``Guard._issue``),但不能因此对 ``MAX_TOKENS = 64`` 这个更底层
#: 的内存兜底敞开口子** —— ``TokenStore.issue`` 满了之后是纯插入顺序
#: FIFO 淘汰,不看 channel 也不看 operator,顶到 64 会把最早签发的
#: 会话(包括正在作业的非本机会话)无通知踢掉,正好绕开 ``Guard._issue``
#: 里"名额满了只挤同名、从不挤别人"这句承诺。作为攻击不值一提(能走回
#: 环的人早就能 ``systemctl stop``),但作为**事故**很现实:一个本地脚本
#: 或服务在循环里重复认证,就能把正在作业的运维静默踢掉,日志里还看不出
#: 来 —— 而"控制权无声易主"正是这一卷存在的理由,不能被自己开的口子破
#: 坏。所以本机也要有一个远小于 ``MAX_TOKENS`` 的上限:**MAX_SESSIONS +
#: MAX_LOCAL_SESSIONS 必须远小于 MAX_TOKENS**,这样那条 FIFO 淘汰永远轮
#: 不到被触发(见 test_两个会话上限之和远小于内存兜底)。数值给个位数 ——
#: 本机会话的正当用途是"有人在控制台前",不是承载并发。
#:
#: **本机满了之后一样硬拒,不是又变回"没钥匙"。** 人在控制台前,他还能
#: 重启服务或杀掉那个失控的脚本,这两件事射程内的攻击者都做不到 —— 权限
#: 梯度仍然在对的那一头。免限没有被撤销,只是从"无限"改成"一个够用的
#: 有限值"。
MAX_LOCAL_SESSIONS = 4

#: Host 头里允许出现的名字。别的一律只收 IP 字面量。
_LOCAL_NAMES = frozenset({"localhost"})

#: 热点上发出去的 token 闲置多久就作废。比局域网短得多 —— 热点的信任边界是
#: "射程",人走出射程之后,那个凭证不该还能用(§6.5 措施 3)。
AP_TOKEN_IDLE_S = 30 * 60.0

#: 三条通道。判的是"这个请求从哪个网络面进来",**不是"这个人是谁"**。
CHANNEL_LOCAL = "local"
CHANNEL_AP = "ap"
CHANNEL_LAN = "lan"
CHANNELS: frozenset[str] = frozenset({CHANNEL_LOCAL, CHANNEL_AP, CHANNEL_LAN})

#: 狗自己那块热点的网段。
AP_NETS: tuple[str, ...] = ("192.168.168.0/24",)

#: 操作人姓名最长多少字。超了截断,**从不拒绝** —— 见 normalize_operator。
MAX_OPERATOR_LEN = 32

#: token 指纹取 sha256 的前几位十六进制。32 位够把 3 个并发会话分开,又短到
#: 人能在屏幕上对得上。
REF_HEX = 8

#: §6.3:这句话必须原样跟着每一个带 operator 的响应发出去。
OPERATOR_NOTICE = (
    "操作人姓名是本机记账用的,狗不核实它。要可信的人身份,得接服务器。"
)

#: 质询有效期。够手机做一次往返,不够别人捡回去重放。
NONCE_TTL_S = 60.0

#: 全局同时最多留几个没用掉的质询。防的是很多个来源一起狂发把内存撑爆。
#: 一条记录几十字节,这个量级可以忽略。
MAX_NONCES = 512

#: **同一个来源 IP** 同时最多留几个没用掉的质询。防的是一个未鉴权的人把别人
#: 的质询挤掉(见 ``NonceStore`` 的威胁模型)。4 个够手机重试几次,不够拿来当
#: 挤兑工具。
MAX_NONCES_PER_CLIENT = 4

#: 一个质询多少字节随机。
NONCE_BYTES = 16

#: 证明用什么算。写进响应里,客户端不用猜。
PROOF_ALG = "HMAC-SHA256"

#: 取质询的接口。跟 AUTH_PATH 一样不能要 token —— 要了就没人换得到 token。
CHALLENGE_PATH = "/api/auth/challenge"

#: 不要 token 的路径,就这两条。
OPEN_PATHS: frozenset[str] = frozenset({AUTH_PATH, CHALLENGE_PATH})

#: 只读凭证也放行的两条写接口。急停永远不需要控制权、也永远不需要"写权限"
#: (§3.5 规则 2);退出是把自己的名额还回去,拦它只会让名额漏光。
READONLY_OPEN_PATHS: frozenset[str] = frozenset({
    "/api/estop", "/api/auth/logout",
})


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
    """内存里一个 token 的全部家当。"""

    last: float
    ref: str = ""
    operator: str = ""
    channel: str = CHANNEL_LAN
    readonly: bool = False
    idle_s: float = TOKEN_IDLE_S


@dataclass(frozen=True, slots=True)
class Session:
    """一个活着的会话。**不含 token 本身** —— 发给客户端的是这个。"""

    ref: str
    operator: str
    channel: str
    readonly: bool
    idle_for_s: float
    idle_limit_s: float

    def to_wire(self) -> dict[str, Any]:
        return {"ref": self.ref, "operator": self.operator,
                # 恒为 False。§6.3:狗记名字,但从不核实它。
                "operator_verified": False,
                "channel": self.channel, "readonly": self.readonly,
                "idle_for_s": round(self.idle_for_s, 1),
                "idle_limit_s": self.idle_limit_s}


def _session_of(rec: _Live, now: float) -> Session:
    return Session(ref=rec.ref, operator=rec.operator, channel=rec.channel,
                   readonly=rec.readonly,
                   idle_for_s=max(0.0, now - rec.last),
                   idle_limit_s=rec.idle_s)


class TokenStore:
    """内存里的 token 表。线程安全 —— HTTP 是一请求一线程的。"""

    def __init__(self, *, idle_s: float = TOKEN_IDLE_S,
                 cap: int = MAX_TOKENS) -> None:
        self._idle_s = idle_s
        self._cap = cap
        self._lock = threading.Lock()
        #: 插入序就是签发序,满了从头上淘汰。
        self._live: dict[str, _Live] = {}

    def issue(self, *, now: float | None = None, operator: str = "",
              channel: str = CHANNEL_LAN, readonly: bool = False,
              idle_s: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            self._sweep(now)
            while len(self._live) >= self._cap:
                self._live.pop(next(iter(self._live)))
            self._live[token] = _Live(
                last=now, ref=token_ref(token), operator=operator,
                channel=channel, readonly=readonly,
                idle_s=self._idle_s if idle_s is None else idle_s)
        return token

    def info(self, token: str, *, now: float | None = None) -> Session | None:
        """认一个 token 并把它的身份带回来,顺带续期。不认识就 ``None``。

        直接查字典而不是逐个 ``compare_digest``:token 是 256 位随机数,猜不
        中的东西不存在计时侧信道可利用的余地,而线性扫描会让 token 一多就慢。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(now)
            rec = self._live.get(token)
            if rec is None:
                return None
            # 先做快照再续期:带回去的 idle_for_s 是"这次请求之前闲了多久",
            # 那才是有信息量的那个数。
            sess = _session_of(rec, now)
            rec.last = now
            return sess

    def valid(self, token: str, *, now: float | None = None) -> bool:
        """认一个 token,顺带续期。"""
        return self.info(token, now=now) is not None

    def sessions(self, *, now: float | None = None) -> tuple[Session, ...]:
        """现在有哪些活着的会话。**只读,不续期** —— 值守屏每半秒看一眼,不
        该因此让别人的闲置计时永远归零。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(now)
            return tuple(_session_of(r, now) for r in self._live.values())

    def live_refs(self, *, now: float | None = None) -> frozenset[str]:
        """还活着的 token 指纹。租约靠它收租(§6.4)。同样不续期。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(now)
            return frozenset(r.ref for r in self._live.values())

    def revoke_ref(self, ref: str) -> bool:
        """按指纹撤一个 token。撤到了返回 ``True``。"""
        with self._lock:
            dead = [t for t, r in self._live.items() if r.ref == ref]
            for t in dead:
                del self._live[t]
            return bool(dead)

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
        """清掉闲置超时的。**每个 token 有自己的闲置期** —— 热点上发出去的
        那些短得多(§6.5 措施 3)。调用方已经拿着锁了。
        """
        dead = [t for t, r in self._live.items() if now - r.last > r.idle_s]
        for t in dead:
            del self._live[t]


# ------------------------------------------------------------------ 限速


@dataclass
class _Fails:
    count: int = 0
    #: 锁到什么时候(monotonic)。0 表示没锁。
    until: float = 0.0
    #: 最后一次记失败是什么时候(monotonic)。内存兜底按它挑最老的淘汰。
    last: float = 0.0


class Throttle:
    """按来源 IP 记 PIN 试错。连错就退避,退避时间翻倍。

    **内存兜底,以及它买到的和没买到的。** 每来一个新的来源 IP 就多一条记录,
    没有兜底的话,局域网上一个扫端口的人能让它长到整个网段那么大 —— 这是这
    一卷里唯一一个会随外部输入无限长的结构(``TokenStore`` 有 ``MAX_TOKENS``,
    ``NonceStore`` 有 ``MAX_NONCES``,``LeaseBook`` 有 ``AUDIT_MAX``)。所以
    上限是 ``max_clients``,超了就淘汰。

    **淘汰分两档,第一档优先,第二档是兜底** —— 完整规矩在 ``_sweep`` 里。
    正常情况下只扔没锁着的(``until <= now``),按最后一次失败从老到新;只有
    第一档一条都扔不动、上限仍被突破时,才动锁着的,挑**最快要解锁**的那些。
    **所以"锁着的绝不被淘汰"这句话是不成立的,别照它写代码** —— 一个能从很
    多地址发请求的人可以让每条记录都处在锁定中,那时不动锁着的就等于没有上限,
    而"内存有界"是这个类唯一必须守住的承诺。

    **说实话它没买到什么:**一个能从很多个源地址发请求的人,可以把自己那条
    "还没到 ``max_fails``"的计数顶出去,于是他的失败计数被清零、重新从头数。
    但他本来就能靠换源 IP 直接得到同一个效果(计数本来就是按 IP 记的),所以
    这里没有多让出任何东西 —— 而真正挡离线穷举的从来不是这个类(见
    ``proof_for`` 的 docstring)。
    """

    def __init__(self, *, max_fails: int = MAX_FAILS,
                 lockout_s: float = LOCKOUT_S,
                 max_lockout_s: float = MAX_LOCKOUT_S,
                 max_clients: int = MAX_THROTTLE_CLIENTS) -> None:
        self._max_fails = max_fails
        self._lockout_s = lockout_s
        self._max_lockout_s = max_lockout_s
        self._max_clients = max_clients
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
            rec.last = now
            if rec.count < self._max_fails:
                self._sweep(now)
                return 0.0
            grow = 2.0 ** (rec.count - self._max_fails)
            wait = min(self._lockout_s * grow, self._max_lockout_s)
            rec.until = now + wait
            self._sweep(now)
            return wait

    def succeed(self, client: str) -> None:
        with self._lock:
            self._by_client.pop(client, None)

    @property
    def tracked(self) -> int:
        """现在记着多少个来源。给测试和排障看。"""
        with self._lock:
            return len(self._by_client)

    def _sweep(self, now: float) -> None:
        """超过上限就淘汰,**分两档**。调用方已经拿着锁了。

        第一档:没锁着的(``until <= now``),按最后一次失败从老到新扔。正常情
        况下够用 —— 扫端口的人每个地址只留一条没到阈值的记录。

        第二档(**兜底,不得已**):第一档扔完还超,就扔**最快要解锁**的那些。
        为什么必须有这一档:一个能从很多地址发请求的人可以让每一条记录都处在
        锁定中,那时第一档一条都扔不动,上限就不再是上限了 —— 而"内存有界"是
        这个类唯一必须守住的承诺。为什么它没让出多少:计数本来就是按 IP 记
        的,他换一个新地址就直接得到一个干净的计数,不必费劲把自己顶出去;真
        正挡离线穷举的从来不是这个类(见 ``proof_for``)。
        """
        if len(self._by_client) <= self._max_clients:
            return
        free = sorted((r.last, c) for c, r in self._by_client.items()
                      if r.until <= now)
        for _, client in free:
            if len(self._by_client) <= self._max_clients:
                return
            del self._by_client[client]
        locked = sorted((r.until, c) for c, r in self._by_client.items())
        for _, client in locked:
            if len(self._by_client) <= self._max_clients:
                return
            del self._by_client[client]


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


def token_ref(token: str) -> str:
    """token 的指纹。**审计和状态快照里出现的是它,不是 token 本身。**

    威胁模型:能看到留痕的人(值守屏前的同事、事后拿到日志的人)不该顺手捡
    到一把能开狗的钥匙。sha256 截 8 位十六进制不可逆,而 token 是 256 位随机
    数,从指纹反推不出来。

    **8 位是一个想过的取舍,不是随手定的长度。** ``REF_HEX = 8`` 只有 32 位
    空间,而这个 ref 还被 ``revoke_ref()`` 拿来按前缀撤号 —— 两个同时活着的
    token 撞上同一个 ref,撤一个会把另一个也撤掉。

    - **概率**:同时活着的会话上限是个位数(``MAX_SESSIONS`` +
      ``MAX_LOCAL_SESSIONS``),生日碰撞在 2**32 空间里是 1e-7~1e-10 的量级。
    - **后果有界**:误撤的那个人被踢下线,重新输一次 PIN 就回来了。狗不会
      因此动起来,也不会因此停下 —— 撤号只会**减少**权限,不会增加。
    - **所以不加宽。** ref 要能被人在值守屏上读出来、在对讲机里报出来;
      加到 16 位换来的是十位数分之一的概率改善,和一串没人念得完的十六进制。

    **谁将来想动它,先回答一个问题:这个 ref 还只被用来"认人"和"撤号"吗?**
    只要它开始被用来**授予**什么(比如"拿 ref 就能续租""按 ref 找回会话"),
    上面"后果有界"这一条当场作废,那时候该做的不是加宽,是别让 ref 承担
    那个职责。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:REF_HEX]


def channel_of(client: str, *, ap_nets: Sequence[str] = AP_NETS) -> str:
    """这个请求是从哪条通道进来的。

    **威胁模型是分档的,绝不许把"局域网"当成"可信"**(§6.5):

    - ``local`` —— 回环。能从回环发请求的人已经在这台机器上了,再防他没意义。
    - ``ap`` —— 狗自己的热点。密码出厂固定、改不了(要走厂家支持通道),而
      WPA2-PSK 意味着**射程之内、知道密码的任何人都能解开别人的报文**。所以
      这条通道上 PIN 和 token 都要当成"已经公开"来设计:凭证短命,明文 PIN
      只换只读(任务 5)。
    - ``lan`` —— 别的网(CPE 回传、办公网)。**不等于可信**,只是少了"同一
      把 PSK 人人可解"这条额外的坏性质。

    认不出来的来源(空串、代理写坏的头、Unix socket)一律按 ``ap`` 算 ——
    **失败要往严的方向倒**。

    **前提:``client`` 必须是 TCP 对端地址,不是任何请求头里的东西。**
    今天成立 —— 唯一的调用路径是 ``server.py`` 的 ``_dispatch``,它传的是
    ``self.client_address[0]``,内核给的,伪造不了。整套分档的全部安全性都
    压在这一句上。

    **哪天有人在服务前面加了反向代理**(Nginx、网关),并且把这里改成读
    ``X-Forwarded-For`` 之类的头,**三档会整体失效**:那种头是请求方自己
    写的,射程之内的人伪造一行就能把自己从 ``ap`` 抬成 ``lan`` 甚至
    ``local``,于是明文 PIN 换到全权凭证。**而那一天不会有任何一条测试变
    红,也不会有任何一条日志异常** —— 判据还在,只是喂给它的东西被换掉了。

    所以要加代理之前先重新设计这一层(至少要有可信代理名单、并且只信最后
    一跳)。同一句话也写在 ``docs/鉴权与控制权.md`` 第六节,给现场运维看。

    **IPv4 映射的 IPv6 地址先归一化,不归一化会掉进最松的一档。**
    ``::ffff:192.168.168.5`` 说的就是 ``192.168.168.5``,但
    ``IPv4Network.__contains__`` 对一个 v6 地址直接返回 ``False``,而
    ``IPv6Address.is_loopback`` 不看 ``ipv4_mapped`` —— 不归一化的话,热点上
    的人和本机自己都会被判成 ``lan``:明文 PIN 换到的从只读凭证变成完整凭证,
    闲置期从 30 分钟变回 12 小时。**这一类不是"认不出来",是认出来了、认错了,
    而且错在松的那一头** —— 上面那句"认不出来一律按 ap 算"恰好盖不住它。

    今天不可达(``_HTTPServer`` 是 ``AF_INET``,对端地址永远是点分十进制),
    但兑现它只要一句 ``address_family = socket.AF_INET6`` —— 那是让服务同时
    听 v4/v6 的标准写法,看起来完全无害。所以这三行不是"以后可能有用",是
    **提前把一个一句配置就能打开的口子焊死**。
    """
    try:
        addr = ipaddress.ip_address(client.strip())
    except ValueError:
        return CHANNEL_AP
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_loopback:
        return CHANNEL_LOCAL
    for raw in ap_nets:
        if addr in ipaddress.ip_network(raw):
            return CHANNEL_AP
    return CHANNEL_LAN


def normalize_operator(raw: object) -> str:
    """把操作人姓名收拾干净。**从不抛异常。**

    §6.3:这个名字是**记账,不是鉴权**。单机档里根本没有可信的人身份来源 ——
    狗没有任何办法核实"张三"真的是张三。既然核实不了,就不该假装在校验它:
    一个会拒绝的字段会让人误以为它被验过。所以这里只做三件无害的事 —— 去掉
    首尾空白、把控制字符换成空格、超长截断。给不出名字就是空串,而空串是合法
    的(现场可能就是没人愿意报名)。
    """
    if not isinstance(raw, str):
        return ""
    text = "".join(c if c.isprintable() else " " for c in raw).strip()
    return text[:MAX_OPERATOR_LEN]


def proof_for(pin: str, nonce: str) -> str:
    """这一轮质询的证明:``HMAC-SHA256(PIN, nonce)`` 的十六进制。

    **它买到的是什么。** 热点是 WPA2-PSK,密码出厂固定、改不了(要改得走厂家
    支持通道),射程之内知道密码的任何人都能解开别人的报文(§6.5)。明文 PIN
    走一趟 HTTP,等于当着所有人的面把钥匙念一遍 —— 而 PIN 是长期凭证,念一次
    就永久泄露。走 HMAC 之后,单次抓包里不再出现 PIN 明文;质询一次性,同一对
    ``(nonce, proof)`` 重放不了。这两条是真的。

    **它防不了什么 —— 别把这句话读成"PIN 安全了"。** 抓到一对 ``(nonce, proof)``
    之后,拿它离线穷举 PIN 是可行的:``deploy/install.sh`` 生成的出厂 PIN 是
    6 位数字,候选空间只有 10**6,笔记本上跑一遍 HMAC-SHA256 是秒级的事 ——
    这道题挡不住肯坐下来算的人。**这台机器上的在线限速
    (``Throttle``/``MAX_FAILS``/``LOCKOUT_S``)对这件事完全无效**:它们只管得住
    "谁在往这台机器发请求",而离线穷举压根不再发请求。真正管用的两条防线不在
    这个函数里:一是 §6.5 措施 1 的 TLS + 证书钉扎(规格自己称它是"真解",挡的
    正是"抓包之后能拿它做什么"),二是把 PIN 的熵提上去(产品决策,牵动手机
    app,不归这一轮)。这个函数只是纵深里的一层,不是终点 —— 主动中间人换掉
    整个响应,以及离线穷举,都要靠这两条防线兜,不假装自己解决了它们。
    """
    return hmac.new(pin.encode("utf-8"), nonce.encode("utf-8"),
                    hashlib.sha256).hexdigest()


class NonceStore:
    """发出去还没用掉的质询。**一次性,过期就扔。**

    威胁模型:能在热点射程内抓包的人(§6.5)能看到质询和证明,但看不到 PIN。
    如果同一个质询能反复核验,他捡到一次报文就等于捡到了一把能反复使用的
    钥匙 —— 质询-应答就退化成一个可重放的固定口令。``take`` 因此在核验成功
    的那一刻就把 nonce 作废,不管核验的是明文对比还是签名匹配。过期时间防的
    是另一件事:质询发出去、迟迟没被用掉,窗口越长,被人从报文里捡回去重放
    的机会就越大 —— TTL 把这个窗口钉死在"够一次手机往返"的量级上。

    **第二个假想敌:一个连 PIN 都没有的人,只想把别人挤下去。** 取质询这条路
    (``CHALLENGE_PATH``)必须放在 ``OPEN_PATHS`` 里 —— 要 token 就没人换得到
    token —— 所以热点射程之内、或者客户局域网上任何能连到这个端口的人,都能
    无限次地打它,而且不必先解锁。这个池子早先是**一个全局 FIFO**:满了就从
    头上淘汰,不看那条 nonce 属于谁、过没过期。于是他只要连发 ``cap`` 次,就能
    把每一个正当用户刚取到、还没用掉的质询全挤掉。

    **他挤掉的后果不是"登不上",是把人降级回明文 PIN**,而这两条降级都实打实
    地伤到东西:在狗自己的热点上,明文 PIN 只换只读凭证(``Guard.unlock``)——
    应急通道当场开不动狗;在客户局域网上,人只能把 PIN 明文发上网 —— §6.5
    措施 2 存在的全部理由就是别让它上网。而 ``unlock_proof`` 里"质询过期不记
    失败计数"那个决定(它本身是对的,不然信号弱的现场会被自己的重试锁死)
    恰好意味着他可以无限重复,一条限速也碰不到他。

    **所以这个池子按来源 IP 分桶。** 每桶 ``per_client`` 个,淘汰**只淘汰过期
    的** —— 新 nonce 永远挤不掉别人还没过期的那一条。自己那一桶满了返 429
    (不是 400:429 才说得清"这是限流,等一会儿再来")。攻击者能撑爆的只有他
    自己那一桶;要影响别人就得换源 IP,一个 IP 只顶 4 个,量级完全不同。

    **上限的真实形状,不许说得比实际硬。** ``cap`` 不是一个咬死的天花板:全局
    到顶之后**新来源**会被 429 挡在外面,而**已经有桶的来源**照旧能取到自己
    那 4 个 —— 这是有意的,不然攻击者凑够 ``cap`` 条就又能把所有人挡在门外,
    分桶等于白做。所以最坏情况是 ``cap × per_client`` 条(今天是 2048 条,几十
    KB 的量级),仍然是有界的,内存兜底照样成立。
    """

    def __init__(self, *, ttl_s: float = NONCE_TTL_S,
                 cap: int = MAX_NONCES,
                 per_client: int = MAX_NONCES_PER_CLIENT) -> None:
        self._ttl_s = ttl_s
        self._cap = cap
        self._per_client = per_client
        self._lock = threading.Lock()
        #: 来源 IP -> {nonce: 签发时刻}。分桶就是这道防线本身。
        self._buckets: dict[str, dict[str, float]] = {}
        #: nonce -> 来源 IP。``take`` 靠它找回桶,不用遍历。
        self._owner: dict[str, str] = {}

    def mint(self, client: str, *, now: float | None = None) -> str:
        """给这个来源发一个质询。桶满了抛 ``Denied(429)``。

        ``client`` 必须是 TCP 对端地址(``server.py`` 的 ``client_address[0]``),
        不是任何请求头里的东西 —— 跟 ``channel_of`` 是同一条前提。哪天它变成
        一个请求方自己写得了的值,分桶当场失效:伪造一行头就能换一个桶。
        """
        now = time.monotonic() if now is None else now
        nonce = secrets.token_hex(NONCE_BYTES)
        with self._lock:
            self._sweep(now)
            bucket = self._buckets.get(client)
            if bucket is not None and len(bucket) >= self._per_client:
                raise Denied(
                    429, "质询取得太快了",
                    f"同一个来源同时最多留 {self._per_client} 个没用掉的质询。"
                    f"用掉一个,或者等 {self._ttl_s:.0f} 秒它自己过期。")
            if bucket is None and len(self._owner) >= self._cap:
                # 清过期的已经清过了(``_sweep``),清不出来就只挡**新来源**:
                # 已经有桶的人不受影响,不然攻击者凑够 cap 条又能封住所有人。
                raise Denied(
                    429, "这台机器上待用的质询太多了",
                    "等几秒再取一次。持续如此说明有人在刷这条接口。")
            if bucket is None:
                bucket = self._buckets.setdefault(client, {})
            bucket[nonce] = now
            self._owner[nonce] = client
        return nonce

    def take(self, nonce: object, *, now: float | None = None) -> bool:
        """用掉一个质询。**用过就没了** —— 重放打不进来。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(now)
            if not isinstance(nonce, str) or nonce not in self._owner:
                return False
            self._drop(nonce)
            return True

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._owner)

    def count_for(self, client: str) -> int:
        """这个来源手上还有几个没用掉的。给测试和排障看。"""
        with self._lock:
            return len(self._buckets.get(client, ()))

    def _drop(self, nonce: str) -> None:
        """把一条 nonce 从两个索引里一起摘掉。调用方已经拿着锁了。"""
        client = self._owner.pop(nonce)
        bucket = self._buckets.get(client)
        if bucket is not None:
            bucket.pop(nonce, None)
            if not bucket:
                del self._buckets[client]

    def _sweep(self, now: float) -> None:
        """清掉过期的。**只清过期的** —— 淘汰不许碰别人还能用的那条。

        调用方已经拿着锁了。
        """
        dead = [n for n, c in self._owner.items()
                if now - self._buckets[c][n] > self._ttl_s]
        for n in dead:
            self._drop(n)


class Guard:
    """鉴权闸门。``pin`` 给 ``None`` 就是不鉴权(只在本机听时才允许)。"""

    def __init__(self, pin: str | None = None, *,
                 tokens: TokenStore | None = None,
                 throttle: Throttle | None = None,
                 nonces: NonceStore | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 ap_nets: Sequence[str] = AP_NETS,
                 max_sessions: int = MAX_SESSIONS,
                 max_local_sessions: int = MAX_LOCAL_SESSIONS) -> None:
        self._pin = normalize_pin(pin) if pin is not None else None
        self.tokens = tokens if tokens is not None else TokenStore()
        self.throttle = throttle if throttle is not None else Throttle()
        self.nonces = nonces if nonces is not None else NonceStore()
        self.max_sessions = max_sessions
        self.max_local_sessions = max_local_sessions
        self._clock = clock
        self._ap_nets = tuple(ap_nets)

    @property
    def enabled(self) -> bool:
        return self._pin is not None

    # ---------------------------------------------------------------- 解锁

    def unlock(self, pin: object, client: str, *, operator: str = "",
               now: float | None = None) -> str:
        """PIN 换 token。不对就抛 ``Denied``。

        ``operator`` 是操作人自己报的名字,**狗记下来但不核实**(§6.3)。
        """
        now = self._clock() if now is None else now
        if self._pin is None:
            raise Denied(400, "这台没设 PIN", "启动时没给 --pin,不需要解锁。")
        wait = self.throttle.locked_for(client, now=now)
        if wait > 0:
            raise Denied(429, "试得太多了,先等等",
                         f"还要等 {wait:.0f} 秒才能再试。")
        # 先比长度再比内容会漏长度;``compare_digest`` 对 str 要求两边都是
        # ASCII,所以统一编成 bytes 再比。
        want = self._pin.encode("utf-8")
        got = pin.encode("utf-8") if isinstance(pin, str) else b""
        if not secrets.compare_digest(want, got):
            wait = self.throttle.fail(client, now=now)
            detail = (f"连错太多次,锁 {wait:.0f} 秒。" if wait > 0
                      else "再试一次。")
            raise Denied(401, "PIN 不对", detail)
        self.throttle.succeed(client)
        # 热点上的明文 PIN 只换只读凭证:那条通道上报文人人可解,这个 PIN 已经
        # 等于公开的了。要能操作,走质询-应答(手机 app),或者换个网络连。
        return self._issue(client, operator,
                           readonly=self.channel_of(client) == CHANNEL_AP,
                           now=now)

    def challenge(self, client: str, *, now: float | None = None) -> str:
        """发一个一次性质询。**这条不要 token** —— 要了就没人换得到 token。

        正因为它不要 token,任何能连到这个端口的人都能无限次打它,所以配额按
        ``client`` 分桶(见 ``NonceStore`` 的第二个假想敌)。桶满了这里会把
        ``Denied(429)`` 原样抛出去。
        """
        if self._pin is None:
            raise Denied(400, "这台没设 PIN", "启动时没给 --pin,不需要解锁。")
        return self.nonces.mint(client,
                                now=self._clock() if now is None else now)

    def unlock_proof(self, nonce: object, proof: object, client: str, *,
                     operator: str = "", now: float | None = None) -> str:
        """质询-应答换 token。**PIN 不上网**(§6.5 措施 2)。

        限速跟明文那条走同一个 ``Throttle``:两条路都是在猜同一个六位 PIN,
        分开计数等于把爆破防线开一半。
        """
        now = self._clock() if now is None else now
        if self._pin is None:
            raise Denied(400, "这台没设 PIN", "启动时没给 --pin,不需要解锁。")
        wait = self.throttle.locked_for(client, now=now)
        if wait > 0:
            raise Denied(429, "试得太多了,先等等",
                         f"还要等 {wait:.0f} 秒才能再试。")
        if not self.nonces.take(nonce, now=now):
            # **不记失败。** 质询过期是网络往返慢,不是在猜 PIN;把它算进爆破
            # 计数,信号弱的现场会被自己的重试锁在门外。
            raise Denied(400, "质询过期了,重新取一个",
                         f"质询只有 {NONCE_TTL_S:.0f} 秒,而且只能用一次。")
        want = proof_for(self._pin, nonce if isinstance(nonce, str) else "")
        got = proof if isinstance(proof, str) else ""
        if not secrets.compare_digest(want, got):
            wait = self.throttle.fail(client, now=now)
            detail = (f"连错太多次,锁 {wait:.0f} 秒。" if wait > 0
                      else "重新取一个质询再试。")
            raise Denied(401, "证明不对", detail)
        self.throttle.succeed(client)
        return self._issue(client, operator, readonly=False, now=now)

    def channel_of(self, client: str) -> str:
        """这个来源走哪条通道。见模块级 :func:`channel_of`。"""
        return channel_of(client, ap_nets=self._ap_nets)

    def sessions(self, *, now: float | None = None) -> tuple[Session, ...]:
        """现在有哪些活着的会话,**含本机**。§3.6 的 ``max_sessions`` 只算
        非本机通道(见 ``Guard._issue``)—— 这里返回的总数比
        ``max_sessions`` 大不代表爆表,只是把本机也如实列出来了;
        ``GET /api/sessions`` 用的就是这份原始列表,数字不矛盾。
        """
        return self.tokens.sessions(now=self._clock() if now is None else now)

    def live_refs(self, *, now: float | None = None) -> frozenset[str]:
        return self.tokens.live_refs(now=self._clock() if now is None else now)

    def session_of(self, token: str, *,
                   now: float | None = None) -> Session | None:
        return self.tokens.info(token,
                                now=self._clock() if now is None else now)

    def logout(self, token: str) -> bool:
        """交回一个会话的名额。原来有就返回 ``True``。

        **必须有这条。** 没有它,一个装完就卸载的 app 会占着 1/3 的名额到闲
        置期走完为止 —— 在局域网上那是 12 小时。
        """
        had = self.tokens.info(token, now=self._clock()) is not None
        self.tokens.revoke(token)
        return had

    def _issue(self, client: str, operator: object, *, readonly: bool,
               now: float) -> str:
        """发一个 token。**先看有没有名额**(§3.6),再看通道(§6.5)。

        **本机(``CHANNEL_LOCAL``)有自己的一套名额,``MAX_LOCAL_SESSIONS``,
        不跟非本机共用 ``MAX_SESSIONS`` 那 3 个。** 理由是"CPE 的无线上行
        是共享的",回环流量根本不走那条上行,这条理由对它不成立;而能从
        回环发请求的人已经在这台机器上了(SSH/控制台到场),这是比"在热
        点射程内"更强的权限。**但本机不是没有上限** —— 更底层的
        ``MAX_TOKENS = 64`` 是纯插入顺序 FIFO 淘汰,不看 channel 也不看
        operator,如果本机真的不设限,顶到 64 会把最早签发的非本机会话
        无通知踢掉。``MAX_SESSIONS + MAX_LOCAL_SESSIONS`` 因此被钉在远小
        于 ``MAX_TOKENS`` 的量级,让那条 FIFO 永远轮不到触发(见常量定义
        处、以及 test_两个会话上限之和远小于内存兜底)。**本机满了一样硬
        拒,不是又变回"没钥匙"** —— 人在控制台前,他还能重启服务或杀掉
        那个失控的脚本,这两件事射程内的攻击者都做不到,权限梯度仍然在
        对的那一头;免限没有被撤销,只是从"无限"改成"一个够用的有限值"。

        名额满了淘汰谁,是安全判据不是容量细节:这里选的是"同名换座" ——
        只挤同一个报名者自己的老会话,从不挤别人的。威胁模型是反复登录的
        攻击者:如果谁都能靠猜中或不带 operator 挤掉别人的名额,这道闸就
        从一道闸变成了一件武器。所以空名字(§6.3:狗从不核实报名,空名字
        谁都能报)不算身份,不许互相挤 —— 换座只发生在"同一个字符串"之间。
        本机和非本机各自的名额池互不相干:同名换座只在同一个池子里发生,
        本机会话挤不掉非本机会话,反过来也一样。

        **但"同名 = 同一个人"这个等式不总成立。** 名字是操作人自报的,狗
        从不核实(``OPERATOR_NOTICE``)。这条防线挡得住的是"攻击者不知道
        在线的人叫什么、猜不中那个字符串";挡不住的是**猜一个现场惯用的
        通用名**——"运维""值班"这类词谁都报得出来,报中了就会把当前占着
        这个名字的会话直接换座挤掉,不管挤掉的是不是"同一个人"。两个人各
        自不知情地撞上同一个通用名,也会互相顶替。**被顶替的一方拿不到任
        何通知**,只有下次发现自己的 token 失效才会察觉。这不是这段代码
        能解决的(现场约定不用通用名是运营手段),但不能把"同名"读成一个
        可靠的身份判据 —— 它只是"记账用的字符串相等",跟 §6.3 说的是同一
        回事。

        **这条威胁还有没写的一半:换座连带把控制权也拿走了,而且不留接管
        的痕迹。** 老会话的 token 一被 ``revoke_ref`` 掉,``ControlDesk``
        下一轮 sweep 就会按 §6.4 把他的租约一起释放(``keep_only``),新来
        的那个人于是可以直接 ``acquire``。审计上这一串读起来是"同一个名字
        掉线又回来了"(``acquired`` → ``dropped`` → ``acquired``),跟一次
        普通的闲置失效分不开 —— **没有 ``taken_over``、没有理由、也没有
        任何一条指向新持有者的因果链**,而正规的接管(§3.4/§3.5)是一定要
        留这些的。前提很硬(名额先得满、名字得猜中、手上得有 PIN;而有 PIN
        的人本来就能走留痕的 ``force``),所以这不是一条新的夺权路径,**它
        换到的只有"审计规避"这一样**。修它要跨 ``Guard``/``ControlDesk``/
        ``LeaseBook`` 三处并且可能要动 ``AUDIT_KINDS``,已登记为
        docs/第2卷待办.md 第 55 条 —— 在那之前,读到这段的人至少要知道
        审计在这一档上会说谎。

        **非本机名额都满、又没有同名可换座时,远端的人拿不回座位。**
        ``logout()`` 只能退自己手里的 token,``revoke_ref()`` 没有对外的
        HTTP 路由,这一卷里唯一能从远端挤开别人的办法就是上面的同名换座。
        走不通的话,能进来的只有本机(SSH/控制台到场),或者重启整个服务
        —— 代价是打断正在跑的巡检。**这不是"已解决"**,是一把物理到场
        才能用的钥匙,不是远程钥匙。
        """
        name = normalize_operator(operator)
        channel = self.channel_of(client)
        if channel == CHANNEL_LOCAL:
            cap = self.max_local_sessions
            pool = [s for s in self.tokens.sessions(now=now)
                    if s.channel == CHANNEL_LOCAL]
            full_msg = f"本机(回环)同时最多 {cap} 个会话"
        else:
            cap = self.max_sessions
            pool = [s for s in self.tokens.sessions(now=now)
                    if s.channel != CHANNEL_LOCAL]
            full_msg = f"这只狗最多同时连 {cap} 个人"
        if len(pool) >= cap:
            # 同一个报名的人回来了(换设备、重装 app、清了缓存):挤掉的是
            # 他自己那个老会话,不是别人的。空名字不算身份 —— 让空名字互
            # 相挤等于把上限废掉。本机和非本机各自一个池子,互不侵占。
            mine = [s for s in pool if name and s.operator == name]
            if not mine:
                raise Denied(409, full_msg, self._占位说明(pool))
            for s in mine:
                self.tokens.revoke_ref(s.ref)
        idle = AP_TOKEN_IDLE_S if channel == CHANNEL_AP else None
        return self.tokens.issue(now=now, operator=name, channel=channel,
                                 readonly=readonly, idle_s=idle)

    def _占位说明(self, live: list[Session]) -> str:
        """名额被谁占着。**拒绝必须说清原因**(§3.6),不然现场只会反复重试。"""
        who = "、".join(s.operator or f"未具名({s.ref})" for s in live)
        return (f"现在连着的是:{who}。等一个人退出,或者请他在 app 上退出登录。"
                f"上限是硬的 —— 热点的上行带宽是共享的,人一多每个人的画面都卡。")

    # ---------------------------------------------------------------- 放行

    def gate(self, method: str, path: str, headers: Mapping[str, str],
             query: Mapping[str, str]) -> Session | None:
        """放行就把会话带回来,不放行就抛 ``Denied``。

        没设 PIN 的部署上放行但没有会话(返回 ``None``)—— 那种部署按定义只
        听本机,没有"谁是谁"这个问题(见 ``server.check_exposure``)。
        """
        if not host_is_literal(headers.get("host", "")):
            raise Denied(403, "Host 头不对",
                         "只收 IP 地址,不收域名 —— 防的是 DNS rebinding。")
        if not self.enabled:
            return None
        # 页面和静态资源不拦:人得先能把页面打开,才有地方输 PIN。
        # 里面没有任何秘密 —— 数据全在 /api/ 后面。
        if not path.startswith("/api/"):
            return None
        if path in OPEN_PATHS:
            return None
        token = bearer(headers)
        if token is None and query_token_ok(method, path):
            token = query.get("token")
        if not token:
            raise Denied(401, "要先解锁", "在页面上输一次 PIN,或者用 app 连。")
        sess = self.tokens.info(token, now=self._clock())
        if sess is None:
            raise Denied(401, "登录过期了,重新输一次 PIN",
                         "token 无效或太久没用。")
        if (sess.readonly and method != "GET"
                and path not in READONLY_OPEN_PATHS):
            raise Denied(403, "这个凭证只能看,不能操作",
                         "它是在狗的热点上用明文 PIN 换的。热点的密码是出厂"
                         "固定的,射程之内谁都能解开报文 —— 所以这条通道上只"
                         "发只读凭证。要操作,用手机 app(它走质询-应答),或者"
                         "从别的网连进来。")
        return sess
