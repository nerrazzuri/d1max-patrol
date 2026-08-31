"""厂商后端的响应匹配。协议地雷全在这里拆。"""

import asyncio

import pytest

from d1max_patrol.backends import vendor_nav
from d1max_patrol.backends.base import (
    AlgErrorEvent,
    NavConnectionError,
    NavRequestError,
    NavTimeoutError,
)
from d1max_patrol.backends.vendor_nav import VendorNavBackend, _Pending
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import AlgErrorItem, Response
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)
from d1max_sim.nav_server import SimNavServer

#: 把状态轮询器静默掉。本文件测的是响应匹配层,而轮询器是一个后台请求
#: 发生器 —— 它会占着 _pending、把自己的迟到响应计进 dropped_frames、
#: 还会拨动仿真器那个全局响应奇偶计数器(延迟注入正是靠奇偶性打中特定
#: 请求的)。这三样都是本文件的断言直接依赖的东西。
#: 因为 _poll_loop 的 sleep 在循环体开头,这个间隔意味着测试期间它一次都不发。
_POLLER_OFF = 3600.0


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def backend(sim):
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0, status_poll_interval_s=_POLLER_OFF))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


async def _await_status(backend, req, wanted: str, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await backend.request(req) == wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{req.req_func} 未在 {timeout_s}s 内变为 {wanted}")


async def test_连接后可用(backend):
    assert backend.connected is True
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_未连接时请求抛连接错误(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, status_poll_interval_s=_POLLER_OFF))
    with pytest.raises(NavConnectionError):
        await b.request(R.get_nav_status())


async def test_连不上的地址抛连接错误():
    b = VendorNavBackend(NavConfig(url="ws://127.0.0.1:1", connect_timeout_s=1.0))
    with pytest.raises(NavConnectionError):
        await b.connect()


async def test_重复连接与重复关闭都安全(backend):
    await backend.connect()
    assert backend.connected is True
    await backend.close()
    await backend.close()
    assert backend.connected is False


async def test_连续请求不留挂起也不降级(backend):
    for _ in range(5):
        await backend.request(R.get_nav_status())
    assert backend.fallback_matches == 0
    assert backend.dropped_frames == 0
    assert backend.pending_count == 0


async def test_并发请求各自拿到自己的响应(backend):
    """并发下一旦匹配写错,这里就会串台。"""
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
        backend.request(R.get_mapping_status()),
        backend.request(R.get_all_pgm_map()),
    )
    assert results[0] == NavStatus.STANDBY.value
    assert results[1] == LocStatus.INIT.value
    assert results[2] == MappingStatus.PASSIVE.value
    assert R.parse_map_ids(results[3]) == []


async def test_乱序到达也不串台(sim, backend):
    """注入乱序:一半响应被延迟,到达顺序与发出顺序不同。"""
    sim.faults.response_delay_s = 0.15
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
    )
    assert results == [NavStatus.STANDBY.value, LocStatus.INIT.value,
                       NavStatus.STANDBY.value, LocStatus.INIT.value]
    assert backend.fallback_matches == 0


async def test_地雷1_速度接口的嵌套外壳被剥掉(backend):
    speed = await backend.request(R.get_navigation_speed())
    assert set(speed) == {"x", "y", "z"}          # 不是 {"AppReponseObjectData"}


async def test_地雷2_响应函数名与请求不同也能匹配(sim, backend):
    """loc_load_map 的响应叫 load_localization_map。"""
    map_id = sim.store.create_map()
    await backend.request(R.loc_load_map(map_id))
    assert backend.fallback_matches == 0          # 走的是主匹配,不是降级
    assert backend.dropped_frames == 0


async def test_地雷3_建图推送不会污染匹配表(backend):
    """notify_stop_mapping_status 顶着 app_resp 和 frame_count=1 进来。"""
    await backend.request(R.start_mapping())
    await _await_status(backend, R.get_mapping_status(),
                        MappingStatus.MAPPING_RUNNING.value)
    await backend.request(R.stop_mapping())
    await _await_status(backend, R.get_mapping_status(),
                        MappingStatus.MAPPING_SAVE_END.value)

    assert backend.fallback_matches == 0
    assert backend.dropped_frames == 0            # 推送被识别,不算丢帧
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_I3_主匹配帧号命中但函数名不符不被认领(backend):
    """帧号相符不足以认领 —— 函数名也必须对上,否则推送会冒领挂起请求。

    直接驱动 `_route()`:手工挂一条 `get_nav_status`(frame_count=7),
    喂一条 frame_count 同为 7、但 req_func 是 `get_loc_status` 的响应。
    删掉主匹配里 `entry.response_func == message.req_func` 这个条件,
    这条测试立刻变红 —— 这是简报最强调的"两个条件缺一不可"。
    """
    future = asyncio.get_running_loop().create_future()
    backend._pending[7] = _Pending("get_nav_status", "get_nav_status", future)
    stray = Response(frame_count=7, req_func="get_loc_status", ok=True,
                     msg=None, data="whatever", raw={})

    backend._route(stray)

    assert not future.done()


