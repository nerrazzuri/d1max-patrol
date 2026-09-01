"""旁路进程线协议的编解码。

这一层不碰 socket,所以测得起每一条畸形输入 —— 而畸形输入正是现场最常见的
东西:固件升级多出个字段、C++ 那头少写一个键、日志混进 stdout。
"""

import json

import pytest

from d1max_patrol.protocol.agent_frames import (
    PROTO_VERSION,
    Ack,
    AgentProtocolError,
    ControlLostFrame,
    EmergencyStatus,
    FaultFrame,
    Hello,
    MotionStatus,
    OdomFrame,
    StateFrame,
    decode_command,
    decode_frame,
    encode_command,
)


def _state(**over) -> StateFrame:
    base = dict(motion=MotionStatus.STAND_UP, battery1=50.0, battery2=60.0,
                estop_software=EmergencyStatus.RECOVER,
                estop_hardware=EmergencyStatus.RECOVER)
    base.update(over)
    return StateFrame(**base)


# ---------------------------------------------------------------- 上行


def test_命令编码带换行且是合法JSON():
    line = encode_command(7, "walk", seconds=2.0, fwd=0.4)
    assert line.endswith(b"\n")
    obj = json.loads(line)
    assert obj == {"id": 7, "cmd": "walk", "seconds": 2.0, "fwd": 0.4}


def test_命令能原样解回来():
    cmd_id, cmd, args = decode_command(encode_command(3, "stand"))
    assert (cmd_id, cmd, args) == (3, "stand", {})


def test_命令解码拒绝缺cmd():
    with pytest.raises(AgentProtocolError, match="缺 cmd"):
        decode_command('{"id": 1}')


def test_命令解码拒绝非整数id():
    with pytest.raises(AgentProtocolError, match="id 必须是整数"):
        decode_command('{"id": "1", "cmd": "stand"}')


def test_命令解码拒绝非字符串cmd():
    with pytest.raises(AgentProtocolError, match="cmd 必须是字符串"):
        decode_command('{"id": 1, "cmd": 5}')


def test_命令解码在缺id时默认为0():
    # 真机那头是 C++ 手写的解析,少写 id 完全可能。默认 0 而不是抛错 ——
    # 抛错会让一条打错的命令干掉整条链路。
    assert decode_command('{"cmd": "status"}') == (0, "status", {})


# ---------------------------------------------------------------- 下行


def test_hello解码():
    frame = decode_frame(json.dumps({
        "t": "hello", "proto": PROTO_VERSION, "sdk": "0.1.1",
        "held": True, "robot": "192.168.168.168:8082"}))
    assert frame == Hello(proto=PROTO_VERSION, sdk="0.1.1", held=True,
                          robot="192.168.168.168:8082")


def test_hello缺proto直接抛错():
    # 版本号是唯一能挡住"新旁路进程配旧 Python"的东西,不许默认。
    with pytest.raises(AgentProtocolError, match="proto 必须是整数"):
        decode_frame('{"t": "hello", "sdk": "0.1.1"}')


def test_ack解码():
    assert decode_frame('{"t":"ack","id":4,"ok":false,"error":"被拒"}') == \
        Ack(id=4, ok=False, error="被拒")


def test_ack的id不能是bool():
    # Python 里 True 是 int 的子类,不显式挡就会被当成 id=1 —— 那是真会
    # 让回执认错人的一类 bug。
    with pytest.raises(AgentProtocolError, match="ack.id 必须是整数"):
        decode_frame('{"t":"ack","id":true,"ok":true}')


def test_state用整数码解码():
    frame = decode_frame(json.dumps({
        "t": "state", "motion": 5, "battery1": 71.0, "battery2": 68.0,
        "estop_sw": 1, "estop_hw": 2, "ts_ms": 99}))
    assert isinstance(frame, StateFrame)
    assert frame.motion is MotionStatus.GENERAL
    assert frame.estop_software is EmergencyStatus.RECOVER
    assert frame.estop_hardware is EmergencyStatus.STOP
    assert frame.ts_ms == 99


def test_state用枚举名解码():
    frame = decode_frame('{"t":"state","motion":"Gait","estop_sw":"Stop"}')
    assert isinstance(frame, StateFrame)
    assert frame.motion is MotionStatus.GAIT
    assert frame.estop_software is EmergencyStatus.STOP


