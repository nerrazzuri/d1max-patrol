"""``--runs-root`` 这几个默认值要跟着 ``$D1MAX_DATA_ROOT`` 走。"""

from __future__ import annotations

from pathlib import Path

from d1max_patrol.app.server import _build_parser, _warn_if_data_in_slot
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


# ------------------------------------------------ _warn_if_data_in_slot


def test_设了数据根不警告(tmp_path):
    """`D1MAX_DATA_ROOT` 设了,说明装机脚本这一版跑过 —— 没什么好喊的。"""
    slot = tmp_path / "opt" / "d1max" / "releases" / "2026-09-20-x"
    slot.mkdir(parents=True)
    assert _warn_if_data_in_slot(slot, {DATA_ROOT_ENV: "/var/lib/d1max"}) is None


def test_没设数据根但跑在版本槽里就警告(tmp_path):
    """OTA 升上来、单元文件没更新的那台机器:`D1MAX_DATA_ROOT` 没设,而
    当前目录长在 `.../releases/<版本>` 底下 —— 巡检数据会悄悄落回槽里。"""
    slot = tmp_path / "opt" / "d1max" / "releases" / "2026-09-20-x"
    slot.mkdir(parents=True)
    msg = _warn_if_data_in_slot(slot, {})
    assert msg is not None
    assert "OTA" in msg


def test_没设数据根但也不在版本槽里就不警告(tmp_path):
    """开发机在仓库里跑,`cwd` 压根不在哪个 releases/ 底下 —— 这是今天的
    正常行为,不该被这条警告绊住。"""
    cwd = tmp_path / "home" / "robot" / "d1max-patrol"
    cwd.mkdir(parents=True)
    assert _warn_if_data_in_slot(cwd, {}) is None
