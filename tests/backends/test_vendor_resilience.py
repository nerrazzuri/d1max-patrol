"""状态轮询、断链与重连。第四个地雷:设备没有状态推送通道。

时序约定(评审 C1):凡是"注入之后要做后续动作"的地方,一律**等真实信号**
(`_drain_until` 等到 `BackendDisconnected`、`_wait_until` 等到某个可观测量
成立),不要 `await asyncio.sleep(0.2)` 赌它已经发生了 —— 断链注入不是同步
生效的,生效链条是 tick 循环下一拍 → `_apply_faults()` → 独立任务里关连接 →
客户端读循环退出 → `_on_link_lost()`,没有任何东西保证它在写死的零点几秒内
走完。只有**负向断言的观察窗口**("再等 0.8s 看它会不会发生")才保留固定
sleep,那种 sleep 是断言的一部分。
"""

import asyncio
import contextlib

import pytest
from websockets.asyncio.server import serve

from d1max_patrol.backends.base import (
    BackendDisconnected,
    BackendReconnected,
    LocStatusEvent,
    MappingStatusEvent,
    NavBackendError,
    NavConnectionError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import Response
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
)
from d1max_sim.nav_server import SimNavServer

FAST = dict(request_timeout_s=3.0, status_poll_interval_s=0.05,
            connect_timeout_s=2.0, reconnect_min_s=0.05, reconnect_max_s=0.2)

#: 轮询周期大到一次都发不出来(`_poll_loop` 的 sleep 在循环体第一句)。
#: 给那些只想单独测某条路径、不希望轮询器插进来搅局的用例。
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
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


async def _drain_until(queue, predicate, timeout_s: float = 10.0):
    async def loop():
        while True:
            event = await queue.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(loop(), timeout=timeout_s)


async def _collect_until(queue, predicate, timeout_s: float = 15.0) -> list:
    """收到 predicate 命中为止,返回**这期间收到的全部事件**(含命中那条)。

    `_drain_until` 会把不匹配的事件丢掉,数不出"中间夹了几条"。要断言
    "一次断链只发一条 BackendDisconnected",就得把中间那些也留下来。
    """
    received: list = []

    async def loop():
        while True:
            event = await queue.get()
            received.append(event)
            if predicate(event):
                return
    await asyncio.wait_for(loop(), timeout=timeout_s)
    return received


async def _wait_until(predicate, timeout_s: float = 10.0, tick_s: float = 0.01) -> None:
    """轮询等一个条件成立。用于没有事件可等的场合(服务端在线连接数之类)。"""
    async def loop():
        while not predicate():
            await asyncio.sleep(tick_s)
    await asyncio.wait_for(loop(), timeout=timeout_s)


async def _ready(backend) -> str:
    await backend.start_mapping()
    while await backend.mapping_status() is not MappingStatus.MAPPING_RUNNING:
        await asyncio.sleep(0.02)
    await backend.stop_mapping()
    while await backend.mapping_status() is not MappingStatus.MAPPING_SAVE_END:
        await asyncio.sleep(0.02)
    map_id = (await backend.list_maps())[0]
    await backend.load_map(map_id)
    while await backend.loc_status() is not LocStatus.CONTINUOUS_LOC:
        await asyncio.sleep(0.02)
    return map_id


async def test_轮询器开局就广播当前状态(backend):
    q = backend.subscribe()
    event = await _drain_until(q, lambda e: isinstance(e, NavStatusEvent))
    assert event.status is NavStatus.STANDBY
    assert event.previous is None


async def test_状态不变时不重复发事件(backend):
    # 开局那一轮会连发 Nav/Loc/Mapping 三条 previous=None 的事件。必须先把这
    # 三条收干净再断言"不再发" —— 原来靠 `sleep(0.3)` 赌它们已经发完,整机
    # 负载下它们落在 0.3s 之后,新订阅者就正好接住(评审实测:把轮询周期调到
    # 0.35 即确定性变红)。
    需要 = {NavStatusEvent, LocStatusEvent, MappingStatusEvent}
    with backend.subscription() as 预热:
        收到 = set()
        while not 需要 <= 收到:
            收到.add(type(await asyncio.wait_for(预热.get(), 10.0)))
    q = backend.subscribe()
    await asyncio.sleep(0.3)          # 让轮询器再跑好几轮 —— 这段是断言的一部分
    assert q.empty()


