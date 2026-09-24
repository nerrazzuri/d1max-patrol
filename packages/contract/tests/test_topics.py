"""主题形状与代理侧 ACL(W00 决定 4:代理自己就拒绝越界,broker 侧 ACL 归 W00c)。"""

from __future__ import annotations

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.topics import TopicAcl, Topics

T = Topics(site_id="penang-1", robot_id="D1MAX-C40011")
PFX = "site/penang-1/robot/D1MAX-C40011"


def test_七个主题的精确形状():
    assert T.capabilities == f"{PFX}/capabilities"
    assert T.status == f"{PFX}/status"
    assert T.cmd == f"{PFX}/cmd"
    assert T.ack == f"{PFX}/cmd/ack"
    assert T.event == f"{PFX}/event"
    assert T.reconcile == f"{PFX}/reconcile"
    assert T.telemetry == f"{PFX}/telemetry"


@pytest.mark.parametrize("kind", ["capabilities", "status", "cmd", "cmd/ack", "event",
                                  "reconcile", "telemetry"])
def test_parse往返(kind):
    assert Topics.parse(f"{PFX}/{kind}") == ("penang-1", "D1MAX-C40011", kind)


@pytest.mark.parametrize("bad", ["site/a/robot/b", "site/a/robot/b/cmd/extra", "x/a/robot/b/cmd",
                                 "site/a/dog/b/cmd", "", "site//robot/b/cmd",
                                 "site/a/robot/b/nope"])
def test_parse拒绝不成形的(bad):
    with pytest.raises(ContractError):
        Topics.parse(bad)


def test_id里不许有通配和斜杠():
    for bad in ("a/b", "a+b", "a#", "", " "):
        with pytest.raises(ContractError):
            Topics(site_id=bad, robot_id="x")
        with pytest.raises(ContractError):
            Topics(site_id="x", robot_id=bad)


def test_acl_只许发自己的六个主题():
    acl = TopicAcl(T)
    for ok in (T.capabilities, T.status, T.ack, T.event, T.reconcile, T.telemetry):
        assert acl.may_publish(ok), ok
    assert not acl.may_publish(T.cmd), "狗不许给自己(或别人)下命令"
    other = Topics(site_id="penang-1", robot_id="D1MAX-OTHER")
    for bad in (other.status, other.ack, other.event, other.cmd, "site/penang-1/robot/+/status",
                "site/penang-2/robot/D1MAX-C40011/status"):
        assert not acl.may_publish(bad), bad


def test_acl_只许订自己的cmd():
    acl = TopicAcl(T)
    assert acl.may_subscribe(T.cmd)
    for bad in ("site/+/robot/+/cmd", "site/penang-1/robot/+/cmd", f"{PFX}/#",
                Topics(site_id="penang-1", robot_id="D1MAX-OTHER").cmd, T.status, T.ack):
        assert not acl.may_subscribe(bad), bad
