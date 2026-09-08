"""§6.3 记名 + §6.5 通道分档:token 有名字、有来路、有各自的寿命。"""

from __future__ import annotations

from d1max_patrol.app.auth import (
    AP_TOKEN_IDLE_S,
    CHANNEL_AP,
    CHANNEL_LAN,
    CHANNEL_LOCAL,
    CHANNELS,
    MAX_OPERATOR_LEN,
    MAX_TOKENS,
    OPERATOR_NOTICE,
    TOKEN_IDLE_S,
    Guard,
    TokenStore,
    channel_of,
    normalize_operator,
    token_ref,
)

PIN = "428913"


class 假钟:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------------ 指纹


def test_指纹是稳定的八位十六进制():
    ref = token_ref("abcdefg")
    assert len(ref) == 8
    assert all(c in "0123456789abcdef" for c in ref)
    assert ref == token_ref("abcdefg")


def test_不同的token指纹不同():
    assert token_ref("aaa") != token_ref("bbb")


def test_指纹里没有token本身():
    tok = "这是一个很长的token字符串abcdefghijklmnop"
    assert token_ref(tok) not in tok
    assert tok[:8] != token_ref(tok)


# ------------------------------------------------------------------ 通道


def test_回环是本机():
    assert channel_of("127.0.0.1") == CHANNEL_LOCAL
    assert channel_of("::1") == CHANNEL_LOCAL


def test_狗自己的热点网段是ap():
    assert channel_of("192.168.168.55") == CHANNEL_AP


def test_别的网段是lan():
    assert channel_of("10.20.30.40") == CHANNEL_LAN
    assert channel_of("192.168.1.7") == CHANNEL_LAN


def test_认不出来的来源按最严的算():
    """失败要往严的方向倒 —— 认不出来就当它在热点上。"""
    assert channel_of("") == CHANNEL_AP
    assert channel_of("?") == CHANNEL_AP
    assert channel_of("不是个地址") == CHANNEL_AP


def test_ipv4映射的v6地址不许掉进最松的一档():
    """``::ffff:x.x.x.x`` 说的就是 ``x.x.x.x``,分档得认这一点。

    不归一化的话两条都会判成 ``lan``(``IPv4Network.__contains__`` 对 v6
    地址直接返回 ``False``,``IPv6Address.is_loopback`` 不看 ``ipv4_mapped``)
    —— 热点上的人明文 PIN 换到的从只读凭证变成**完整**凭证,闲置期从 30 分钟
    变回 12 小时。**这一类不是"认不出来",是认错了,而且错在松的那一头**,
    所以上面那条"认不出来按 ap 算"恰好盖不住它。

    今天不可达(服务是 ``AF_INET``),但一句 ``address_family = AF_INET6``
    就兑现 —— 那是让服务同时听 v4/v6 的标准写法,看起来完全无害。这条测试
    就是为了让那一天有东西变红。
    """
    assert channel_of("::ffff:127.0.0.1") == CHANNEL_LOCAL
    assert channel_of("::ffff:192.168.168.5") == CHANNEL_AP
    assert channel_of("::ffff:10.20.30.40") == CHANNEL_LAN
    # 大小写得认 —— 这条不归一化就会判成 lan。
    assert channel_of("::FFFF:192.168.168.5") == CHANNEL_AP


def test_带方括号的写法不认_而且倒向严的那一头():
    """``[::1]`` 这种带方括号的写法 **``channel_of`` 不认**,倒进兜底那一档。

    这是有意的,不是漏了。方括号是 URL 和 Host 头里的文本写法,而 TCP 对端
    地址(``client_address[0]``,内核给的)永远不带方括号。哪天这里开始接受
    方括号,等于说"我也吃得下从请求头里来的东西" —— 而这个函数的全部安全性
    就压在"喂进来的必须是 TCP 对端地址"这一句上(见 ``channel_of`` 的
    docstring)。**所以宽容在这里是坏事,不是好事。**

    断言挑的是 ``127.0.0.1``:方括号真被解析的话答案是 ``local``(最松的一档),
    走兜底才是 ``ap``。**两条路答案不同,所以这条断言测得到东西** —— 换成热点
    网段里的地址就成了一条永远绿的空断言,两条路都返回 ``ap``。
    """
    assert channel_of("[::ffff:127.0.0.1]") == CHANNEL_AP
    assert channel_of("[::1]") == CHANNEL_AP