async def test_导航状态变化被转成事件(backend):
    await _ready(backend)
    q = backend.subscribe()
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    event = await _drain_until(
        q, lambda e: isinstance(e, NavStatusEvent) and e.status is NavStatus.SUCCEED)
    assert event.previous in (NavStatus.ACTIVE, NavStatus.INITIALIZING)


async def test_定位与建图状态也有事件(backend):
    q = backend.subscribe()
    await _ready(backend)
    # 等 MAPPING_SAVE_END 而不是 MAPPING_RUNNING: 后者只在 _ready() 发现它、
    # 立刻 stop_mapping() 之间存在 0~20ms,比后端 0.05s 的轮询周期还短,轮询
    # 注定经常抓不住 —— 这是轮询式状态跟踪的固有性质,不是 bug(真机待验证
    # 清单第 10 条)。SAVE_END 是终态,状态机里它后面没有任何排期,会一直保持。
    # 不要"优化"回 MAPPING_RUNNING。
    await _drain_until(
        q, lambda e: isinstance(e, MappingStatusEvent)
        and e.status is MappingStatus.MAPPING_SAVE_END)
    await _drain_until(
        q, lambda e: isinstance(e, LocStatusEvent)
        and e.status is LocStatus.CONTINUOUS_LOC)


async def test_等待导航终态可用(backend):
    await _ready(backend)
    # base.py 文档化的无竞态写法:先订阅、再下发、把队列交给 wait_nav_terminal,
    # 订阅与下发之间没有 await,不存在"终态在这两步之间就推过来了"的窗口。
    with backend.subscription() as q:
        await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
        assert await backend.wait_nav_terminal(timeout_s=15.0, queue=q) is NavStatus.SUCCEED


async def test_导航失败也是终态(sim, backend):
    await _ready(backend)
    sim.faults.fail_next_nav = True
    with backend.subscription() as q:
        await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
        assert await backend.wait_nav_terminal(timeout_s=15.0, queue=q) is NavStatus.FAILED


