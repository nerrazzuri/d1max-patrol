"""§3.6:一只狗同时最多 3 个 app 会话,第 4 个直接拒,不排队。"""

from __future__ import annotations

import pytest

from d1max_patrol.app.auth import (
    MAX_SESSIONS,
    MAX_TOKENS,
    Denied,
    Guard,
    proof_for,
    token_ref,
)

PIN = "428913"


class 假钟:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_上限是三():
    assert MAX_SESSIONS == 3


def test_三个人连得上():
    g = Guard(PIN, clock=假钟())
    for i, name in enumerate(("张三", "李四", "王五")):
        g.unlock(PIN, f"10.0.0.{i}", operator=name)
    assert len(g.sessions()) == 3


def test_第四个被拒():
    g = Guard(PIN, clock=假钟())
    for i, name in enumerate(("张三", "李四", "王五")):
        g.unlock(PIN, f"10.0.0.{i}", operator=name)
    with pytest.raises(Denied) as err:
        g.unlock(PIN, "10.0.0.9", operator="赵六")
    assert err.value.status == 409
    assert "3 个人" in err.value.error


def test_被拒时说清名额被谁占着():
    g = Guard(PIN, clock=假钟())
    for i, name in enumerate(("张三", "李四", "王五")):
        g.unlock(PIN, f"10.0.0.{i}", operator=name)
    with pytest.raises(Denied) as err:
        g.unlock(PIN, "10.0.0.9", operator="赵六")
    for name in ("张三", "李四", "王五"):
        assert name in err.value.detail


def test_没报名字的占位也说得出是谁():
    g = Guard(PIN, clock=假钟())
    toks = [g.unlock(PIN, f"10.0.0.{i}") for i in range(3)]
    with pytest.raises(Denied) as err:
        g.unlock(PIN, "10.0.0.9", operator="赵六")
    for tok in toks:
        assert token_ref(tok) in err.value.detail


def test_第四个不排队而是当场失败():
    """排队意味着"等着等着突然就连上了" —— 现场没人受得了这个。"""
    g = Guard(PIN, clock=假钟())
    for i in range(3):
        g.unlock(PIN, f"10.0.0.{i}", operator=f"人{i}")
    with pytest.raises(Denied):
        g.unlock(PIN, "10.0.0.9", operator="赵六")
    assert len(g.sessions()) == 3


def test_有人退出就腾出名额():
    g = Guard(PIN, clock=假钟())
    toks = [g.unlock(PIN, f"10.0.0.{i}", operator=f"人{i}") for i in range(3)]
    assert g.logout(toks[0]) is True
    tok = g.unlock(PIN, "10.0.0.9", operator="赵六")
    assert g.session_of(tok) is not None


def test_退一个不存在的token不算错():
    g = Guard(PIN, clock=假钟())
    assert g.logout("根本没这个") is False


def test_退出之后那个token就废了():
    g = Guard(PIN, clock=假钟())
    tok = g.unlock(PIN, "10.0.0.5", operator="张三")
    g.logout(tok)
    assert g.session_of(tok) is None


def test_闲置到期腾出名额():
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    for i in range(3):
        g.unlock(PIN, f"192.168.168.{i}", operator=f"人{i}")
    clock.t = 30 * 60.0 + 1.0
    tok = g.unlock(PIN, "10.0.0.9", operator="赵六")
    assert g.session_of(tok) is not None
    assert len(g.sessions()) == 1


def test_同一个人换台设备挤掉自己的老会话():
    """app 重装了、手机换了,回来还是同一个报名的人 —— 挤掉的是他自己。"""
    g = Guard(PIN, clock=假钟())
    老 = g.unlock(PIN, "10.0.0.1", operator="张三")
    g.unlock(PIN, "10.0.0.2", operator="李四")
    g.unlock(PIN, "10.0.0.3", operator="王五")
    新 = g.unlock(PIN, "10.0.0.4", operator="张三")
    assert g.session_of(老) is None
    assert g.session_of(新) is not None
    assert len(g.sessions()) == 3


def test_没报名字的不许互相挤():
    """空名字不是身份。让空名字互相挤,等于把上限废掉。"""
    g = Guard(PIN, clock=假钟())
    for i in range(3):
        g.unlock(PIN, f"10.0.0.{i}")
    with pytest.raises(Denied):
        g.unlock(PIN, "10.0.0.9")


def test_用证明解锁也受上限管():
    g = Guard(PIN, clock=假钟())
    for i in range(3):
        g.unlock(PIN, f"10.0.0.{i}", operator=f"人{i}")
    nonce = g.challenge()
    with pytest.raises(Denied) as err:
        g.unlock_proof(nonce, proof_for(PIN, nonce), "10.0.0.9",
                       operator="赵六")
    assert err.value.status == 409


def test_名额满不算一次pin错误():
    """名额满跟猜 PIN 是两件事,不该把人往爆破锁里推。"""
    clock = 假钟(0.0)
    g = Guard(PIN, clock=clock)
    for i in range(3):
        g.unlock(PIN, f"10.0.0.{i}", operator=f"人{i}")
    for _ in range(10):
        with pytest.raises(Denied):
            g.unlock(PIN, "10.0.0.9", operator="赵六")
    assert g.throttle.locked_for("10.0.0.9", now=clock.t) == 0.0


def test_上限可以调低方便测试():
    g = Guard(PIN, clock=假钟(), max_sessions=1)
    g.unlock(PIN, "10.0.0.1", operator="张三")
    with pytest.raises(Denied):
        g.unlock(PIN, "10.0.0.2", operator="李四")


def test_会话上限和内存兜底是两条不同的线():
    assert MAX_SESSIONS == 3
    assert MAX_TOKENS == 64
    assert MAX_SESSIONS < MAX_TOKENS
