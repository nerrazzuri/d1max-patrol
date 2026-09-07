"""§6.5 措施 2:PIN 从不上网。以及热点上明文 PIN 只换只读凭证。"""

from __future__ import annotations

import pytest

from d1max_patrol.app.auth import (
    AUTH_PATH,
    CHALLENGE_PATH,
    MAX_NONCES,
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


def 拿证明(g: Guard, pin: str = PIN) -> tuple[str, str]:
    nonce = g.challenge()
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
    n = s.mint(now=0.0)
    assert s.take(n, now=1.0) is True
    assert s.take(n, now=2.0) is False


def test_质询过期就用不了():
    s = NonceStore()
    n = s.mint(now=0.0)
    assert s.take(n, now=NONCE_TTL_S + 1.0) is False


def test_没发过的质询用不了():
    s = NonceStore()
    assert s.take("随便编的", now=0.0) is False
    assert s.take(None, now=0.0) is False
    assert s.take(12345, now=0.0) is False


def test_质询数量有上限():
    s = NonceStore(cap=4)
    for _ in range(50):
        s.mint(now=0.0)
    assert s.count == 4


def test_默认上限是三十二():
    assert MAX_NONCES == 32


def test_两次质询不一样():
    s = NonceStore()
    assert s.mint(now=0.0) != s.mint(now=0.0)


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
        nonce = g.challenge()
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
        g.challenge()
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
