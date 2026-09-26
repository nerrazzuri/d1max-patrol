"""报文数据类是真理源:每类 to_wire/from_wire 往返、坏字段拒、主版本不同拒。"""

from __future__ import annotations

import pytest

from d1max_contract import SCHEMA
from d1max_contract.errors import ContractError, SchemaMismatch
from d1max_contract.messages import (
    Ack,
    AckResult,
    Capabilities,
    Command,
    Event,
    MapPose,
    Precondition,
    Ready,
    Reconcile,
    Status,
    TaskState,
    TaskSummary,
    Telemetry,
)

POSE = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=1.5, y=-2.0, yaw=0.3)
CMD = Command(command_id="c-1", task_id="t-1", kind="goto", issued_at=1_000, expires_at=61_000,
              control_epoch=3, payload={"target": POSE.to_wire(), "max_speed_mps": 0.8},
              priority=1, offline_policy="default",
              precondition=Precondition(expect_task_state=None))
ACK = Ack(command_id="c-1", task_id="t-1", result=AckResult.ACCEPTED)
EVT = Event(event_id="e-1", seq=1, boot_id="b-1", stamp=1_500, kind="task_progress",
            data={"task_id": "t-1", "distance_m": 2.5})
READY = Ready(control=True, motion=True, estop_clear=True, loc_ok=True)
STATUS = Status(online=True, boot_id="b-1", ready=READY, control_epoch=3, last_seen=2_000,
                task=TaskSummary(task_id="t-1", kind="goto", state=TaskState.RUNNING))
CAPS = Capabilities(robot_id="D1MAX-C40011", agent="0.1.0", adapter="sim/0.1.0",
                    tasks={"goto": {"max_speed_mps": 1.0}},
                    actuators={"light": [], "siren": False, "speaker": False, "spotlight": False},
                    sensing={"lidar": False, "depth": False, "thermal": False, "imu_hz": 0,
                             "joint_effort": False, "foot_force": False})
RECON = Reconcile(boot_id="b-1", control_epoch=3, task=STATUS.task,
                  unacked_from_seq=4, unacked_to_seq=9)
TELE = Telemetry(stamp=3_000, pose=POSE, battery_pct=87.5, task_state=TaskState.RUNNING,
                 loc_quality=1.0, net={"rssi": -55})

样本 = [POSE, CMD, ACK, EVT, STATUS, CAPS, RECON, TELE,
      Ack(command_id="c-2", task_id="t-1", result=AckResult.DUPLICATE,
          original=ACK.to_wire()),
      Status(online=False, boot_id="b-1", ready=READY, control_epoch=3, last_seen=0, task=None),
      Reconcile(boot_id="b-2", control_epoch=4, task=None, unacked_from_seq=0, unacked_to_seq=0),
      Telemetry(stamp=1, pose=None, battery_pct=0.0, task_state=None, loc_quality=0.0, net={})]


@pytest.mark.parametrize("msg", 样本, ids=lambda m: type(m).__name__)
def test_往返(msg):
    wire = msg.to_wire()
    assert wire["schema"] == SCHEMA
    assert type(msg).from_wire(wire) == msg


@pytest.mark.parametrize("msg", 样本, ids=lambda m: type(m).__name__)
def test_主版本不同拒_次版本高接受(msg):
    wire = msg.to_wire()
    with pytest.raises(SchemaMismatch):
        type(msg).from_wire({**wire, "schema": "2.0"})
    assert type(msg).from_wire({**wire, "schema": "1.7"}) == msg
    with pytest.raises(SchemaMismatch):
        type(msg).from_wire({k: v for k, v in wire.items() if k != "schema"})


@pytest.mark.parametrize("msg", 样本, ids=lambda m: type(m).__name__)
def test_缺字段要说出字段名(msg):
    wire = msg.to_wire()
    for key in [k for k in wire if k != "schema"]:
        broken = {k: v for k, v in wire.items() if k != key}
        with pytest.raises(ContractError, match=key):
            type(msg).from_wire(broken)


def test_字段类型错拒():
    with pytest.raises(ContractError, match="x"):
        MapPose.from_wire({**POSE.to_wire(), "x": "1.5"})
    with pytest.raises(ContractError, match="seq"):
        Event.from_wire({**EVT.to_wire(), "seq": 1.0})       # 浮点不算整数
    with pytest.raises(ContractError, match="online"):
        Status.from_wire({**STATUS.to_wire(), "online": 1})   # bool 不接受 int
    with pytest.raises(ContractError, match="payload"):
        Command.from_wire({**CMD.to_wire(), "payload": []})


