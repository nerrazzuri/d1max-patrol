"""仿真导航服务端。用真实 WebSocket 客户端对着它跑。"""

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import (
    AlgErrorItem,
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
from d1max_sim.nav_server import _TICK_IO_TIMEOUT_S, SimNavServer


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
        # Mi-8: frame_count == 1 是厂商地雷 3 的核心特征 —— 客户端必须靠它(而不是
        # 猜测)识别出这是一条推送而不是某个 frame_count=1 请求的响应。Task 12/13
        # 的帧号匹配实现要以这个数值为红线,这里必须钉住。
        assert notify.frame_count == 1
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


async def test_带查询串的控制通道地址仍然走控制通道(sim):
    """MIN-4: `/control?x=1` 必须还是控制通道,不能被当成导航通道。

    路由原来是 `ws.request.path.rstrip("/") == CONTROL_PATH`,不剥查询串。
    带查询串的连接会掉进 `_handle_nav()`:注入命令被当成导航请求,
    `parse_request` 解析不了、只在日志里记一句"忽略畸形报文",客户端
    **一个字都收不到**,表现为静默挂起而不是报错。
    """
    async with connect(sim.control_url + "?x=1") as ws:
        await ws.send(json.dumps({"cmd": "slow 2"}))
        out = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
    assert out["ok"] is True
    assert sim.faults.speed_scale == 0.5


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
    # Mi-5: 不能用 OSError —— Python 3.11 起内置 TimeoutError 是 OSError 的子类,
    # 一旦 stop() 又出现"端口未释放导致 connect() 挂起超时"的回归,3.11+ 上这条
    # 断言会照样通过。这里钉的是"连接被拒绝",不是"某种 OSError"。
    with pytest.raises(ConnectionRefusedError):
        await asyncio.wait_for(connect(url), timeout=3.0)


async def test_stuck注入让机器人完全不动(sim):
    """M2: `stuck` 是 `FaultState.stuck` -> `Planar2DModel.frozen` 的唯一接线点,
    删掉接线这条用例必须变红(见修复报告里的变异实验)。"""
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)

        await _control(sim, "stuck on")
        x0, y0 = sim.model.x, sim.model.y
        await asyncio.sleep(0.3)
        assert sim.model.x == pytest.approx(x0)
        assert sim.model.y == pytest.approx(y0)
        # 状态正常但完全不动,这正是 stuck 的定义 —— 不是导航卡死或报错。
        assert (await _call(ws, R.get_nav_status(), 11)).data == NavStatus.ACTIVE.value


async def test_slow注入让行走显著变慢(sim):
    """M2: `slow` 接线到 `Planar2DModel.speed_scale`。"""
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)

        await _control(sim, "slow 4")   # speed_scale = 0.25
        x0 = sim.model.x
        await asyncio.sleep(0.3)
        moved = sim.model.x - x0
        # 常速下 0.3s 的理论位移量级是 linear_speed(0.6) * 0.3 = 0.18;
        # slow 4 应显著小于这个量级,但仍然 > 0(不是完全不动,那是 stuck 的定义)。
        assert 0.0 < moved < 0.09


async def test_合法请求内部抛类型错误也回错误而不断链(sim):
    """M4: `set_navigation_speed` 传 x=None 会在 handler 内部触发 `float(None)`
    抛出的 TypeError —— 请求本身是合法 JSON、req_func 也认识,只是参数类型
    古怪。这类异常必须变成一条 error 响应,链路不能被静默关掉。"""
    async with connect(sim.url) as ws:
        resp = await _call(ws, R.set_navigation_speed(None), 1)
        assert resp.ok is False
        assert (await _call(ws, R.get_nav_status(), 2)).ok   # 链路仍在


async def test_reorder注入制造响应乱序到达(sim):
    """Mi-1: `reorder` 只延迟"全局响应序号"为奇数位的那一半响应
    (`nav_server.py` `_reply` 里的奇偶交替),用来制造真乱序。"""
    async with connect(sim.url) as ws:
        await _control(sim, "reorder 0.2")
        await ws.send(encode_request("get_nav_status", None, 1))
        await ws.send(encode_request("get_loc_status", None, 2))
        first = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        second = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        # 两条请求按 nav/loc 顺序发出;第一条响应命中全局序号的奇数位、被延迟,
        # 第二条命中偶数位、不延迟,所以到达顺序反过来变成 loc/nav。
        assert first.req_func == "get_loc_status"
        assert second.req_func == "get_nav_status"


async def test_预约导航失败不会残留到下一次不相关的导航(sim):
    """Mi-3 真 bug 回归: `fail_next_nav` 是一次性开关,如果它在
    `start_nav` 被 `nav.start()` 自身拒绝(不在 StandBy)时已经被消费掉,
    会残留到下一次毫不相干的成功导航上,让它莫名其妙地失败。"""
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)

        await _control(sim, "nav_fail")
        # 此时 nav 处于 Active,不在 StandBy —— start_nav 会被 nav.start() 自身
        # 的状态检查拒绝,根本没机会消费 fail_next_start。
        rejected = await _call(ws, R.start_nav(Pose.from_xy_yaw(2.0, 0.0)), 11)
        assert rejected.ok is False

        assert (await _call(ws, R.stop_nav(), 12)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.CANCELLED.value)
        await _poll_until(ws, R.get_nav_status(), NavStatus.STANDBY.value)

        # 不相关的下一次导航必须正常成功,不能被上面残留的 fail_next_start 拖累。
        assert (await _call(ws, R.start_nav(Pose.from_xy_yaw(1.0, 0.0)), 13)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.SUCCEED.value)


