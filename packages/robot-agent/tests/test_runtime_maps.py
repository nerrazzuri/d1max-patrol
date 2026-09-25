"""W00c5d 第二部分:站点下发地图、建图,经运行时(内存 MQTT、仿真狗、假下载)。"""

from __future__ import annotations

import hashlib
import json

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.maps import MapKeeper
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities, Command
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics
from d1max_contract.transport import Message
from d1max_patrol.protocol.nav_types import Pose

REG = Registration(site_id="s", robot_id="r", credential_fingerprint="f", issued_at=0,
                   expires_at=10**13)
T = Topics(site_id="s", robot_id="r")


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000
        self.mono = 50.0

    def __call__(self) -> int:
        return self.ms


class 站点:
    def __init__(self):
        self.files: dict[tuple[str, str, str], bytes] = {}

    def add(self, map_id, version, files):
        for n, d in files.items():
            self.files[(map_id, version, n)] = d
        return {"map_id": map_id, "version": version, "files": [
            {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
            for n, d in files.items()]}

    def fetch(self, map_id, version, name):
        yield self.files[(map_id, version, name)]


class 耳朵:
    def __init__(self):
        self.by: dict[str, list] = {}

    async def __call__(self, m):
        self.by.setdefault(T.parse(m.topic)[2], []).append(json.loads(m.payload))


@pytest.fixture
async def 台(tmp_path):
    broker, c, site = MemoryBroker(), 钟(), 站点()
    r = SimRobot(now_ms=c)
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    keeper = MapKeeper(tmp_path / "agent" / "maps", fetch=site.fetch)

    def mk():
        return AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                            store_dir=tmp_path / "agent", now_ms=c, loaded_map=("m", "1"),
                            boot_id="b", home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono,
                            maps=keeper)
    rt = mk()
    await rt.start()
    yield broker, c, r, ears, rt, site, mk
    await rt.close()


def _cmd(kind, payload, cid, c):
    cmd = Command(command_id=cid, task_id=f"{kind}-{cid}", kind=kind, issued_at=c.ms,
                  expires_at=c.ms + 60_000, control_epoch=1, payload=payload)
    return Message(T.cmd, json.dumps(cmd.to_wire()).encode(), 1, False)


async def _跑(rt, broker, n=20, r=None, c=None):
    import asyncio
    for _ in range(n):
        await rt.step(0.1)
        if r is not None:                          # 仿真狗也往前走(停车要走完才算停稳)
            r.tick(0.1)
            c.ms += 100
            c.mono += 0.1
        await asyncio.sleep(0.01)
    await broker.drain()


async def test_下发一张图_下载核对载入_能力里的地图跟着变_重启还用它(台):
    broker, c, r, ears, rt, site, mk = 台
    caps = Capabilities.from_wire(ears.by["capabilities"][-1])
    assert "map_activate" in caps.tasks and caps.tasks["patrol"]["map_version"] == "1"
    ref = site.add("m", "2", {"m.pgm": b"new map", "home.json": b'{"x":1,"y":2,"yaw":0}'})
    await rt._on_cmd(_cmd("map_activate", ref, "a1", c))
    await _跑(rt, broker)
    assert ears.by["cmd/ack"][-1]["result"] == "accepted"
    assert r.loaded_map[:2] == ("m", "2")
    assert rt.loaded_map == ("m", "2") and rt.processor.loaded_map == ("m", "2")
    assert rt.parts.nav._map_id == "m" and rt.parts.home.pose.position.x == 1.0
    caps = Capabilities.from_wire(ears.by["capabilities"][-1])
    assert caps.tasks["patrol"]["map_version"] == "2"
    assert any(e["kind"] == "map_activated" for e in ears.by["event"])
    await rt.close()
    r.loaded_map = None
    rt2 = mk()
    await rt2.start()
    assert rt2.loaded_map == ("m", "2") and r.loaded_map[:2] == ("m", "2"), \
        "重启照样用站点下发的那张"
    await rt2.close()


