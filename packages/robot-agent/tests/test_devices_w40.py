"""W40 设备健康:每台设备在线、离线、降级、故障,随遥测每 10 s 带一份。"""

from __future__ import annotations

import asyncio
from collections import namedtuple
from pathlib import Path

import pytest

from d1max_agent.devices import DeviceHealth, camera_links, parse_links
from d1max_contract.devices import device, parse_devices
from d1max_contract.errors import ContractError

U = namedtuple("U", "total used free")


class 假RTK:
    def __init__(self, fix=None, error=""):
        self.fix, self.error = fix, error

    def latest(self):
        return self.fix


def _by(items):
    return {d["id"]: (d["state"], d["detail"]) for d in items}


def test_契约_设备名状态写错就拒_遥测里坏的扔掉():
    assert device("camera:thermal", "online", "x" * 300)["detail"] == "x" * 200
    for bad in (("Relay", "online"), ("relay", "ok"), ("link:", "online"), ("a:b c", "online")):
        with pytest.raises(ContractError):
            device(*bad)
    got = parse_devices([{"id": "relay", "state": "online"}, {"id": "x", "state": "??"}, 3,
                         {"id": "rtk", "state": "degraded", "detail": 5}])
    assert got == [{"id": "relay", "state": "online", "detail": ""},
                   {"id": "rtk", "state": "degraded", "detail": ""}]
    assert parse_devices("x") is None


@pytest.mark.parametrize("fix, state, word", [
    (None, "offline", "尚未收到"),
    ({"fix": "fixed", "stale": True}, "offline", "未更新"),
    ({"fix": "fixed", "heading_deg": 87.4}, "online", "朝向 87°"),
    ({"fix": "float"}, "degraded", "浮点"),
    ({"fix": "single"}, "degraded", "单点"),
    ({"fix": "none"}, "degraded", "没有定位"),
])
async def test_RTK按解的好坏分(fix, state, word):
    got = _by(await DeviceHealth(hal=object(), rtk=假RTK(fix)).refresh())
    assert got["rtk"][0] == state and word in got["rtk"][1]


async def test_RTK串口开不了_带上原因():
    got = _by(await DeviceHealth(hal=object(), rtk=假RTK(None, "串口没了")).refresh())
    assert got["rtk"] == ("offline", "串口没了")


async def test_盘_没挂上故障_快满降级_够用在线(tmp_path):
    dh = DeviceHealth(hal=object(), outbox=tmp_path, outbox_mount="/mnt/ssd",
                      ismount=lambda p: False)
    assert _by(await dh.refresh())["disk"][0] == "error"
    dh._ismount = lambda p: True
    dh._disk_usage = lambda p: U(1000 * 2**30, 950 * 2**30, 50 * 2**30)
    assert _by(await dh.refresh())["disk"][0] == "degraded"
    dh._disk_usage = lambda p: U(1000 * 2**30, 100 * 2**30, 900 * 2**30)
    assert _by(await dh.refresh())["disk"] == ("online", "剩 900 GB")


async def test_网络设备_连得上在线_连不上离线_并着查():
    seen = []

    def reach(h, p):
        seen.append((h, p))
        return h == "192.168.10.1"
    dh = DeviceHealth(hal=object(), links={"router": ("192.168.10.1", 80),
                                           "thermal": ("192.168.10.64", 554)}, reach=reach)
    got = _by(await dh.refresh())
    assert got["link:router"][0] == "online" and got["link:thermal"] == (
        "offline", "无法连接 192.168.10.64:554")
    assert sorted(seen) == [("192.168.10.1", 80), ("192.168.10.64", 554)]


def test_网络设备的写法_相机地址自动查():
    assert parse_links("router=192.168.10.1:80, nas=10.0.0.2:445") == {
        "router": ("192.168.10.1", 80), "nas": ("10.0.0.2", 445)}
    assert parse_links("") == {}
    for bad in ("router", "router=1.2.3.4", "router=1.2.3.4:x", "Router!=1.2.3.4:80",
                "r=1.2.3.4:70000"):
        with pytest.raises(ValueError):
            parse_links(bad)
    assert camera_links({"thermal": {"url": "rtsp://admin:pw@192.168.10.64/Streaming"},
                         "front": {"url": "rtsp://10.0.0.5:8554/front"}}) == {
        "thermal": ("192.168.10.64", 554), "front": ("10.0.0.5", 8554)}


async def test_在录的每一路_断了故障带原因():
    class 录像:
        def camera_health(self):
            return {"front": "", "thermal": "ffmpeg 退了(1):401 Unauthorized"}
    got = _by(await DeviceHealth(hal=object(), recorder=录像()).refresh())
    assert got["camera:front"] == ("online", "在录")
    assert got["camera:thermal"][0] == "error" and "401" in got["camera:thermal"][1]


async def test_HAL报的上装那几样带上_HAL炸了别的照报():
    class 好HAL:
        def device_health(self):
            return [device("relay", "online", "6 个线圈")]

    class 坏HAL:
        def device_health(self):
            raise RuntimeError("boom")
    assert _by(await DeviceHealth(hal=好HAL()).refresh())["relay"][0] == "online"
    got = _by(await DeviceHealth(hal=坏HAL(), rtk=假RTK({"fix": "fixed"})).refresh())
    assert "relay" not in got and got["rtk"][0] == "online"


def test_一样都没有就不报():
    assert not DeviceHealth(hal=object()).wanted()
    assert DeviceHealth(hal=object(), outbox=Path("/x")).wanted()


async def test_仿真狗装了上装_随遥测带设备健康(tmp_path):
    from test_runtime_maps import REG, T, _跑, 耳朵, 钟

    from d1max_adapter_sim.robot import SimRobot
    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_patrol.protocol.nav_types import Pose
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    dog = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2, payload=True)
    dog.device_faults["relay"] = ("error", "左聚光灯未确认")
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=dog,
                      store_dir=tmp_path, now_ms=c, loaded_map=("m", "1"), boot_id="b",
                      home=Pose.from_xy_yaw(0.0, 0.0), monotonic=lambda: c.mono,
                      odom_identity=True, telemetry_period_ms=100)
    await rt.start()
    for _ in range(4):
        await _跑(rt, broker, n=3, r=dog, c=c)
        await asyncio.sleep(0.02)                      # 后台那一份收好
    with_dev = [t for t in ears.by.get("telemetry", []) if "devices" in t]
    assert with_dev, "收完成下一拍带上"
    got = _by(with_dev[-1]["devices"])
    assert got["relay"] == ("error", "左聚光灯未确认") and got["speaker"][0] == "online"
    n = len(with_dev)
    await _跑(rt, broker, n=10, r=dog, c=c)            # 不到 10 s 不再带
    assert len([t for t in ears.by.get("telemetry", []) if "devices" in t]) == n
    await rt.close()