async def test_断链发出断开事件(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    event = await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    assert event.reason


async def test_一次断链只发一条断开事件(sim, backend):
    """I2 / M3: 事件契约是"每次链路丢失一条",不是"每次重连失败一条"。

    断链窗口(2.0s)远长于重连退避(0.05~0.2s),期间会有好几次重连尝试:
    `_open_link()` 每次都把 `_ws` 设上、随即被仿真器以 1012 关掉,读循环退出
    再次走进 `_on_link_lost`。老守卫在这段窗口里形同虚设(评审实测 12 条)。
    """
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 2.0
    事件 = await _collect_until(q, lambda e: isinstance(e, BackendReconnected),
                                timeout_s=30.0)
    断开 = [e for e in 事件 if isinstance(e, BackendDisconnected)]
    assert len(断开) == 1, f"一次断链只应有一条 BackendDisconnected,实收 {len(断开)} 条"
    # 确认这段窗口里确实反复重试过 —— 否则上面那条断言是空过的
    assert backend.reconnect_attempts >= 2


async def test_自动重连并重新对齐状态(sim, backend):
    # I1: 先等轮询器把 last_nav_status 钉成 STANDBY。断链若发生在首轮轮询之前,
    # 三个 last_* 本来就是 None,"重连后清缓存"这条规则清不清都一样,这条测试
    # 就守不住它了(评审 MUT2:删掉那三行清缓存,无人变红)。
    await _wait_until(lambda: backend.last_nav_status is not None)
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    await _drain_until(q, lambda e: isinstance(e, BackendReconnected), timeout_s=15.0)

    assert backend.reconnect_attempts >= 1
    assert backend.connected is True
    # 重连后缓存作废,当前状态被重新广播一遍
    event = await _drain_until(
        q, lambda e: isinstance(e, NavStatusEvent) and e.previous is None)
    assert event.status is NavStatus.STANDBY
    assert await backend.nav_status() is NavStatus.STANDBY


async def test_断链期间的请求抛错而不是永久挂起(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 1.0
    await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    with pytest.raises(NavBackendError):
        await backend.nav_status()


async def test_等待重连(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    await backend.wait_connected(timeout_s=15.0)
    assert backend.connected is True


async def test_等待重连超时(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 30.0
    # 等到断链真的被登记再开始等重连:此刻静默窗口还剩 ~30s,重连必然还在失败,
    # NavTimeoutError 是确定的(原来先 sleep(0.2) 再等,断链没登记就
    # wait_connected 立刻返回 → DID NOT RAISE)。
    await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    with pytest.raises(NavTimeoutError):
        await backend.wait_connected(timeout_s=0.5)


async def test_关闭时不再重连(sim, backend):
    """I3: close() 之后重连必须彻底停下。

    原来先 sleep(0.1) 再 close():断链常常还没发生,`reconnect_attempts` 前后
    自然相等,这条测试静默退化成空测;偶尔时序对上了就抓出真 bug
    (`assert 13 == 2`)。改成等真信号之后,每一次跑都在真正验证。
    """
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    await backend.close()
    before = backend.reconnect_attempts
    await asyncio.sleep(0.8)          # 负向断言的观察窗口:真要重连,早该重连了
    assert backend.reconnect_attempts == before
    assert backend.connected is False


async def test_关掉自动重连时只发断开事件(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST), auto_reconnect=False)
    await b.connect()
    try:
        q = b.subscribe()
        sim.faults.disconnect_seconds = 0.2
        await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
        await asyncio.sleep(0.8)      # 负向断言的观察窗口
        assert b.connected is False
        assert b.reconnect_attempts == 0
    finally:
        await b.close()


async def test_轮询遇到超时不会杀死轮询器(sim):
    # 单开一个 request_timeout_s 很短的后端,好让"轮询这一轮超时"这件事快速、
    # 确定地发生。仿真器的 response_delay_s 只延迟一半的响应(全局奇偶计数器),
    # 所以要等的是一个确定信号而不是一段时长:迟到的响应回来时挂起表里已经
    # 没人认领了,dropped_frames 自增 —— 那就是"这一轮确实以 NavTimeoutError
    # 收场"的铁证。
    b = VendorNavBackend(NavConfig(url=sim.url, **dict(FAST, request_timeout_s=0.3)))
    await b.connect()
    try:
        sim.faults.response_delay_s = 1.0
        await _wait_until(lambda: b.dropped_frames > 0, timeout_s=15.0)
        sim.faults.response_delay_s = 0.0
        q = b.subscribe()
        await b.start_mapping()
        # 轮询器还活着 —— 状态重新流动起来
        await _drain_until(q, lambda e: isinstance(e, MappingStatusEvent), timeout_s=10.0)
    finally:
        sim.faults.response_delay_s = 0.0
        await b.close()


async def test_并发connect只建一条链路(sim):
    """M2: `connect()` 里的 `_connect_lock`(Task 12 评审遗留修复)的守卫。

    去掉那把锁,并发 5 次 `connect()` 会双双越过 `if self._ws is not None`
    守卫,漏掉几个 socket 加几个读循环任务(评审实测:服务端在线 3 个,
    `close()` 之后仍是 3 个,全泄漏)。
    """
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST))
    try:
        await asyncio.gather(*[b.connect() for _ in range(5)])
        await _wait_until(lambda: len(sim._clients) >= 1)
        await asyncio.sleep(0.2)      # 负向断言的观察窗口:再等等看会不会冒出第二条
        assert len(sim._clients) == 1
    finally:
        await b.close()
        await _wait_until(lambda: len(sim._clients) == 0)


async def test_建链途中关闭不留下链路(sim):
    """I3: `close()` 必须与在飞的 `connect()` 互斥。

    不互斥的话,`close()` 在 `await` 上让出的空档里,一个正卡在握手上的
    `connect()` 会在 `close()` 返回**之后**才把 `_ws` 与读循环装回去 —— 建出
    一条 `close()` 再也不追踪的链路(评审 closerace.py 实测:close() 之后
    `connected=True`、读循环任务还活着、服务端在线客户端数 = 1,全泄漏)。
    """
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST))
    t = asyncio.create_task(b.connect())
    await asyncio.sleep(0)        # 让 connect() 跑到握手的 await 上
    await b.close()               # 用户在建链途中关闭
    await t
    assert b.connected is False
    assert b._reader is None, "读循环任务泄漏了:close() 之后没人再取消它"
    assert b._poller is None
    await _wait_until(lambda: len(sim._clients) == 0)


