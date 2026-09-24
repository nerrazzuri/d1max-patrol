"""黄金夹具由数据类生成;盘上那份必须与生成结果逐字节一致(改了报文这里先红)。"""

from __future__ import annotations

from d1max_contract import fixtures
from d1max_contract.messages import (
    Ack,
    Capabilities,
    Command,
    Event,
    MapPose,
    Reconcile,
    Status,
    Telemetry,
)
from d1max_contract.registration import Registration

解析器 = {"map_pose": MapPose, "command_goto": Command, "command_abort": Command,
       "ack_accepted": Ack, "ack_rejected": Ack, "ack_expired": Ack, "ack_duplicate": Ack,
       "event_progress": Event, "event_aborted": Event, "status_online": Status,
       "status_offline_lwt": Status, "capabilities": Capabilities, "reconcile": Reconcile,
       "telemetry": Telemetry, "registration": Registration}


def test_每个样本都能被对应的类读回():
    样本 = fixtures.generate()
    assert set(样本) == set(解析器), "新样本要登记解析器;删样本要一起删"
    for name, d in 样本.items():
        解析器[name].from_wire(d)


def test_盘上夹具与生成结果一致():
    样本 = fixtures.generate()
    盘上 = {p.stem for p in fixtures.FIXTURES_DIR.glob("*.json")}
    assert 盘上 == set(样本), (
        f"夹具文件集合不一致: 多 {盘上 - set(样本)} 少 {set(样本) - 盘上} —— "
        "跑 python -m d1max_contract.fixtures --write")
    for name, d in 样本.items():
        期望 = fixtures.render(d)
        实际 = (fixtures.FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8")
        assert 实际 == 期望, (f"{name}.json 与数据类生成的不一致 —— "
                          "跑 python -m d1max_contract.fixtures --write")


def test_夹具目录在包里而不是src里():
    assert fixtures.FIXTURES_DIR.name == "fixtures"
    assert fixtures.FIXTURES_DIR.parent.name == "contract"
