"""§6.5 措施 2:PIN 从不上网。以及热点上明文 PIN 只换只读凭证。"""

from __future__ import annotations

import pytest

from d1max_patrol.app.auth import (
    AUTH_PATH,
    CHALLENGE_PATH,
    MAX_NONCES,
    MAX_NONCES_PER_CLIENT,
    NONCE_TTL_S,
    OPEN_PATHS,
    PROOF_ALG,
    READONLY_OPEN_PATHS,
    Denied,
    Guard,
    NonceStore,
    proof_for,
)

PIN = "428913"
HOST = {"host": "192.168.168.100:8095"}


class 假钟:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def 拿证明(g: Guard, pin: str = PIN,
         client: str = "10.0.0.5") -> tuple[str, str]:
    nonce = g.challenge(client)
    return nonce, proof_for(pin, nonce)


# -------------------------------------------------------------- 证明本身


def test_证明是六十四位十六进制():
    p = proof_for(PIN, "abcd")
    assert len(p) == 64
    assert all(c in "0123456789abcdef" for c in p)


def test_同样的pin和质询算出同样的证明():
    assert proof_for(PIN, "abcd") == proof_for(PIN, "abcd")


def test_pin不同证明就不同():
    assert proof_for(PIN, "abcd") != proof_for("000000", "abcd")


def test_质询不同证明就不同():
    assert proof_for(PIN, "abcd") != proof_for(PIN, "efgh")


def test_证明里看不出pin():
    assert PIN not in proof_for(PIN, "abcd")


def test_算法名字写死了给客户端看():
    assert PROOF_ALG == "HMAC-SHA256"


# ------------------------------------------------------------------ 质询


def test_质询是一次性的():
    s = NonceStore()
    n = s.mint("10.0.0.5", now=0.0)
    assert s.take(n, now=1.0) is True
    assert s.take(n, now=2.0) is False


def test_质询过期就用不了():
    s = NonceStore()
    n = s.mint("10.0.0.5", now=0.0)
    assert s.take(n, now=NONCE_TTL_S + 1.0) is False


def test_没发过的质询用不了():
    s = NonceStore()
    assert s.take("随便编的", now=0.0) is False
    assert s.take(None, now=0.0) is False
    assert s.take(12345, now=0.0) is False


def test_质询数量有上限():
    """内存兜底:很多个来源一起狂发,总量还是压得住。

    每个来源顶多 ``per_client`` 个,全局到顶之后**新来源**被 429 挡在外面。
    这里用 100 个不同来源去撞 cap=8 的全局上限。
    """
    s = NonceStore(cap=8, per_client=1)
    发出去 = 0
    for i in range(100):
        try:
            s.mint(f"10.0.0.{i}", now=0.0)
        except Denied:
            continue
        发出去 += 1
    assert 发出去 == 8
    assert s.count == 8


def test_默认全局上限和每个来源的上限():
    """全局 512 是内存兜底,每个来源 4 个才是那道防线本身。

    早先只有一个全局 FIFO(32),于是一个未鉴权的人连发 32 次就能把所有人
    还没用掉的质询挤光 —— 见 ``NonceStore`` 的第二个假想敌。
    """
    assert MAX_NONCES == 512
    assert MAX_NONCES_PER_CLIENT == 4
    # 每个来源的配额必须**远小于**全局上限,不然分桶等于没分。
    assert MAX_NONCES_PER_CLIENT * 8 < MAX_NONCES


# ------------------------------------------------ 别人挤不掉我的质询(P1-2)


def test_攻击者灌满之后好人照样换得到token():
    """**这是这条修复的正题。**

    假想敌:热点射程之内、或者局域网上任何能连到这个端口的人。他没有 PIN、
    没有 token —— 取质询这条路本来就不要 token(要了就没人换得到 token)。
    他能做的就是狂发 ``GET /api/auth/challenge``。

    早先这个池子是一个全局 FIFO,他连发 ``cap`` 次就把好人刚取到、还没用掉
    的那条挤掉,好人只能回去用明文 PIN —— 在热点上那是**只读**凭证,应急
    通道当场开不动狗。所以这里断的不是"他被挡住了",是**好人没被影响**。
    """
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    nonce = g.challenge("192.168.168.20")           # 好人先取一个
    for _ in range(200):                            # 攻击者可劲儿灌
        try:
            g.challenge("192.168.168.99")
        except Denied:
            pass
    token = g.unlock_proof(nonce, proof_for(PIN, nonce), "192.168.168.20")
    assert token