async def test_断链静默窗口内连接先握手成功再被关闭(sim):
    """Mi-6 裁决: 静默窗口内新连接的语义是"握手先成功,随后立刻以 1012
    关闭",不是 TCP 层拒连。这与真机断网(客户端连 TCP 都建立不起来)不同,
    是本卷有意保留的已知简化 —— 给 Task 14 的提示: 重连逻辑不能把
    `connect()` 成功本身当成"链路已恢复"的证据,必须继续看后续是否又被
    1012 关闭。"""
    ws = await connect(sim.url)
    await _control(sim, "disconnect 1")
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), timeout=5.0)

    ws2 = await connect(sim.url)   # 握手必须成功,不应在这里抛异常
    with pytest.raises(ConnectionClosed) as exc_info:
        await asyncio.wait_for(ws2.recv(), timeout=5.0)
    # `.code` 在 websockets 13.1+ 已弃用,改用 `.rcvd.code`(见弃用提示)。
    assert exc_info.value.rcvd.code == 1012


async def test_断链静默窗口内控制通道仍可用(sim):
    """Mi-6: 断链注入模拟的是导航链路故障,控制通道是有意不受影响的 ——
    运维/测试仍需要能在静默窗口内连控制通道下达、查询、撤销注入。"""
    ws = await connect(sim.url)
    await _control(sim, "disconnect 1")
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), timeout=5.0)

    out = await _control(sim, "status")
    assert out["ok"] is True


async def test_reset_loc冒烟(sim):
    """Mi-9: `reset_loc` 还在 `UNVERIFIED_RESPONSE_FUNCS` 里,响应名未经真机
    验证,但至少要有一条冒烟用例证明这条链路走得通。"""
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        resp = await _call(ws, R.reset_loc(), 1)
        assert resp.ok is True
        await _poll_until(ws, R.get_loc_status(), LocStatus.CONTINUOUS_LOC.value)


async def test_get_pgm_map冒烟(sim):
    """Mi-9: `get_pgm_map` 的载荷形状(`MapRecord.to_grid()`)是下游读图的
    唯一契约,之前完全没有测试覆盖。"""
    async with connect(sim.url) as ws:
        map_id = await _ready_map(ws)
        resp = await _call(ws, R.get_pgm_map(map_id), 1)
        assert resp.ok is True
        assert resp.data["info"]["width"] > 0
        assert resp.data["info"]["height"] > 0


class _卡死的连接:
    """M3 回归用的假连接: send() 永不返回,但可被 cancel() 正常打断。

    刻意不构造真实拥塞 socket —— 那需要把写缓冲真的塞满,在 CI 里对机器
    负载敏感、容易 flaky。这里换一种造法:直接顶替一个"送不出去也不报错、
    只是永远不返回"的连接,纯粹验证 `_broadcast` 对每个客户端的
    `asyncio.wait_for(..., timeout=_TICK_IO_TIMEOUT_S)` 这个界确实生效,
    不涉及真实网络、不依赖墙钟精度数 tick 次数。
    """

    def __init__(self) -> None:
        self.remote_address = ("stuck", 0)

    async def send(self, _text: str) -> None:
        await asyncio.Event().wait()   # 永远挂起,直到被 cancel


async def test_tick循环不会被不读取的客户端永久拖住(sim):
    """M3 回归: 删掉 `_broadcast`/`_apply_faults` 里的 `asyncio.wait_for`
    (换成裸 `await`)必须让这条测试变红或挂住 —— 见修复报告里的变异验证。"""
    stuck = _卡死的连接()
    sim._clients.add(stuck)
    try:
        async with connect(sim.url) as ws:
            await _ready_map(ws)
            await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
            await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)

            # 直接入队一条待推送的故障码(不经控制通道的真实网络往返),
            # 逼下一次 _flush_pushes -> _broadcast 命中 stuck 连接。
            sim.faults.queued_alg_errors.append(
                AlgErrorItem(code=13330, description="injected", severity=0))

            # 给 tick 循环几个周期,确保它已经取到这条待推送项、正卡在对
            # stuck 连接的 send() 上(此刻若无 wait_for 保护,tick 循环已经
            # 永久停摆)。
            await asyncio.sleep(1.0 / sim.tick_hz * 5)
            x1 = sim.model.x

            # 再等一段明显长于 _TICK_IO_TIMEOUT_S 的时间。若这个界还在生效,
            # tick 循环会在超时后恢复,机器人应当继续朝目标走出一段肉眼可辨
            # 的距离;若界被去掉,tick 循环永久卡死,位置纹丝不动。这里只
            # 断言"确实又走了一截",不掐着墙钟精度数 tick 次数。
            await asyncio.sleep(_TICK_IO_TIMEOUT_S + 1.0)
            x2 = sim.model.x
            assert x2 - x1 > 0.1
    finally:
        sim._clients.discard(stuck)
