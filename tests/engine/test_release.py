"""盘上的两份加一条链 —— 读的那一半。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from d1max_patrol.engine.release import (
    MANIFEST_NAME,
    Layout,
    Pending,
    ReleaseError,
    current_name,
    installed,
    read_pending,
    safe_name,
    tree_sha256,
    verify_package,
    write_pending,
)


def _pkg(root: Path, name: str, *, files: dict[str, str] | None = None,
         schema: int = 1, sha: str | None = None) -> Path:
    """造一个包目录:先摆文件,再按真实内容算指纹写进 release.json。"""
    where = root / name
    where.mkdir(parents=True, exist_ok=True)
    for rel, text in (files or {"bin/run.py": "print(1)\n"}).items():
        path = where / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    payload = {
        "name": name,
        "version": "0.2.0",
        "content_sha256": sha if sha is not None else tree_sha256(where),
        "requires_mission_schema": schema,
        "built_at": "2026-09-20T03:11:00Z",
    }
    (where / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return where


def test_树指纹把路径也算进去(tmp_path):
    a = _pkg(tmp_path / "a", "2026-09-20-77b2de", files={"x": "同样的内容\n"})
    b = _pkg(tmp_path / "b", "2026-09-20-77b2de", files={"y": "同样的内容\n"})
    # 内容一模一样,只是文件名不同 —— 指纹必须不同,否则改名字这种改动是隐形的。
    assert tree_sha256(a) != tree_sha256(b)


def test_树指纹不把release_json自己算进去(tmp_path):
    where = _pkg(tmp_path, "2026-09-20-77b2de")
    before = tree_sha256(where)
    (where / MANIFEST_NAME).write_text('{"改了": 1}', encoding="utf-8")
    assert tree_sha256(where) == before


def test_包的哈希对不上就拒收(tmp_path):
    where = _pkg(tmp_path, "2026-09-20-77b2de", sha="0" * 64)
    with pytest.raises(ReleaseError, match="哈希对不上"):
        verify_package(where)


def test_包里少了release_json就拒收(tmp_path):
    where = tmp_path / "光秃秃"
    where.mkdir()
    with pytest.raises(ReleaseError, match=MANIFEST_NAME):
        verify_package(where)


def test_合格的包读得出版本与schema(tmp_path):
    where = _pkg(tmp_path, "2026-09-20-77b2de", schema=3)
    got = verify_package(where)
    assert got.name == "2026-09-20-77b2de"
    assert got.version == "0.2.0"
    assert got.requires_mission_schema == 3


def test_release_json里的名字跟目录名不一致就拒收(tmp_path):
    where = _pkg(tmp_path, "2026-09-20-77b2de")
    raw = json.loads((where / MANIFEST_NAME).read_text(encoding="utf-8"))
    raw["name"] = "2026-01-01-000000"
    (where / MANIFEST_NAME).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ReleaseError, match="目录名"):
        verify_package(where)


@pytest.mark.parametrize("bad", ["../跑了", "a/b", "", "带空格 的", "..", "x" * 80])
def test_名字不合规就拒绝(bad):
    with pytest.raises(ReleaseError):
        safe_name(bad)


def test_没有current时问是哪一版回空串(tmp_path):
    layout = Layout(root=tmp_path)
    layout.releases.mkdir(parents=True)
    assert current_name(layout) == ""


def test_current断着的时候也回得出名字(tmp_path):
    """链还在,指的目录没了。名字要读得出来 —— 守卫靠它才知道该修成什么。"""
    layout = Layout(root=tmp_path)
    layout.releases.mkdir(parents=True)
    layout.current.symlink_to(layout.releases / "2026-09-20-77b2de",
                              target_is_directory=True)
    assert not layout.current.is_dir()
    assert current_name(layout) == "2026-09-20-77b2de"


def test_装了几版就列出几版且排过序(tmp_path):
    layout = Layout(root=tmp_path)
    for name in ("2026-09-20-77b2de", "2026-09-06-a3f9c1"):
        _pkg(layout.releases, name)
    (layout.releases / "不是版本的目录").mkdir()
    assert installed(layout) == ("2026-09-06-a3f9c1", "2026-09-20-77b2de")


def test_pending用from当线上的键(tmp_path):
    layout = Layout(root=tmp_path)
    layout.root.mkdir(parents=True, exist_ok=True)
    write_pending(layout, Pending(to="新", src="旧", attempts=1,
                                  at_ms=1_700_000_000_000, auto=False))
    raw = json.loads(layout.pending.read_text(encoding="utf-8"))
    assert raw["from"] == "旧" and raw["to"] == "新" and raw["attempts"] == 1
    assert read_pending(layout) == Pending(to="新", src="旧", attempts=1,
                                           at_ms=1_700_000_000_000, auto=False)


def test_pending坏了当没有(tmp_path):
    """守卫开机时读它。半个 JSON 不该让守卫炸掉 —— 炸了狗就起不来。"""
    layout = Layout(root=tmp_path)
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.pending.write_text("{半个", encoding="utf-8")
    assert read_pending(layout) is None


def test_readlink在windows上带前缀也取得对名字(tmp_path):
    layout = Layout(root=tmp_path)
    target = layout.releases / "2026-09-20-77b2de"
    target.mkdir(parents=True)
    layout.current.symlink_to(target, target_is_directory=True)
    # Windows 上 os.readlink 回的是 \\?\D:\... ,直接切字符串会切错。
    assert Path(os.readlink(layout.current)).name == current_name(layout)
