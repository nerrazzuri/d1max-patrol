"""命令幂等落盘:command_id → 结果,重启后仍在;坏行只丢那一行。"""

from __future__ import annotations

from d1max_agent.idempotency import IdempotencyStore
from d1max_contract.messages import Ack, AckResult

A = Ack(command_id="c1", task_id="t1", result=AckResult.ACCEPTED)
R = Ack(command_id="c2", task_id="t2", result=AckResult.REJECTED, reason="busy")


def test_记住再查(tmp_path):
    s = IdempotencyStore(tmp_path / "idem.jsonl")
    assert s.lookup("c1") is None
    s.remember(A)
    s.remember(R)
    assert s.lookup("c1") == A and s.lookup("c2") == R


def test_重开同一路径仍能查到(tmp_path):
    IdempotencyStore(tmp_path / "idem.jsonl").remember(A)
    again = IdempotencyStore(tmp_path / "idem.jsonl")
    assert again.lookup("c1") == A
    assert len(again) == 1


def test_坏一行只丢那一行(tmp_path, caplog):
    p = tmp_path / "idem.jsonl"
    IdempotencyStore(p).remember(A)
    with p.open("a", encoding="utf-8") as f:
        f.write("{garbage\n")
    IdempotencyStore(p).remember(R)
    s = IdempotencyStore(p)
    assert s.lookup("c1") == A and s.lookup("c2") == R
    assert any("坏" in r.getMessage() or "丢" in r.getMessage() for r in caplog.records)


def test_同一命令记两次以第一次为准(tmp_path):
    s = IdempotencyStore(tmp_path / "idem.jsonl")
    s.remember(A)
    s.remember(Ack(command_id="c1", task_id="t1", result=AckResult.REJECTED, reason="x"))
    assert s.lookup("c1") == A
