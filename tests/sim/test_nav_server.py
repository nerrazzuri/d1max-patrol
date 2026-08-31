"""仿真导航服务端。用真实 WebSocket 客户端对着它跑。"""

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import (
    AlgErrorNotify,
    Response,
    encode_request,
    parse_message,
)
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)
from d1max_sim.nav_server import SimNavServer


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _call(ws, req, frame_count: int) -> Response:
    """发一条请求,读到对应响应为止(跳过途中的推送)。"""
    await ws.send(encode_request(req.req_func, req.args, frame_count))
    while True:
        msg = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        if isinstance(msg, Response) and msg.req_func == req.response_func:
            return msg


async def _poll_until(ws, req, wanted: str, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    fc = 1000
    while loop.time() < deadline:
        fc += 1
        resp = await _call(ws, req, fc)
        if resp.data == wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{req.req_func} 未在 {timeout_s}s 内变为 {wanted}")


async def _ready_map(ws) -> str:
    """建一张图并加载定位,返回 map_id。"""
    await _call(ws, R.start_mapping(), 1)
    await _poll_until(ws, R.get_mapping_status(), MappingStatus.MAPPING_RUNNING.value)
    await _call(ws, R.stop_mapping(), 2)
    await _poll_until(ws, R.get_mapping_status(), MappingStatus.MAPPING_SAVE_END.value)
    maps = R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 3)).data)
    assert maps
    await _call(ws, R.loc_load_map(maps[0]), 4)
    await _poll_until(ws, R.get_loc_status(), LocStatus.CONTINUOUS_LOC.value)
    return maps[0]


async def test_服务端可连且初始状态就绪(sim):
    async with connect(sim.url) as ws:
        assert (await _call(ws, R.get_nav_status(), 1)).data == NavStatus.STANDBY.value
        assert (await _call(ws, R.get_loc_status(), 2)).data == LocStatus.INIT.value


async def test_响应帧号与请求一致(sim):
    async with connect(sim.url) as ws:
        assert (await _call(ws, R.get_nav_status(), 42)).frame_count == 42


async def test_建图全流程并推送保存完成通知(sim):
    async with connect(sim.url) as ws:
        await _call(ws, R.start_mapping(), 1)
        await _poll_until(ws, R.get_mapping_status(),
                          MappingStatus.MAPPING_RUNNING.value)
        await _call(ws, R.stop_mapping(), 2)

        # 在轮询之外单独收推送
        async def wait_notify():
            while True:
                msg = parse_message(await ws.recv())
                if isinstance(msg, Response) and \
                        msg.req_func == "notify_stop_mapping_status":
                    return msg

        notify = await asyncio.wait_for(wait_notify(), timeout=5.0)
        assert notify.ok is True
        assert R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 3)).data) == ["map_1"]


async def test_加载定位地图后进入持续定位(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)


async def test_加载不存在的地图回错误(sim):
    async with connect(sim.url) as ws:
        resp = await _call(ws, R.loc_load_map("不存在"), 1)
        assert resp.ok is False
        assert "不存在" in (resp.msg or "")


async def test_定位未就绪时拒绝导航(sim):
    async with connect(sim.url) as ws:
        resp = await _call(ws, R.start_nav(Pose.from_xy_yaw(1.0, 0.0)), 1)
        assert resp.ok is False
        assert "定位" in (resp.msg or "")


async def test_逐点导航能走到成功(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        assert (await _call(ws, R.start_nav(Pose.from_xy_yaw(1.0, 0.0)), 10)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.SUCCEED.value)
        await _poll_until(ws, R.get_nav_status(), NavStatus.STANDBY.value)
        assert sim.model.x == pytest.approx(1.0, abs=0.1)


async def test_停止导航进入取消(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)
        assert (await _call(ws, R.stop_nav(), 11)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.CANCELLED.value)


async def test_暂停与继续(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)
        assert (await _call(ws, R.pause_nav(), 11)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.PAUSE.value)
        assert (await _call(ws, R.continue_nav(), 12)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)


async def test_路径增删查往返(sim):
    async with connect(sim.url) as ws:
        map_id = await _ready_map(ws)
        wps = [Waypoint("A", Pose.from_xy_yaw(1.0, 2.0)),
               Waypoint("B", Pose.from_xy_yaw(3.0, 4.0))]
        assert (await _call(ws, R.add_nav_path(map_id, "p1", wps), 20)).ok
        got = R.parse_paths_payload(
            (await _call(ws, R.get_all_paths_by_mapid(map_id), 21)).data)
        assert [w.name for w in got["p1"]] == ["A", "B"]
        assert got["p1"][1].pose.position.y == 4.0

        assert (await _call(ws, R.modify_nav_path(map_id, "p1", "p1", wps[:1]), 22)).ok
        got = R.parse_paths_payload(
            (await _call(ws, R.get_all_paths_by_mapid(map_id), 23)).data)
        assert [w.name for w in got["p1"]] == ["A"]

        assert (await _call(ws, R.remove_nav_path([(map_id, "p1")]), 24)).ok
        got = R.parse_paths_payload(
            (await _call(ws, R.get_all_paths_by_mapid(map_id), 25)).data)
        assert got == {}


async def test_地图删除与重命名的数组参数(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        assert (await _call(ws, R.rename_map_name("map_1", "厂区"), 30)).ok
        assert R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 31)).data) == ["厂区"]
        assert (await _call(ws, R.remove_map_by_id(["厂区"]), 32)).ok
        assert R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 33)).data) == []


