"""W46(决策 74):上装主控板过热,运行时降负载 —— 降负载停热像录像、暂停上传、不接新建图(巡逻照走);
危急不接新的自主任务(引擎那边结束这一趟、回家,见 tests/engine)。"""

from __future__ import annotations

import pytest
from test_runtime import REG, T, 站点耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_patrol.protocol.nav_types import Pose


class 假板子:
    def __init__(self):
        self.level, self.reason = "ok", "SoC 50 °C"
        self.emit = None
        self.steps = 0
        self.closed = False

    async def step(self):
        self.steps += 1

    def health(self):
        return [{"id": "board", "state": "online", "detail": self.reason}]

    def close(self):
        self.closed = True


class 假录像:
    def __init__(self):
        self.held: set[str] = set()
        self.cameras = ["front", "back", "thermal"]

    def hold(self, cams):
        self.held = set(cams)

    def step(self):
        pass

    def close(self):
        pass

    def caps(self):
        return {"cameras": self.cameras, "segment_s": 60}

    def camera_health(self):
        return dict.fromkeys(self.cameras, "")


def _cmd(kind):
    return type("C", (), {"kind": kind})()


@pytest.fixture
async def 台子(tmp_path):
    broker = MemoryBroker()
    c = 钟()
    r = SimRobot(now_ms=c)
    site = MemoryTransport(broker, "site")
    await site.connect()
    await site.subscribe(f"{T.prefix}/#", 站点耳朵())
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                      boot_id="boot-1", home=Pose.from_xy_yaw(0.0, 0.0),
                      monotonic=lambda: c.mono)
    await rt.start()
    yield c, r, rt
    await rt.close()


async def test_过热三级_降负载和拒任务(台子):
    c, r, rt = 台子
    board, rec, holds = 假板子(), 假录像(), []
    rt.recorder = rec
    rt.upload_hold = holds.append
    rt.attach_board(board)
    assert board.emit == rt.events.emit, "级别变了的事件进事件簿、上站点"
    assert rt.parts.device.overheat() == ("ok", "SoC 50 °C"), "引擎从设备桥拿过热级别"
    await rt.step(0.1)
    assert board.steps == 1 and rec.held == set() and holds == []
    board.level = "warn"
    await rt.step(0.1)
    assert rec.held == set() and holds == [], "警戒只报警"
    board.level = "shed"
    await rt.step(0.1)
    assert rec.held == {"thermal"} and holds == [True]
    assert rt.processor.admit_hook(_cmd("patrol")) == "", "降负载:巡逻照走"
    assert rt._overheat_refusal("mapping") == "overheat", "不接新的建图"
    board.level = "critical"
    await rt.step(0.1)
    assert holds == [True], "已经在降负载:不重复下"
    for k in ("patrol", "goto", "standoff"):
        assert rt.processor.admit_hook(_cmd(k)) == "overheat"
    assert rt.processor.admit_hook(_cmd("teleop")) == "", "遥控照接(人把它开回来)"
    board.level = "ok"
    await rt.step(0.1)
    assert rec.held == set() and holds == [True, False]
    assert rt.processor.admit_hook(_cmd("patrol")) == ""
    assert rt._overheat_refusal("mapping") == ""
    got = await rt.devices.refresh()
    assert any(d["id"] == "board" for d in got)


async def test_过热时站点下的建图被拒(台子, tmp_path):
    import json

    from d1max_contract.messages import Command
    from d1max_contract.transport import Message
    c, r, rt = 台子
    board = 假板子()
    rt.attach_board(board)
    board.level = "shed"
    acks = []

    async def pub(topic, data, qos=1, retain=False):
        if topic.endswith("cmd/ack"):
            acks.append(json.loads(data))
    rt.transport.publish = pub
    rt.mapper = type("M", (), {"recording": False})()
    cmd = Command(command_id="m1", task_id="mapping-m1", kind="mapping", issued_at=c.ms,
                  expires_at=c.ms + 60_000, control_epoch=1,
                  payload={"action": "start", "name": "yard"})
    await rt._on_cmd(Message(T.cmd, json.dumps(cmd.to_wire()).encode(), 1, False))
    assert acks and acks[-1]["reason"] == "overheat"


def test_装机入口_没有配置不看_写错就不起(tmp_path):
    import argparse
    import json

    from d1max_agent.main import _board
    assert _board(argparse.Namespace(board=tmp_path / "nope.json")) is None
    p = tmp_path / "board.json"
    p.write_text(json.dumps({"fans": [{"name": "Bad", "chip": "gpiochip1", "line": 1}]}))
    with pytest.raises(SystemExit, match="风扇名"):
        _board(argparse.Namespace(board=p))
    p.write_text(json.dumps({"log_dir": str(tmp_path / "log")}))
    m = _board(argparse.Namespace(board=p))
    assert m is not None and m.level == "ok"
    m.close()


async def test_热像镜头检测_后台取图不卡主循环_进设备健康_事件进事件簿(台子):
    import asyncio
    c, r, rt = 台子
    release = asyncio.Event()

    class 假镜头:
        emit = None
        steps = 0

        def due(self):
            return True

        async def step(self):
            self.steps += 1
            await release.wait()                     # 取一帧要等很久(ffmpeg 最长 15 s)

        def health(self):
            return {"id": "lens:thermal", "state": "degraded", "detail": "挡了"}
    lens = 假镜头()
    rt.attach_lens(lens)
    assert lens.emit == rt.events.emit
    await asyncio.wait_for(rt.step(0.1), 1.0)
    await asyncio.sleep(0)
    assert lens.steps == 1
    await asyncio.wait_for(rt.step(0.1), 1.0)
    await asyncio.sleep(0)
    assert lens.steps == 1, "上一帧还没取完:不叠着再起"
    release.set()
    await asyncio.sleep(0.01)
    await rt.step(0.1)
    await asyncio.sleep(0)
    assert lens.steps == 2
    assert any(d["id"] == "lens:thermal" for d in await rt.devices.refresh())