def test_同一个来源灌到桶满返429():
    """桶满了必须是 429 不是 400。

    400 会被手机端读成"这个质询坏了,再取一个",于是它立刻再打一次,把自己
    锁得更死;429 才说得清"这是限流,等一会儿"。
    """
    s = NonceStore(per_client=4)
    for _ in range(4):
        s.mint("10.0.0.7", now=0.0)
    with pytest.raises(Denied) as err:
        s.mint("10.0.0.7", now=0.0)
    assert err.value.status == 429
    # 别人的桶不受影响。
    assert s.mint("10.0.0.8", now=0.0)


def test_桶里过期的会被回收():
    """淘汰**只淘汰过期的** —— 时间靠参数传,不许 sleep(§8.5 第 2 条)。"""
    s = NonceStore(per_client=2)
    s.mint("10.0.0.7", now=0.0)
    s.mint("10.0.0.7", now=0.0)
    with pytest.raises(Denied):
        s.mint("10.0.0.7", now=0.0)
    # 过期之后位置腾出来了。
    assert s.mint("10.0.0.7", now=NONCE_TTL_S + 1.0)
    assert s.count_for("10.0.0.7") == 1


def test_新取的质询挤不掉别人还没过期的():
    """这是 FIFO 淘汰和"只清过期的"之间的差别,单独钉一条。"""
    s = NonceStore(cap=2, per_client=1)
    我的 = s.mint("10.0.0.7", now=0.0)
    s.mint("10.0.0.8", now=0.0)
    with pytest.raises(Denied):                     # 全局满了,新来源被挡
        s.mint("10.0.0.9", now=1.0)
    assert s.take(我的, now=2.0) is True             # 我的还在


def test_两次质询不一样():
    s = NonceStore()
    assert s.mint("10.0.0.5", now=0.0) != s.mint("10.0.0.5", now=0.0)


# ------------------------------------------------------------ 用证明解锁


def test_证明对了换得到token():
    g = Guard(PIN, clock=假钟())
    nonce, proof = 拿证明(g)
    tok = g.unlock_proof(nonce, proof, "192.168.168.20", operator="张三")
    sess = g.session_of(tok)
    assert sess is not None
    assert sess.operator == "张三"


def test_热点上用证明换到的是能操作的凭证():
    g = Guard(PIN, clock=假钟())
    nonce, proof = 拿证明(g)
    sess = g.session_of(g.unlock_proof(nonce, proof, "192.168.168.20"))
    assert sess is not None
    assert sess.readonly is False


def test_证明不对就拒():
    g = Guard(PIN, clock=假钟())
    nonce, _ = 拿证明(g)
    with pytest.raises(Denied) as err:
        g.unlock_proof(nonce, proof_for("000000", nonce), "10.0.0.5")
    assert err.value.status == 401


def test_同一个质询用两次不行():
    g = Guard(PIN, clock=假钟())
    nonce, proof = 拿证明(g)
    g.unlock_proof(nonce, proof, "10.0.0.5")
    with pytest.raises(Denied) as err:
        g.unlock_proof(nonce, proof, "10.0.0.5")
    assert err.value.status == 400


def test_质询过期了要重新取():
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    nonce, proof = 拿证明(g)
    clock.t = NONCE_TTL_S + 1.0
    with pytest.raises(Denied) as err:
        g.unlock_proof(nonce, proof, "10.0.0.5")
    assert err.value.status == 400
    assert "质询" in err.value.error


