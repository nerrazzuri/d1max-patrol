"""``d1max bundle pack``(W00c5e):老服务退役之后,改任务、造任务包只剩这条命令(站点导入它)。"""

from __future__ import annotations

import json

from d1max_contract.bundle_format import read_manifest, verify_bundle
from d1max_patrol.cli import main

_MISSION = {"mission": "night", "map_id": "estate", "waypoints": [
    {"name": "P1", "pose": {"position": {"x": 1, "y": 0},
                            "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}]}


def _src(tmp_path):
    src = tmp_path / "src"
    (src / "missions").mkdir(parents=True)
    (src / "missions" / "night.json").write_text(json.dumps(_MISSION), encoding="utf-8")
    return src


def test_打出来的包过得了校验(tmp_path, capsys):
    assert main(["bundle", "pack", str(_src(tmp_path)), str(tmp_path / "out"),
                 "--id", "site-kl", "--version", "3", "--built-by", "alice"]) == 0
    out = capsys.readouterr().out
    assert "d1max-site import-bundle" in out
    dest = tmp_path / "out" / "site-kl-3"
    verify_bundle(dest)
    m = read_manifest(dest)
    assert (m.bundle_id, m.version, m.built_by) == ("site-kl", 3, "alice")


def test_源目录里有脚本打不了(tmp_path, capsys):
    src = _src(tmp_path)
    (src / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert main(["bundle", "pack", str(src), str(tmp_path / "out"),
                 "--id", "site-kl", "--version", "1"]) == 2
    assert "打不了任务包" in capsys.readouterr().err
