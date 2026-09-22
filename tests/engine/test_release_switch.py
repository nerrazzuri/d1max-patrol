"""换链那一半。升级和回滚是同一个动作,只是方向相反。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_patrol.engine.release import (
    KEEP_RELEASES,
    MANIFEST_NAME,
    Layout,
    ReleaseError,
    activate,
    commit,
    current_name,
    installed,
    prune,
    read_pending,
    rollback,
    stage,
    tree_sha256,
)

NOW = 1_700_000_000_000


def _pkg(root: Path, name: str, *, body: str = "print(1)\n") -> Path:
    where = root / name
    (where / "bin").mkdir(parents=True, exist_ok=True)
    (where / "bin" / "run.py").write_text(body, encoding="utf-8")
    payload = {"name": name, "version": "0.2.0",
               "content_sha256": tree_sha256(where),
               "requires_mission_schema": 1, "built_at": "2026-09-20T03:11:00Z"}
    (where / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return where


def _layout(tmp_path: Path) -> Layout:
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    return layout


def test_落槽把整棵树拷进去(tmp_path):
    layout = _layout(tmp_path)
    got = stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=NOW)
    assert got.name == "2026-09-20-77b2de"
    assert (layout.releases / got.name / "bin" / "run.py").is_file()
    assert installed(layout) == ("2026-09-20-77b2de",)


def test_坏包落不了槽而且不留半个目录(tmp_path):
    layout = _layout(tmp_path)
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    (pkg / "bin" / "run.py").write_text("偷偷改了\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="哈希对不上"):
        stage(layout, pkg, now_ms=NOW)
    # 半个目录比没有目录更坏:它看着像装上了。
    assert installed(layout) == ()


def test_同一版重复落槽是幂等的(tmp_path):
    """装机脚本要可重放。同一个包跑两遍不该炸,也不该留下垃圾。"""
    layout = _layout(tmp_path)
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    stage(layout, pkg, now_ms=NOW)
    stage(layout, pkg, now_ms=NOW)
    assert installed(layout) == ("2026-09-20-77b2de",)


def test_换链之前标记就已经在盘上了(tmp_path):
    """本卷的枢纽。用一个换链途中炸掉的桩来证。"""
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-06-a3f9c1"), now_ms=NOW)
    stage(layout, _pkg(tmp_path / "p2", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-06-a3f9c1", now_ms=NOW)
    commit(layout)

    import d1max_patrol.engine.release as rel

    def 炸(_layout, _name):
        raise OSError("换到一半断电")

    original, rel._point_current = rel._point_current, 炸
    try:
        with pytest.raises(OSError):
            activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    finally:
        rel._point_current = original

    pending = read_pending(layout)
    assert pending is not None
    assert pending.to == "2026-09-20-77b2de" and pending.src == "2026-09-06-a3f9c1"


def test_换链之后current指着新版而且标记还在(tmp_path):
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-06-a3f9c1"), now_ms=NOW)
    stage(layout, _pkg(tmp_path / "p2", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-06-a3f9c1", now_ms=NOW)
    commit(layout)
    got = activate(layout, "2026-09-20-77b2de", now_ms=NOW, auto=True)
    assert current_name(layout) == "2026-09-20-77b2de"
    assert got.src == "2026-09-06-a3f9c1" and got.auto is True
    # 还没坐实 —— commit 之前标记必须在,不然守卫就没得数了。
    assert read_pending(layout) is not None


def test_装机第一次没有上一版(tmp_path):
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-20-77b2de"), now_ms=NOW)
    got = activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    assert got.src == ""


def test_切到没装的版本要拒绝而且不留标记(tmp_path):
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-20-77b2de"), now_ms=NOW)
    with pytest.raises(ReleaseError):
        activate(layout, "2026-01-01-aaaaaa", now_ms=NOW)
    assert read_pending(layout) is None


def test_切到正在跑的那版要拒绝(tmp_path):
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    commit(layout)
    with pytest.raises(ReleaseError, match="已经是"):
        activate(layout, "2026-09-20-77b2de", now_ms=NOW)


def test_回滚就是反着换一次链(tmp_path):
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-06-a3f9c1"), now_ms=NOW)
    stage(layout, _pkg(tmp_path / "p2", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-06-a3f9c1", now_ms=NOW)
    commit(layout)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    back = rollback(layout, now_ms=NOW + 1000)
    assert back == "2026-09-06-a3f9c1"
    assert current_name(layout) == "2026-09-06-a3f9c1"
    assert read_pending(layout) is None


def test_没有在途升级时回滚要拒绝(tmp_path):
    """回滚的扳机只有一个(§7.3)。没有在途的升级就没有该退回哪儿这回事。"""
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    commit(layout)
    with pytest.raises(ReleaseError, match="没有在途"):
        rollback(layout, now_ms=NOW)


def test_装机第一次失败没得退(tmp_path):
    layout = _layout(tmp_path)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    with pytest.raises(ReleaseError, match="上一版"):
        rollback(layout, now_ms=NOW)


def test_只留两份多的删掉(tmp_path):
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    dropped = commit(layout)
    assert dropped == ("2026-09-01-aaaaaa",)
    assert len(installed(layout)) == KEEP_RELEASES


def test_在跑的那版和退路那版绝不删(tmp_path):
    """按名字排序的话最老的可能正好是在跑的那版 —— 删了就没得跑了。"""
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    activate(layout, "2026-09-01-aaaaaa", now_ms=NOW)
    dropped = prune(layout, keep=1)
    assert "2026-09-01-aaaaaa" not in dropped
    assert "2026-09-01-aaaaaa" in installed(layout)


def test_槽里还有巡检数据就不删(tmp_path, caplog):
    """W01 的保险:数据本该在 /var/lib/d1max,但万一老机器没迁干净,
    宁可多占盘也不删证据。"""
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    old_runs = layout.releases / "2026-09-01-aaaaaa" / "runs" / "20260901-0800"
    old_runs.mkdir(parents=True)
    (old_runs / "manifest.json").write_text("{}", encoding="utf-8")
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    dropped = commit(layout)
    assert dropped == ()
    assert "2026-09-01-aaaaaa" in installed(layout)
    assert (old_runs / "manifest.json").exists()
    assert "还有巡检数据" in caplog.text


def test_槽里只有queue_jsonl也不删(tmp_path):
    """``SLOT_DATA_ITEMS`` 覆盖四样,不止 ``runs``:``runs/`` 被手动删掉、但
    ``queue.jsonl`` 还在的槽,一样不能被当成"已经迁完"删掉。"""
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    (layout.releases / "2026-09-01-aaaaaa" / "queue.jsonl").write_text(
        '{"k":1}\n', encoding="utf-8")
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    dropped = commit(layout)
    assert dropped == ()
    assert "2026-09-01-aaaaaa" in installed(layout)


def test_槽里runs目录是空的照删(tmp_path):
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    (layout.releases / "2026-09-01-aaaaaa" / "runs").mkdir()
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    assert commit(layout) == ("2026-09-01-aaaaaa",)


def test_槽里只有手写任务也不删(tmp_path):
    """``missions/*.yaml`` 是运行时写的,不在包里 —— 跟 runs 一样受保险保护。"""
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    m = layout.releases / "2026-09-01-aaaaaa" / "missions"
    m.mkdir()
    (m / "night.yaml").write_text("id: night\n", encoding="utf-8")
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    assert commit(layout) == ()
    assert (m / "night.yaml").exists()