def test_热点网段可以换():
    assert channel_of("10.9.9.9", ap_nets=("10.9.9.0/24",)) == CHANNEL_AP
    assert channel_of("192.168.168.5", ap_nets=("10.9.9.0/24",)) == CHANNEL_LAN


def test_三条通道就这三条():
    assert CHANNELS == {CHANNEL_LOCAL, CHANNEL_AP, CHANNEL_LAN}


# -------------------------------------------------------------- 操作人名


def test_名字收拾干净():
    assert normalize_operator("  张三  ") == "张三"


def test_不给名字就是空串():
    assert normalize_operator("") == ""
    assert normalize_operator(None) == ""
    assert normalize_operator(123) == ""


def test_名字太长就截断而不是报错():
    long = "张" * 100
    assert len(normalize_operator(long)) == MAX_OPERATOR_LEN


def test_名字里的控制字符换成空格():
    assert normalize_operator("张\x00三") == "张 三"
    assert normalize_operator("张\n三") == "张 三"


def test_记账不是鉴权那句话得在():
    assert "不核实" in OPERATOR_NOTICE
    assert "服务器" in OPERATOR_NOTICE


# ------------------------------------------------------------ token 存储


def test_签发时带上名字和通道():
    s = TokenStore()
    tok = s.issue(now=100.0, operator="张三", channel=CHANNEL_AP)
    sess = s.info(tok, now=100.0)
    assert sess is not None
    assert sess.operator == "张三"
    assert sess.channel == CHANNEL_AP
    assert sess.ref == token_ref(tok)


def test_没给名字的会话名字是空的():
    s = TokenStore()
    tok = s.issue(now=100.0)
    sess = s.info(tok, now=100.0)
    assert sess is not None
    assert sess.operator == ""
    assert sess.channel == CHANNEL_LAN
    assert sess.readonly is False


def test_认不出来的token没有会话():
    s = TokenStore()
    assert s.info("不存在", now=100.0) is None
    assert s.valid("不存在", now=100.0) is False


def test_每个token有自己的闲置期():
    s = TokenStore()
    长 = s.issue(now=0.0)
    短 = s.issue(now=0.0, idle_s=60.0)
    # 边界探测要用另一个短闲置期的 token(而不是接下来要判死刑的那个
    # "短"),因为 info() 命中会续期(见 test_用一下就续上了) —— 摸一下
    # "短" 自己就会把它的死期往后推,死刑那条断言就测不出东西了。用
    # "另短" 探测,还顺带证明了续期是按 token 各自计的:摸"另短"救不
    # 活"短"。
    另短 = s.issue(now=0.0, idle_s=60.0)
    assert s.info(另短, now=59.0) is not None
    assert s.info(短, now=100.0) is None
    assert s.info(长, now=100.0) is not None


def test_短命的过期不影响长命的():
    s = TokenStore()
    长 = s.issue(now=0.0)
    s.issue(now=0.0, idle_s=60.0)
    s.info(长, now=1000.0)
    assert s.count == 1


def test_用一下就续上了():
    s = TokenStore()
    tok = s.issue(now=0.0, idle_s=60.0)
    assert s.info(tok, now=50.0) is not None
    assert s.info(tok, now=100.0) is not None


def test_列会话不给token本身():
    s = TokenStore()
    tok = s.issue(now=100.0, operator="张三")
    rows = s.sessions(now=100.0)
    assert len(rows) == 1
    assert rows[0].ref == token_ref(tok)
    assert tok not in str(rows[0].to_wire())


def test_列会话不会把闲置计时清零():
    s = TokenStore()
    tok = s.issue(now=0.0, idle_s=60.0)
    for t in (10.0, 20.0, 30.0, 40.0, 50.0):
        s.sessions(now=t)
    assert s.info(tok, now=61.0) is None


