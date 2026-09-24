"""§6.4 的三个时间尺度:量级差着三个数量级,谁也不许替谁。

这个文件放在 tests 顶层而不是 tests/engine 或 tests/app 下面,因为它横跨两
层 —— 分层规矩管的是 src/,测试本身不是一层。
"""

from __future__ import annotations

from d1max_agent.engine.lease import (
    LEASE_HEARTBEAT_MS,
    LEASE_TTL_MS,
    TAKEOVER_GRACE_MS,
)
from d1max_patrol.app.auth import TOKEN_IDLE_S
from d1max_patrol.app.teleop import HEARTBEAT_TIMEOUT_S


def test_三个尺度的值就是规格里写的那三个():
    assert HEARTBEAT_TIMEOUT_S == 0.6
    assert LEASE_TTL_MS == 30_000
    assert LEASE_HEARTBEAT_MS == 10_000
    assert TOKEN_IDLE_S == 12 * 3600.0


def test_三个尺度严格分开而且量级拉得够远():
    守死人_ms = HEARTBEAT_TIMEOUT_S * 1000
    token_ms = TOKEN_IDLE_S * 1000
    assert 守死人_ms < LEASE_HEARTBEAT_MS < LEASE_TTL_MS < token_ms
    assert LEASE_HEARTBEAT_MS >= 10 * 守死人_ms
    assert token_ms >= 100 * LEASE_TTL_MS


def test_心跳周期是ttl的三分之一():
    """丢两拍还有救,丢三拍才掉 —— 跟守死人那一层是同一个比例。"""
    assert LEASE_TTL_MS == 3 * LEASE_HEARTBEAT_MS


def test_接管宽限期夹在心跳和ttl之间():
    """礼貌接管的 15 秒不是随手定的,它被两头夹死(§3.5 规则 3)。

    **下界是一拍心跳**:持有者是靠 10 秒一次的心跳往返才看得见"有人要接管"
    这件事的,宽限期短于一拍,他还没轮询到就已经被移交了 —— 那不叫礼貌接管,
    叫延迟了的强夺,而界面上还写着"对方有机会同意"。
    **上界是 TTL**:宽限期长到超过 30 秒,礼貌接管就永远等不到头 —— 租约自己
    先过期了,接管的人拿到的是一份"过期继承"而不是一次留了痕的移交,审计上
    看是 ``expired`` 不是 ``taken_over``,现场问"谁把我挤下去的"就没有答案。
    """
    assert LEASE_HEARTBEAT_MS <= TAKEOVER_GRACE_MS < LEASE_TTL_MS