class _只握手不应答的桩服务端:
    """第一条连接收到请求就关掉,之后的连接照单全收但一个字节都不回。

    用来让重连探针以 **NavTimeoutError** 失败(仿真器的静默窗口只会让它以
    NavConnectionError 失败)。`_reconnect_loop` 的宽捕获 `except
    NavBackendError` 正是为这种失败准备的:收窄成 `except NavConnectionError`
    的话,重连循环会带着异常静默死掉,从此再也不重连。
    """

    def __init__(self) -> None:
        self._server = None
        self.port = 0
        self.connections = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = next(iter(self._server.sockets)).getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws) -> None:
        self.connections += 1
        with contextlib.suppress(Exception):
            if self.connections == 1:
                await ws.recv()       # 等客户端发第一条请求,再制造一次断链
                await ws.close(code=1012, reason="制造一次断链")
                return
            async for _ in ws:        # 之后:收下请求,永不应答
                pass


@pytest.fixture
async def 哑服务端():
    server = _只握手不应答的桩服务端()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_探针超时不会杀死重连循环(哑服务端):
    """M1: `_reconnect_loop` 的宽捕获。"""
    b = VendorNavBackend(NavConfig(
        url=哑服务端.url, request_timeout_s=0.3, connect_timeout_s=2.0,
        status_poll_interval_s=_POLLER_OFF, reconnect_min_s=0.05, reconnect_max_s=0.1))
    await b.connect()
    try:
        # 这条请求会被桩服务端用"收下就关连接"回应,于是登记一次断链、起重连循环
        with pytest.raises(NavBackendError):
            await b.nav_status()
        # 之后每一次重连: 握手成功 → 探针发出去 → 对端永不应答 → NavTimeoutError。
        # 宽捕获在,重连循环就该一次次接着试。
        await _wait_until(lambda: b.reconnect_attempts >= 3, timeout_s=10.0)
        assert b._reconnector is not None      # 重连循环没有被异常掀掉
    finally:
        await b.close()


class _发送必失败的链路:
    """替身链路:send 一定失败,close 什么也不做。

    用来单独构造"`ws.send()` 失败"这条路径 —— 真实断链会同时惊动读循环,
    分不清 `_connected_event` 是被谁清掉的。
    """

    def __init__(self) -> None:
        self.closed = False

    async def send(self, _text: str) -> None:
        raise ConnectionResetError("替身链路:发送必失败")

    async def close(self) -> None:
        self.closed = True


async def test_发送失败会被登记为断链(sim):
    """裁定 6: send 失败本身就是链路故障,必须走 `_on_link_lost` 这条统一入口。

    否则 `_connected_event` 不会被清:调用者刚拿到 `NavConnectionError`,回头
    调 `wait_connected()` 却立刻拿到一个假的"已连接"(评审 C1 的 EVIDENCE-1)。
    """
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=1.0, connect_timeout_s=2.0,
                  status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False)
    await b.connect()
    真链路 = b._ws
    try:
        await b.wait_connected(timeout_s=1.0)      # 前提:此刻确实是"已连接"
        b._ws = _发送必失败的链路()
        with pytest.raises(NavConnectionError):
            await b.nav_status()
        assert b.connected is False
        with pytest.raises(NavTimeoutError):
            await b.wait_connected(timeout_s=0.2)
    finally:
        await b.close()
        await 真链路.close()