def test_会话带着已经闲了多久():
    s = TokenStore()
    s.issue(now=0.0)
    assert s.sessions(now=25.0)[0].idle_for_s == 25.0


def test_活着的指纹拿得到():
    s = TokenStore()
    a = s.issue(now=0.0)
    b = s.issue(now=0.0)
    assert s.live_refs(now=0.0) == {token_ref(a), token_ref(b)}


def test_过期的指纹不在活人名单里():
    s = TokenStore()
    a = s.issue(now=0.0, idle_s=10.0)
    assert s.live_refs(now=100.0) == frozenset()
    assert token_ref(a) not in s.live_refs(now=100.0)


def test_按指纹撤token():
    s = TokenStore()
    tok = s.issue(now=0.0)
    assert s.revoke_ref(token_ref(tok)) is True
    assert s.info(tok, now=0.0) is None
    assert s.revoke_ref(token_ref(tok)) is False


def test_内存兜底那条上限一点没动():
    """§6.6:内存存储三条现状全部不动。这条是防内存爆的兜底,跟 §3.6 的
    会话上限是两件事 —— 那条在任务 6。
    """
    assert MAX_TOKENS == 64
    s = TokenStore(cap=3)
    for _ in range(10):
        s.issue(now=0.0)
    assert s.count == 3


def test_会话能整个发出去():
    s = TokenStore()
    s.issue(now=0.0, operator="张三", channel=CHANNEL_AP, readonly=True,
            idle_s=AP_TOKEN_IDLE_S)
    wire = s.sessions(now=5.0)[0].to_wire()
    assert wire["operator"] == "张三"
    assert wire["operator_verified"] is False
    assert wire["channel"] == CHANNEL_AP
    assert wire["readonly"] is True
    assert wire["idle_for_s"] == 5.0
    assert wire["idle_limit_s"] == AP_TOKEN_IDLE_S


# ------------------------------------------------------------------ 闸门


def test_解锁时报的名字记下来了():
    clock = 假钟()
    g = Guard(PIN, clock=clock)
    tok = g.unlock(PIN, "10.0.0.5", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    assert sess.operator == "张三"


def test_解锁返回的还是一个token字符串():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "10.0.0.5")
    assert isinstance(tok, str)
    assert len(tok) > 20


def test_热点上换的token寿命短():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "192.168.168.20")
    sess = g.session_of(tok)
    assert sess is not None
    assert sess.channel == CHANNEL_AP
    assert sess.idle_limit_s == AP_TOKEN_IDLE_S


def test_局域网上换的token寿命是十二小时():
    g = Guard(PIN, clock=假钟())
    sess = g.session_of(g.unlock(PIN, "10.0.0.5"))
    assert sess is not None
    assert sess.channel == CHANNEL_LAN
    assert sess.idle_limit_s == TOKEN_IDLE_S


def test_本机换的token也是十二小时():
    g = Guard(PIN, clock=假钟())
    sess = g.session_of(g.unlock(PIN, "127.0.0.1"))
    assert sess is not None
    assert sess.channel == CHANNEL_LOCAL
    assert sess.idle_limit_s == TOKEN_IDLE_S


def test_热点上的token过了半小时就作废():
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    tok = g.unlock(PIN, "192.168.168.20")
    clock.t = AP_TOKEN_IDLE_S + 1.0
    assert g.session_of(tok) is None


def test_闸门能列出活着的会话和指纹():
    clock = 假钟()
    g = Guard(PIN, clock=clock)
    tok = g.unlock(PIN, "10.0.0.5", operator="张三")
    assert [s.operator for s in g.sessions()] == ["张三"]
    assert g.live_refs() == {token_ref(tok)}


def test_名字太长解锁时也不报错():
    g = Guard(PIN, clock=假钟())
    sess = g.session_of(g.unlock(PIN, "10.0.0.5", operator="李" * 200))
    assert sess is not None
    assert len(sess.operator) == MAX_OPERATOR_LEN
