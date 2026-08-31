"""请求构造。每个断言都对应 refs/nav-api 文档里的一段样例报文。"""

import inspect

import pytest

from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_types import Pose, Waypoint


def test_建图请求参数是数字零而不是_null():
    """§1.1/§1.2 的样例是 {"start_mapping": 0},照抄。"""
    assert R.start_mapping().args == 0
    assert R.stop_mapping().args == 0


def test_无参查询用_null():
    for req in (R.get_mapping_status(), R.get_all_pgm_map(), R.get_nav_status(),
                R.get_loc_status(), R.reset_loc(), R.stop_nav(), R.pause_nav(),
                R.continue_nav(), R.start_nav_return_home()):
        assert req.args is None, req.req_func


def test_删除地图参数是数组():
    """§1.6: remove_map_by_id 收的是 ["map_id_1", "map_id_2"]。"""
    req = R.remove_map_by_id(["m1", "m2"])
    assert req.req_func == "remove_map_by_id"
    assert req.args == ["m1", "m2"]


def test_删除地图接受单个字符串以外的任意序列():
    assert R.remove_map_by_id(("m1",)).args == ["m1"]


def test_重命名地图参数是二元数组():
    """§1.7: rename_map_name 收的是 ["old", "new"]。"""
    assert R.rename_map_name("旧", "新").args == ["旧", "新"]


def test_添加路径参数形状():
    """§2.2: [map_id, path_id, [[点名, pose], ...]]。"""
    wps = [Waypoint("P1", Pose.from_xy_yaw(1.0, 2.0)),
           Waypoint("P2", Pose.from_xy_yaw(3.0, 4.0))]
    req = R.add_nav_path("m1", "p1", wps)
    assert req.req_func == "add_nav_path"
    assert req.args[0] == "m1"
    assert req.args[1] == "p1"
    assert req.args[2][0][0] == "P1"
    assert req.args[2][0][1]["position"]["x"] == 1.0
    assert req.args[2][1][0] == "P2"


def test_修改路径是四元参数且可改名():
    """§2.3: [map_id, old_path_id, new_path_id, waypoints] —— 比 add 多一个改名位。"""
    wps = [Waypoint("P1", Pose.from_xy_yaw(0.0, 0.0))]
    req = R.modify_nav_path("m", "old", "new", wps)
    assert req.req_func == "modify_nav_path"
    assert req.args[:3] == ["m", "old", "new"]
    # 路点段的编码与 add_nav_path 完全一致,只是位置不同
    assert req.args[3] == R.add_nav_path("m", "p", wps).args[2]


def test_删除路径参数是二元组数组():
    """§2.4: [["map_id_1","path_id_1"], ["map_id_2","path_id_2"]]。"""
    assert R.remove_nav_path([("m1", "p1"), ("m2", "p2")]).args == [
        ["m1", "p1"], ["m2", "p2"]]


def test_单点导航参数就是_pose():
    req = R.start_nav(Pose.from_xy_yaw(1.0, 2.0))
    assert req.req_func == "start_nav"
    assert req.args["position"] == {"x": 1.0, "y": 2.0, "z": 0.0}
    assert set(req.args["orientation"]) == {"x", "y", "z", "w"}


def test_多点导航按路径_id():
    assert R.start_multi_nav("m1", "p1").args == ["m1", "p1"]


def test_多点导航按点列表():
    req = R.start_multi_nav_by_points("m1", [Pose.from_xy_yaw(1.0, 2.0),
                                             Pose.from_xy_yaw(3.0, 4.0)])
    assert req.args[0] == "m1"
    assert len(req.args[1]) == 2
    assert req.args[1][1]["position"]["y"] == 4.0


def test_导航速度请求带_type_字段():
    """§3.9/§3.10 的参数里有一个固定的 type: navigation_speed。"""
    assert R.get_navigation_speed().args == {"type": "navigation_speed"}
    assert R.set_navigation_speed(0.8, 0.4, 1.2).args == {
        "type": "navigation_speed", "x": 0.8, "y": 0.4, "z": 1.2}


def test_设置速度可只传_x_由设备取默认():
    """文档注:只传 x 时设备端 y 默认 0.5、z 默认 1.5。这里不替设备补默认值。"""
    assert R.set_navigation_speed(0.8).args == {"type": "navigation_speed", "x": 0.8}


def test_速度接口的响应带嵌套外壳():
    assert R.get_navigation_speed().nested is True
    assert R.set_navigation_speed(0.8).nested is True
    assert R.start_nav(Pose.from_xy_yaw(0.0, 0.0)).nested is False


def test_加载定位地图的响应名与请求名不同():
    """协议地雷 2: 请求发 loc_load_map,响应回 load_localization_map。"""
    req = R.loc_load_map("m1")
    assert req.req_func == "loc_load_map"
    assert req.args == "m1"
    assert req.response_func == "load_localization_map"


def test_绝大多数接口请求名即响应名():
    for req in (R.start_mapping(), R.get_nav_status(), R.stop_nav(),
                R.add_nav_path("m", "p", []), R.get_navigation_speed()):
        assert req.response_func == req.req_func


def test_别名表只收录不一致的条目():
    assert R.REQUEST_TO_RESPONSE_FUNC["loc_load_map"] == "load_localization_map"
    for k, v in R.REQUEST_TO_RESPONSE_FUNC.items():
        assert k != v


