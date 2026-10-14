"""R1(地图与导航能力对账,2026-10-14):巡检任务绑地图版本。

地图号 + 版本才是一个坐标系 —— 每次建图原点都不一样,同一张图的第 4 版上 (3, 2) 跟第 3 版上的
(3, 2) 不是一个地方。任务以前只带 ``map_id``,站点把狗自己加载的版本填进命令,狗的版本核对就空转了。
"""

from __future__ import annotations

import json

import pytest

from d1max_contract.bundle_format import BUNDLE_SCHEMA, BundleError, build_bundle
from d1max_contract.mission import MissionError, parse_mission

_WP = {"name": "P1", "pose": {"position": {"x": 1.0, "y": 2.0, "z": 0.0},
                              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}}


def _raw(**extra) -> dict:
    return {"mission": "night", "map_id": "floor1", "waypoints": [dict(_WP)], **extra}


def test_任务带地图版本_往返不变():
    m = parse_mission(_raw(map_version="4"))
    assert m.map_version == "4"
    wire = m.to_wire()
    assert wire["map_version"] == "4"
    assert parse_mission(wire) == m


def test_没写地图版本_读得进来但算没绑_线格式跟老的一样():
    """站点库里升级前导入的任务、狗自己拼的任务(回充 goto)都没有版本:契约照读,空串 = 没绑;
    派不派、收不收由站点和狗判。线格式不凭空多出一个键(老夹具、老代理不受影响)。"""
    m = parse_mission(_raw())
    assert m.map_version == ""
    assert "map_version" not in m.to_wire()


@pytest.mark.parametrize("bad", [4, 4.0, "", " 4", "4\n", None, True, "x" * 65, ["4"]])
def test_地图版本写坏了当场拒_报错带值(bad):
    with pytest.raises(MissionError, match="map_version"):
        parse_mission(_raw(map_version=bad))


def test_地图版本写成数字_提示写成字符串():
    with pytest.raises(MissionError, match='"4"'):
        parse_mission(_raw(map_version=4))


def _src(tmp_path, mission: dict):
    src = tmp_path / "src"
    (src / "missions").mkdir(parents=True)
    (src / "missions" / "night.json").write_text(json.dumps(mission), encoding="utf-8")
    (src / "schedule.yaml").write_text("timezone: UTC\nentries: []\n", encoding="utf-8")
    return src


def test_打包_没绑版本的任务打不出包_点名是哪个文件(tmp_path):
    with pytest.raises(BundleError) as ei:
        build_bundle(_src(tmp_path, _raw()), tmp_path / "out", bundle_id="site-kl", version=1,
                     built_at="2026-10-14T10:00:00+08:00")
    assert "night.json" in str(ei.value) and "map_version" in str(ei.value)
    assert not (tmp_path / "out" / "site-kl-1").exists(), "半个包比没有包更糟"


def test_打包_绑了版本的任务原样带着版本(tmp_path):
    out = build_bundle(_src(tmp_path, _raw(map_version="4")), tmp_path / "out",
                       bundle_id="site-kl", version=1, built_at="2026-10-14T10:00:00+08:00")
    got = json.loads((out / "missions" / "night.json").read_text(encoding="utf-8"))
    assert got["map_version"] == "4" and got["schema"] == BUNDLE_SCHEMA == 2