async def test_M1_推送识别先于降级匹配(monkeypatch, backend):
    """PUSH_ONLY_FUNCS 未来会长(比如计划里待验证的 exit_charging);一旦
    某个成员与某条挂起请求的响应名撞车,必须优先当推送处理,不能被降级
    匹配偷走 —— 目前集合只有一个成员且不与任何请求同名,这个顺序当前在
    行为上不可观测,得用 monkeypatch 造一个会撞车的成员才测得出来。

    注意要 patch `vendor_nav` 模块里的名字 —— 它是 `from ... import
    PUSH_ONLY_FUNCS` 按值导入的,patch 源模块 `nav_requests` 无效。
    """
    monkeypatch.setattr(vendor_nav, "PUSH_ONLY_FUNCS", frozenset({"get_nav_status"}))
    future = asyncio.get_running_loop().create_future()
    backend._pending[99] = _Pending("get_nav_status", "get_nav_status", future)
    backend._last_issued = 99
    fake_push = Response(frame_count=12345, req_func="get_nav_status", ok=True,
                         msg=None, data="pretend-push", raw={})

    backend._route(fake_push)

    assert not future.done()
    assert backend.fallback_matches == 0
    assert backend.dropped_frames == 0


async def test_帧号归零时降级到按名字匹配(sim, backend):
    sim.faults.frame_count_zero = True
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value
    assert backend.fallback_matches == 1


async def test_降级匹配按先进先出认领同名响应(sim, backend):
    """两条同名请求挂起时,降级匹配必须按 FIFO 认领。

    两条请求都叫 `get_all_paths_by_mapid`,但查的是不同地图、各自的路径点
    名字可以区分 —— 这样才测得出"先发的请求必须拿到先发的那份数据",而
    不只是随便哪条同名响应都行。把降级循环从 `for` 改成
    `reversed(list(...))`(LIFO),这条测试就会变红。
    """
    m1, m2 = sim.store.create_map(), sim.store.create_map()
    sim.store.set_path(m1, "path", [Waypoint("P_ONE", Pose.from_xy_yaw(0.0, 0.0))])
    sim.store.set_path(m2, "path", [Waypoint("P_TWO", Pose.from_xy_yaw(0.0, 0.0))])
    sim.faults.frame_count_zero = True
    results = await asyncio.gather(
        backend.request(R.get_all_paths_by_mapid(m1)),
        backend.request(R.get_all_paths_by_mapid(m2)),
    )
    names = [R.parse_paths_payload(r)["path"][0].name for r in results]
    assert names == ["P_ONE", "P_TWO"]
    assert backend.fallback_matches == 2


async def test_C1_超时后迟到响应不会冒领同名请求(sim):
    """迟到的旧帧号必须走丢弃,不能被同名的新请求捡走。

    第 1 条请求(m1)的真实回复被故障注入拖住,客户端短超时先一步判定
    超时、把这条挂起记录清理掉。随后正常发出第 2 条同名请求(m2)并保持
    挂起,再手工构造 m1 那条"迟到"的响应(复用它真实的 frame_count),
    验证它被丢弃、绝不会被降级匹配安在 m2 头上。中间插一条 `get_nav_status`
    占掉偶数位回复,只是为了让 m2 落在奇数位、能被故障注入单独拖住 ——
    这是仿真器"只延迟一半响应"的实现细节,不是本测试关心的东西。
    """
    m1, m2 = sim.store.create_map(), sim.store.create_map()
    sim.store.set_path(m1, "path", [Waypoint("P_ONE", Pose.from_xy_yaw(0.0, 0.0))])
    sim.store.set_path(m2, "path", [Waypoint("P_TWO", Pose.from_xy_yaw(0.0, 0.0))])

    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=0.5, status_poll_interval_s=_POLLER_OFF))
    await b.connect()
    try:
        sim.faults.response_delay_s = 5.0
        with pytest.raises(NavTimeoutError):
            await b.request(R.get_all_paths_by_mapid(m1))     # frame_count = 1,超时
        assert b.pending_count == 0

        sim.faults.response_delay_s = 0.0
        await b.request(R.get_nav_status())                    # 占掉偶数位回复

        sim.faults.response_delay_s = 0.3
        task2 = asyncio.create_task(b.request(R.get_all_paths_by_mapid(m2)))
        await asyncio.sleep(0)                                  # 让 task2 先挂进 _pending

        await sim._broadcast({
            "head": {"type": "app_resp", "frame_count": 1, "source": "alg_control_node"},
            "data": {"req_result": {"req_func": "get_all_paths_by_mapid", "status": "ok",
                                    "msg": None, "data": sim.store.paths_payload(m1)}},
        })
        await asyncio.sleep(0.05)
        assert b.dropped_frames == 1
        assert b.fallback_matches == 0
        assert b.pending_count == 1            # m2 还在等它自己真正的回复

        result2 = await task2
        paths2 = R.parse_paths_payload(result2)
        assert paths2["path"][0].name == "P_TWO"
    finally:
        sim.faults.response_delay_s = 0.0
        await b.close()


async def test_设备回error抛请求错误(backend):
    with pytest.raises(NavRequestError) as info:
        await backend.request(R.loc_load_map("不存在的图"))
    assert info.value.operation == "loc_load_map"
    assert "不存在" in info.value.message


