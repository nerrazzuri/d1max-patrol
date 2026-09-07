"""§6.4 的三个时间尺度:量级差着三个数量级,谁也不许替谁。

这个文件放在 tests 顶层而不是 tests/engine 或 tests/app 下面,因为它横跨两
层 —— 分层规矩管的是 src/,测试本身不是一层。
"""

from __future__ import annotations

from d1max_patrol.app.auth import TOKEN_IDLE_S
from d1max_patrol.app.teleop import HEARTBEAT_TIMEOUT_S
from d1max_patrol.engine.lease import LEASE_HEARTBEAT_MS, LEASE_TTL_MS


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
