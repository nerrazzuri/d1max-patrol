"""写盘失败不卡死引擎(W00c6a,核查 B)。

以前:第一张照片存不下 → 兜底 ``_do_abort`` 第一步要写事件流、又抛 → ``_finish`` 再抛 →
``_done.set()`` 走不到。引擎任务带着 ``OSError`` 死掉,对外快照一直是 RUNNING,代理的任务永远
在跑。盘满、eMMC 只读重挂都会触发。现在:归档自己兜住写失败;照片存不下按这个点失败;收尾
路径自己炸了也把状态落成 ABORTED、先停导航、把 ``_done`` 置上。
"""

from __future__ import annotations

import errno

import pytest

from d1max_agent.engine import archive as arc
from d1max_agent.engine.machine import FINAL_STATES, RunState

from .conftest import _HOME


@pytest.fixture
def 盘(monkeypatch):
    """``state["full"]`` 一置上,归档的每一处写都抛 ENOSPC —— 跟真盘满一样,不是只坏一处。"""
    state = {"full": False, "at_photo": False}
    real_line, real_atomic = arc.RunArchive._line, arc._atomic_write
    real_save = arc.RunArchive.save_photo

    def 满():
        raise OSError(errno.ENOSPC, "No space left on device")

    def line(self, handle_name, filename, payload):
        if state["full"] and getattr(self, handle_name) is None:
            满()
        if state["full"]:
            setattr(self, handle_name, _满句柄())
        return real_line(self, handle_name, filename, payload)

    def atomic(path, text):
        if state["full"]:
            满()
        return real_atomic(path, text)

    def save(self, *a, **k):
        if state["at_photo"]:
            state["full"] = True
        if state["full"]:
            from pathlib import Path
            real_write = Path.write_bytes
            Path.write_bytes = lambda p, d: 满()
            try:
                return real_save(self, *a, **k)
            finally:
                Path.write_bytes = real_write
        return real_save(self, *a, **k)

    monkeypatch.setattr(arc.RunArchive, "_line", line)
    monkeypatch.setattr(arc, "_atomic_write", atomic)
    monkeypatch.setattr(arc.RunArchive, "save_photo", save)
    return state


class _满句柄:
    def write(self, _):
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        pass

    def close(self):
        pass


async def test_第一张照片时盘满_引擎照样走完_这个点按照片存不下失败(make_engine, sample_mission,
                                                     nav, 盘):
    盘["at_photo"] = True
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)
    state = await engine.wait_done(timeout_s=3.0)
    assert state in FINAL_STATES, "引擎不许卡在 RUNNING"
    assert engine.snapshot.state is state, "对外快照也是终态"
    first = engine.snapshot.results[0]
    assert not first.ok and "照片存不下" in first.note
    assert "No space" in engine.archive_error
    await engine.aclose()


async def test_跑到一半所有写都失败_引擎照样走完(make_engine, sample_mission, nav, 盘):
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)
    await engine.wait_state(RunState.RUNNING)
    盘["full"] = True
    state = await engine.wait_done(timeout_s=3.0)
    assert state in FINAL_STATES and engine.snapshot.state is state
    assert engine.archive_error
    await engine.aclose()


async def test_收尾路径自己炸了_状态落成ABORTED_先停导航_等得到结束(make_engine, sample_mission,
                                                    nav, monkeypatch):
    """兜底的兜底:就算广播本身抛(不是归档,是我们没想到的那种),引擎也要落成终态、停导航、
    把 ``wait_done`` 放回来 —— 不然代理的任务永远等一个死掉的引擎。"""
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)
    await engine.wait_state(RunState.RUNNING)

    def 炸(*_a, **_k):
        raise RuntimeError("广播炸了")
    monkeypatch.setattr(engine, "_publish", 炸)
    stops = nav.stop_calls
    await engine.abort("人工中止")
    state = await engine.wait_done(timeout_s=3.0)
    assert state is RunState.ABORTED
    assert nav.stop_calls > stops, "导航要停"
    assert not engine.running
    await engine.aclose()
