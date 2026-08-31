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
            await self._open_link()
            self._connected_event.set()

    async def _open_link(self) -> None:
        """建链 + 起读循环 + 起轮询器。不发任何请求,不碰就绪标志。

        初次连接刻意不做探针: 探针会多消耗一个 frame_count、并拨动仿真器
        那个全局响应奇偶计数器,而匹配测试正靠奇偶性把延迟注入打在特定
        请求上(见 test_vendor_matching.py 的 test_C1 docstring)。缺陷 19
        真正要防的是"重连把半开链路当成功",初次连接不涉及这个问题。
        """
        self._closing = False
        try:
            self._ws = await asyncio.wait_for(
                connect(self.config.url, open_timeout=None),
                timeout=self.config.connect_timeout_s,
            )
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            raise NavConnectionError(f"连接 {self.config.url} 失败: {exc}") from exc
        self._reader = asyncio.create_task(self._read_loop())
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
        try:
            await self.request(R.get_nav_status())
        except NavBackendError:
            await self._teardown_link()
            raise
        self._connected_event.set()

    async def _teardown_link(self) -> None:
        """探针失败后拆干净,好让下一次重连能真的重来。

        不收 `_poller` —— 它跨断链存活,只由 close() 收。
        """
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_pending("探针失败,链路未建立")

    async def close(self) -> None:
        self._closing = True
        self._connected_event.clear()
        tasks = [self._reader, self._poller, self._reconnector]
        self._reader = self._poller = self._reconnector = None
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
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
        with contextlib.suppress(Exception):
            await ws.close()

    # ------------------------------------------------------------ 读循环

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
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
                self._on_link_lost(f"读循环异常: {exc}")
            return
        if not self._closing:
            self._on_link_lost("设备关闭了连接")

    def _on_link_lost(self, reason: str) -> None:
        """链路断开的统一入口,接着重连。"""
        if self._ws is None and not self._connected_event.is_set():
            return                       # 已经在处理同一次断链了
        log.warning("导航链路断开: %s", reason)
        ws, self._ws = self._ws, None
        self._connected_event.clear()
        # I2: 光丢引用不够 —— 旧连接的 socket 还开着,服务端会一直把它算作
        # 在线客户端。真正关掉它,丢给后台任务做,免得阻塞读循环本身的收尾。
        if ws is not None:
            self._spawn(self._safe_close(ws))
        self._fail_pending(reason)
        self.emit(BackendDisconnected(reason))
        if self.auto_reconnect and not self._closing and self._reconnector is None:
            self._reconnector = asyncio.create_task(self._reconnect_loop())

    # ------------------------------------------------------------ 重连

    async def wait_connected(self, timeout_s: float) -> None:
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
                except NavBackendError as exc:
                    # 控制者订正: 原文只捕 NavConnectionError。_reconnect_once()
                    # 带探针,探针可能抛 NavTimeoutError / NavRequestError ——
                    # 窄捕获会让重连循环带着异常静默死掉,从此再也不重连。
                    # 重连循环里没有任何错误值得杀死重连。
                    log.info("第 %d 次重连失败: %s", self.reconnect_attempts, exc)
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
            self._pending.pop(frame_count, None)
            raise NavConnectionError(f"发送 {req.req_func} 失败: {exc}") from exc

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
