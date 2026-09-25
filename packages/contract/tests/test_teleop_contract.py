"""W00c5c:遥控经站点的契约。帧走专用主题 ``teleop``(QoS 0,站点写、狗只读自己的 —— **绝不经
cmd**:持久会话会补投旧帧,那就是决策 7 禁止的重放);租约与停车走 cmd。"""

from __future__ import annotations

import math

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.teleop import (
    FRAME_TTL_MAX_MS,
    TeleopFrame,
    TeleopLease,
    parse_teleop_grant,
    parse_teleop_lease,
    teleop_grant_payload,
)
from d1max_contract.topics import PUBLISH_KINDS, TopicAcl, Topics

T = Topics(site_id="s", robot_id="r")


def test_遥控主题_狗只能订自己的_不能发():
    assert T.teleop == "site/s/robot/r/teleop"
    acl = TopicAcl(T)
    assert acl.may_subscribe(T.teleop) and acl.may_subscribe(T.cmd)
    assert not acl.may_publish(T.teleop), "帧只有站点能发"
    assert "teleop" not in PUBLISH_KINDS
    assert not acl.may_subscribe(Topics(site_id="s", robot_id="other").teleop)
    assert Topics.parse("site/s/robot/r/teleop") == ("s", "r", "teleop")


def test_帧往返():
    f = TeleopFrame(lease_epoch=3, seq=17, sent_at=1_800_000_000_000, ttl_ms=300, vx=0.2, wz=-0.4)
    assert TeleopFrame.from_wire(f.to_wire()) == f


@pytest.mark.parametrize("bad", [
    {"lease_epoch": 0}, {"seq": 0}, {"seq": True}, {"sent_at": -1}, {"ttl_ms": 10},
    {"ttl_ms": FRAME_TTL_MAX_MS + 1}, {"vx": math.nan}, {"wz": math.inf}, {"vx": 9.0},
    {"vx": "0.1"},
])
def test_坏帧都拒(bad):
    d = TeleopFrame(lease_epoch=1, seq=1, sent_at=1, ttl_ms=300, vx=0.0, wz=0.0).to_wire() | bad
    with pytest.raises(ContractError):
        TeleopFrame.from_wire(d)


def test_授予与续租放租的载荷():
    g = teleop_grant_payload(lease_epoch=4, operator="gina", lease_ttl_ms=5000)
    assert parse_teleop_grant(g) == (4, "gina", 5000)
    for bad in ({**g, "lease_epoch": 0}, {**g, "operator": ""}, {**g, "lease_ttl_ms": 100},
                {**g, "operator": "x" * 65}):
        with pytest.raises(ContractError):
            parse_teleop_grant(bad)
    r = TeleopLease(action="renew", lease_epoch=4, lease_ttl_ms=5000)
    assert parse_teleop_lease(r.to_payload()) == r
    assert parse_teleop_lease(TeleopLease("release", 4).to_payload()).action == "release"
    for bad in ({"action": "steal", "lease_epoch": 4}, {"action": "renew", "lease_epoch": -1},
                {"action": "renew", "lease_epoch": 4, "lease_ttl_ms": 999_999}):
        with pytest.raises(ContractError):
            parse_teleop_lease(bad)


def test_遥控任务登记了资源与断线策略():
    from d1max_contract.policy import policy_for
    from d1max_contract.resources import conflicts, resources_for
    assert resources_for("teleop") == frozenset({"motion"})
    assert conflicts("teleop", "patrol") and conflicts("teleop", "goto")
    assert policy_for("teleop").on_disconnect == "stop_and_wait"
    assert resources_for("halt") == frozenset()
