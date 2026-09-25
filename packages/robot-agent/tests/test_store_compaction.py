"""W00c5d(决策 8):狗上的事件簿与幂等记录不再只增不减。已确认的事件、太老的幂等记录定期清掉;
没确认的事件、最近的幂等记录一条不丢。"""

from __future__ import annotations

from d1max_agent import events as events_mod
from d1max_agent import idempotency as idem_mod
from d1max_agent.events import EventBook
from d1max_agent.idempotency import IdempotencyStore
from d1max_contract.messages import Ack, AckResult


def _lines(p):
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def test_事件簿_确认过的多了就压实_没确认的一条不丢(tmp_path, monkeypatch):
    monkeypatch.setattr(events_mod, "COMPACT_MIN_LINES", 50)
    p = tmp_path / "events.jsonl"
    b = EventBook(p, boot_id="b1", now_ms=lambda: 1)
    for i in range(200):
        b.emit("tick", {"i": i})
    b.mark_acked(190)                                # 这一下压实:手里还有 10 条没确认的
    assert _lines(p) < 60, "确认过的不该一直留着"
    again = EventBook(p, boot_id="b1", now_ms=lambda: 1)
    assert [e.seq for e in again.pending()] == list(range(191, 201))
    assert again.last_seq == 200


def test_事件簿_换了boot_上一个boot没确认的带过来补发_确认过的清掉(tmp_path):
    """决策 8:断网暂存的东西恢复后要传上去 ——
    代理重启(新的 boot)不许把没确认的任务结果、故障丢了。"""
    p = tmp_path / "events.jsonl"
    b = EventBook(p, boot_id="b1", now_ms=lambda: 1)
    for i in range(5):
        b.emit("tick", {"i": i})
    b.mark_acked(3)
    b2 = EventBook(p, boot_id="b2", now_ms=lambda: 99)
    got = b2.pending()
    assert [e.data["i"] for e in got] == [3, 4] and [e.seq for e in got] == [1, 2]
    assert all(e.boot_id == "b2" and e.stamp == 1 for e in got), "原来的时刻"
    assert got[0].data["carried_from"].startswith("evt-b1-")
    assert _lines(p) == 2
    b3 = EventBook(p, boot_id="b3", now_ms=lambda: 99)
    assert [e.data["i"] for e in b3.pending()] == [3, 4], "再重启:不重复带"
    assert b3.pending()[0].data["carried_from"] == got[0].data["carried_from"]


def test_事件簿_带到一半断电_不重复(tmp_path):
    p = tmp_path / "events.jsonl"
    b = EventBook(p, boot_id="b1", now_ms=lambda: 1)
    first = b.emit("tick", {"i": 0})
    b2 = EventBook(p, boot_id="b2", now_ms=lambda: 1)
    carried = b2.pending()[0]
    import json
    with p.open("a", encoding="utf-8") as f:           # 模拟压实之前断电:老的那条还在文件里
        f.write(json.dumps({"op": "event", "event": first.to_wire()}) + "\n")
    b3 = EventBook(p, boot_id="b3", now_ms=lambda: 1)
    assert [e.data.get("carried_from") for e in b3.pending()] == [carried.data["carried_from"]]


def _ack(i):
    return Ack(f"c{i}", f"t{i}", AckResult.ACCEPTED)


def test_幂等记录_只留最近的一批_重启照样认得最近的(tmp_path, monkeypatch):
    monkeypatch.setattr(idem_mod, "KEEP", 100)
    p = tmp_path / "idem.jsonl"
    s = IdempotencyStore(p)
    for i in range(350):
        s.remember(_ack(i))
    assert _lines(p) <= 200 and len(s) <= 200
    again = IdempotencyStore(p)
    assert again.lookup("c349") is not None and again.lookup("c250") is not None
    assert again.lookup("c0") is None, "太老的清掉了(命令早过期,再来也只会回 expired)"