async def test_哈希不对_或适配器载不进_不换图_发失败事件(台):
    broker, c, r, ears, rt, site, mk = 台
    ref = site.add("m", "3", {"m.pgm": b"x"})
    site.files[("m", "3", "m.pgm")] = b"y"
    await rt._on_cmd(_cmd("map_activate", ref, "b1", c))
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "1")
    fails = [e for e in ears.by["event"] if e["kind"] == "map_activate_failed"]
    assert fails and "对不上" in fails[-1]["data"]["reason"]
    ref = site.add("m", "4", {"m.pgm": b"z"})
    r.fail_load = "定位起不来"
    await rt._on_cmd(_cmd("map_activate", ref, "b2", c))
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "1")
    from d1max_contract.maps import MapRef
    assert not rt.maps.dir_of(MapRef.from_wire(ref)).exists(), "载不进去的扔掉,狗上不留"
    assert "定位起不来" in [e for e in ears.by["event"]
                            if e["kind"] == "map_activate_failed"][-1]["data"]["reason"]


async def test_跑着任务先下载_等狗空下来才换_坏载荷拒(台):
    broker, c, r, ears, rt, site, mk = 台
    await rt._on_cmd(_cmd("map_activate", {"map_id": "../x"}, "c0", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"].startswith("payload")
    from d1max_contract.messages import MapPose
    goto = {"target": MapPose(map_id="m", map_version="1", frame_id="map", x=5.0, y=0.0,
                              yaw=0.0).to_wire()}
    await rt._on_cmd(_cmd("goto", goto, "c1", c))
    await rt.step(0.1)
    ref = site.add("m", "5", {"m.pgm": b"5", "home.json": b'{"x":0,"y":0,"yaw":0}'})
    await rt._on_cmd(_cmd("map_activate", ref, "c2", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["result"] == "accepted", "收下:下载不挡任务"
    await _跑(rt, broker, 10, r, c)
    assert rt.loaded_map == ("m", "1"), "跑着任务不换坐标系"
    await rt._on_cmd(_cmd("abort", {"task_id": "goto-c1"}, "c3", c))
    for _ in range(80):
        await _跑(rt, broker, 5, r, c)
        if rt.loaded_map == ("m", "5"):
            break
    assert rt.loaded_map == ("m", "5"), "狗空下来就换"


async def test_没有载入这一步的适配器_不报换图能力_命令回unsupported(tmp_path):
    class 没载入(SimRobot):
        load_map = None
    broker, c = MemoryBroker(), 钟()
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=没载入(now_ms=c), store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", monotonic=lambda: c.mono,
                      maps=MapKeeper(tmp_path / "maps", fetch=lambda *a: iter(())))
    assert "map_activate" not in rt._extra_tasks()
    await rt.close()


async def test_下载时照接任务_只有载入切坐标系那一小段不接(台):
    import asyncio
    import threading
    broker, c, r, ears, rt, site, mk = 台
    gate = threading.Event()
    ref = site.add("m", "6", {"m.pgm": b"6", "home.json": b'{"x":0,"y":0,"yaw":0}'})
    real = site.fetch

    def 慢(map_id, version, name):
        gate.wait(5)
        yield from real(map_id, version, name)
    rt.maps._fetch = 慢
    await rt._on_cmd(_cmd("map_activate", ref, "d1", c))
    await asyncio.sleep(0.05)                     # 换图那一趟已经开跑、卡在下载里
    from d1max_contract.messages import MapPose
    goto = {"target": MapPose(map_id="m", map_version="1", frame_id="map", x=0.2, y=0.0,
                              yaw=0.0).to_wire()}
    await rt._on_cmd(_cmd("goto", goto, "d2", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["result"] == "accepted", "下载不挡出警"
    await rt._on_cmd(_cmd("abort", {"task_id": "goto-d2"}, "d2x", c))
    await _跑(rt, broker, 30, r, c)
    loading = asyncio.Event()
    release = asyncio.Event()
    real_load = r.load_map

    async def 慢载(map_id, version, path):
        loading.set()
        await release.wait()
        return await real_load(map_id, version, path)
    r.load_map = 慢载
    gate.set()
    for _ in range(200):
        if loading.is_set():
            break
        await _跑(rt, broker, 1, r, c)
    assert loading.is_set()
    await rt._on_cmd(_cmd("goto", goto, "d3", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "map_switching"
    release.set()
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "6")


class 假发布:
    def __init__(self):
        self.cur = "2026-09-20-aaaaaa"
        self.installed = set()
        self.calls = []

    def current(self):
        return self.cur

    def ready(self, name):
        return name in self.installed

    def install(self, ref):
        self.calls.append(("install", ref.name))
        self.installed.add(ref.name)

    def activate(self, name):
        self.calls.append(("activate", name))
        self.cur = name
        return {"unit": "installed"}

    def rollback(self):
        self.calls.append(("rollback",))
        return "2026-09-20-aaaaaa"

    def commit_if_pending(self):
        return None


async def test_发布命令_装在后台_切要空闲要装好_能力里报在跑哪一版(tmp_path):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rel = 假发布()
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono,
                      releases=rel)
    await rt.start()
    await broker.drain()
    caps = Capabilities.from_wire(ears.by["capabilities"][-1])
    assert caps.tasks["release_install"] == {"current": "2026-09-20-aaaaaa"}
    new = "2026-09-25-bbbbbb"
    await rt._on_cmd(_cmd("release_activate", {"name": new}, "r1", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "not_installed"
    await rt._on_cmd(_cmd("release_install", {"name": new, "sha256": "a" * 64, "size": 9},
                          "r2", c))
    await _跑(rt, broker)
    assert ("install", new) in rel.calls
    assert any(e["kind"] == "release_installed" for e in ears.by["event"])
    from d1max_contract.messages import MapPose
    goto = {"target": MapPose(map_id="m", map_version="1", frame_id="map", x=5.0, y=0.0,
                              yaw=0.0).to_wire()}
    await rt._on_cmd(_cmd("goto", goto, "r3", c))
    await rt.step(0.1)
    await rt._on_cmd(_cmd("release_activate", {"name": new}, "r4", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "busy", "跑着任务不切版本"
    await rt._on_cmd(_cmd("release_rollback", {}, "r5", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "busy"
    await rt.close()


async def test_原点随下发的命令走_记在正在用的图里_重启照样认(台):
    broker, c, r, ears, rt, site, mk = 台
    ref = site.add("m", "7", {"m.pgm": b"7"})               # 图里没带 home.json
    await rt._on_cmd(_cmd("map_activate", ref | {"home": {"x": 2.0, "y": 1.0, "yaw": 0.5}},
                          "e1", c))
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "7") and rt.parts.home.pose.position.x == 2.0
    await rt.close()
    rt2 = mk()
    await rt2.start()
    assert rt2.parts.home is not None and rt2.parts.home.pose.position.y == 1.0
    await rt2._on_cmd(_cmd("map_activate", ref | {"home": {"x": "a"}}, "e2", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"].startswith("payload"), "原点不是数:拒"
    await rt2.close()


async def test_记不下正在用的图_把原来那张载回去(台):
    broker, c, r, ears, rt, site, mk = 台
    first = site.add("m", "8", {"m.pgm": b"8", "home.json": b'{"x":0,"y":0,"yaw":0}'})
    await rt._on_cmd(_cmd("map_activate", first, "f1", c))
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "8")
    ref = site.add("m", "9", {"m.pgm": b"9", "home.json": b'{"x":0,"y":0,"yaw":0}'})

    def 坏(*a, **k):
        raise OSError("盘满了")
    rt.maps.commit = 坏
    await rt._on_cmd(_cmd("map_activate", ref, "f2", c))
    await _跑(rt, broker)
    assert rt.loaded_map == ("m", "8") and r.loaded_map[:2] == ("m", "8"), "适配器也载回原来那张"
    assert "记不下" in [e for e in ears.by["event"]
                        if e["kind"] == "map_activate_failed"][-1]["data"]["reason"]


class 假录包:
    def __init__(self):
        self.recording = False
        self.last_bag = ""
        self.calls = []

    async def start(self, name):
        self.calls.append(("start", name))
        self.recording, self.last_bag = True, f"{name}-x"

    async def stop(self):
        self.calls.append(("stop",))
        self.recording = False


async def test_盘满了不录包_没在录不停_隔离的让它再传(tmp_path):
    from d1max_contract.storage import StorageFacts
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    full = StorageFacts(disk_used_ratio=0.95, outbox_bytes=10, outbox_cap_bytes=100,
                        backlog_files=0, backlog_bytes=0, oldest_backlog_s=None)
    ok = StorageFacts(disk_used_ratio=0.5, outbox_bytes=10, outbox_cap_bytes=100,
                      backlog_files=0, backlog_bytes=0, oldest_backlog_s=None)
    facts = {"f": full}
    rec = 假录包()
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono,
                      mapper=rec, storage_facts=lambda: facts["f"])
    retried = []
    rt._outbox_retry = lambda: retried.append(1) or 3
    await rt.start()
    await rt._on_cmd(_cmd("mapping", {"action": "stop", "name": "yard"}, "m1", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "not_recording"
    await rt._on_cmd(_cmd("mapping", {"action": "start", "name": "yard"}, "m2", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "storage_full" and rec.calls == []
    facts["f"] = ok
    await rt._on_cmd(_cmd("mapping", {"action": "start", "name": "yard"}, "m3", c))
    await _跑(rt, broker, n=3)
    assert rec.calls == [("start", "yard")]
    await rt._on_cmd(_cmd("outbox_retry", {}, "o1", c))
    await _跑(rt, broker, n=3)
    assert retried == [1] and ears.by["cmd/ack"][-1]["result"] == "accepted"
    got = [e for e in ears.by["event"] if e["kind"] == "outbox_retry"]
    assert got[-1]["data"]["released"] == 3
    await rt.close()


async def test_发布_电量不够不切不退_盘满了不装(tmp_path):
    from d1max_contract.storage import StorageFacts
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rel = 假发布()
    new = "2026-09-25-bbbbbb"
    rel.installed.add(new)
    dog = SimRobot(now_ms=c)
    full = StorageFacts(disk_used_ratio=0.95, outbox_bytes=10, outbox_cap_bytes=100,
                        backlog_files=0, backlog_bytes=0, oldest_backlog_s=None)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono, releases=rel,
                      storage_facts=lambda: full)
    await rt.start()
    await rt._on_cmd(_cmd("release_install", {"name": "2026-09-26-cccccc", "sha256": "a" * 64,
                                              "size": 9}, "i1", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "storage_full", "装要下载、建 venv:盘满了不装"
    dog.inject_battery(29.0)
    for kind, payload in (("release_activate", {"name": new}), ("release_rollback", {})):
        await rt._on_cmd(_cmd(kind, payload, f"{kind}-1", c))
        await broker.drain()
        assert ears.by["cmd/ack"][-1]["reason"] == "low_battery", kind
    assert rel.calls == []
    dog.inject_battery(31.0)
    await rt._on_cmd(_cmd("release_activate", {"name": new}, "a2", c))
    await _跑(rt, broker, n=3)
    assert rel.calls == [("activate", new)]
    await rt.close()


async def test_换图时不装版本(台):
    import asyncio
    import threading
    broker, c, r, ears, rt, site, mk = 台
    rt.releases = 假发布()
    gate = threading.Event()
    ref = site.add("m", "5", {"m.pgm": b"5", "home.json": b'{"x":0,"y":0,"yaw":0}'})
    real = site.fetch

    def 慢(map_id, version, name):
        gate.wait(5)
        yield from real(map_id, version, name)
    rt.maps._fetch = 慢
    await rt._on_cmd(_cmd("map_activate", ref, "w1", c))
    await asyncio.sleep(0.05)
    await rt._on_cmd(_cmd("release_install", {"name": "2026-09-25-bbbbbb", "sha256": "a" * 64,
                                              "size": 9}, "w2", c))
    await broker.drain()
    assert ears.by["cmd/ack"][-1]["reason"] == "busy"
    gate.set()
    await _跑(rt, broker)
    assert rt.releases.calls == []


async def test_起来连上站点_在途的那次升级算成(tmp_path):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    rel = 假发布()
    rel.commit_if_pending = lambda: "2026-09-25-bbbbbb"
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"),
                      boot_id="b", home=Pose.from_xy_yaw(0, 0, 0), monotonic=lambda: c.mono,
                      releases=rel)
    await rt.start()
    await _跑(rt, broker, n=3)
    got = [e for e in ears.by["event"] if e["kind"] == "release_committed"]
    assert got and got[-1]["data"]["name"] == "2026-09-25-bbbbbb"
    await rt.close()
