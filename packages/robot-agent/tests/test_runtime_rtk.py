"""W09e 代理接 RTK:订站点的 ``rtcm`` 主题交给 RTK 来源、遥测带 ``rtk``、能力里说来源、录包时记
``rtk.jsonl``、收尾时停掉。RTK 来源用假的(真的在 ``test_rtk_sources.py`` 里用 pty 测)。"""

from __future__ import annotations

import json

from test_runtime_maps import REG, T, _跑, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Telemetry
from d1max_patrol.protocol.nav_types import Pose


class 假RTK:
    kind = "own"

    def __init__(self):
        self.fed: list[bytes] = []
        self.started = self.closed = False
        self.fix = {"fix": "fixed", "lat": 5.41, "lon": 100.32, "alt": 20.0, "sats": 22,
                    "hdop": 0.7, "std_h_m": 0.012, "age_s": 1.0, "stamp_ms": 1, "stale": False}

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def feed(self, data):
        self.fed.append(data)

    def latest(self):
        return dict(self.fix)


class 听上行(耳朵):
    """只听 JSON 的上行;站点自己发的改正数据(二进制)不管。"""

    async def __call__(self, m):
        if T.parse(m.topic)[2] != "rtcm":
            await super().__call__(m)


class 假录包:
    def __init__(self, root):
        self.recording = False
        self.bags_root = root
        self.last_bag = "yard-1"


async def _台(tmp_path, rtk):
    broker, c = MemoryBroker(), 钟()
    ears = 听上行()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    dog = SimRobot(now_ms=c)
    mapper = 假录包(tmp_path / "bags")
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono, rtk=rtk,
                      mapper=mapper)
    await rt.start()
    await broker.drain()
    return broker, c, ears, dog, rt, st, mapper


async def test_改正交给来源_遥测带rtk_能力说来源_收尾停掉(tmp_path):
    rtk = 假RTK()
    broker, c, ears, dog, rt, st, _ = await _台(tmp_path, rtk)
    assert rtk.started
    assert ears.by["capabilities"][-1]["tasks"]["rtk"] == {"source": "own"}
    await st.publish(T.rtcm, b"\xd3\x00\x01x\x00\x00\x00", qos=0)
    await broker.drain()
    assert rtk.fed == [b"\xd3\x00\x01x\x00\x00\x00"]
    await _跑(rt, broker, n=15, r=dog, c=c)
    tele = Telemetry.from_wire(ears.by["telemetry"][-1])
    assert tele.rtk["fix"] == "fixed" and tele.rtk["sats"] == 22
    await rt.close()
    assert rtk.closed


async def test_没配RTK_不订不带(tmp_path):
    broker, c, ears, dog, rt, st, _ = await _台(tmp_path, None)
    assert "rtk" not in ears.by["capabilities"][-1]["tasks"]
    await _跑(rt, broker, n=15, r=dog, c=c)
    assert "rtk" not in ears.by["telemetry"][-1]
    await rt.close()


async def test_录包时记rtk_jsonl_只记浮点固定_同一条不重记(tmp_path):
    rtk = 假RTK()
    broker, c, ears, dog, rt, st, mapper = await _台(tmp_path, rtk)
    (tmp_path / "bags" / "yard-1").mkdir(parents=True)
    await _跑(rt, broker, n=3, r=dog, c=c)
    assert not (tmp_path / "bags" / "yard-1" / "rtk.jsonl").exists(), "没在录不记"
    mapper.recording = True
    await _跑(rt, broker, n=3, r=dog, c=c)
    rtk.fix |= {"stamp_ms": 2, "fix": "single"}
    await _跑(rt, broker, n=2, r=dog, c=c)
    rtk.fix |= {"stamp_ms": 3, "fix": "float", "lat": 5.42}
    await _跑(rt, broker, n=2, r=dog, c=c)
    rtk.fix |= {"stamp_ms": 4, "fix": "fixed", "stale": True}
    await _跑(rt, broker, n=2, r=dog, c=c)
    rows = [json.loads(ln) for ln in
            (tmp_path / "bags" / "yard-1" / "rtk.jsonl").read_text().splitlines()]
    assert [(r["t"], r["fix"]) for r in rows] == [(0.001, "fixed"), (0.003, "float")]
    assert rows[1]["lat"] == 5.42 and set(rows[0]) == {"t", "fix", "lat", "lon", "alt",
                                                        "std_h_m", "sats"}
    await rt.close()


async def test_来源写改正炸了_不碍代理(tmp_path):
    rtk = 假RTK()

    def 炸(data):
        raise OSError("串口没了")
    rtk.feed = 炸
    broker, c, ears, dog, rt, st, _ = await _台(tmp_path, rtk)
    await st.publish(T.rtcm, b"x", qos=0)
    await broker.drain()
    await _跑(rt, broker, n=15, r=dog, c=c)
    assert ears.by["telemetry"], "代理照常发遥测"
    await rt.close()


def test_命令行_选来源(tmp_path):
    import argparse

    from d1max_agent.main import make_rtk
    from d1max_agent.rtk import OwnRtk, VendorRtk
    base = dict(rtk_device="/dev/ttyTHS3", rtk_baud=460800, rtk_init=None,
                rtk_vendor_cmd="/x/d1max-rtk-vendor")
    assert make_rtk(argparse.Namespace(rtk="none", **base)) is None
    own = make_rtk(argparse.Namespace(rtk="own", **base))
    assert isinstance(own, OwnRtk) and own.device == "/dev/ttyTHS3" and own.baud == 460800
    init = tmp_path / "init.txt"
    init.write_text("# 让模组出 GGA\nGPGGA COM1 0.2\n\nGPGST COM1 1\n")
    own = make_rtk(argparse.Namespace(rtk="own", **(base | {"rtk_init": init})))
    assert own.init == ["GPGGA COM1 0.2", "GPGST COM1 1"]
    v = make_rtk(argparse.Namespace(rtk="vendor", **base))
    assert isinstance(v, VendorRtk) and v.argv == ["/x/d1max-rtk-vendor"]
