"""各接口的请求构造与响应函数名映射。

参数形状逐条对照 refs/nav-api/自主导航_WEBSOCKET_API.md,
章节号写在每个函数的 docstring 里。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .nav_types import Pose, Waypoint

#: 协议地雷 2: 请求名与响应 req_func 不一致的接口。
#: 只收录不一致的条目;相同的不写,由 response_func_for 兜底。
REQUEST_TO_RESPONSE_FUNC: dict[str, str] = {
    "loc_load_map": "load_localization_map",
}

#: 协议地雷 3: 只会作为设备主动推送出现的响应 req_func。
#: 它们的 head.type 也是 app_resp,但 frame_count 不对应任何请求。
PUSH_ONLY_FUNCS = frozenset({"notify_stop_mapping_status"})

#: 协议地雷 1: 响应带外壳(AppReponseObjectData,厂商把 Response 拼成 Reponse)的
#: 请求名——这管的是仿真器**发**响应时要不要套壳,不是解析器**读**得懂哪些拼法。
#: 读的一侧(nav_frames._parse_response)两种外壳键名都认:AppReponseObjectData
#: (§3.9/§3.10 速度接口)与拼写正确的 AppResponse(§7 回充/对桩接口,如
#: get_arc_alg_status §7.13、exit_charging §7.14,已由 Task 19 的文档样例证实)。
#: 本卷不发 §7 的请求,仿真器也就不必学会套 AppResponse——只放宽读、不放宽写。
NESTED_RESPONSE_FUNCS = frozenset({"get_navigation_speed", "set_navigation_speed"})

#: 文档只给了请求没给响应样例的接口,响应 req_func 名未经验证。
#: 真机联调前不得依赖这几个名字,见计划"协议地雷"小节。
UNVERIFIED_RESPONSE_FUNCS = frozenset(
    {"reset_loc", "start_nav_return_home", "start_multi_nav_by_points"}
)


def response_func_for(req_func: str) -> str:
    """给出该请求预期的响应 req_func 名。"""
    return REQUEST_TO_RESPONSE_FUNC.get(req_func, req_func)


@dataclass(frozen=True)
class NavRequest:
    """一次请求的函数名与参数。不含 frame_count —— 那是传输层的事。"""

    req_func: str
    args: Any = None

    @property
    def response_func(self) -> str:
        return response_func_for(self.req_func)

    @property
    def nested(self) -> bool:
        """响应是否套着 AppReponseObjectData 外壳。"""
        return self.req_func in NESTED_RESPONSE_FUNCS


# ---------------------------------------------------------------- 建图与地图管理

def start_mapping() -> NavRequest:
    """§1.1 开始建图。参数是数字 0,不是 null。"""
    return NavRequest("start_mapping", 0)


def stop_mapping() -> NavRequest:
    """§1.2 停止建图。完成后设备会推送 notify_stop_mapping_status。"""
    return NavRequest("stop_mapping", 0)


def get_mapping_status() -> NavRequest:
    """§1.3 获取建图状态,返回 MappingStatus 字符串。"""
    return NavRequest("get_mapping_status", None)


def get_pgm_map(map_id: str) -> NavRequest:
    """§1.4 获取指定 PGM 地图。"""
    # 假设(待真机验证): 文档 §1.4 的请求样例写的是 {"get_pgm_map": null},没有给出
    # 按 map_id 取图的形状。这里仍按 map_id 下发 —— 上层需要指定地图,而多传一个
    # 参数通常被忽略;若真机拒绝,改回 None 即可,调用方签名不变。
    return NavRequest("get_pgm_map", map_id)


def get_all_pgm_map() -> NavRequest:
    """§1.5 获取所有 PGM 地图,返回 {map_id: [OccupancyGrid, 标签列表]}。"""
    return NavRequest("get_all_pgm_map", None)


def remove_map_by_id(map_ids: Sequence[str]) -> NavRequest:
    """§1.6 删除地图。参数是 id 数组,即使只删一张也要包成数组。"""
    return NavRequest("remove_map_by_id", list(map_ids))


def rename_map_name(old_id: str, new_id: str) -> NavRequest:
    """§1.7 重命名地图。参数是 [旧id, 新id]。"""
    return NavRequest("rename_map_name", [old_id, new_id])


# -------------------------------------------------------------------- 路径管理

def _waypoints_wire(waypoints: Sequence[Waypoint]) -> list[Any]:
    return [wp.to_wire() for wp in waypoints]


def get_all_paths_by_mapid(map_id: str) -> NavRequest:
    """§2.1 获取指定地图下的所有导航路径。"""
    return NavRequest("get_all_paths_by_mapid", map_id)


def add_nav_path(map_id: str, path_id: str, waypoints: Sequence[Waypoint]) -> NavRequest:
    """§2.2 新增导航路径。"""
    return NavRequest("add_nav_path", [map_id, path_id, _waypoints_wire(waypoints)])


def modify_nav_path(map_id: str, old_path_id: str, new_path_id: str,
                    waypoints: Sequence[Waypoint]) -> NavRequest:
    """§2.3 修改导航路径。比 add_nav_path 多一个改名位:老路径名改成新路径名。

    同名覆盖就把 old 和 new 传成同一个值。
    """
    return NavRequest(
        "modify_nav_path",
        [map_id, old_path_id, new_path_id, _waypoints_wire(waypoints)],
    )


def remove_nav_path(pairs: Sequence[tuple[str, str]]) -> NavRequest:
    """§2.4 删除导航路径。参数是 [[map_id, path_id], ...]。"""
    return NavRequest("remove_nav_path", [[m, p] for m, p in pairs])


# -------------------------------------------------------------------- 导航控制

def start_nav(pose: Pose) -> NavRequest:
    """§3.1 单点导航。巡检任务的主力接口 —— 全逐点执行。"""
    return NavRequest("start_nav", pose.to_wire())


def start_multi_nav(map_id: str, path_id: str) -> NavRequest:
    """§3.2 按路径 id 多点导航。

    本项目不用它行走(没有到点事件),仅保留以备排查与对照。
    """
    return NavRequest("start_multi_nav", [map_id, path_id])


def start_multi_nav_by_points(map_id: str, poses: Sequence[Pose]) -> NavRequest:
    """§3.3 按点列表多点导航。同上,不用于行走。响应名未验证。"""
    # 假设(待真机验证):
    return NavRequest("start_multi_nav_by_points",
                      [map_id, [p.to_wire() for p in poses]])


def start_nav_return_home() -> NavRequest:
    """§3.4 开始返航。文档未给响应样例,响应名未验证。"""
    # 假设(待真机验证):
    return NavRequest("start_nav_return_home", None)


def stop_nav() -> NavRequest:
    """§3.5 停止导航。"""
    return NavRequest("stop_nav", None)


def get_nav_status() -> NavRequest:
    """§3.6 获取导航状态,返回 NavStatus 字符串。"""
    return NavRequest("get_nav_status", None)


def pause_nav() -> NavRequest:
    """§3.7 暂停导航。"""
    return NavRequest("pause_nav", None)


def continue_nav() -> NavRequest:
    """§3.8 继续导航。"""
    return NavRequest("continue_nav", None)


def get_navigation_speed() -> NavRequest:
    """§3.9 获取导航速度配置。响应带 AppReponseObjectData 外壳。"""
    return NavRequest("get_navigation_speed", {"type": "navigation_speed"})


def set_navigation_speed(x: float, y: float | None = None,
                         z: float | None = None) -> NavRequest:
    """§3.10 设置导航速度配置。

    文档注明:只传 x 时设备端 y 取 0.5、z 取 1.5。此处不替设备补默认值,
    省略即省略,让设备行为保持文档描述的样子。
    """
    args: dict[str, Any] = {"type": "navigation_speed", "x": x}
    if y is not None:
        args["y"] = y
    if z is not None:
        args["z"] = z
    return NavRequest("set_navigation_speed", args)


# -------------------------------------------------------------------- 定位相关

def loc_load_map(map_id: str) -> NavRequest:
    """§4.1 加载定位地图。响应的 req_func 是 load_localization_map。"""
    return NavRequest("loc_load_map", map_id)


def reset_loc() -> NavRequest:
    """§4.2 重置定位。文档未给响应样例,响应名未验证。"""
    # 假设(待真机验证):
    return NavRequest("reset_loc", None)


def get_loc_status() -> NavRequest:
    """§4.3 获取定位状态,返回 LocStatus 字符串。"""
    return NavRequest("get_loc_status", None)


# ---------------------------------------------------------------- 响应载荷解析

def parse_paths_payload(data: Any) -> dict[str, list[Waypoint]]:
    """解析 §2.1 的 data: [map_id, [path_ids], {path_id: [[点名, pose], ...]}]。"""
    if not isinstance(data, (list, tuple)) or len(data) < 3:
        raise ValueError(f"路径载荷应为 [map_id, path_ids, paths] 三元列表,实际为 {data!r}")
    raw_paths = data[2]
    if not isinstance(raw_paths, dict):
        raise ValueError(f"路径载荷第三项应为映射,实际为 {raw_paths!r}")
    result: dict[str, list[Waypoint]] = {}
    for path_id, points in raw_paths.items():
        if not isinstance(points, list):
            raise ValueError(f"路径 {path_id!r} 的点列表应为数组,实际为 {points!r}")
        result[str(path_id)] = [Waypoint.from_wire(p) for p in points]
    return result


def parse_map_ids(data: Any) -> list[str]:
    """解析 §1.5 的 data: {map_id: [OccupancyGrid, 标签列表]}。只取地图 id 并排序。"""
    if not isinstance(data, dict):
        raise ValueError(f"地图列表应为映射,实际为 {data!r}")
    return sorted(str(k) for k in data)