def test_枚举非法值拒():
    with pytest.raises(ContractError, match="result"):
        Ack.from_wire({**ACK.to_wire(), "result": "maybe"})
    with pytest.raises(ContractError, match="state"):
        Status.from_wire({**STATUS.to_wire(),
                          "task": {**STATUS.task.to_wire(), "state": "napping"}})


def test_命令过期时刻不许早于签发时刻():
    with pytest.raises(ContractError, match="expires_at"):
        Command.from_wire({**CMD.to_wire(), "expires_at": 999})
    with pytest.raises(ContractError):
        Command(command_id="c", task_id="t", kind="goto", issued_at=10, expires_at=9,
                control_epoch=0, payload={})


def test_命令的id不许为空():
    for key in ("command_id", "task_id", "kind"):
        with pytest.raises(ContractError, match=key):
            Command.from_wire({**CMD.to_wire(), key: ""})


def test_duplicate回执必须带原结果_其他不带():
    with pytest.raises(ContractError, match="original"):
        Ack(command_id="c", task_id="t", result=AckResult.DUPLICATE)
    with pytest.raises(ContractError, match="original"):
        Ack(command_id="c", task_id="t", result=AckResult.ACCEPTED, original={"x": 1})


def test_rejected回执必须带理由():
    with pytest.raises(ContractError, match="reason"):
        Ack(command_id="c", task_id="t", result=AckResult.REJECTED)
    Ack(command_id="c", task_id="t", result=AckResult.REJECTED, reason="map_mismatch")


def test_ready整体判定():
    assert READY.ok is True
    assert Ready(control=True, motion=True, estop_clear=False, loc_ok=True).ok is False


def test_事件seq和boot_id():
    with pytest.raises(ContractError, match="seq"):
        Event(event_id="e", seq=0, boot_id="b", stamp=1, kind="x", data={})
    with pytest.raises(ContractError, match="boot_id"):
        Event(event_id="e", seq=1, boot_id="", stamp=1, kind="x", data={})


def test_非有限数一律拒():
    """json.loads 默认收 NaN/Infinity;喂进 goto 会让距离永远算不出来、速度取满值原地转圈。"""
    import json as _json
    for bad in ("NaN", "Infinity", "-Infinity"):
        wire = _json.loads(_json.dumps(POSE.to_wire()).replace("1.5", bad, 1))
        with pytest.raises(ContractError, match="x"):
            MapPose.from_wire(wire)
    with pytest.raises(ContractError):
        MapPose(map_id="m", map_version="1", frame_id="map", x=float("nan"), y=0.0, yaw=0.0)


def test_robot_fault事件的数据往返_坏的不收():
    """W00c5a:代理只报故障事实(故障集合变了才发),判定在站点。"""
    from d1max_contract.hal import Fault
    from d1max_contract.messages import EVENT_KINDS, fault_event_data, parse_fault_event_data

    assert "robot_fault" in EVENT_KINDS and "patrol_waypoint" in EVENT_KINDS
    fs = (Fault(code="7", fatal=True, text="左前腿过流"), Fault(code="9", fatal=False, text=""))
    d = fault_event_data(fs)
    assert d == {"faults": [{"code": "7", "fatal": True, "text": "左前腿过流"},
                            {"code": "9", "fatal": False, "text": ""}]}
    assert parse_fault_event_data(d) == fs
    assert parse_fault_event_data({"faults": []}) == ()
    for bad in ({}, {"faults": None}, {"faults": [{"code": 7}]},
                {"faults": [{"code": "7", "fatal": "yes", "text": ""}]},
                {"faults": [{"code": "7", "fatal": True}]}, {"faults": ["x"]}):
        with pytest.raises(ContractError):
            parse_fault_event_data(bad)


def test_回执可以带数据_只在有的时候写_老报文照读():
    """W00c6d:查询类命令(``release_precheck``)的答复、切版本被拒时的整份清单,都放在回执的
    ``data``。"""
    a = Ack(command_id="c", task_id="t", result=AckResult.ACCEPTED, data={"checks": [1]})
    assert Ack.from_wire(a.to_wire()) == a
    assert "data" not in ACK.to_wire(), "没有数据不写这个键,老夹具不变"
    old = {k: v for k, v in a.to_wire().items() if k != "data"}
    assert Ack.from_wire(old).data is None
    with pytest.raises(ContractError, match="data"):
        Ack.from_wire({**a.to_wire(), "data": [1]})
