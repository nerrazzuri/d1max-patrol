"""``--runs-root`` 这几个默认值要跟着 ``$D1MAX_DATA_ROOT`` 走。"""

from __future__ import annotations

from pathlib import Path

from d1max_patrol.app.server import _build_parser
from d1max_patrol.engine.datadir import DATA_ROOT_ENV


def test_没设环境变量默认值不变(monkeypatch):
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    a = _build_parser().parse_args([])
    assert (a.runs_root, a.maps_dir, a.bags_dir, a.missions_dir) == (
        "runs", "runs/slam", "runs/bags", "missions")


def test_设了环境变量四个目录都落到数据根(monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, "/var/lib/d1max")
    a = _build_parser().parse_args([])
    assert Path(a.runs_root) == Path("/var/lib/d1max/runs")
    assert Path(a.maps_dir) == Path("/var/lib/d1max/runs/slam")
    assert Path(a.bags_dir) == Path("/var/lib/d1max/runs/bags")
    assert Path(a.missions_dir) == Path("/var/lib/d1max/missions")


def test_命令行显式给的赢过环境变量(monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, "/var/lib/d1max")
    a = _build_parser().parse_args(["--runs-root", "/mnt/x/runs"])
    assert a.runs_root == "/mnt/x/runs"
    assert Path(a.maps_dir) == Path("/var/lib/d1max/runs/slam")