class _已废弃的旧链路:
    """替身:代表一条早已被换掉的连接。只需要能被 close()。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_陈旧链路的断链报告不拆当前链路(sim, backend):
    """重连风暴里,上一次尝试留下的读循环会慢半拍才退出。它那句"断链了"报的是
    一条早被换掉的连接,绝不能让它把当前这条刚探针通过的新链路当场打死 ——
    否则会倒着发一条 BackendDisconnected,还得再重连一轮。
    """
    当前链路 = backend._ws
    q = backend.subscribe()
    旧链路 = _已废弃的旧链路()

    backend._on_link_lost("上一次重连尝试的读循环慢半拍才退出", ws=旧链路)

    assert backend._ws is 当前链路, "陈旧报告把当前链路拆掉了"
    assert backend.connected is True
    await asyncio.sleep(0.05)          # 观察窗口:确认没有倒发断开事件
    事件 = [q.get_nowait() for _ in range(q.qsize())]
    assert not [e for e in 事件 if isinstance(e, BackendDisconnected)]
    await _wait_until(lambda: 旧链路.closed)   # 那条旧连接仍然要被关干净
    assert await backend.nav_status() is not None      # 链路确实还能用


async def test_重连循环不被意外异常掀掉(sim, backend):
    """`_link_down` 置位之后,所有 `_on_link_lost` 一律早退 —— 重连循环是唯一
    还能把链路救回来的人。它若带着一个非 `NavBackendError` 的异常死掉,后端就
    永久停在"已断开且无人重连"(实测过的现场:读循环的 AssertionError 顺着
    `_teardown_link()` 里的 `await reader` 逃出来,40 轮断链注入卡死 3 轮)。
    """
    真身 = backend._reconnect_once
    炸过 = []

    async def 前两次先炸个非导航异常():
        if len(炸过) < 2:
            炸过.append(1)
            raise RuntimeError("重连路径上的意外异常")
        await 真身()

    backend._reconnect_once = 前两次先炸个非导航异常
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    await _drain_until(q, lambda e: isinstance(e, BackendReconnected), timeout_s=15.0)
    assert len(炸过) == 2, "两次意外异常没有真的发生,这条测试是空过的"
    assert backend.connected is True


async def _进入半开链路(sim) -> None:
    """把仿真器切成半开链路(TCP 还在、设备什么都不再读、不回关闭帧)。

    等的是服务端自己的 `_half_open` 标志(tick 循环施加注入时置上),不是挂钟。
    """
    sim.faults.half_open = True
    await _wait_until(lambda: sim._half_open)


async def _退出半开链路(sim) -> None:
    """恢复读取,让服务端察觉到对端已经走了,好让 sim.stop() 干净收尾。"""
    sim.faults.half_open = False
    await _wait_until(lambda: not sim._half_open)


async def test_半开链路下关闭不泄漏socket(sim):
    """F1: 对端不回关闭帧时,`close()` 必须**真的**把 TCP 拆掉。

    `ws.close()` 自带 close_timeout 兜底,到点会 `transport.abort()` ——
    那是这种链路下唯一真正拆掉 TCP 的动作。从外面套 `asyncio.wait_for` 把
    close() 取消,等于赶在 abort() 之前把它打断:连接永久泄漏。所以超时旋钮
    拧在建链处的 `close_timeout=`,`_safe_close()` 裸调 close()。

    IMP-1(终审): 这条测试原来写的是 `connect_timeout_s=1.0` + `用时 < 5.0`。
    那样只断言得出"没卡死",断不出"abort() 到底跑没跑到" —— 一个重新套上
    `wait_for(ws.close(), 外层超时)` 的改动,只要外层超时比 close_timeout 短
    一点点,连接就永久泄漏,而 `用时 < 5.0` 照样成立;唯一能拦住它的
    `transport.is_closing()` 断言,靠的是两个截止时刻谁先谁后这种毫秒级巧合
    (实测余量 7ms)。

    所以这里**把两个数拆开**: `close_timeout`(= connect_timeout_s)单独设一个
    值,再断言 close() 的耗时必须落在 [0.8×它, 1.5×它] 里。
    (派单举的例子是 3.0;这里取 2.0 —— 性质完全一样"任何短于 close_timeout
    的外层 wait_for 都会违反下界",只是可捕获区间从 <2.4s 收窄到 <1.6s,
    换来全量耗时少 1 秒。全量预算 90s 已经很紧,见修复报告的"顾虑"一节。)
    * 下界挡住"提前被打断"——任何短于 close_timeout 的外层 wait_for 都会让
      close() 早退,立刻违反下界,不再依赖巧合;
    * 上界挡住"根本没有超时兜底"(裸 close() 会等 websockets 的默认 10s)。
    """
    关闭超时 = 2.0
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=1.0,
                  connect_timeout_s=关闭超时,
                  status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False)
    await b.connect()
    真链路 = b._ws
    await _wait_until(lambda: len(sim._clients) == 1)
    await _进入半开链路(sim)

    循环 = asyncio.get_running_loop()
    起 = 循环.time()
    await b.close()
    用时 = 循环.time() - 起

    assert 真链路.transport.is_closing(), "对端不回关闭帧时,这条 TCP 根本没被拆掉"
    assert 用时 >= 0.8 * 关闭超时, (
        f"close() 只花了 {用时:.2f}s,短于 close_timeout({关闭超时}s)—— "
        f"关闭握手是被外面打断的,websockets 内部那句 transport.abort() "
        f"没有机会跑,这条 TCP 会永久泄漏")
    assert 用时 <= 1.5 * 关闭超时, (
        f"close() 花了 {用时:.2f}s,远超 close_timeout({关闭超时}s)—— "
        f"关闭超时根本没有按 close_timeout 生效")
    # 服务端一恢复读取就会读到 EOF —— 这是"socket 真的没了"的对端确认。
    await _退出半开链路(sim)
    await _wait_until(lambda: len(sim._clients) == 0, timeout_s=15.0)


async def test_拆链路摘走的连接一定有人负责关掉(sim):
    """F2: `_teardown_link()` 摘下 `_ws` 之后若自己被取消,那条 socket 仍须有人关。

    老写法是先摘、再 `await self._safe_close(ws)`;取消落在后半段时
    `suppress(Exception)` 不捕 CancelledError,取消直接穿透,而 `close()` 那边
    取到的 `_ws` 已经是 None —— 这条 socket 谁都不关(再评审 q4b_isolated.py:
    race → LEAK conn#2,control → clean)。半开链路把那个窗口拉到肉眼可见。
    """
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=1.0, connect_timeout_s=1.0,
                  status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False)
    await b.connect()
    真链路, 读循环 = b._ws, b._reader
    await _wait_until(lambda: len(sim._clients) == 1)
    await _进入半开链路(sim)

    拆解 = asyncio.create_task(b._teardown_link())
    # 等到"已摘走、读循环也收完了"—— 老写法这时正卡在 _safe_close 的关闭握手上,
    # 那一段有 close_timeout(1.0s)那么宽,取消必定落在里面。
    await _wait_until(lambda: b._ws is None and 读循环.done())
    拆解.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await 拆解

    await b.close()          # close() 取到的 _ws 已经是 None,指望不上它
    assert 真链路.transport.is_closing(), "被 _teardown_link 摘走的连接没有任何人关"
    await _退出半开链路(sim)
    await _wait_until(lambda: len(sim._clients) == 0, timeout_s=15.0)


# --------------------------------------------------------------------------
# BLOCK-1: close() 不许因为轮询器而白等一个 request_timeout_s
# --------------------------------------------------------------------------


async def _等到轮询器挂在请求上(backend, req_func: str, timeout_s: float = 10.0):
    """等到轮询器把 `req_func` 发出去、**并且已经在 wait_for 里挂起**。

    两个条件缺一不可:

    * `_pending` 里出现了这条请求 —— 说明 `ws.send()` 已经发出去了;
    * 这条 future 上已经挂了 done 回调 —— `asyncio.wait_for` 在
      `await waiter` 之前紧挨着一句 `fut.add_done_callback(cb)`,两者之间
      没有任何 await。所以从别的任务观察到"回调非空",就等价于
      "轮询器此刻停在 `wait_for` 里那个 `await waiter` 上"。

    第二个条件是承重的,不是保险丝: 轮询器若还卡在 `ws.send()` 里,
    `cancel()` 会正常穿透、`close()` 本来就是快的 —— 构造落不到要测的那个
    窗口上,这条测试就退化成一条永远绿的空测试。
    """
    命中: list = []

    def 挂住了() -> bool:
        for entry in list(backend._pending.values()):
            # `future._callbacks` 是 CPython 的内部字段,没有对应的公开 API。
            # 用它是有意的:这条测试要断言的正是 asyncio 内部的一个状态。
            if entry.req_func == req_func and entry.future._callbacks:
                命中.append(entry)
                return True
        return False

    await _wait_until(挂住了, timeout_s=timeout_s)
    return 命中[0]


async def test_关闭时轮询器手上有已结清的在飞请求也必须立刻返回(sim):
    """BLOCK-1: `close()` 必须立刻返回,不许白等一个 `request_timeout_s`。

    守的是 `request()` 顶部那道 `_closing` 闸门。删掉那两行,这条测试就会
    量到约 3.00s(= `request_timeout_s`)。完整因果链写在 `close()` 上方,
    根因是 CPython 3.10 的 `asyncio.wait_for` 在 `fut.done()` 时把
    `CancelledError` 整个丢掉(`return fut.result()`)。

    构造分两步,两步都是确定性的,不靠抢时序:

    1. 让仿真器**吞掉** `get_nav_status` 的回复。轮询器的这条请求于是稳定
       挂起一整个 `request_timeout_s`,给了一个三秒宽的构造窗口 ——
       不需要去抢"读循环刚结清"那一两个事件循环回合。
    2. 由本测试**亲手** `set_result()` 结清这条 future。这与读循环
       `_settle()` 做的是同一个动作;区别只在于这里能保证"结清"与
       "`close()` 里那句 `poller.cancel()`"之间一个 await 都没有,
       也就精确地落在缺陷要求的那个窗口里(future 已 done、轮询器尚未唤醒
       → `waiter.cancel()` 返回 True → `_must_cancel` 保持 False → 取消蒸发)。
    """
    真回复 = sim._reply

    async def 吞掉状态查询的回复(ws, req_func, frame_count, ok, msg, data):
        if req_func == "get_nav_status":
            return
        await 真回复(ws, req_func, frame_count, ok, msg, data)

    sim._reply = 吞掉状态查询的回复

    b = VendorNavBackend(NavConfig(url=sim.url, **FAST), auto_reconnect=False)
    await b.connect()
    条目 = await _等到轮询器挂在请求上(b, "get_nav_status")

    条目.future.set_result(Response(
        frame_count=None,
        req_func=R.get_nav_status().response_func,
        ok=True,
        msg=None,
        data=NavStatus.STANDBY.value,
        raw={},
    ))

    循环 = asyncio.get_running_loop()
    起 = 循环.time()
    await b.close()          # ← 与上面那句 set_result 之间不许插入任何 await
    用时 = 循环.time() - 起

    assert 用时 < 0.5, (
        f"close() 花了 {用时:.2f}s。轮询器手上那条在飞请求的取消被 "
        f"asyncio.wait_for 吞掉了,它接着在还没关掉的 socket 上又发出一条状态"
        f"查询,而读循环早已收摊 —— close() 于是白等一个 request_timeout_s"
        f"({b.config.request_timeout_s}s)"
    )
