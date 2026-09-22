"""数据根:巡检数据搬出版本槽之后落在哪。"""

from __future__ import annotations

import json
from pathlib import Path

from d1max_patrol.engine.datadir import DataPaths, migrate_slot_data, resolve_paths
from d1max_patrol.engine.release import Layout


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


# --------------------------------------------------------- migrate_slot_data


def _slot(root: Path, name: str) -> Path:
    slot = root / "releases" / name
    slot.mkdir(parents=True)
    (slot / "release.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return slot


def test_把每个槽里的数据搬到数据根并改名源目录(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    b = _slot(root, "2026-09-06-a3f9c1")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("a", encoding="utf-8")
    (a / "queue.jsonl").write_text('{"k":1}\n', encoding="utf-8")
    (b / "runs" / "r2").mkdir(parents=True)
    (b / "runs" / "r2" / "manifest.json").write_text("b", encoding="utf-8")
    (b / "baselines").mkdir()
    (b / "baselines" / "p1.jpg").write_bytes(b"jpg")

    report = migrate_slot_data(Layout(root=root), data)

    assert (data / "runs" / "r1" / "manifest.json").read_text(encoding="utf-8") == "a"
    assert (data / "runs" / "r2" / "manifest.json").read_text(encoding="utf-8") == "b"
    assert (data / "queue.jsonl").read_text(encoding="utf-8") == '{"k":1}\n'
    assert (data / "baselines" / "p1.jpg").read_bytes() == b"jpg"
    assert not (a / "runs").exists() and (a / "runs.migrated").is_dir()
    assert not (a / "queue.jsonl").exists() and (a / "queue.jsonl.migrated").is_file()
    assert not (b / "runs").exists() and (b / "runs.migrated").is_dir()
    assert report.copied == 4 and report.skipped == 0
    assert set(report.slots) == {"2026-09-01-aaaaaa", "2026-09-06-a3f9c1"}


def test_目标已有的文件不覆盖(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("old", encoding="utf-8")
    (data / "runs" / "r1").mkdir(parents=True)
    (data / "runs" / "r1" / "manifest.json").write_text("new", encoding="utf-8")

    report = migrate_slot_data(Layout(root=root), data)

    assert (data / "runs" / "r1" / "manifest.json").read_text(encoding="utf-8") == "new"
    assert report.copied == 0 and report.skipped == 1
    assert (a / "runs.migrated" / "r1" / "manifest.json").exists()


def test_没有槽或槽里没数据什么都不做(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    _slot(root, "2026-09-01-aaaaaa")
    report = migrate_slot_data(Layout(root=root), data)
    assert report.copied == 0 and report.slots == ()
    assert not data.exists()


def test_重跑是幂等的(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("a", encoding="utf-8")
    migrate_slot_data(Layout(root=root), data)
    report = migrate_slot_data(Layout(root=root), data)
    assert report.copied == 0 and report.slots == ()


def test_目标里残留的半截拷贝会被清掉重新拷(tmp_path):
    """上一轮在拷 ``manifest.json`` 的路上断了电,只留下一个 ``.part``。

    这份半截文件不能被当成"已经搬完"而跳过 —— 得先清掉,这一轮重新、
    完整地拷一遍。
    """
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("real", encoding="utf-8")
    (data / "runs" / "r1").mkdir(parents=True)
    (data / "runs" / "r1" / "manifest.json.part").write_text(
        "垃圾半截", encoding="utf-8")

    migrate_slot_data(Layout(root=root), data)

    assert not (data / "runs" / "r1" / "manifest.json.part").exists()
    assert (data / "runs" / "r1" / "manifest.json").read_text(
        encoding="utf-8") == "real"


def test_源改名撞上已有的部分搬迁结果时并进去而不是报错(tmp_path):
    """上一趟跑到一半就断了:``runs.migrated`` 已经在,``runs`` 也还在。

    这次跑完两边该合成一份,``runs`` 这个源目录本身不该再留着。
    """
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "runs.migrated" / "old").mkdir(parents=True)
    (a / "runs.migrated" / "old" / "x").write_text("x", encoding="utf-8")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("a", encoding="utf-8")

    migrate_slot_data(Layout(root=root), data)

    assert (a / "runs.migrated" / "old" / "x").read_text(encoding="utf-8") == "x"
    assert (a / "runs.migrated" / "r1" / "manifest.json").read_text(
        encoding="utf-8") == "a"
    assert not (a / "runs").exists()
