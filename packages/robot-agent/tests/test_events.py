"""事件簿:(boot_id, seq) 单调、落盘 outbox、未确认区间给 reconcile 用。"""

from __future__ import annotations

from d1max_agent.events import EventBook


def _book(tmp_path, boot="b1"):
    return EventBook(tmp_path / "events.jsonl", boot_id=boot, now_ms=lambda: 42)


def test_seq从1单调(tmp_path):
    b = _book(tmp_path)
    e1 = b.emit("task_progress", {"d": 1})
    e2 = b.emit("task_done", {})
    assert (e1.seq, e2.seq) == (1, 2)
    assert e1.boot_id == "b1" and e1.stamp == 42 and e1.event_id
    assert e1.event_id != e2.event_id


def test_未确认区间与确认(tmp_path):
    b = _book(tmp_path)
    assert b.unacked_range() == (0, 0)
    for _ in range(3):
        b.emit("task_progress", {})
    assert b.unacked_range() == (1, 3)
    b.mark_acked(2)
    assert b.unacked_range() == (3, 3)
    assert [e.seq for e in b.pending()] == [3]
    b.mark_acked(3)
    assert b.unacked_range() == (0, 0) and b.pending() == []


def test_重启换boot_id后seq归1(tmp_path):
    _book(tmp_path).emit("task_progress", {})
    b2 = _book(tmp_path, boot="b2")
    e = b2.emit("task_progress", {})
    assert e.seq == 1 and e.boot_id == "b2"


def test_outbox落盘重放_同boot续号(tmp_path):
    b = _book(tmp_path)
    b.emit("task_progress", {})
    b.emit("task_progress", {})
    again = EventBook(tmp_path / "events.jsonl", boot_id="b1", now_ms=lambda: 43)
    assert again.unacked_range() == (1, 2)
    assert again.emit("task_done", {}).seq == 3
