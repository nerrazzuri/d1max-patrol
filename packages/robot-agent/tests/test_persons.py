"""人员检测,代理这一头(W24):三帧里两帧看到才算有人、一帧误检不算;进 5 米报一次靠近、
退到 6 米外才重武装;20 秒没看到报走了;检测节点不在不报走了;
真套接字端到端:握手、事件、能力、截图存成归档。"""

from __future__ import annotations

import asyncio
import json

from test_runtime_obstacles import REG, T, _连

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.persons import GONE_S, PersonView
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities
from d1max_contract.persbridge import Hello, Person, Persons, encode
from d1max_patrol.protocol.nav_types import Pose


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _帧(seq, *ranges, camera="front", check="ok", snapshot=""):
    return Persons(seq=seq, stamp_ns=0, camera=camera, check=check,
                   reason="" if check == "ok" else "没模型",
                   people=tuple(Person(bearing_deg=5.0, range_m=r, score=0.9) for r in ranges),
                   snapshot=snapshot)


def _台():
    c, ev = 钟(), []
    v = PersonView(emit=lambda k, d: ev.append((k, d)), monotonic=c)
    v.on_connect()
    return c, ev, v


def test_一帧误检不算_三帧里两帧才算有人_只报一次():
    c, ev, v = _台()
    v.on_persons(_帧(1, 8.0))
    v.on_persons(_帧(2))
    v.on_persons(_帧(3))
    assert ev == [], "一帧不算"
    v.on_persons(_帧(4, 8.0))
    v.on_persons(_帧(5, 7.5))
    assert [k for k, _ in ev] == ["person_seen"]
    assert ev[0][1] == {"camera": "front", "count": 1, "bearing_deg": 5.0, "nearest_m": 7.5}
    v.on_persons(_帧(6, 7.0))
    assert len(ev) == 1, "一直有人不重报"


def test_进5米报一次靠近_退到6米外才重新武装():
    c, ev, v = _台()
    for i, r in enumerate((8.0, 8.0, 4.8, 4.5, 5.5, 4.9, 6.5, 4.0), start=1):
        v.on_persons(_帧(i, r))
    kinds = [k for k, _ in ev]
    assert kinds == ["person_seen", "person_near", "person_near"]
    assert ev[1][1]["nearest_m"] == 4.8 and ev[2][1]["nearest_m"] == 4.0


def test_20秒没看到报走了_之后再看到重新报有人():
    c, ev, v = _台()
    v.on_persons(_帧(1, 9.0))
    v.on_persons(_帧(2, 9.0))
    for i in range(3, 30):
        c.t += 1.0
        v.on_persons(_帧(i))                               # 一直报帧,就是没人
        v.tick()
    assert [k for k, _ in ev] == ["person_seen", "person_gone"]
    assert ev[1][1]["after_s"] >= GONE_S
    v.on_persons(_帧(40, 3.0))
    v.on_persons(_帧(41, 3.0))
    assert [k for k, _ in ev][-2:] == ["person_seen", "person_near"]


def test_检测节点不在_不报走了_看不见不等于没人():
    c, ev, v = _台()
    v.on_persons(_帧(1, 9.0))
    v.on_persons(_帧(2, 9.0))
    c.t += 60
    v.tick()                                              # 60 秒没帧:stale
    assert v.state() == "stale" and [k for k, _ in ev] == ["person_seen"]
    v.on_persons(_帧(3, check="no_model"))
    v.tick()
    assert v.state() == "no_model" and len(ev) == 1
    v.on_disconnect()
    v.tick()
    assert v.state() == "off" and len(ev) == 1
    assert v.caps() == {"state": "off"}


def test_两个相机各算各的窗口():
    c, ev, v = _台()
    v.on_persons(_帧(1, 9.0, camera="front"))
    v.on_persons(_帧(2, camera="back"))
    v.on_persons(_帧(3, camera="back"))
    assert ev == []
    v.on_persons(_帧(4, 9.0, camera="front"))
    assert [k for k, _ in ev] == ["person_seen"]


class 耳朵:
    def __init__(self):
        self.caps, self.events = [], []

    async def __call__(self, m):
        kind = T.parse(m.topic)[2]
        if kind == "capabilities":
            self.caps.append(json.loads(m.payload))
        elif kind == "event":
            self.events.append(json.loads(m.payload))


async def test_端到端_真套接字_有人走了_能力_截图存成归档(tmp_path):
    broker = MemoryBroker()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    c = 钟()
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    (snaps / "s1.jpg").write_bytes(b"\xff\xd8jpg\xff\xd9")
    r = SimRobot(now_ms=lambda: 1_000_000)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=lambda: 1_000_000,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      persons="bridge", persons_socket=tmp_path / "p.sock",
                      persons_snapshots=snaps, monotonic=c)
    await rt.start()
    try:
        await broker.drain()
        assert Capabilities.from_wire(ears.caps[-1]).tasks["persons"] == {"state": "off"}
        rd, w = await _连(tmp_path / "p.sock")
        w.write(encode(Hello(proto=1, name="fake")))
        await w.drain()
        assert json.loads(await rd.readline())["t"] == "hello"
        w.write(encode(_帧(1, 4.0)))
        w.write(encode(_帧(2, 4.0, snapshot="s1.jpg")))
        await w.drain()

        async def 跑(n):
            for _ in range(n):
                await rt.step(0.1)
                await asyncio.sleep(0.01)
                await broker.drain()
        await 跑(10)
        kinds = [e["kind"] for e in ears.events]
        assert "person_seen" in kinds and "person_near" in kinds, kinds
        seen = next(e for e in ears.events if e["kind"] == "person_seen")["data"]
        assert seen["snapshot_run"] == "persons" and seen["nearest_m"] == 4.0
        root = rt.parts.engine._runs_root / "persons"
        photos = list(root.glob("*/photos/person__front__*.jpg"))
        assert len(photos) == 1 and photos[0].read_bytes() == b"\xff\xd8jpg\xff\xd9"
        assert not (snaps / "s1.jpg").exists(), "存进归档了,截图目录不留"
        assert Capabilities.from_wire(ears.caps[-1]).tasks["persons"] == {"state": "ok"}
        for i in range(3, 30):                            # 一直报帧、没人:20 秒后走了
            c.t += 1.0
            w.write(encode(_帧(i)))
            await w.drain()
            await 跑(1)
        assert "person_gone" in [e["kind"] for e in ears.events]
        w.close()
    finally:
        await rt.close()