@pytest.mark.parametrize("code,expected", [
    (0, MotionStatus.UNKNOWN),
    (1, MotionStatus.STAND_UP),
    (2, MotionStatus.LIE_DOWN),
    (3, MotionStatus.CRAWL),
    (4, MotionStatus.LOCKED),
    (5, MotionStatus.GENERAL),
    (6, MotionStatus.IN_PLACE),
    (7, MotionStatus.STAIR),
    (8, MotionStatus.CLIMB),
    (9, MotionStatus.SLIM),
    (10, MotionStatus.GAIT),
])
def test_运动状态整数码逐条对上SDK头文件(code, expected):
    # 这张表直接对应 sdk_type.hpp 的 MotionStatus 声明顺序。
    # motion/hold02.cpp 那张手写表只到 6,7-10 会打印成 '?' —— 这里必须是全的。
    assert decode_frame(json.dumps({"t": "state", "motion": code})).motion \
        is expected


def test_未知运动状态码降级为UNKNOWN而不是抛错():
    # 固件升级新增枚举值不该让整个巡检崩掉 —— 和 NavBackend 那边同一条原则。
    assert decode_frame('{"t":"state","motion":99}').motion is MotionStatus.UNKNOWN
    assert decode_frame('{"t":"state","motion":"Somersault"}').motion \
        is MotionStatus.UNKNOWN


def test_运动状态不能是bool():
    with pytest.raises(AgentProtocolError, match="motion 不能是 bool"):
        decode_frame('{"t":"state","motion":true}')


def test_急停字段不能是bool():
    # 急停是三值(Unknown/Recover/Stop),压成 bool 会把"还不知道"和
    # "没急停"混成一个 —— 安全字段不许这么省。
    with pytest.raises(AgentProtocolError, match="急停字段不能是 bool"):
        decode_frame('{"t":"state","estop_sw":true}')


def test_未知急停码降级为UNKNOWN():
    assert decode_frame('{"t":"state","estop_hw":77}').estop_hardware \
        is EmergencyStatus.UNKNOWN


def test_odom解码():
    frame = decode_frame(json.dumps({
        "t": "odom", "x": 1.5, "y": -0.5, "yaw": 0.25,
        "vx": 0.6, "vy": 0.0, "vyaw": 0.1, "ts_ms": 7}))
    assert frame == OdomFrame(x=1.5, y=-0.5, yaw=0.25, vx=0.6, vy=0.0,
                              vyaw=0.1, ts_ms=7)


def test_odom缺字段按0补齐():
    assert decode_frame('{"t":"odom","x":2}') == OdomFrame(x=2.0, y=0.0, yaw=0.0)


def test_odom字段是字符串时抛错():
    with pytest.raises(AgentProtocolError, match="x 必须是数字"):
        decode_frame('{"t":"odom","x":"2"}')


def test_fault解码():
    assert decode_frame('{"t":"fault","level":2,"code":17,"message":"腿过热"}') \
        == FaultFrame(level=2, code=17, message="腿过热")


def test_control_lost可以没有原因():
    # SDK 的 ControlLostInfo 是空结构体,厂商压根没给原因字段。
    assert decode_frame('{"t":"control_lost"}') == ControlLostFrame(reason="")


@pytest.mark.parametrize("line,match", [
    ("не json", "不是 JSON"),
    ("[1,2,3]", "顶层必须是对象"),
    ('{"t":"nope"}', "不认识的帧类型"),
    ('{}', "不认识的帧类型"),
])
def test_畸形下行帧都抛AgentProtocolError(line, match):
    with pytest.raises(AgentProtocolError, match=match):
        decode_frame(line)


def test_非UTF8字节抛协议错而不是UnicodeDecodeError():
    with pytest.raises(AgentProtocolError, match="不是 UTF-8"):
        decode_frame(b'\xff\xfe{"t":"ack"}')


# ---------------------------------------------------------------- 派生字段


def test_电量取两块里低的那块():
    # 关心的是"还能撑多久",取低的才是保守值。
    assert _state(battery1=71.0, battery2=17.0).battery == 17.0
    assert _state(battery1=17.0, battery2=71.0).battery == 17.0


def test_只有一块电池在位时取在位那块():
    assert _state(battery1=0.0, battery2=64.0).battery == 64.0


def test_两块都读不到时返回0而不是崩():
    assert _state(battery1=0.0, battery2=0.0).battery == 0.0


@pytest.mark.parametrize("sw,hw,expected", [
    (EmergencyStatus.RECOVER, EmergencyStatus.RECOVER, False),
    (EmergencyStatus.STOP, EmergencyStatus.RECOVER, True),
    (EmergencyStatus.RECOVER, EmergencyStatus.STOP, True),
    (EmergencyStatus.STOP, EmergencyStatus.STOP, True),
    (EmergencyStatus.UNKNOWN, EmergencyStatus.UNKNOWN, False),
])
def test_任一路急停生效即为急停(sw, hw, expected):
    assert _state(estop_software=sw, estop_hardware=hw).emergency is expected
