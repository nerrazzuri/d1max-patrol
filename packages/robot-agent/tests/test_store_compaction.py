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


def test_事件簿_换了boot之后上一个boot的整个清掉(tmp_path):
    p = tmp_path / "events.jsonl"
    b = EventBook(p, boot_id="b1", now_ms=lambda: 1)
    for i in range(5):
        b.emit("tick", {"i": i})
    EventBook(p, boot_id="b2", now_ms=lambda: 1)
    assert _lines(p) == 0


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
