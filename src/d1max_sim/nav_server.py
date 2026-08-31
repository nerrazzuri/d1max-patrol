"""仿真导航服务端。

它是接口契约的可执行定义:客户端行为有争议时,以这台仿真器的表现为准,
直到真机把它推翻。凡文档未明写、由本仿真器补齐的行为,一律在源码里
加注待真机验证标记。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve

from d1max_patrol.protocol.nav_frames import (
    ProtocolError,
    build_alg_error_notify,
    build_response,
    parse_request,
)
from d1max_patrol.protocol.nav_requests import (
    NESTED_RESPONSE_FUNCS,
    PUSH_ONLY_FUNCS,
    response_func_for,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose, Waypoint

from .inject import FaultState, InjectError, apply_command
from .kinematics import Planar2DModel
from .nav_state import (
    LocStateMachine,
    MappingStateMachine,
    NavStateMachine,
    SimRejected,
)
from .store import MapStore, StoreError

log = logging.getLogger(__name__)

CONTROL_PATH = "/control"

#: 设备默认导航速度,见 refs/nav-api §3.10 的注解
_DEFAULT_SPEED = {"x": 0.8, "y": 0.5, "z": 1.5}

# N-1: 复用协议层已有的 PUSH_ONLY_FUNCS,不再在本模块里另外硬编码一份同样的
# 字面量 —— 两处各写一份的话,改名时很容易漏改其中一处而不被任何测试发现。
(_MAP_SAVED_PUSH_FUNC,) = PUSH_ONLY_FUNCS

#: M3: tick 循环里的网络 I/O(广播、断链注入触发的关闭)一律要有上限,
#: 否则一个不读取/不响应关闭握手的客户端能把整台仿真器的心跳拖停。
_TICK_IO_TIMEOUT_S = 0.5


def _as_str(args: Any, name: str) -> str:
    if not isinstance(args, str):
        raise ValueError(f"{name} 的参数应为字符串,实际为 {args!r}")
    return args


def _as_list(args: Any, name: str, length: int | None = None) -> list[Any]:
    if not isinstance(args, (list, tuple)):
        raise ValueError(f"{name} 的参数应为数组,实际为 {args!r}")
    if length is not None and len(args) != length:
        raise ValueError(f"{name} 的参数应为长度 {length} 的数组,实际长度 {len(args)}")
    return list(args)


class SimNavServer:
    """一台可连的仿真 D1 Max 导航设备。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        tick_hz: float = 50.0,
        store: MapStore | None = None,
        faults: FaultState | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.tick_hz = tick_hz

        self.model = Planar2DModel()
        self.nav = NavStateMachine(model=self.model)
        self.loc = LocStateMachine()
        self.mapping = MappingStateMachine(on_saved=self._on_map_saved)
        self.store = store if store is not None else MapStore()
        self.faults = faults if faults is not None else FaultState()
        self.speed: dict[str, float] = dict(_DEFAULT_SPEED)

        self._clients: set[ServerConnection] = set()
        self._server: Server | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending_map_saved = False
        self._silent_until = 0.0
        #: 半开链路注入当前是否已经施加到连接上。tick 循环负责让它跟上
        #: faults.half_open;测试等这个标志,而不是等挂钟。
        self._half_open = False
        # Mi-1: 这是**全局**响应序号(跨所有连接共用),不是按连接单独计数。
        # reorder 注入靠它的奇偶性决定"延迟这一半、放行那一半";多个客户端
        # 并发下,哪个客户端的哪条请求命中奇数位不可预测 —— 这个注入只保证
        # "确实会出现乱序",不保证"稳定命中某一条特定请求"。
        self._response_seq = 0

    # ------------------------------------------------------------ 生命周期

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def control_url(self) -> str:
        return f"ws://{self.host}:{self.port}{CONTROL_PATH}"

    async def start(self) -> None:
        self._server = await serve(self._handle, self.host, self.port)
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]
        self._ticker = asyncio.create_task(self._tick_loop())
        log.info("仿真导航服务端已启动: %s", self.url)

    async def stop(self) -> None:
        # M1 / Mi-4: 必须先停止接受新连接、结束所有 handler,再去取消收尾任务。
        # 反过来做(先取消再关服务器)的问题是: 关服务器期间仍在跑的 handler
        # 还能继续 _spawn 新任务,旧顺序会把这些新任务的引用直接丢掉,既不
        # 取消也不 await,事件循环关闭时表现为 "Task was destroyed but it is
        # pending"。
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                log.exception("关闭监听套接字时出错")
            self._server = None
        # 上面两步执行期间可能又冒出新任务,这里必须重新取一次快照,
        # 不能复用关服务器之前的那份。
        tasks = [t for t in (self._ticker, *self._tasks) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("停止时某个后台任务异常退出")
        self._ticker = None
        self._tasks.clear()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ 连接处理

    async def _handle(self, ws: ServerConnection) -> None:
        # MIN-4: 必须先把查询串剥掉再比。裸 `path.rstrip("/")` 下
        # `/control?x=1` 匹配不上 CONTROL_PATH,会被当成导航通道接进来 ——
        # 而带查询串的 URL 在真实客户端里再普通不过。
        路径 = urlsplit(ws.request.path).path if ws.request is not None else ""
        if ws.request is not None and 路径.rstrip("/") == CONTROL_PATH:
            # 控制通道不受 disconnect 注入影响,这是有意的设计(评审 Mi-6 确认):
            # 断链注入模拟的是导航链路故障,运维/测试仍需要能连控制通道下达、
            # 查询、撤销注入。
            await self._handle_control(ws)
            return
        if asyncio.get_running_loop().time() < self._silent_until:
            # Mi-6 裁决: 静默窗口内新连接的语义是"握手成功后立即以 1012 关闭",
            # 不是 TCP 层拒连 —— 真机断网时客户端多半连 TCP 握手都建立不起来,
            # 这里的表现并不完全一致。这是仿真器有意保留的已知简化(改造成
            # 传输层 abort 代价大且容易引入新的并发问题),不是对真机行为的
            # 猜测,故不用"假设待真机验证"标记。
            # 给 Task 14 的提示: 收到 code=1012 的关闭仍要判定为"未恢复",
            # 不能因为 connect() 握手本身成功就认为链路已经好了。
            await ws.close(code=1012, reason="injected disconnect")
            return
        await self._handle_nav(ws)

    async def _handle_nav(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        if self._half_open:
            # 注入生效期间新接进来的连接也一样是哑的,免得客户端一重连就"好了"。
            self._set_half_open(ws, True)
        try:
            async for raw in ws:
                try:
                    frame_count, req_func, args = parse_request(raw)
                except ProtocolError as exc:
                    log.warning("忽略畸形报文: %s", exc)
                    continue
                ok, msg, data = self._dispatch(req_func, args)
                self._spawn(self._reply(ws, req_func, frame_count or 0, ok, msg, data))
        except Exception as exc:  # noqa: BLE001 —— 客户端断开不应打断服务端
            log.debug("导航连接结束: %s", exc)
        finally:
            self._clients.discard(ws)

    async def _handle_control(self, ws: ServerConnection) -> None:
        try:
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                    command = payload["cmd"]
                except (ValueError, KeyError, TypeError):
                    await ws.send(json.dumps({"ok": False, "msg": "请求应为 {\"cmd\": ...}"}))
                    continue
                try:
                    result = apply_command(self.faults, command)
                except InjectError as exc:
                    await ws.send(json.dumps({"ok": False, "msg": str(exc)},
                                             ensure_ascii=False))
                    continue
                await ws.send(json.dumps({"ok": True, "msg": result}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 —— Mi-2: 控制通道断开不应打印未处理异常
            log.debug("控制连接结束: %s", exc)

    async def _reply(self, ws: ServerConnection, req_func: str, frame_count: int,
                     ok: bool, msg: str | None, data: Any) -> None:
        self._response_seq += 1
        # 乱序注入: 只延迟一半的响应,才能真正制造乱序到达
        delay = self.faults.response_delay_s if self._response_seq % 2 == 1 else 0.0
        if delay > 0:
            await asyncio.sleep(delay)
        frame = build_response(
            response_func_for(req_func),
            0 if self.faults.frame_count_zero else frame_count,
            ok=ok,
            msg=msg,
            data=data,
            nested=req_func in NESTED_RESPONSE_FUNCS,
        )
        # Mi-7: json.dumps 挪到 try 外面 —— 序列化本身出错(比如 handler 将来
        # 返回了不可序列化的对象)不该被当成"客户端断开"悄悄吞掉。
        text = json.dumps(frame, ensure_ascii=False)
        try:
            await ws.send(text)
        except Exception:  # noqa: BLE001 —— 回一条响应失败不该打断服务端
            log.debug("回复发送失败,客户端可能已断开: req_func=%s", req_func)

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        text = json.dumps(frame, ensure_ascii=False)
        for ws in list(self._clients):
            try:
                # M3: 广播发生在 tick 循环的调用链上,send() 不能无界等待 ——
                # 一个不读取的客户端会让写缓冲填满、drain() 永久挂起,进而
                # 拖停所有客户端共用的这条 tick 循环。
                await asyncio.wait_for(ws.send(text), timeout=_TICK_IO_TIMEOUT_S)
            except Exception:  # noqa: BLE001 —— 任何发送故障都只摘这一个客户端
                log.warning("广播发送超时或失败,已从客户端列表摘除")
                self._clients.discard(ws)
                self._spawn(self._safe_close(ws, code=1011, reason="slow consumer"))

    async def _safe_close(self, ws: ServerConnection, *, code: int, reason: str) -> None:
        """带超时地关闭一个连接 (M3)。

        tick 循环绝不能因为对端不配合关闭握手而卡住 —— `close()` 的关闭握手
        默认要等到 `close_timeout`(10s),这条协程本身作为独立任务跑,
        自己再兜一层超时,双保险。
        """
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=code, reason=reason),
                                   timeout=_TICK_IO_TIMEOUT_S)

    # ------------------------------------------------------------ 心跳

    def _on_map_saved(self) -> None:
        self.store.create_map()
        self._pending_map_saved = True

    async def _tick_loop(self) -> None:
        dt = 1.0 / self.tick_hz
        while True:
            await asyncio.sleep(dt)
            try:
                await self._apply_faults()
                self.nav.step(dt)
                self.loc.step(dt)
                self.mapping.step(dt)
                # 假设(待真机验证): 定位丢失时进行中的导航转入 Failed。
                if self.loc.status is LocStatus.LOC_LOST and self.nav.status in (
                    NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.PAUSE
                ):
                    self.nav.fail("定位丢失")
                await self._flush_pushes()
            except asyncio.CancelledError:
                raise
            except Exception:
                # M1: 心跳绝不能静默死掉。旧实现里这里没有 try/except,任何一次
                # 状态机/推送抛异常都会让整条 while True 连带 Task 一起悄悄终结
                # ——服务端此后会一直"健康地"应答一个再也不会变化的状态,而且
                # 没有任何日志,直到 stop() 去 await 这个已死的 Task 才炸出来
                # (那时监听端口也不会被释放,见 stop() 里的顺序修复)。
                log.exception("tick 循环单次迭代异常,已跳过本次 tick")

    async def _apply_faults(self) -> None:
        f = self.faults
        self.model.speed_scale = f.speed_scale
        self.model.frozen = f.stuck
        if f.loc_lost_requested:
            f.loc_lost_requested = False
            self.loc.lose()
        if f.loc_recover_requested:
            f.loc_recover_requested = False
            self.loc.recover()
        if f.disconnect_seconds > 0:
            seconds, f.disconnect_seconds = f.disconnect_seconds, 0.0
            self._silent_until = asyncio.get_running_loop().time() + seconds
            # M3: 不能在 tick 循环里逐个 await ws.close() —— 一个不回关闭帧的
            # 对端(这正是 disconnect 注入要模拟的"链路异常"本身)会让 close()
            # 卡到 close_timeout(默认 10s),期间导航/定位/其它注入全部停摆。
            # 摘掉 _clients 的引用是同步的,真正的关闭动作丢给独立任务去做。
            for ws in list(self._clients):
                self._spawn(self._safe_close(ws, code=1012, reason="injected disconnect"))
            self._clients.clear()
        if f.half_open != self._half_open:
            self._half_open = f.half_open
            for ws in list(self._clients):
                self._set_half_open(ws, f.half_open)

    def _set_half_open(self, ws: ServerConnection, on: bool) -> None:
        """半开链路: 让这条连接的接收侧彻底哑掉,但不动 TCP 本身。

        做法是暂停传输层的读取 —— 服务端从此收不到这个客户端的任何东西,
        包括它的关闭帧,自然也就不会回一帧关闭帧。连接对象、socket、发送侧
        都还在,与"设备跑出 wifi 覆盖"的形态一致。

        为什么不用别的做法: 直接 abort 传输层是"干脆地断",客户端立刻拿到
        ConnectionResetError,压根考验不到"关闭握手等不到回应"这条路径 ——
        而那正是 F1 那类客户端缺陷唯一能被抓住的地方。
        """
        transport = getattr(ws, "transport", None)
        if transport is None:
            return
        with contextlib.suppress(Exception):
            if on:
                transport.pause_reading()
            else:
                transport.resume_reading()

    async def _flush_pushes(self) -> None:
        for item in self.faults.take_alg_errors():
            await self._broadcast(build_alg_error_notify([item]))
        if self._pending_map_saved:
            self._pending_map_saved = False
            # 推送复用 app_resp 且 frame_count 照文档填 1 —— 客户端必须能识别出
            # 这是推送而不是某个 frame_count=1 请求的响应。
            await self._broadcast(build_response(_MAP_SAVED_PUSH_FUNC, 1))

    # ------------------------------------------------------------ 请求分发

    def _dispatch(self, req_func: str, args: Any) -> tuple[bool, str | None, Any]:
        handler = _HANDLERS.get(req_func)
        if handler is None:
            return False, f"unsupported req_func: {req_func}", None
        try:
            return True, None, handler(self, args)
        except (SimRejected, StoreError, ValueError) as exc:
            return False, str(exc), None
        except Exception as exc:  # M4: 见下方说明
            # 旧实现的白名单只挡 (SimRejected, StoreError, ValueError),漏网的
            # 异常(比如参数类型不对导致 float(None) 抛 TypeError)会一路逃到
            # _handle_nav 的宽 except,那里只是 log.debug 一句就把连接摘掉,
            # 客户端连"哪里错了"都收不到,只看到一次服务端"正常"关闭连接
            # (code 1000)。这与本文件自己定义的契约矛盾: 坏输入必须变成一条
            # status: "error" 的响应,链路要留着。
            log.exception("处理 %s 时抛出未预期异常", req_func)
            return False, str(exc), None

    # --- 建图与地图 -------------------------------------------------------

    def _h_start_mapping(self, args: Any) -> None:
        self.mapping.start()

    def _h_stop_mapping(self, args: Any) -> None:
        self.mapping.stop()

    def _h_get_mapping_status(self, args: Any) -> str:
        return self.mapping.status.value

    def _h_get_pgm_map(self, args: Any) -> Any:
        return self.store.occupancy_grid(_as_str(args, "get_pgm_map"))

    def _h_get_all_pgm_map(self, args: Any) -> Any:
        return self.store.all_pgm_payload()

    def _h_remove_map_by_id(self, args: Any) -> None:
        self.store.remove_maps([_as_str(x, "remove_map_by_id")
                                for x in _as_list(args, "remove_map_by_id")])

    def _h_rename_map_name(self, args: Any) -> None:
        old, new = _as_list(args, "rename_map_name", 2)
        self.store.rename_map(_as_str(old, "rename_map_name"),
                              _as_str(new, "rename_map_name"))

    # --- 路径 -------------------------------------------------------------

    def _h_get_all_paths_by_mapid(self, args: Any) -> Any:
        return self.store.paths_payload(_as_str(args, "get_all_paths_by_mapid"))

    def _h_add_nav_path(self, args: Any) -> None:
        name = "add_nav_path"
        map_id, path_id, points = _as_list(args, name, 3)
        self.store.set_path(
            _as_str(map_id, name),
            _as_str(path_id, name),
            [Waypoint.from_wire(p) for p in _as_list(points, name)],
        )

    def _h_modify_nav_path(self, args: Any) -> None:
        """§2.3 是四元参数,且允许顺带改名:old_path_id -> new_path_id。"""
        name = "modify_nav_path"
        map_id, old_id, new_id, points = _as_list(args, name, 4)
        map_id = _as_str(map_id, name)
        old_id = _as_str(old_id, name)
        new_id = _as_str(new_id, name)
        self.store.set_path(
            map_id, new_id,
            [Waypoint.from_wire(p) for p in _as_list(points, name)],
        )
        if old_id != new_id:
            self.store.remove_path(map_id, old_id)

    def _h_remove_nav_path(self, args: Any) -> None:
        for pair in _as_list(args, "remove_nav_path"):
            map_id, path_id = _as_list(pair, "remove_nav_path", 2)
            self.store.remove_path(_as_str(map_id, "remove_nav_path"),
                                   _as_str(path_id, "remove_nav_path"))

    # --- 导航 -------------------------------------------------------------

    def _h_start_nav(self, args: Any) -> None:
        # 假设(待真机验证): 定位未进入 ContinuousLoc 时设备拒绝导航。
        if not self.loc.healthy:
            raise SimRejected(f"定位未就绪,当前 {self.loc.status.value}")
        if self.faults.fail_next_nav:
            self.faults.fail_next_nav = False
            self.nav.fail_next_start = True
            try:
                self.nav.start(Pose.from_wire(args))
            except SimRejected:
                # Mi-3: nav.start() 只有在校验通过、真正进入 Initializing 之后
                # 才会读取并清零 nav.fail_next_start;如果它在校验阶段(比如
                # 当前不在 StandBy)就抛错,刚设的这个一次性开关根本没被消费,
                # 会一直留着 True,残留到下一次毫不相干的成功导航上,让它
                # 莫名其妙地失败。这里必须自己把它退回去。
                self.nav.fail_next_start = False
                raise
        else:
            self.nav.start(Pose.from_wire(args))

    def _h_reject_multi(self, args: Any) -> None:
        # 假设(待真机验证): 多点导航一律回 error,是本仿真器的**有意偏离**,不是对设备的猜测。
        # 真机上 start_multi_nav / start_multi_nav_by_points 很可能是能正常走的;本项目
        # 决定全逐点执行(见设计方案"全逐点执行"一节),仿真器便不假装支持它们。
        # 真机核对:确认这两个接口在设备上确实可用、以及它们的响应函数名 —— 若将来要改回
        # 多点下发,这里的拒绝必须先撤掉,否则仿真器会与真机行为相反。
        raise SimRejected("本仿真器不支持多点导航行走,请逐点下发 start_nav")

    def _h_reject_return_home(self, args: Any) -> None:
        raise SimRejected("返航未实现: 响应函数名未经真机验证,见第 3 卷")

    def _h_stop_nav(self, args: Any) -> None:
        self.nav.stop()

    def _h_pause_nav(self, args: Any) -> None:
        self.nav.pause()

    def _h_continue_nav(self, args: Any) -> None:
        self.nav.resume()

    def _h_get_nav_status(self, args: Any) -> str:
        return self.nav.status.value

    def _h_get_navigation_speed(self, args: Any) -> Any:
        # 假设(待真机验证): 响应只回 x/y/z,不回请求里带的 "type" 字段。
        # 真机是否会回显 type 未知,这是仿真器自行补齐的行为。
        return dict(self.speed)

    def _h_set_navigation_speed(self, args: Any) -> Any:
        if not isinstance(args, dict) or "x" not in args:
            raise ValueError("set_navigation_speed 需要至少一个 x")
        # 文档注: 只传 x 时设备端 y 取 0.5、z 取 1.5
        self.speed = {
            "x": float(args["x"]),
            "y": float(args.get("y", _DEFAULT_SPEED["y"])),
            "z": float(args.get("z", _DEFAULT_SPEED["z"])),
        }
        return dict(self.speed)

    # --- 定位 -------------------------------------------------------------

    def _h_loc_load_map(self, args: Any) -> None:
        map_id = _as_str(args, "loc_load_map")
        if map_id not in self.store.maps:
            raise StoreError(f"地图 {map_id!r} 不存在")
        self.loc.load_map(map_id)

    def _h_reset_loc(self, args: Any) -> None:
        self.loc.reset()

    def _h_get_loc_status(self, args: Any) -> str:
        return self.loc.status.value


_HANDLERS: dict[str, Callable[[SimNavServer, Any], Any]] = {
    "start_mapping": SimNavServer._h_start_mapping,
    "stop_mapping": SimNavServer._h_stop_mapping,
    "get_mapping_status": SimNavServer._h_get_mapping_status,
    "get_pgm_map": SimNavServer._h_get_pgm_map,
    "get_all_pgm_map": SimNavServer._h_get_all_pgm_map,
    "remove_map_by_id": SimNavServer._h_remove_map_by_id,
    "rename_map_name": SimNavServer._h_rename_map_name,
    "get_all_paths_by_mapid": SimNavServer._h_get_all_paths_by_mapid,
    "add_nav_path": SimNavServer._h_add_nav_path,
    "modify_nav_path": SimNavServer._h_modify_nav_path,
    "remove_nav_path": SimNavServer._h_remove_nav_path,
    "start_nav": SimNavServer._h_start_nav,
    "start_multi_nav": SimNavServer._h_reject_multi,
    "start_multi_nav_by_points": SimNavServer._h_reject_multi,
    "start_nav_return_home": SimNavServer._h_reject_return_home,
    "stop_nav": SimNavServer._h_stop_nav,
    "pause_nav": SimNavServer._h_pause_nav,
    "continue_nav": SimNavServer._h_continue_nav,
    "get_nav_status": SimNavServer._h_get_nav_status,
    "get_navigation_speed": SimNavServer._h_get_navigation_speed,
    "set_navigation_speed": SimNavServer._h_set_navigation_speed,
    "loc_load_map": SimNavServer._h_loc_load_map,
    "reset_loc": SimNavServer._h_reset_loc,
    "get_loc_status": SimNavServer._h_get_loc_status,
}
