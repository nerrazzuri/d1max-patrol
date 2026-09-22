"""数据根:巡检数据搬出版本槽之后落在哪。"""

from __future__ import annotations

import collections
import json
import shutil
from pathlib import Path

import pytest

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


def test_撞名时新槽赢老槽的旧副本(tmp_path):
    """两个槽都有 ``queue.jsonl``。按名字从新到旧搬,新槽先搬进数据根,
    老槽那份撞上 ``target.exists()`` 被跳过 —— 数据根里留的是新槽的内容,
    老槽自己的那份原样改名进 ``*.migrated``,人想核对随时能看。"""
    root = tmp_path / "opt"
    data = tmp_path / "var"
    old = _slot(root, "2026-09-01-aaaaaa")
    new = _slot(root, "2026-09-06-a3f9c1")
    (old / "queue.jsonl").write_text("old", encoding="utf-8")
    (new / "queue.jsonl").write_text("new", encoding="utf-8")

    migrate_slot_data(Layout(root=root), data)

    assert (data / "queue.jsonl").read_text(encoding="utf-8") == "new"
    assert (old / "queue.jsonl.migrated").read_text(encoding="utf-8") == "old"
    assert not (old / "queue.jsonl").exists()
    assert not (new / "queue.jsonl").exists()


def test_手写的任务文件也在搬的范围内(tmp_path):
    """``missions/*.yaml`` 是 ``PUT /api/missions`` 写出来的,包里不带 —— 它跟
    runs 一样是槽里的运行时数据,不搬就随着 prune 一起没了。"""
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "missions").mkdir()
    (a / "missions" / "night.yaml").write_text("id: night\n", encoding="utf-8")

    report = migrate_slot_data(Layout(root=root), data)

    assert (data / "missions" / "night.yaml").read_text(encoding="utf-8") == "id: night\n"
    assert (a / "missions.migrated").is_dir() and not (a / "missions").exists()
    assert report.copied == 1


# --------------------------------------------------------- 搬迁前的盘余量预检


def test_盘不够就整体拒绝什么都不碰(tmp_path, monkeypatch):
    """先算总共要搬多少字节,盘上剩的不够就报错 —— 一个文件都不许碰。

    不这么做的话,搬一半才发现盘满,槽里的源已经有一部分改名成
    ``*.migrated`` 了,数据根里也躺着半截数据,两头都不干净。
    """
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("real", encoding="utf-8")

    余量 = collections.namedtuple("u", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: 余量(total=0, used=0, free=0))

    with pytest.raises(OSError):
        migrate_slot_data(Layout(root=root), data)

    assert (a / "runs" / "r1" / "manifest.json").exists()
    assert not (a / "runs.migrated").exists()
    assert not data.exists()


def test_没有数据要搬时不查盘(tmp_path, monkeypatch):
    """``needed == 0`` 跳过预检 —— 空槽不该因为盘满被拦下来(反正也没什么要搬的)。"""
    root = tmp_path / "opt"
    data = tmp_path / "var"
    _slot(root, "2026-09-01-aaaaaa")

    def _boom(_path):
        raise AssertionError("needed == 0 不该去查盘余量")

    monkeypatch.setattr(shutil, "disk_usage", _boom)

    report = migrate_slot_data(Layout(root=root), data)
    assert report.copied == 0