async def test_无人认领的响应被计入丢帧(sim, backend):
    """构造一条谁也不认的响应,链路必须活着。

    必须从**服务端**推。`backend._ws.send()` 是客户端→服务端方向,报文会被
    仿真器按 `app_req` 校验拒掉("忽略畸形报文: 报文不是 app_req"),根本回不到
    本端读循环,`dropped_frames` 永远是 0。这条测试要验的是读循环收到一条
    无人认领的响应之后:计数加一、且**不把读循环带崩**——所以最后一行的
    `request()` 才是这条测试的重点,不能改成直接调 `_route()` 了事。
    """
    await sim._broadcast({
        "head": {"type": "app_resp", "frame_count": 9999, "source": "app"},
        "data": {"req_result": {"req_func": "nobody_asked", "status": "ok"}},
    })
    await asyncio.sleep(0.1)
    assert backend.dropped_frames == 1
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_I1a_畸形推送帧不会打死链路(sim, backend):
    """severity 非法(比如厂商填了 null)的算法故障推送不该判死链路。

    在 `nav_frames._parse_alg_error` 把 severity 转换重新包回
    `ProtocolError` 之前,这类畸形帧会以 TypeError/ValueError 逃到
    `_read_loop` 的外层 `except Exception`,被误判成链路已死,
    所有挂起请求连坐失败。
    """
    await sim._broadcast({
        "head": {"type": "alg_error_code_notify", "time_stamp": 1,
                 "source": "alg_control_node", "frame_count": 1},
        "data": {"items": [{"code": 13330, "description": "路径被挡",
                            "severity": None}]},
    })
    await asyncio.sleep(0.1)
    assert backend.connected is True
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_I1b_route内部异常不会打死链路(sim, backend):
    """`_route()` 自己抛的异常(不是解析阶段的 ProtocolError)也不能判死链路。

    I1a 那条测试其实测不到这一层 —— severity 非法在解析阶段就被
    `parse_message` 拦成 ProtocolError 了,根本到不了 `_route()`。这里
    直接替身 `_route`,让它在处理第一帧时抛一个跟解析毫无关系的异常,
    验证 `_read_loop` 自己的 try/except 兜住了它、没有把整条链路带崩。
    """
    original_route = backend._route
    calls = {"n": 0}

    def _flaky_route(message):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟 _route 内部 bug(与解析无关)")
        return original_route(message)

    backend._route = _flaky_route
    try:
        await sim._broadcast({
            "head": {"type": "app_resp", "frame_count": 424242, "source": "alg_control_node"},
            "data": {"req_result": {"req_func": "nobody_asked", "status": "ok",
                                    "msg": None, "data": None}},
        })
        await asyncio.sleep(0.05)
        assert backend.connected is True
    finally:
        backend._route = original_route

    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_I2_断链后旧连接会被关闭而不是泄漏(sim, backend):
    """`_on_link_lost` 必须真正关掉旧连接,不能只是丢引用。

    否则旧连接的 socket 一直开着,仿真器侧永远把它算作在线客户端 ——
    接上 Task 14 的自动重连之后,这会变成"每断一次泄漏一个 socket"。
    """
    assert len(sim._clients) == 1

    backend._on_link_lost("模拟链路异常")
    await asyncio.sleep(0.2)

    assert len(sim._clients) == 0
    await backend.close()          # 事后 close() 也不该报错


async def test_请求超时后挂起表被清理(sim, backend):
    sim.faults.response_delay_s = 5.0
    with pytest.raises(NavTimeoutError):
        await backend.request(R.get_nav_status())
    assert backend.pending_count == 0
    sim.faults.response_delay_s = 0.0


async def test_C2_取消请求不留挂起表垃圾(sim, backend):
    """取消飞行中的请求也不能把挂起表弄脏,否则会被后续降级匹配冒领。

    `request()` 之前只在 `except asyncio.TimeoutError` 分支里清理挂起表,
    外部取消(`gather` 连坐、上层 `wait_for`、Task 14 重连撤销在飞请求)
    会让 `CancelledError` 直接穿出去,挂起条目永久留在表里。
    """
    sim.faults.response_delay_s = 1.0
    task = asyncio.create_task(backend.request(R.get_nav_status()))
    await asyncio.sleep(0.05)
    assert backend.pending_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.pending_count == 0
    sim.faults.response_delay_s = 0.0


async def test_故障码推送转成事件(sim, backend):
    q = backend.subscribe()
    sim.faults.queued_alg_errors.append(AlgErrorItem(13330, "路径被挡", 2))
    event = await asyncio.wait_for(q.get(), timeout=3.0)
    assert isinstance(event, AlgErrorEvent)
    assert event.items[0].code == 13330


async def test_关闭时挂起请求被唤醒而不是永久卡住(sim, backend):
    sim.faults.response_delay_s = 5.0
    task = asyncio.create_task(backend.request(R.get_nav_status()))
    await asyncio.sleep(0.1)
    await backend.close()
    with pytest.raises(NavConnectionError):
        await task