async def test_速度接口走嵌套外壳(sim):
    async with connect(sim.url) as ws:
        resp = await _call(ws, R.get_navigation_speed(), 1)
        assert resp.ok and set(resp.data) == {"x", "y", "z"}
        assert "AppReponseObjectData" in resp.raw["data"]["req_result"]

        resp = await _call(ws, R.set_navigation_speed(0.9), 2)
        assert resp.data == {"x": 0.9, "y": 0.5, "z": 1.5}   # 设备补默认值


@pytest.mark.parametrize(
    "req",
    [R.start_multi_nav("m", "p"),
     R.start_multi_nav_by_points("m", [Pose.from_xy_yaw(1.0, 0.0)]),
     R.start_nav_return_home()],
)
async def test_多点导航与返航被明确拒绝(sim, req):
    """本项目全逐点执行,仿真器不假装支持这些接口。"""
    async with connect(sim.url) as ws:
        resp = await _call(ws, req, 1)
        assert resp.ok is False
        assert "逐点" in (resp.msg or "") or "未实现" in (resp.msg or "")


async def test_未知接口回错误而不是断链(sim):
    async with connect(sim.url) as ws:
        await ws.send(encode_request("fly_to_the_moon", None, 1))
        msg = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert isinstance(msg, Response)
        assert msg.ok is False
        assert (await _call(ws, R.get_nav_status(), 2)).ok   # 链路仍在


async def test_畸形报文不打断链路(sim):
    async with connect(sim.url) as ws:
        await ws.send("{ not json")
        assert (await _call(ws, R.get_nav_status(), 1)).ok


async def _control(sim, command: str) -> dict:
    async with connect(sim.control_url) as ws:
        await ws.send(json.dumps({"cmd": command}))
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))


async def test_控制通道执行注入命令(sim):
    assert (await _control(sim, "slow 2"))["ok"] is True
    assert sim.faults.speed_scale == 0.5
    out = await _control(sim, "status")
    assert "speed_scale=0.5" in out["msg"]


async def test_控制通道对非法命令回_ok_false(sim):
    out = await _control(sim, "fly")
    assert out["ok"] is False
    assert "未知命令" in out["msg"]


async def test_注入的故障码被推送给所有客户端(sim):
    async with connect(sim.url) as ws:
        await _control(sim, "alg_error 13330")

        async def wait_alg():
            while True:
                msg = parse_message(await ws.recv())
                if isinstance(msg, AlgErrorNotify):
                    return msg

        notify = await asyncio.wait_for(wait_alg(), timeout=5.0)
        assert notify.items[0].code == 13330


async def test_帧号归零注入(sim):
    async with connect(sim.url) as ws:
        await _control(sim, "frame_count_zero on")
        await ws.send(encode_request("get_nav_status", None, 77))
        msg = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert msg.frame_count == 0
        assert msg.req_func == "get_nav_status"


async def test_定位丢失使进行中的导航失败(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)
        await _control(sim, "loc_lost")
        await _poll_until(ws, R.get_nav_status(), NavStatus.FAILED.value)
        assert (await _call(ws, R.get_loc_status(), 11)).data == LocStatus.LOC_LOST.value


async def test_预约导航失败(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _control(sim, "nav_fail")
        assert (await _call(ws, R.start_nav(Pose.from_xy_yaw(1.0, 0.0)), 10)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.FAILED.value)


async def test_断链注入会踢掉客户端(sim):
    ws = await connect(sim.url)
    await _control(sim, "disconnect 1")
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), timeout=5.0)


async def test_停止后端口释放(sim):
    url = sim.url
    await sim.stop()
    with pytest.raises(OSError):
        await asyncio.wait_for(connect(url), timeout=3.0)
