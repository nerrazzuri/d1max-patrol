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
