"""仿真器的地图与路径存储。载荷形状必须与 refs/nav-api §1.5 / §2.1 一致。"""

import pytest

from d1max_patrol.protocol.nav_requests import parse_map_ids, parse_paths_payload
from d1max_patrol.protocol.nav_types import Pose, Waypoint
from d1max_sim.store import MapStore, StoreError


def _wp(name: str, x: float, y: float) -> Waypoint:
    return Waypoint(name, Pose.from_xy_yaw(x, y))


def test_新建地图自动编号且递增():
    s = MapStore()
    assert s.create_map() == "map_1"
    assert s.create_map() == "map_2"
    assert s.map_ids() == ["map_1", "map_2"]


def test_新建地图可指定_id():
    s = MapStore()
    assert s.create_map("厂区一层") == "厂区一层"
    assert "厂区一层" in s.maps


def test_重复_id_报错():
    s = MapStore()
    s.create_map("m1")
    with pytest.raises(StoreError, match="已存在"):
        s.create_map("m1")


def test_删除地图返回删掉的个数并连带删路径():
    s = MapStore()
    s.create_map("m1")
    s.create_map("m2")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0)])
    assert s.remove_maps(["m1", "不存在"]) == 1
    assert s.map_ids() == ["m2"]
    assert "m1" not in s.paths


def test_重命名地图连带搬运路径():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0)])
    s.rename_map("m1", "m9")
    assert s.map_ids() == ["m9"]
    assert s.maps["m9"].map_id == "m9"
    assert [w.name for w in s.get_paths("m9")["p1"]] == ["A"]


def test_重命名到已存在的_id_报错():
    s = MapStore()
    s.create_map("m1")
    s.create_map("m2")
    with pytest.raises(StoreError, match="已存在"):
        s.rename_map("m1", "m2")


def test_重命名不存在的地图报错():
    with pytest.raises(StoreError, match="不存在"):
        MapStore().rename_map("m1", "m2")


def test_栅格地图结构():
    s = MapStore()
    s.create_map("m1")
    grid = s.occupancy_grid("m1")
    assert set(grid) == {"header", "info", "data"}
    assert grid["info"]["width"] == 20
    assert grid["info"]["height"] == 20
    assert grid["info"]["resolution"] == 0.05
    assert len(grid["data"]) == 400
    assert set(grid["info"]["origin"]) == {"position", "orientation"}
    assert grid["info"]["map_load_time"] == {"sec": 0, "nanosec": 0}


def test_栅格地图取不存在的地图报错():
    with pytest.raises(StoreError, match="不存在"):
        MapStore().occupancy_grid("m1")


def test_全部地图载荷能被客户端解析器读回():
    s = MapStore()
    s.create_map("m2")
    s.create_map("m1")
    payload = s.all_pgm_payload()
    assert parse_map_ids(payload) == ["m1", "m2"]
    grid, tags = payload["m1"]
    assert grid["info"]["width"] == 20
    assert isinstance(tags, list)


def test_路径载荷能被客户端解析器读回():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0), _wp("B", 3.0, 4.0)])
    s.set_path("m1", "p2", [])
    paths = parse_paths_payload(s.paths_payload("m1"))
    assert set(paths) == {"p1", "p2"}
    assert [w.name for w in paths["p1"]] == ["A", "B"]
    assert paths["p1"][1].pose.position.y == 4.0
    assert paths["p2"] == []


def test_没有路径的地图返回空载荷():
    s = MapStore()
    s.create_map("m1")
    payload = s.paths_payload("m1")
    assert payload[0] == "m1"
    assert payload[1] == []
    assert payload[2] == {}


def test_对不存在的地图加路径报错():
    with pytest.raises(StoreError, match="不存在"):
        MapStore().set_path("m1", "p1", [])


def test_覆盖同名路径():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 0.0, 0.0)])
    s.set_path("m1", "p1", [_wp("B", 1.0, 1.0)])
    assert [w.name for w in s.get_paths("m1")["p1"]] == ["B"]


def test_删除路径():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [])
    s.remove_path("m1", "p1")
    assert s.get_paths("m1") == {}
    # 删不存在的不报错,与厂商"批量删除"的宽松语义一致
    s.remove_path("m1", "p1")
    s.remove_path("不存在", "p1")


def test_取路径返回的字典不会写回存储():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("a", 0.0, 0.0)])
    got = s.get_paths("m1")
    got["p_injected"] = []
    assert "p_injected" not in s.paths["m1"]
    assert list(s.get_paths("m1")) == ["p1"]


def test_存档与读档往返(tmp_path):
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0)])
    f = tmp_path / "state.json"
    s.save(f)

    s2 = MapStore()
    s2.load(f)
    assert s2.map_ids() == ["m1"]
    assert [w.name for w in s2.get_paths("m1")["p1"]] == ["A"]
    assert s2.get_paths("m1")["p1"][0].pose.position.x == 1.0


def test_读档后自动编号不与已有地图冲突(tmp_path):
    s = MapStore()
    s.create_map()          # map_1
    s.create_map()          # map_2
    f = tmp_path / "state.json"
    s.save(f)

    s2 = MapStore()
    s2.load(f)
    assert s2.create_map() == "map_3"


def test_读档不存在的文件不报错(tmp_path):
    s = MapStore()
    s.load(tmp_path / "nope.json")
    assert s.map_ids() == []


def test_读档格式错误报_StoreError(tmp_path):
    s = MapStore()
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all", encoding="utf-8")
    with pytest.raises(StoreError, match="读档失败"):
        s.load(bad_file)
