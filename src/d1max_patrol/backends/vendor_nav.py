"""智元 D1 Max 自主导航 WebSocket 后端。

厂商协议的全部怪癖都关在这个文件里。上层看到的只有 base.NavBackend。

已知怪癖(逐条对应实现里的注释):
  1. 速度相关响应多包一层 AppReponseObjectData(厂商把 Response 拼错了)
  2. loc_load_map 的响应函数名是 load_localization_map
  3. notify_stop_mapping_status 是推送,却顶着 app_resp 和会撞车的 frame_count
  4. 导航/定位/建图状态没有推送通道,只能轮询(见 Task 14)
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import (
    AlgErrorNotify,
    ProtocolError,
    Response,
    encode_request,
    parse_message,
)
from d1max_patrol.protocol.nav_requests import PUSH_ONLY_FUNCS, NavRequest
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
    parse_enum,
)

from .base import (
    AlgErrorEvent,
    BackendDisconnected,
    BackendReconnected,
    LocStatusEvent,
    MappingStatusEvent,
    NavBackend,
    NavBackendError,
    NavConnectionError,
    NavRequestError,
    NavStatusEvent,
    NavTimeoutError,
)

log = logging.getLogger(__name__)

#: 建图保存完成的推送函数名
NOTIFY_MAPPING_SAVED = "notify_stop_mapping_status"


@dataclass
class _Pending:
    """一条挂起的请求。"""

    req_func: str
    response_func: str
    future: asyncio.Future = field(repr=False)


class VendorNavBackend(NavBackend):
    def __init__(self, config: NavConfig, auto_reconnect: bool = True) -> None:
        super().__init__()
        self.config = config
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._frame_counter = itertools.count(1)
        #: 已经发出过的最大帧号。C1: 只有落在 (0, 这个值] 里、却又不在挂起表
        #: 中的响应,才是"我们自己已结算/已超时的旧帧",必须走丢弃而不是降级。
        self._last_issued = 0
        #: frame_count -> 挂起请求。dict 保序,降级匹配靠它取最早一条。
        self._pending: dict[int, _Pending] = {}
        self._closing = False
        #: I2: `_on_link_lost` 派生的后台关闭任务在这里存一份强引用,
        #: 防止任务被 GC 提前回收(以及 close() 时能等它们收尾,不留
        #: "Task was destroyed but it is pending" 这类杂散告警)。
        self._background_tasks: set[asyncio.Task[None]] = set()

        #: 观测指标 —— 契约测试与真机对拍都靠它们判断链路健康
        self.fallback_matches = 0
        self.dropped_frames = 0

        self.auto_reconnect = auto_reconnect
        #: I2: "这条链路已经报过断了"的显式闸门。一次链路丢失只发一条
        #: BackendDisconnected —— 重连尝试期间读循环会反复走进 _on_link_lost,
        #: 靠 `_ws` / `_connected_event` 推断"是不是同一次断链"是推不准的。
        #: 置位: _on_link_lost 首次进入。清位: connect() 建链成功、或
        #: _reconnect_once() 的探针过了。
        self._link_down = False
        self._poller: asyncio.Task[None] | None = None
        self._reconnector: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self.reconnect_attempts = 0

        self.last_nav_status: NavStatus | None = None
        self.last_loc_status: LocStatus | None = None
        self.last_mapping_status: MappingStatus | None = None

    # ------------------------------------------------------------ 生命周期

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def connect(self) -> None:
        # M2(Task 12 评审遗留,到这里才真正兑现): connect() 与 _reconnect_once()
        # 共用同一把 `_connect_lock` 围 `_open_link()`。原来的
        # `if self._ws is not None` 守卫在 await 之前判断,两个并发调用会
        # 双双通过,漏一个 socket 加一个读循环任务。整段建链必须串行。
        async with self._connect_lock:
            if self._ws is not None:
                return
            # I3: 清 `_closing` 的责任在 connect(),置位的责任在 close(),两者
            # 都在 `_connect_lock` 之内。这行原来在 _open_link() 里 —— 一个
            # 建链的低层辅助函数没有资格改生命周期标志: close() 在 await 上
            # 让出 CPU 的空档里,重连路径的 _open_link() 会把 _closing 悄悄
            # 复位成 False,于是 close() 之后重连循环照跑不误
            # (现场: `assert 13 == 2`,见 flake-evidence.txt)。
            self._closing = False
            await self._open_link()
            self._link_down = False
            self._connected_event.set()

    async def _open_link(self) -> None:
        """建链 + 起读循环 + 起轮询器。不发任何请求,不碰任何生命周期标志。

        初次连接刻意不做探针: 探针会多消耗一个 frame_count、并拨动仿真器
        那个全局响应奇偶计数器,而匹配测试正靠奇偶性把延迟注入打在特定
        请求上(见 test_vendor_matching.py 的 test_C1 docstring)。缺陷 19
        真正要防的是"重连把半开链路当成功",初次连接不涉及这个问题。

        `_closing` / `_link_down` / `_connected_event` 一律由调用方
        (connect() 与 _reconnect_once())负责,见 I3。
        """
        try:
            self._ws = await asyncio.wait_for(
                # F1: 关闭握手的超时**必须**在这里交代给 websockets 自己,
                # 不能到 _safe_close() 里用 wait_for 从外面掐。原因见
                # _safe_close() 的 docstring。
                connect(self.config.url, open_timeout=None,
                        close_timeout=self.config.connect_timeout_s),
                timeout=self.config.connect_timeout_s,
            )
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            raise NavConnectionError(f"连接 {self.config.url} 失败: {exc}") from exc
        # 把这条连接**显式**交给读循环。原来读循环不收参数,自己回头取
        # self._ws —— 而 create_task() 只是排期、并不立刻运行:重连风暴里
        # (轮询器 0.05s 一轮、退避 0.05~0.2s)常有另一条协程赶在读循环真正
        # 跑起来之前就把 self._ws 摘成 None,读循环开头那句断言于是炸出
        # AssertionError。后果见 _reconnect_loop 里的说明(会把重连循环整个
        # 掀掉,后端永久停在"已断开且无人重连")。这里到赋值之间没有 await,
        # 所以这条断言现在是真的不可能失败。
        ws = self._ws
        assert ws is not None
        self._reader = asyncio.create_task(self._read_loop(ws))
        if self._poller is None:
            self._poller = asyncio.create_task(self._poll_loop())
        log.info("已连接导航设备 %s", self.config.url)

    async def _reconnect_once(self) -> None:
        """重连一次。与 connect() 的区别: 建链之后要用一条真实请求确认对端
        真的在应答,确认过了才置就绪标志。

        握手成功 ≠ 链路可用: 仿真器的 disconnect 注入是"静默窗口"语义 ——
        窗口内握手会成功,服务端随后立刻以 1012 关闭(nav_server.py:166)。
        只看 connect() 有没有抛错的话,会误报 BackendReconnected、误置就绪
        标志,而且退避会因为重连循环反复重建而永远从头开始。
        """
        async with self._connect_lock:
            if self._ws is None:
                await self._open_link()
        # 假设(待真机验证): "设备对 get_nav_status 有应答" ⟺ "导航链路可用"。
        # 厂商文档没有给出链路健康判据,这条探针的判据是我们自己定的。真机要
        # 确认两件事: (1) 半开连接(TCP 还在、导航服务已死)下设备会不会照样
        # 应答 get_nav_status —— 若会,这条探针挡不住半开链路,得换成更强的
        # 判据(带状态一致性校验,或厂商提供的心跳/保活接口);(2) 设备是否
        # 允许在链路刚建立、其它初始化尚未完成时就收到 get_nav_status。
        try:
            await self.request(R.get_nav_status())
        except NavBackendError:
            await self._teardown_link()
            raise
        if self._ws is None:
            # 探针刚过链路就没了(读循环已在 _on_link_lost 里把 _ws 摘掉)。
            # 此时绝不能宣布重连成功: _link_down 一旦被清掉,而 _on_link_lost
            # 那次已经按"同一次断链"早退过,就再没有人触发下一轮重连了。
            raise NavConnectionError("探针通过后链路随即断开")
        self._link_down = False
        self._connected_event.set()

    async def _teardown_link(self) -> None:
        """探针失败后拆干净,好让下一次重连能真的重来。

        不收 `_poller` —— 它跨断链存活,只由 close() 收。
        """
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        if ws is not None:
            # F2 不变式: **一条被摘下的连接,无论摘它的协程随后是否被取消,
            # 都必须有人负责关掉它。** 原来是先摘、再 `await reader`、最后
            # `await self._safe_close(ws)` —— close() 发过来的取消若落在
            # `_safe_close` 上,`suppress(Exception)` 不捕 CancelledError
            # (它是 BaseException),取消直接穿透;而 close() 那边取到的
            # `self._ws` 早就是 None 了,这条 socket 于是**谁都不关**。
            # 对端不应答时这个窗口有 connect_timeout_s 那么宽
            # (再评审 q4b_isolated.py: race → LEAK,control → clean)。
            #
            # 改法照抄 _on_link_lost 的样板: 派一个后台任务去关。它独立于
            # 本协程存活,被 `_background_tasks` 追踪,由 close() 兜底 await。
            # 关键在于**摘下与派人之间一个 await 都没有** —— 交接是原子的。
            # 代价: 拆解从同步变成异步,下一次重连可能在旧 socket 关完之前
            # 就开始。这与 _on_link_lost 早就有的行为一致,仿真器按在线客户端
            # 计数的那几条测试都不受影响(已验)。
            self._spawn(self._safe_close(ws))
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._fail_pending("探针失败,链路未建立")

    async def close(self) -> None:
        # I3: 先立标志(在锁外),让在飞的重连循环/轮询器尽早看见"要关了";
        # 拆解本身必须与在飞的 connect() / _reconnect_once() 互斥 —— 否则一个
        # 正卡在握手 await 上的 connect() 会在 close() 返回之后才把 _ws / 读
        # 循环装回去,建出一条 close() 再也不追踪的链路(socket 与读循环任务
        # 双泄漏,评审 closerace.py)。
        #
        # **不自锁靠的是 `_closing` 不变式,别弄错承重墙。** 上一版这里写的两条
        # 理由再评审实测都不成立,已订正:
        #   ✗ "锁内唯一的 await 是握手,有 connect_timeout_s 上限" —— 不对。
        #     close() 自己在锁内 await 了三个任务、_safe_close(ws)、以及全部在飞
        #     的 _background_tasks;实测持锁 5.00s(connect_timeout_s 才 2.0)。
        #     持锁时长没有静态上限。
        #   ✗ "被 cancel 的任务卡在 Lock.acquire() 上会被 CancelledError 掀掉"
        #     —— 卡在 acquire() 上时确实如此,但那不是实际发生的事: 实测
        #     `reconnector.cancelled() == False`,close() 发出的取消被
        #     `_teardown_link()` 里的 `suppress(asyncio.CancelledError)` 吞掉了。
        #
        # 真正的保证: 锁内 await 的四类对象里,只有 `_reconnector` 会回头要
        # `_connect_lock`(_reconnect_once() 里那句 `async with`)。而 `_closing`
        # 只有三处赋值(__init__、connect()、close()),close() 在**锁外**先置
        # True,唯一能清掉它的 connect() 在**锁内** —— close() 持锁期间没有任何
        # 东西能把它翻回 False。于是 _reconnect_loop 的 `while not self._closing`
        # 必定为假,重连循环退出,不会再来要锁。(再评审 8 条交叉 + 40 轮随机
        # 压测造不出死锁,最慢 close() 5.00s。)
        #
        # 推论,给后来人: 谁要是在 _reconnect_loop 里放宽/挪走
        # `while not self._closing`,或者在锁内清 `_closing`,就会得到一个真死锁。
        self._closing = True
        async with self._connect_lock:
            self._connected_event.clear()
            # I3: 必须先 cancel + await,再断引用。`_on_link_lost` 的
            # `self._reconnector is None` 去重守卫是承重的(评审 MUT8):提前把
            # 它置成 None,读循环收尾时就看不见在飞的重连循环,于是又起一个新
            # 的 —— 那正是"close() 之后 reconnect_attempts 还在涨"。
            tasks = [self._reader, self._poller, self._reconnector]
            for task in tasks:
                if task is not None:
                    task.cancel()
            for task in tasks:
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            self._reader = self._poller = self._reconnector = None
            ws, self._ws = self._ws, None
            if ws is not None:
                await self._safe_close(ws)
            # I2: 等在飞的后台关闭任务(比如 _on_link_lost 派生的)收尾,
            # 免得进程退出时留下 "Task was destroyed but it is pending"。
            bg, self._background_tasks = list(self._background_tasks), set()
            for task in bg:
                with contextlib.suppress(Exception):
                    await task
            self._fail_pending("连接已关闭")

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for entry in pending.values():
            if not entry.future.done():
                entry.future.set_exception(NavConnectionError(reason))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _safe_close(self, ws: ClientConnection) -> None:
        """关掉一条连接,吞掉关闭过程中的一切异常。链路拆解一律走这里。

        **这里绝对不能再套一层 `asyncio.wait_for`。** 这是踩过的坑,原样记下:

        M4 一开始写成 `asyncio.wait_for(ws.close(), connect_timeout_s)`,想的是
        "对端不回关闭帧时别让 close() 阻塞十秒"。方向对,机制错 ——
        `ws.close()` **自带**超时兜底: websockets 在等关闭握手时挂着一个
        `close_deadline = now + close_timeout`,**到点就 `transport.abort()`**。
        对端不回关闭帧时,那句 `abort()` 是唯一真正拆掉 TCP 的动作。
        从外面用 wait_for 取消 close(),等于赶在它自己的 abort() 之前把它打断:
        阻塞是没有了,连接却**永久泄漏** —— 比不加超时更坏。

        再评审实测的剂量-反应(对端是不回关闭帧的裸 TCP 服务端):

            裸 close()                  10.02s  clean   ← close_timeout 到点 abort
            wait_for(close(), 2.0)       2.00s  LEAK
            wait_for(close(), 5.0)       5.00s  LEAK
            wait_for(close(), 30.0)     10.00s  clean   ← 超时长于 close_timeout,
                                                          abort() 跑到了

        所以超时旋钮要拧在 websockets 内部: 建链时传 `close_timeout=`
        (见 `_open_link()`),这边裸调 `close()`。等待时长一样受控,而 socket
        真的会被拆掉。回归测试: `test_半开链路下关闭不泄漏socket`。
        """
        with contextlib.suppress(Exception):
            await ws.close()

    # ------------------------------------------------------------ 读循环

    async def _read_loop(self, ws: ClientConnection) -> None:
        """读一条**指定**连接直到它结束。断链一律带着这条连接上报,好让
        `_on_link_lost()` 分辨得出报的是不是当前那条链路。
        """
        try:
            async for raw in ws:
                try:
                    message = parse_message(raw)
                except ProtocolError as exc:
                    log.warning("忽略无法解析的报文: %s", exc)
                    continue
                try:
                    self._route(message)
                except Exception:  # noqa: BLE001
                    # I1: 单帧路由失败绝不能判定链路死亡 —— 否则一条畸形
                    # 推送就能把所有挂起请求连坐失败。异常留在日志里
                    # (exc_info=True),不悄悄吞掉真实 bug。
                    log.warning("路由报文时出错,已忽略该帧", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._closing:
                self._on_link_lost(f"读循环异常: {exc}", ws=ws)
            return
        if not self._closing:
            self._on_link_lost("设备关闭了连接", ws=ws)

    def _on_link_lost(self, reason: str, ws: ClientConnection | None = None) -> None:
        """链路断开的统一入口: 拆掉这条链路,发一次断开事件,接着重连。

        发现断链的渠道不止读循环一条 —— `request()` 里 `ws.send()` 失败同样
        是链路故障,也走这里(裁定 6)。统一入口的意义就在于: 无论从哪条渠道
        察觉,`_connected_event` 都被清掉、重连都被触发,`wait_connected()`
        便不会回一个假的"已连接"。
        """
        # 报的是哪条链路: 调用方给了就按它算,没给(旧调用点)按当前链路算。
        current = self._ws
        if ws is not None and current is not None and ws is not current:
            # 陈旧报告: 这条连接早就被换掉了(上一次重连尝试留下的读循环慢半拍
            # 才退出),当前链路是好的,绝不能把它拆掉 —— 那会把一条刚探针通过
            # 的新链路当场打死,还倒着发一条 BackendDisconnected。
            self._spawn(self._safe_close(ws))
            return
        ws, self._ws = current, None
        self._connected_event.clear()
        # I2(Task 12): 光丢引用不够 —— 旧连接的 socket 还开着,服务端会一直把
        # 它算作在线客户端。真正关掉它,丢给后台任务做,免得阻塞读循环的收尾。
        if ws is not None:
            self._spawn(self._safe_close(ws))
        self._fail_pending(reason)
        # I2 + M3: 一次链路丢失只广播一条 BackendDisconnected。老守卫
        # (`_ws is None and not _connected_event.is_set()`)在重连尝试期间形同
        # 虚设: _open_link() 刚把 _ws 设上,仿真器随即以 1012 关闭,读循环退出
        # 再次进来时 _ws 非 None,守卫放行 —— 每失败一次重连就多发一条
        # (实测 2.0s 窗口 12 条)。事件契约是"每次链路丢失一条",上层按事件
        # 计数做状态机的话,一次网络抖动会被记成十几次断链。
        if self._link_down:
            return
        self._link_down = True
        log.warning("导航链路断开: %s", reason)
        self.emit(BackendDisconnected(reason))
        if self.auto_reconnect and not self._closing and self._reconnector is None:
            self._reconnector = asyncio.create_task(self._reconnect_loop())

    # ------------------------------------------------------------ 重连

    async def wait_connected(self, timeout_s: float) -> None:
        """等到链路就绪(初次连接完成,或一次重连的探针过了)。

        契约: 就绪标志表达的是"客户端尚未观察到断链"。所有察觉断链的渠道都
        汇到 `_on_link_lost()`(读循环退出、`ws.send()` 失败),那里 `clear()`
        排在 `_fail_pending()` 之前 —— 所以被在飞请求的异常唤醒的调用者,拿到
        异常时标志已经清了,不会立刻拿到一个假的"已连接"。**这两步的先后不能
        调换。** 若调用者是从后端之外的渠道(上层业务超时、外部心跳)得知链路
        有问题的,本方法仍可能立刻返回;那种场合请先自己收到一条
        `BackendDisconnected` 再等。
        """
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout_s)
        except asyncio.TimeoutError as exc:
            raise NavTimeoutError(f"等待重连超过 {timeout_s}s") from exc

    async def _reconnect_loop(self) -> None:
        delay = self.config.reconnect_min_s
        try:
            while not self._closing:
                await asyncio.sleep(delay)
                self.reconnect_attempts += 1
                try:
                    await self._reconnect_once()
                except Exception as exc:  # noqa: BLE001
                    # 控制者订正: 原文只捕 NavConnectionError。_reconnect_once()
                    # 带探针,探针可能抛 NavTimeoutError / NavRequestError ——
                    # 窄捕获会让重连循环带着异常静默死掉,从此再也不重连。
                    # 重连循环里没有任何错误值得杀死重连,所以这里兜到
                    # Exception 为止(CancelledError 是 BaseException,照旧穿透,
                    # close() 才收得掉这个任务)。
                    # 这道兜底是承重的: _link_down 一旦置位,后续 _on_link_lost
                    # 全部早退,重连循环是唯一还能把链路救回来的人。它带异常死掉
                    # 的话,后端就永久停在"已断开且无人重连"上(实测: 读循环的
                    # AssertionError 顺着 _teardown_link 的 `await reader` 逃出来,
                    # 40 轮断链注入里卡死 3 轮)。
                    if isinstance(exc, NavBackendError):
                        log.info("第 %d 次重连失败: %s", self.reconnect_attempts, exc)
                    else:
                        # 非导航异常落到这里就是真 bug(上一轮那个 AssertionError
                        # 就是这么被 log.info 一行盖掉、害得全量跑三次才暴露的),
                        # 必须带 traceback 喊出来。
                        log.warning("第 %d 次重连遇到意外异常",
                                    self.reconnect_attempts, exc_info=exc)
                    delay = min(delay * 2, self.config.reconnect_max_s)
                    continue
                # 断链期间机器人可能已经走完或已经失败,缓存一律作废,
                # 下一轮轮询会以 previous=None 重新广播真实状态。
                self.last_nav_status = None
                self.last_loc_status = None
                self.last_mapping_status = None
                self.emit(BackendReconnected())
                log.info("重连成功(第 %d 次尝试)", self.reconnect_attempts)
                return
        finally:
            self._reconnector = None

    # ------------------------------------------------------------ 状态轮询

    async def _poll_loop(self) -> None:
        """设备没有状态推送通道,只能自己轮询出"变化"这件事。"""
        while not self._closing:
            await asyncio.sleep(self.config.status_poll_interval_s)
            if self._ws is None:
                continue
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except NavBackendError as exc:
                # 单轮失败不该杀死轮询器 —— 断链有 _on_link_lost 兜底
                log.debug("状态轮询这一轮失败: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("状态轮询意外异常")

    async def _poll_once(self) -> None:
        nav = await self.nav_status()
        if nav is not None and nav is not self.last_nav_status:
            self.emit(NavStatusEvent(nav, self.last_nav_status))
            self.last_nav_status = nav

        loc = await self.loc_status()
        if loc is not None and loc is not self.last_loc_status:
            self.emit(LocStatusEvent(loc, self.last_loc_status))
            self.last_loc_status = loc

        mapping = await self.mapping_status()
        if mapping is not None and mapping is not self.last_mapping_status:
            self.emit(MappingStatusEvent(mapping, self.last_mapping_status))
            self.last_mapping_status = mapping

    # ------------------------------------------------------------ 匹配

    def _route(self, message: Any) -> None:
        if isinstance(message, AlgErrorNotify):
            self.emit(AlgErrorEvent(tuple(message.items),
                                    message.time_stamp_ms or 0))
            return
        assert isinstance(message, Response)

        # 1. 主匹配: 帧号与函数名都对上才认领。缺一不可 —— 推送会撞帧号。
        entry = self._pending.get(message.frame_count)
        if entry is not None and entry.response_func == message.req_func:
            del self._pending[message.frame_count]
            self._settle(entry, message)
            return

        # 2. 推送识别: 顶着 app_resp 进来的设备主动上报。
        #    必须先于降级匹配,否则它会冒领一条同名挂起请求。
        if message.req_func in PUSH_ONLY_FUNCS:
            self._on_push(message)
            return

        # 3. 降级匹配: 只在帧号不可信时才允许降级。
        # 假设(待真机验证): 帧号由我们从 1 单调递增发出,设备原样回显。
        # 于是"落在 [1, 已发出的最大帧号] 里、却不在挂起表中"⟺ 这是我们自己
        # 某条已结算/已超时的旧帧(比如客户端已等超时、之后设备的迟到响应
        # 才姗姗来迟)—— 拿它去冒领另一条同名的挂起请求,就是把 A 的响应
        # 交给了 B,而且没有任何异常,只会在压力下悄悄错发数据。这种帧必须
        # 走步骤 4 丢弃,不能降级。真机要确认: 设备是否真的原样回显
        # frame_count、以及超时后的迟到帧是否真会出现。
        fc = message.frame_count
        if fc is None or not (1 <= fc <= self._last_issued):
            for frame_count, entry in self._pending.items():
                if entry.response_func == message.req_func:
                    del self._pending[frame_count]
                    self.fallback_matches += 1
                    log.warning("帧号 %s 未命中,按函数名 %s 降级匹配(累计 %d 次)",
                                message.frame_count, message.req_func,
                                self.fallback_matches)
                    self._settle(entry, message)
                    return

        # 4. 无人认领
        self.dropped_frames += 1
        log.warning("丢弃无人认领的响应: frame_count=%s req_func=%s",
                    message.frame_count, message.req_func)

    def _settle(self, entry: _Pending, message: Response) -> None:
        if not entry.future.done():
            entry.future.set_result(message)

    def _on_push(self, message: Response) -> None:
        if message.req_func == NOTIFY_MAPPING_SAVED:
            log.info("设备通知: 建图已保存")
            # 状态事件由 Task 14 的轮询器统一发出,这里只记日志,
            # 免得同一次建图完成被广播两遍。
            return
        log.info("收到未处理的设备推送: %s", message.req_func)

    # ------------------------------------------------------------ 请求

    async def request(self, req: NavRequest) -> Any:
        ws = self._ws
        if ws is None:
            raise NavConnectionError("尚未连接导航设备")

        frame_count = next(self._frame_counter)
        self._last_issued = frame_count
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = _Pending(req.req_func, req.response_func, future)
        self._pending[frame_count] = entry
        try:
            await ws.send(encode_request(req.req_func, req.args, frame_count))
        except Exception as exc:  # noqa: BLE001
            reason = f"发送 {req.req_func} 失败: {exc}"
            # I4: 断链与 send 失败几乎同时发生时,`_fail_pending()` 会**先**给
            # 这条 future 设上异常,send 才抛错走到这里。只从表里摘掉的话,那个
            # 已经带着异常的 future 再也没人 await,asyncio 回收它时刷一条
            # "Future exception was never retrieved" —— 一次长断网能刷几十上百
            # 条,把真问题淹掉。这里必须把异常消费掉。
            # 顺序: 先摘掉并结清自己这条,再走 _on_link_lost —— 那里的
            # _fail_pending() 就只会结算**其它**在飞请求,不会二次结算本条。
            # 注意必须用手上这个 `future` 局部变量来消费,不能回表里查 ——
            # `_fail_pending()` 是整张 `_pending` 换成新的空 dict,我们这条早就
            # 不在表里了,查回来只会是 None,异常照样没人消费(第一版就栽在这)。
            self._pending.pop(frame_count, None)
            if future.done() and not future.cancelled():
                future.exception()
            # 裁定 6: send 失败本身就是链路故障,走统一入口。这样
            # `_connected_event` 会被清掉、重连会被触发,此后 wait_connected()
            # 不会再回一个假的"已连接"(评审 C1 里 EVIDENCE-1 那个洞)。
            self._on_link_lost(reason, ws=ws)
            raise NavConnectionError(reason) from exc

        # C2: 无论正常拿到响应、超时、还是被外部取消(gather 连坐、上层
        # wait_for、Task 14 重连撤销在飞请求……),挂起表都必须清干净 ——
        # 否则一条"死"条目会一直排在最前面,被之后每一次降级匹配优先冒领。
        try:
            try:
                response = await asyncio.wait_for(future, self.config.request_timeout_s)
            except asyncio.TimeoutError as exc:
                raise NavTimeoutError(
                    f"{req.req_func} 超过 {self.config.request_timeout_s}s 未收到响应"
                ) from exc
        finally:
            self._pending.pop(frame_count, None)

        if not response.ok:
            raise NavRequestError(req.req_func, response.msg or "设备未给出原因")
        return response.data

    # ------------------------------------------------------------ 地图

    async def list_maps(self) -> list[str]:
        return R.parse_map_ids(await self.request(R.get_all_pgm_map()))

    async def remove_maps(self, map_ids: Sequence[str]) -> None:
        await self.request(R.remove_map_by_id(list(map_ids)))

    async def rename_map(self, old_id: str, new_id: str) -> None:
        await self.request(R.rename_map_name(old_id, new_id))

    async def get_map_grid(self, map_id: str) -> dict[str, Any]:
        return await self.request(R.get_pgm_map(map_id))

    # ------------------------------------------------------------ 建图

    async def start_mapping(self) -> None:
        await self.request(R.start_mapping())

    async def stop_mapping(self) -> None:
        await self.request(R.stop_mapping())

    async def mapping_status(self) -> MappingStatus | None:
        return self._as_enum(MappingStatus, await self.request(R.get_mapping_status()))

    # ------------------------------------------------------------ 路径

    async def list_paths(self, map_id: str) -> dict[str, list[Waypoint]]:
        return R.parse_paths_payload(await self.request(R.get_all_paths_by_mapid(map_id)))

    async def save_path(self, map_id: str, path_id: str,
                        waypoints: Sequence[Waypoint]) -> None:
        # 厂商把新增和修改分成两个接口,但语义都是"整条覆盖"。
        # 上层只需要一个 save,已存在就走 modify。
        existing = await self.list_paths(map_id)
        if path_id in existing:
            # 同名覆盖:老名字新名字传成同一个。
            req = R.modify_nav_path(map_id, path_id, path_id, list(waypoints))
        else:
            req = R.add_nav_path(map_id, path_id, list(waypoints))
        await self.request(req)

    async def remove_path(self, map_id: str, path_id: str) -> None:
        await self.request(R.remove_nav_path([(map_id, path_id)]))

    # ------------------------------------------------------------ 定位

    async def load_map(self, map_id: str) -> None:
        await self.request(R.loc_load_map(map_id))

    async def reset_localization(self) -> None:
        await self.request(R.reset_loc())

    async def loc_status(self) -> LocStatus | None:
        return self._as_enum(LocStatus, await self.request(R.get_loc_status()))

    # ------------------------------------------------------------ 导航

    async def goto(self, pose: Pose) -> None:
        await self.request(R.start_nav(pose))

    async def stop(self) -> None:
        await self.request(R.stop_nav())

    async def pause(self) -> None:
        await self.request(R.pause_nav())

    async def resume(self) -> None:
        await self.request(R.continue_nav())

    async def nav_status(self) -> NavStatus | None:
        return self._as_enum(NavStatus, await self.request(R.get_nav_status()))

    # ------------------------------------------------------------ 速度

    async def get_speed(self) -> dict[str, float]:
        return self._as_speed(await self.request(R.get_navigation_speed()))

    async def set_speed(self, x: float, y: float | None = None,
                        z: float | None = None) -> dict[str, float]:
        return self._as_speed(await self.request(R.set_navigation_speed(x, y, z)))

    # ------------------------------------------------------------ 小工具

    @staticmethod
    def _as_enum(cls, value: Any):
        parsed = parse_enum(cls, value)
        if parsed is None:
            log.warning("设备回了未知的 %s 值: %r", cls.__name__, value)
        return parsed

    @staticmethod
    def _as_speed(payload: Any) -> dict[str, float]:
        if not isinstance(payload, dict):
            raise NavBackendError(f"速度响应格式意外: {payload!r}")
        return {key: float(payload[key]) for key in ("x", "y", "z") if key in payload}