def test_质询过期不算一次猜pin():
    """网络往返慢不是在爆破。质询过期不该把人锁掉。"""
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    for _ in range(20):
        nonce, proof = 拿证明(g)
        clock.t += NONCE_TTL_S + 1.0
        with pytest.raises(Denied):
            g.unlock_proof(nonce, proof, "10.0.0.5")
    assert g.throttle.locked_for("10.0.0.5", now=clock.t) == 0.0


def test_证明连错也会被限速():
    """两条路在猜同一个六位 PIN,分开计数等于把爆破防线开一半。"""
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    for _ in range(6):
        nonce = g.challenge("10.0.0.5")
        with pytest.raises(Denied):
            g.unlock_proof(nonce, proof_for("000000", nonce), "10.0.0.5")
    assert g.throttle.locked_for("10.0.0.5", now=clock.t) > 0


def test_明文和证明共用同一条限速():
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    for _ in range(5):
        with pytest.raises(Denied):
            g.unlock("000000", "10.0.0.5")
    nonce, proof = 拿证明(g)
    with pytest.raises(Denied) as err:
        g.unlock_proof(nonce, proof, "10.0.0.5")
    assert err.value.status == 429


def test_没设pin就没有质询():
    g = Guard(None)
    with pytest.raises(Denied) as err:
        g.challenge("10.0.0.5")
    assert err.value.status == 400


# ------------------------------------------ 热点上的明文 PIN 只换只读凭证


def test_热点上明文pin换到的是只读凭证():
    g = Guard(PIN, clock=假钟())
    sess = g.session_of(g.unlock(PIN, "192.168.168.20"))
    assert sess is not None
    assert sess.readonly is True


def test_局域网上明文pin换到的能操作():
    g = Guard(PIN, clock=假钟())
    sess = g.session_of(g.unlock(PIN, "10.0.0.5"))
    assert sess is not None
    assert sess.readonly is False


def test_本机明文pin换到的能操作():
    g = Guard(PIN, clock=假钟())
    sess = g.session_of(g.unlock(PIN, "127.0.0.1"))
    assert sess is not None
    assert sess.readonly is False


# ------------------------------------------------------------ 闸门认只读


def test_只读凭证能看():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "192.168.168.20")
    sess = g.gate("GET", "/api/state", {**HOST, "authorization": f"Bearer {tok}"}, {})
    assert sess is not None
    assert sess.readonly is True


def test_只读凭证不能写():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "192.168.168.20")
    with pytest.raises(Denied) as err:
        g.gate("POST", "/api/runs", {**HOST, "authorization": f"Bearer {tok}"}, {})
    assert err.value.status == 403
    assert "只能看" in err.value.error


def test_只读凭证也按得了急停():
    """§3.5 规则 2:急停永远不需要控制权,也永远不需要"写权限"。"""
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "192.168.168.20")
    sess = g.gate("POST", "/api/estop",
                  {**HOST, "authorization": f"Bearer {tok}"}, {})
    assert sess is not None


def test_只读凭证也退得了登录():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "192.168.168.20")
    assert g.gate("POST", "/api/auth/logout",
                  {**HOST, "authorization": f"Bearer {tok}"}, {}) is not None


def test_能操作的凭证写得了():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "10.0.0.5")
    sess = g.gate("POST", "/api/runs",
                  {**HOST, "authorization": f"Bearer {tok}"}, {})
    assert sess is not None
    assert sess.readonly is False


def test_放行时把会话带回来():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "10.0.0.5", operator="张三")
    sess = g.gate("GET", "/api/state",
                  {**HOST, "authorization": f"Bearer {tok}"}, {})
    assert sess is not None
    assert sess.operator == "张三"


def test_没设pin时闸门放行但没有会话():
    g = Guard(None)
    assert g.gate("GET", "/api/state", {"host": "127.0.0.1:8095"}, {}) is None


def test_质询接口不要token():
    g = Guard(PIN, clock=假钟())
    assert g.gate("GET", CHALLENGE_PATH, HOST, {}) is None


def test_两条免token的路径就这两条():
    assert OPEN_PATHS == {AUTH_PATH, CHALLENGE_PATH}


def test_只读也放行的两条就这两条():
    assert READONLY_OPEN_PATHS == {"/api/estop", "/api/auth/logout"}
