"""数据根:巡检数据搬出版本槽之后落在哪。"""

from __future__ import annotations

from pathlib import Path

from d1max_patrol.engine.datadir import DataPaths, resolve_paths


def test_没设数据根就是今天的相对路径():
    """开发机上在仓库里跑,行为一个字不变。"""
    assert resolve_paths(None) == DataPaths(
        runs_root=Path("runs"), maps_dir=Path("runs/slam"),
        bags_dir=Path("runs/bags"), missions_dir=Path("missions"))


def test_设了数据根全部落到它底下():
    assert resolve_paths("/var/lib/d1max") == DataPaths(
        runs_root=Path("/var/lib/d1max/runs"),
        maps_dir=Path("/var/lib/d1max/runs/slam"),
        bags_dir=Path("/var/lib/d1max/runs/bags"),
        missions_dir=Path("/var/lib/d1max/missions"))


def test_空串和空白当没设():
    assert resolve_paths("   ") == resolve_paths(None)