def test_只推送不响应的函数名():
    """协议地雷 3: 这些名字只会作为主动推送出现,匹配不上请求属正常。"""
    assert R.PUSH_ONLY_FUNCS == frozenset({"notify_stop_mapping_status"})
    # exit_charging 不在此列: §7.14 有完整请求/响应样例,是答复不是推送。
    assert "exit_charging" not in R.PUSH_ONLY_FUNCS


def test_未验证响应名的接口被标注():
    assert R.UNVERIFIED_RESPONSE_FUNCS == frozenset(
        {"reset_loc", "start_nav_return_home", "start_multi_nav_by_points"})


def test_response_func_for_对未知名字原样返回():
    assert R.response_func_for("some_future_api") == "some_future_api"


# 构造函数请求名测试数据 —— 参数表与完整性检查都使用此常量
REQ_FUNC_CASES = [
    # 建图与地图管理
    (R.start_mapping(), "start_mapping"),
    (R.stop_mapping(), "stop_mapping"),
    (R.get_mapping_status(), "get_mapping_status"),
    (R.get_pgm_map("m"), "get_pgm_map"),
    (R.get_all_pgm_map(), "get_all_pgm_map"),
    (R.remove_map_by_id([]), "remove_map_by_id"),
    (R.rename_map_name("", ""), "rename_map_name"),
    # 路径管理
    (R.get_all_paths_by_mapid(""), "get_all_paths_by_mapid"),
    (R.add_nav_path("", "", []), "add_nav_path"),
    (R.modify_nav_path("", "", "", []), "modify_nav_path"),
    (R.remove_nav_path([]), "remove_nav_path"),
    # 导航控制
    (R.start_nav(Pose.from_xy_yaw(0.0, 0.0)), "start_nav"),
    (R.start_multi_nav("", ""), "start_multi_nav"),
    (R.start_multi_nav_by_points("", []), "start_multi_nav_by_points"),
    (R.start_nav_return_home(), "start_nav_return_home"),
    (R.stop_nav(), "stop_nav"),
    (R.get_nav_status(), "get_nav_status"),
    (R.pause_nav(), "pause_nav"),
    (R.continue_nav(), "continue_nav"),
    (R.get_navigation_speed(), "get_navigation_speed"),
    (R.set_navigation_speed(0.0), "set_navigation_speed"),
    # 定位相关
    (R.loc_load_map(""), "loc_load_map"),
    (R.reset_loc(), "reset_loc"),
    (R.get_loc_status(), "get_loc_status"),
]


@pytest.mark.parametrize("builder_call,expected_req_func", REQ_FUNC_CASES)
def test_所有构造函数的请求名(builder_call, expected_req_func):
    """表驱动测试:验证每个构造函数的 req_func 完全字面匹配,防止拼写错误。"""
    assert builder_call.req_func == expected_req_func


def test_表驱动测试覆盖所有构造函数():
    """验证参数表不遗漏任何构造函数。新增构造函数必须加入参数表。"""
    # 已知的非构造函数需排除(不返回 NavRequest)。显式列出排除原因
    # 防止新增辅助函数时被意外作为构造函数遗漏。
    exclusions = {
        "response_func_for",      # 辅助函数: 响应名查询
        "parse_paths_payload",    # 响应解析: 返回 dict,不返回 NavRequest
        "parse_map_ids",          # 响应解析: 返回 list,不返回 NavRequest
    }

    # 发现所有本模块定义的公开函数(排除导入的、私有的、非构造的)
    builders = set()
    for name, obj in inspect.getmembers(R, inspect.isfunction):
        # 跳过私有函数(以 _ 开头)
        if name.startswith("_"):
            continue
        # 跳过导入的函数(来自其他模块);本模块定义的才计数
        if obj.__module__ != R.__name__:
            continue
        # 跳过非构造函数(辅助函数、解析函数等)
        if name in exclusions:
            continue
        builders.add(name)

    # 从参数表提取预期的构造函数名(第二元素是 req_func 字符串)
    expected_builders = {expected_req_func for _, expected_req_func in REQ_FUNC_CASES}

    # 验证: 所有发现的构造函数都在参数表中
    assert builders == expected_builders, (
        f"参数表不完整。漏掉的函数: {builders - expected_builders}。"
        f"表中错误的函数: {expected_builders - builders}"
    )


def test_解析路径载荷():
    """§2.1 的 data 是 [map_id, [path_ids], {path_id: [[点名, pose], ...]}]。"""
    data = [
        "m1",
        ["p1", "p2"],
        {
            "p1": [["A", {"position": {"x": 1.0, "y": 2.0, "z": 0.0},
                          "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}]],
            "p2": [],
        },
    ]
    paths = R.parse_paths_payload(data)
    assert set(paths) == {"p1", "p2"}
    assert paths["p1"][0].name == "A"
    assert paths["p1"][0].pose.position.y == 2.0
    assert paths["p2"] == []


def test_解析路径载荷_只在第三元素缺失时报错():
    with pytest.raises(ValueError, match="路径载荷"):
        R.parse_paths_payload(["m1", ["p1"]])


def test_解析地图列表():
    """§1.5 的 data 是 {map_id: [OccupancyGrid, 标签列表]}。只取键。"""
    data = {"m2": [{}, []], "m1": [{}, []]}
    assert R.parse_map_ids(data) == ["m1", "m2"]


def test_解析地图列表_空与非法():
    assert R.parse_map_ids({}) == []
    with pytest.raises(ValueError, match="地图列表"):
        R.parse_map_ids(["m1"])
