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

from websockets.asyncio.server import Server, ServerConnection, serve

from d1max_patrol.protocol.nav_frames import (
    ProtocolError,
    build_alg_error_notify,
    build_response,
    parse_request,
)
from d1max_patrol.protocol.nav_requests import NESTED_RESPONSE_FUNCS, response_func_for
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
        for task in (self._ticker, *self._tasks):
            if task is not None:
                task.cancel()
        for task in (self._ticker, *self._tasks):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._ticker = None
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ 连接处理

    async def _handle(self, ws: ServerConnection) -> None:
        if ws.request is not None and ws.request.path.rstrip("/") == CONTROL_PATH:
            await self._handle_control(ws)
            return
        if asyncio.get_running_loop().time() < self._silent_until:
            await ws.close(code=1012, reason="injected disconnect")
            return
        await self._handle_nav(ws)

    async def _handle_nav(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
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
        with contextlib.suppress(Exception):
            await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        text = json.dumps(frame, ensure_ascii=False)
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.send(text)

    # ------------------------------------------------------------ 心跳

    def _on_map_saved(self) -> None:
        self.store.create_map()
        self._pending_map_saved = True

    async def _tick_loop(self) -> None:
        dt = 1.0 / self.tick_hz
        while True:
            await asyncio.sleep(dt)
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
            for ws in list(self._clients):
                with contextlib.suppress(Exception):
                    await ws.close(code=1012, reason="injected disconnect")
            self._clients.clear()

    async def _flush_pushes(self) -> None:
        for item in self.faults.take_alg_errors():
            await self._broadcast(build_alg_error_notify([item]))
        if self._pending_map_saved:
            self._pending_map_saved = False
            # 推送复用 app_resp 且 frame_count 照文档填 1 —— 客户端必须能识别出
            # 这是推送而不是某个 frame_count=1 请求的响应。
            await self._broadcast(build_response("notify_stop_mapping_status", 1))

    # ------------------------------------------------------------ 请求分发

    def _dispatch(self, req_func: str, args: Any) -> tuple[bool, str | None, Any]:
        handler = _HANDLERS.get(req_func)
        if handler is None:
            return False, f"unsupported req_func: {req_func}", None
        try:
            return True, None, handler(self, args)
        except (SimRejected, StoreError, ValueError) as exc:
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
