"""上装命令(W21,``deter``):真代理 + 装了上装的仿真狗。不占任务槽(巡检路上照样能开)、到点关、
喇叭优先级仲裁(低的回 busy、一样高或更高的打断、关不看优先级)、每次开关记一条事件、
没装就不报能力。"""

from __future__ import annotations

from test_runtime_maps import REG, T, _cmd, _跑, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import MapPose
from d1max_patrol.protocol.nav_types import Pose


async def _台(tmp_path, *, payload=True):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    dog = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2, payload=payload)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono,
                      odom_identity=True, telemetry_period_ms=100)
    await rt.start()
    await broker.drain()
    return broker, c, ears, dog, rt


async def _发(rt, broker, ears, payload, cid, c):
    await rt._on_cmd(_cmd("deter", payload, cid, c))
    await broker.drain()
    return ears.by["cmd/ack"][-1]


def _事件(ears, kind="deter"):
    return [e["data"] for e in ears.by.get("event", []) if e["kind"] == kind]


async def test_能力里报接了哪几路_能放哪些话术(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    d = ears.by["capabilities"][-1]["tasks"]["deter"]
    assert d == {"outputs": ["strobe", "siren", "spotlight", "speaker"], "max_s": 600.0,
                 "clips": ["warn-zh", "warn-en"], "tts": False}
    await rt.close()


async def test_没装上装_不报能力_来了回unsupported(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path, payload=False)
    assert "deter" not in ears.by["capabilities"][-1]["tasks"]
    ack = await _发(rt, broker, ears, {"output": "siren", "on": True, "max_s": 5}, "d1", c)
    assert ack["result"] == "rejected" and ack["reason"] == "unsupported"
    await rt.close()


async def test_开警笛_到点仿真狗自己算关_开关都记事件(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    ack = await _发(rt, broker, ears, {"output": "siren", "on": True, "max_s": 5}, "d1", c)
    assert ack["result"] == "accepted", ack
    assert dog.deter_on("siren")
    c.ms += 6000
    assert not dog.deter_on("siren")
    ack = await _发(rt, broker, ears, {"output": "strobe", "on": True, "max_s": 30}, "d2", c)
    assert ack["result"] == "accepted" and dog.deter_on("strobe")
    ack = await _发(rt, broker, ears, {"output": "strobe", "on": False}, "d3", c)
    assert ack["result"] == "accepted" and not dog.deter_on("strobe")
    await _跑(rt, broker, n=2, r=dog, c=c)          # 事件下一拍发出去
    assert _事件(ears) == [
        {"task_id": "deter-d1", "output": "siren", "on": True, "max_s": 5.0},
        {"task_id": "deter-d2", "output": "strobe", "on": True, "max_s": 30.0},
        {"task_id": "deter-d3", "output": "strobe", "on": False}]
    await rt.close()


async def test_载荷不对回payload_不动设备(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    for bad in ({"output": "siren", "on": True}, {"output": "siren", "on": True, "max_s": 601},
                {"output": "horn", "on": True, "max_s": 5},
                {"output": "speaker", "on": True, "max_s": 5, "clip": "../x"}):
        ack = await _发(rt, broker, ears, bad, f"b{len(str(bad))}", c)
        assert ack["result"] == "rejected" and ack["reason"].startswith("payload"), ack
    await _跑(rt, broker, n=2, r=dog, c=c)
    assert not any(dog.deter_on(o) for o in ("siren", "speaker")) and _事件(ears) == []
    await rt.close()


async def test_喇叭优先级_低的busy_一样高打断_关不看优先级_放完了谁都能放(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)

    def sp(clip, prio, max_s=20):
        return {"output": "speaker", "on": True, "max_s": max_s, "clip": clip, "priority": prio}
    assert (await _发(rt, broker, ears, sp("warn-zh", 80), "s1", c))["result"] == "accepted"
    ack = await _发(rt, broker, ears, sp("warn-en", 50), "s2", c)
    assert ack["result"] == "rejected" and ack["reason"] == "busy"
    assert dog.deter_clip == "warn-zh"
    assert (await _发(rt, broker, ears, sp("warn-en", 80), "s3", c))["result"] == "accepted"
    assert dog.deter_clip == "warn-en"
    ack = await _发(rt, broker, ears, {"output": "speaker", "on": False}, "s4", c)
    assert ack["result"] == "accepted" and not dog.deter_on("speaker")
    assert (await _发(rt, broker, ears, sp("warn-zh", 10), "s5", c))["result"] == "accepted"
    c.mono += 21                                     # 放完了(按单调钟)
    assert (await _发(rt, broker, ears, sp("warn-en", 0), "s6", c))["result"] == "accepted"
    await rt.close()


async def test_没有这段话术_回设备的错(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    ack = await _发(rt, broker, ears, {"output": "speaker", "on": True, "max_s": 5,
                                       "clip": "nope", "priority": 50}, "n1", c)
    assert ack["result"] == "rejected" and ack["reason"].startswith("device:")
    assert ack["reason"].find("没有这段话术") > 0
    await rt.close()


async def test_不占任务槽_去一个点的路上照样能开警灯(tmp_path):
    broker, c, ears, dog, rt = await _台(tmp_path)
    goto = _cmd("goto", {"target": MapPose(map_id="m", map_version="1", frame_id="map", x=3.0,
                                           y=0.0, yaw=0.0).to_wire()}, "g1", c)
    await rt._on_cmd(goto)
    await _跑(rt, broker, n=5, r=dog, c=c)
    assert rt.processor.current is not None
    ack = await _发(rt, broker, ears, {"output": "strobe", "on": True, "max_s": 60}, "d1", c)
    assert ack["result"] == "accepted" and dog.deter_on("strobe")
    assert rt.processor.current is not None and rt.processor.current.task_id == "goto-g1"
    await rt.close()


async def test_代理只派接上的那几路_没接的不碰设备():
    from types import SimpleNamespace

    from d1max_agent.deter import DeterDesk
    from d1max_contract.messages import Command

    class 只有警笛:
        calls: list = []

        def hal_capabilities(self):
            return SimpleNamespace(actuators={"siren": True, "spotlight": False})

        async def siren(self, on, max_s):
            self.calls.append(("siren", on))

        async def spotlight(self, on, max_s):            # 没报能力,但调了也不抛:代理自己要挡
            self.calls.append(("spotlight", on))
    hal = 只有警笛()
    desk = DeterDesk(hal, emit=lambda k, d: None)

    def cmd(p):
        return Command(command_id="c", task_id="t", kind="deter", issued_at=1, expires_at=2,
                       control_epoch=1, payload=p)
    assert await desk.handle(cmd({"output": "spotlight", "on": True, "max_s": 5})) == "unsupported"
    assert await desk.handle(cmd({"output": "siren", "on": True, "max_s": 5})) == ""
    assert hal.calls == [("siren", True)]
    assert desk.caps() == {"outputs": ["siren"], "max_s": 600.0}


class _有回调的喇叭:
    """像真狗 HAL:放完、放坏了经 ``set_sound_listener`` 回调;``fail`` 时当场起不来。"""

    def __init__(self):
        self.listener = None
        self.fail = ""
        self.clips = []

    def hal_capabilities(self):
        from types import SimpleNamespace
        return SimpleNamespace(actuators={"speaker": True})

    def set_sound_listener(self, fn):
        self.listener = fn

    async def sound(self, clip, max_s):
        if self.fail:
            raise RuntimeError(self.fail)
        self.clips.append(clip)


def _喇叭命令(clip, prio, cid="c"):
    from d1max_contract.messages import Command
    p = {"output": "speaker", "on": True, "max_s": 60, "clip": clip, "priority": prio}
    return Command(command_id=cid, task_id=f"deter-{cid}", kind="deter", issued_at=1,
                   expires_at=2, control_epoch=1, payload=p)


async def test_外审3_喇叭起不来_回拒绝_不记开_不占优先级():
    from d1max_agent.deter import DeterDesk
    hal, ev = _有回调的喇叭(), []
    desk = DeterDesk(hal, emit=lambda k, d: ev.append((k, d)))
    hal.fail = "喇叭放不出来: aplay 不在"
    why = await desk.handle(_喇叭命令("warn-zh", 90))
    assert why.startswith("device:") and "aplay" in why
    assert ev == [] and desk.playing() is None
    hal.fail = ""
    assert await desk.handle(_喇叭命令("warn-en", 10, "c2")) == "", "没占着:低的照样能放"


async def test_外审3_短话术放完了_当场放开优先级_记一条结束事件():
    from d1max_agent.deter import DeterDesk
    hal, ev = _有回调的喇叭(), []
    desk = DeterDesk(hal, emit=lambda k, d: ev.append((k, d)))
    assert await desk.handle(_喇叭命令("warn-zh", 90)) == ""
    assert await desk.handle(_喇叭命令("warn-en", 10, "c2")) == "busy"
    hal.listener(True, "放完了")                          # 3 秒的话术放完了,远没到 max_s
    assert desk.playing() is None
    assert ev[-1] == ("deter", {"output": "speaker", "on": False, "ended": "done",
                                "reason": "放完了"})
    assert await desk.handle(_喇叭命令("warn-en", 10, "c3")) == ""
    hal.listener(False, "播放器退出码 1")
    assert ev[-1][1]["ended"] == "failed" and desk.playing() is None
