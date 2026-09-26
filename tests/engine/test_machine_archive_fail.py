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


async def test_写失败的一趟_汇总里标着残缺(make_engine, sample_mission, nav, monkeypatch):
    """W00c6a 内审 S4:盘恢复后 ``finish`` 写上汇总,这一趟就「安定」了、传完即删;站点收到的东西
    缺了一截,清单里看不出来。汇总里带上归档写失败的信息。这里只让照片写失败一次,收尾写得进去。"""
    import errno
    real = arc.RunArchive.save_photo
    n = {"left": 1}

    def 照片存不下(self, *a, **k):
        if n["left"]:
            n["left"] -= 1
            self._failed("photo", OSError(errno.ENOSPC, "No space left on device"))
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *a, **k)
    monkeypatch.setattr(arc.RunArchive, "save_photo", 照片存不下)
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)
    await engine.wait_done(timeout_s=3.0)
    await engine.aclose()
    from d1max_agent.engine.archive import read_manifest
    summary = read_manifest(engine.archive.path)["summary"]
    assert summary["archive"]["write_failures"] == 1 and "No space" in summary["archive"]["error"]


async def test_停导航抛非导航错_中止照样走完_原因不丢(make_engine, sample_mission, nav,
                                            monkeypatch):
    """W00c6a 内审 S1:HAL 停车超时这类错不是 ``NavBackendError``,以前 ``_stop_nav_quietly``
    只接导航错,这种错把中止路径炸穿,最后靠收尾兜成「引擎收尾时出错」,人工中止的原因丢了。"""
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)
    await engine.wait_state(RunState.RUNNING)

    async def 停不了():
        nav.stop_calls += 1
        raise TimeoutError("旁路进程回不了停车回执")
    monkeypatch.setattr(nav, "stop", 停不了)
    await engine.abort("人工中止")
    state = await engine.wait_done(timeout_s=3.0)
    assert state is RunState.ABORTED and engine.snapshot.reason == "人工中止"
    assert engine.crash == "" and nav.stop_calls >= 1
    await engine.aclose()
