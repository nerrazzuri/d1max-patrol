"""资源仲裁:占 motion 的互斥;抢占要先停旧、等停止确认、再给新。"""

from __future__ import annotations

from d1max_agent.resources import ResourceLedger


def test_互斥与释放():
    L = ResourceLedger()
    assert L.conflicts("goto") == set()
    L.acquire("t1", "goto")
    assert L.holder("motion") == "t1"
    assert L.conflicts("goto") == {"t1"}
    assert L.conflicts("abort") == set()
    L.release("t1")
    assert L.holder("motion") is None


def test_未知任务不许默认():
    import pytest
    L = ResourceLedger()
    with pytest.raises(KeyError):
        L.conflicts("dance")
