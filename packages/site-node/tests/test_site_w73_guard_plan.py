"""W73 第一期:防区的查看点 / 值守位自动算,防区路线自动生成(设计 §4)。

台子:20 m × 10 m 的屋子,x=10 m 一道墙,墙中间开 1.6 m 的门(y 4.2–5.8);建图时蛇形走满全屋
(coverage 每 1 m 一行)。东区 = 左半间,西区 = 右半间;待命点在 (1, 1)。
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
from test_site_maps import _files

from d1max_site.areas import AreaBook
from d1max_site.db import SiteDB
from d1max_site.guard_plan import GuardPlanError, ZonePosts, generate
from d1max_site.intercept_reach import InterceptReach
from d1max_site.maps import MapCatalog
from d1max_site.nav_zones import NavZones
from d1max_site.routes import RouteStore

RES = 0.1
W_M, H_M = 20.0, 10.0
EAST = [[0.5, 0.5], [9.5, 0.5], [9.5, 9.5], [0.5, 9.5]]
WEST = [[10.5, 0.5], [19.5, 0.5], [19.5, 9.5], [10.5, 9.5]]


def _两间(d, *, closed: bool = False, pillar: bool = False):
    w, h = int(W_M / RES), int(H_M / RES)
    rows = []
    for r in range(h):
        y = (h - 1 - r + 0.5) * RES
        line = []
        for c in range(w):
            x = (c + 0.5) * RES
            edge = c in (0, w - 1) or r in (0, h - 1)
            wall = abs(x - 10.0) < 0.1 and (closed or abs(y - 5.0) > 0.8)
            # 东区正中一根 2 m × 2 m 的柱子(防区中心站不下人)
            col = pillar and abs(x - 5.0) < 1.0 and abs(y - 5.0) < 1.0
            line.append("#" if edge or wall or col else ".")
        rows.append("".join(line))
    v = {".": 254, "#": 0}
    (d / "floor.pgm").write_bytes(f"P5\n{w} {h}\n255\n".encode()
                                  + bytes(v[ch] for row in rows for ch in row))
    (d / "floor.yaml").write_text(f"image: floor.pgm\nresolution: {RES}\norigin: [0.0, 0.0, 0.0]\n"
                                  "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
                                  "mode: trinary\n")
    path = []
    for i, y in enumerate([1.0 + k for k in range(9)]):
        xs = [0.5 + 0.5 * j for j in range(39)]
        path += [[x, y] for x in (xs if i % 2 == 0 else xs[::-1])]
    (d / "coverage.json").write_text(json.dumps({"version": 1, "step_m": 0.5, "path": path}))


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    cat = MapCatalog(tmp_path, db, now_ms=lambda: 1000)
    zones = NavZones(db, now_ms=lambda: 1000)
    for ver, kw in (("1", {}), ("closed", {"closed": True}), ("pillar", {"pillar": True})):
        src = _files(tmp_path / f"src-{ver}")
        _两间(src, **kw)
        cat.import_dir(src, map_id="shop", version=ver)
    for ver in ("1", "closed", "pillar"):
        db.query("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                 "is_default) VALUES ('A', ?, 'shop', ?, 1.0, 1.0, 0, 1)", (f"dock-{ver}", ver))
    reach = InterceptReach(db, cat, zones)
    areas = AreaBook(db, now_ms=lambda: 1000)
    for ver in ("1", "closed", "pillar"):
        areas.set("shop", ver, "东区", EAST, by="a")
        areas.set("shop", ver, "西区", WEST, by="a")
    store = RouteStore(db, maps=cat, now_ms=lambda: 2000)
    store.reach = reach
    posts = ZonePosts(db, cat, reach, areas, now_ms=lambda: 3000)
    yield SimpleNamespace(db=db, cat=cat, zones=zones, reach=reach, areas=areas, store=store,
                          posts=posts)
    db.close()


def _in(p, poly) -> bool:
    xs, ys = [q[0] for q in poly], [q[1] for q in poly]
    return min(xs) <= p["x"] <= max(xs) and min(ys) <= p["y"] <= max(ys)


# ------------------------------------------------------------ 查看点、值守位


def test_自动查看点_在防区里_离中心最近_站得下(台):
    got = {p["zone"]: p for p in 台.posts.view("shop", "1")}
    east = got["东区"]
    assert east["auto"] is True
    assert _in(east["view"], EAST)
    assert math.hypot(east["view"]["x"] - 5.0, east["view"]["y"] - 5.0) < 0.3
    assert east["watch"]["x"] == east["view"]["x"] and east["watch"]["y"] == east["view"]["y"]


def test_防区中心是柱子_挑柱子外面最近的点_值守位朝着中心(台):
    east = {p["zone"]: p for p in 台.posts.view("shop", "pillar")}["东区"]
    v, w = east["view"], east["watch"]
    d = math.hypot(v["x"] - 5.0, v["y"] - 5.0)
    assert 1.0 < d < 2.0, "柱子外沿 + 机身半径"
    toward = math.atan2(5.0 - w["y"], 5.0 - w["x"])
    assert abs(math.remainder(w["yaw"] - toward, math.tau)) < 0.05


def test_走不到的防区_自动点给不出来_说清原因(台):
    west = {p["zone"]: p for p in 台.posts.view("shop", "closed")}["西区"]
    assert west["view"] is None and "走不到" in west["note"]


def test_人挪过的点按人的_重置回自动(台):
    台.posts.set("shop", "1", "东区", view={"x": 2.0, "y": 2.0},
                 watch={"x": 3.0, "y": 3.0, "yaw": 1.0}, by="bob")
    east = {p["zone"]: p for p in 台.posts.view("shop", "1")}["东区"]
    assert east["auto"] is False
    assert (east["view"], east["watch"]) == ({"x": 2.0, "y": 2.0},
                                             {"x": 3.0, "y": 3.0, "yaw": 1.0})
    assert east["updated_by"] == "bob"
    台.posts.reset("shop", "1", "东区")
    assert {p["zone"]: p for p in 台.posts.view("shop", "1")}["东区"]["auto"] is True


def test_挪点要挪在防区里_站得下(台):
    with pytest.raises(GuardPlanError, match="防区外"):
        台.posts.set("shop", "1", "东区", view={"x": 15.0, "y": 5.0},
                     watch={"x": 3.0, "y": 3.0, "yaw": 0.0}, by="b")
    with pytest.raises(GuardPlanError, match="墙|障碍"):
        台.posts.set("shop", "pillar", "东区", view={"x": 5.0, "y": 5.0},
                     watch={"x": 3.0, "y": 3.0, "yaw": 0.0}, by="b")
    with pytest.raises(GuardPlanError, match="没有防区"):
        台.posts.set("shop", "1", "北区", view={"x": 2.0, "y": 2.0},
                     watch={"x": 2.0, "y": 2.0, "yaw": 0.0}, by="b")


def test_防区删了_它的点也不显示(台):
    台.posts.set("shop", "1", "东区", view={"x": 2.0, "y": 2.0},
                 watch={"x": 2.0, "y": 2.0, "yaw": 0.0}, by="b")
    台.areas.remove("shop", "1", "东区")
    assert [p["zone"] for p in 台.posts.view("shop", "1")] == ["西区"]


# ------------------------------------------------------------ 防区路线自动生成


def _gen(台, zones, ver="1", **kw):
    return generate(台.cat, 台.reach, 台.areas, 台.posts, "shop", ver, zones,
                    store=台.store, **kw)


def test_生成_每个防区撒点_都在防区里_相隔大约间距_整条存得下(台):
    got = _gen(台, [{"zone": "东区"}, {"zone": "西区"}])
    wps = got["waypoints"]
    east = [w for w in wps if w["name"].startswith("东区-")]
    west = [w for w in wps if w["name"].startswith("西区-")]
    assert len(east) >= 2 and len(west) >= 2
    assert all(_in(w, EAST) for w in east) and all(_in(w, WEST) for w in west)
    # 从待命点 (1, 1) 出发,先东区(近)后西区
    assert wps[0]["name"].startswith("东区-") and wps[-1]["name"].startswith("西区-")
    for a, b in zip(east, east[1:], strict=False):
        assert math.hypot(a["x"] - b["x"], a["y"] - b["y"]) > 3.0
    assert got["problems"] == []
    r = 台.store.put_route("g", {"map_id": "shop", "map_version": "1", "kind": "guard",
                                 "waypoints": wps, "base_revision": 0}, by="a")
    assert r["kind"] == "guard" and len(r["waypoints"]) == len(wps)


def test_生成_点名不重复_多转几圈_每圈来回换方向(台):
    one = _gen(台, [{"zone": "东区"}])["waypoints"]
    two = _gen(台, [{"zone": "东区", "laps": 2}])["waypoints"]
    assert len(one) > 1
    # 掉头那个点刚到过,第二圈不再连着去一遍(演示站点实测 11-04:「东区-7」后面紧跟「东区-7·2」)
    assert len(two) == 2 * len(one) - 1
    names = [w["name"] for w in two]
    assert len(set(names)) == len(names)
    xy = [(w["x"], w["y"]) for w in two]
    assert all(a != b for a, b in zip(xy, xy[1:], strict=False))
    assert xy[len(one):] == [(w["x"], w["y"]) for w in one][::-1][1:]


def test_生成_停几秒拍几张_朝防区里面拍(台):
    wps = _gen(台, [{"zone": "东区", "dwell_s": 5, "photos": 2}])["waypoints"]
    for w in wps:
        kinds = [a["type"] for a in w["actions"]]
        assert kinds.count("photo") == 2 and {"type": "dwell", "seconds": 5} in w["actions"]
    far = max(wps, key=lambda w: math.hypot(w["x"] - 5.0, w["y"] - 5.0))
    toward = math.atan2(5.0 - far["y"], 5.0 - far["x"])
    assert abs(math.remainder(far["yaw"] - toward, math.tau)) < 0.05


def test_生成_间距可调_间距小点多(台):
    a = _gen(台, [{"zone": "东区"}], spacing_m=6.0)["waypoints"]
    b = _gen(台, [{"zone": "东区"}], spacing_m=3.0)["waypoints"]
    assert len(b) > len(a)


def test_生成_走不到的防区说清楚_不硬塞点(台):
    got = _gen(台, [{"zone": "东区"}, {"zone": "西区"}], ver="closed")
    assert all(w["name"].startswith("东区-") for w in got["waypoints"])
    notes = {z["zone"]: z for z in got["zones"]}
    assert notes["西区"]["points"] == 0 and "走不到" in notes["西区"]["note"]


def test_生成_参数不对的报错(台):
    with pytest.raises(GuardPlanError, match="没有防区"):
        _gen(台, [{"zone": "北区"}])
    with pytest.raises(GuardPlanError, match="至少选一个"):
        _gen(台, [])
    with pytest.raises(GuardPlanError, match="laps"):
        _gen(台, [{"zone": "东区", "laps": 0}])
    with pytest.raises(GuardPlanError, match="spacing"):
        _gen(台, [{"zone": "东区"}], spacing_m=0.5)
    with pytest.raises(GuardPlanError, match="500"):
        _gen(台, [{"zone": "东区", "laps": 10}], spacing_m=1.0)


def test_只在斜角碰着的两块不算连通_跟规划一样不切墙角():
    """演示站点商场图实测(11-04):生成的点之间站点又说「走不通」—— 规划不许斜着擦过墙角,
    放点用的连通区域也得这么算。"""
    import numpy as np

    from d1max_contract.planning.astar import plan
    from d1max_contract.planning.costmap import LETHAL, Costmap
    from d1max_site.guard_plan import _Ground
    lethal = np.array([[0, 0, 1, 1],
                       [0, 0, 1, 1],
                       [1, 1, 0, 0],
                       [1, 1, 0, 0]], dtype=bool)
    hard = np.zeros_like(lethal)
    cm = Costmap(res=1.0, origin=(0.0, 0.0), hard=hard, lethal=lethal,
                 cost=np.where(lethal, np.uint8(LETHAL), np.uint8(0)).astype(np.uint8),
                 speed=np.full(lethal.shape, np.inf, dtype=np.float32))
    from d1max_contract.planning.astar import PlanError
    with pytest.raises(PlanError):
        plan(cm.cost.tobytes(), hard.astype("uint8").tobytes(), 4, 4, (0, 0), (3, 3))
    g = _Ground(cm, [(0.5, 0.5)])
    assert g.ok[1, 1] and not g.ok[2, 2] and not g.ok[3, 3]


def test_没有待命点_按最大的一片连通区域放点_不放进封死的小间():
    """演示站点商场图实测(11-04):没设待命点时把点放进了门关着的铺面里,点和点之间走不通。"""
    import numpy as np

    from d1max_contract.planning.costmap import LETHAL, Costmap
    from d1max_site.guard_plan import _Ground
    lethal = np.ones((6, 8), dtype=bool)
    lethal[1:5, 1:5] = False                     # 大厅 4×4
    lethal[2:4, 6:7] = False                     # 封死的小间(中间隔一列墙)
    hard = np.zeros_like(lethal)
    cm = Costmap(res=1.0, origin=(0.0, 0.0), hard=hard, lethal=lethal,
                 cost=np.where(lethal, np.uint8(LETHAL), np.uint8(0)).astype(np.uint8),
                 speed=np.full(lethal.shape, np.inf, dtype=np.float32))
    g = _Ground(cm, [])
    assert g.ok[2, 2] and not g.ok[2, 6] and not g.ok[3, 6]
