"""W00c5d:狗的盘况随遥测上站点(决策 8:发件箱有上限、看得见)。"""

from __future__ import annotations

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.messages import TaskState, Telemetry
from d1max_contract.storage import StorageFacts


def _facts(**kw):
    base = dict(disk_used_ratio=0.42, outbox_bytes=1_000, outbox_cap_bytes=20 * 2**30,
                backlog_files=3, backlog_bytes=900, oldest_backlog_s=12)
    return StorageFacts(**(base | kw))


def test_盘况往返():
    f = _facts()
    assert StorageFacts.from_wire(f.to_wire()) == f
    assert StorageFacts.from_wire(_facts(oldest_backlog_s=None).to_wire()).oldest_backlog_s \
        is None


@pytest.mark.parametrize("bad", [
    {"disk_used_ratio": 1.5}, {"disk_used_ratio": -0.1}, {"disk_used_ratio": float("nan")},
    {"outbox_bytes": -1}, {"backlog_files": True}, {"backlog_bytes": 1.5},
    {"oldest_backlog_s": -3}, {"outbox_cap_bytes": 0},
])
def test_坏盘况都拒(bad):
    w = _facts().to_wire() | bad
    with pytest.raises(ContractError):
        StorageFacts.from_wire(w)


def test_遥测带不带盘况都行_老狗不带也收():
    t = Telemetry(stamp=1, pose=None, battery_pct=50.0, task_state=TaskState.RUNNING,
                  loc_quality=1.0, storage=_facts())
    back = Telemetry.from_wire(t.to_wire())
    assert back.storage == _facts()
    w = Telemetry(stamp=1, pose=None, battery_pct=50.0, task_state=None,
                  loc_quality=1.0).to_wire()
    assert "storage" not in w, "没有就不发(1 Hz 的报文不白长)"
    assert Telemetry.from_wire(w).storage is None


def test_满没满():
    assert not _facts().full(stop_ratio=0.9)
    assert _facts(disk_used_ratio=0.91).full(stop_ratio=0.9)
    assert _facts(outbox_bytes=20 * 2**30).full(stop_ratio=0.9), "发件箱到上限也算满"
