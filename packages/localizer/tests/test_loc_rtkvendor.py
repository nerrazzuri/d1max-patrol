"""W09e 厂家 RTK 辅助进程:厂家消息 → 一行 JSON(字段名按候选取)。"""

from __future__ import annotations

import json
import types

from d1max_localizer.rtkvendor import main, to_json


def test_字段按候选名取_取不到不带():
    m = types.SimpleNamespace(pos_type=50, svs_num=24, diff_age=1.5, latitude=5.41,
                              longitude=100.32, height=20.0, lat_std=0.01, lon_std=float("nan"))
    d = json.loads(to_json(m))
    assert d == {"pos_type": 50, "svs_num": 24, "diff_age_s": 1.5, "lat": 5.41, "lon": 100.32,
                 "alt": 20.0, "lat_std": 0.01}


def test_没有经纬度不出():
    assert to_json(types.SimpleNamespace(pos_type=0)) is None


def test_没有厂家消息包_说清楚退2(capsys):
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "起不来" in err
