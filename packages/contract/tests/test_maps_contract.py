"""W00c5d 第二部分:地图经站点的契约。"""

from __future__ import annotations

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.maps import MANIFEST, MapFile, MapRef, parse_map_build, parse_mapping

H = "a" * 64


def _ref(**kw):
    d = {"map_id": "estate-1", "version": "8",
         "files": [{"name": "estate-1.pgm", "size": 10, "sha256": H},
                   {"name": "estate-1.yaml", "size": 2, "sha256": H}]}
    return d | kw


def test_地图往返():
    r = MapRef.from_wire(_ref())
    assert MapRef.from_wire(r.to_wire()) == r
    assert [f.name for f in r.files] == ["estate-1.pgm", "estate-1.yaml"]


@pytest.mark.parametrize("bad", [
    {"map_id": "../x"}, {"map_id": ""}, {"version": "a/b"}, {"files": []},
    {"files": [{"name": "x", "size": -1, "sha256": H}]},
    {"files": [{"name": "x", "size": 1, "sha256": "ABC"}]},
    {"files": [{"name": "..", "size": 1, "sha256": H}]},
    {"files": [{"name": "map.json", "size": 1, "sha256": H}]},
    {"files": [{"name": "x", "size": 1, "sha256": H}, {"name": "x", "size": 2, "sha256": H}]},
    {"files": [{"name": f"f{i}", "size": 1, "sha256": H} for i in range(17)]},
])
def test_坏地图都拒(bad):
    with pytest.raises(ContractError):
        MapRef.from_wire(_ref(**bad))


def test_建图命令的载荷():
    assert parse_mapping({"action": "start", "name": "yard-0925"}) == ("start", "yard-0925")
    assert parse_mapping({"action": "stop"}) == ("stop", "")
    for bad in ({"action": "go"}, {"action": "start"}, {"action": "start", "name": "a b"}, []):
        with pytest.raises(ContractError):
            parse_mapping(bad)
    assert parse_map_build({"bag": "yard-0925", "map_id": "estate-1", "version": "9"}) == \
        ("yard-0925", "estate-1", "9")
    with pytest.raises(ContractError):
        parse_map_build({"bag": "yard", "map_id": "estate-1"})
    assert MapFile.from_wire({"name": "home.json", "size": 0, "sha256": H}).size == 0


def test_地图版本的几何文件_名字合法_不超数():
    """W09c1:MOLA 建出来的一个版本就这几样;狗上收产物、站点查「哪里有图」都按这张表。"""
    from d1max_contract.maps import COVERAGE, GEOMETRY_FILES, MAX_FILES, NAME_RE
    assert GEOMETRY_FILES == ("prior.mm", "frames.json", "floor.pgm", "floor.yaml",
                              "coverage.json", "build.json")
    assert COVERAGE in GEOMETRY_FILES and MANIFEST not in GEOMETRY_FILES
    assert all(NAME_RE.match(n) for n in GEOMETRY_FILES) and len(GEOMETRY_FILES) < MAX_FILES


def test_哪里有图_解析与离路线多远():
    """W09c 决定 5:建图时走过的路(地图平面上每 0.5 m 一点)。"""
    import math

    from d1max_contract.maps import COVERAGE_RADIUS_M, Coverage, parse_coverage
    assert COVERAGE_RADIUS_M == 5.0
    cov = parse_coverage({"version": 1, "step_m": 0.5,
                          "path": [[0.5 * i, 0.0] for i in range(41)] + [[100.0, 100.0]]})
    assert isinstance(cov, Coverage) and len(cov.points) == 42
    assert cov.gap(3.0, 0.0) == 0.0
    assert cov.gap(3.0, 4.0) == pytest.approx(4.0)
    assert cov.gap(-3.0, -4.0) == pytest.approx(5.0)
    assert cov.gap(20.0 + 30.0, 0.0) == pytest.approx(30.0), "远的也要报准:提示里要说离多远"
    assert cov.gap(100.0, 97.0) == pytest.approx(3.0)
    assert math.isinf(parse_coverage({"version": 1, "path": []}).gap(0, 0))
    for bad in (None, [], {"version": 2, "path": []}, {"version": 1},
                {"version": 1, "path": [[0, "x"]]}, {"version": 1, "path": [[0, float("nan")]]},
                {"version": 1, "path": [[0, 1, 2]]}, {"version": 1, "path": [[True, 0]]}):
        with pytest.raises(ContractError):
            parse_coverage(bad)


def test_边走边建_开始录包可以带地图号和版本():
    """W09c2(决策 17):``mapping start`` 带着地图号与版本 = 录包的同时在线建这一版;
    不带照旧只录包。"""
    from d1max_contract.maps import mapping_target
    assert mapping_target({"action": "start", "name": "y", "map_id": "estate-1",
                           "version": "3"}) == ("estate-1", "3")
    assert mapping_target({"action": "start", "name": "y"}) is None
    assert mapping_target({"action": "stop"}) is None
    for bad in ({"action": "start", "name": "y", "map_id": "estate-1"},
                {"action": "start", "name": "y", "version": "3"},
                {"action": "start", "name": "y", "map_id": "../x", "version": "3"},
                {"action": "stop", "map_id": "estate-1", "version": "3"}):
        with pytest.raises(ContractError):
            mapping_target(bad)
