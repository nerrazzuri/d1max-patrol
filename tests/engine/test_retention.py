"""归档的分类:多大、多老、安定没有、传过没有。

**这一层只读盘。** 删除计划在 `test_retention_sweep.py`(Task 5)。分开是
因为两件事失败的方式完全不同:这里的坑是"读错了",那里的坑是"删错了"。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from d1max_agent.engine.retention import SETTLE_HOURS, is_settled

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _run(root: Path, mission: str, started: datetime, *, suffix: str = "",
         retention: int | None = 90, done: bool = True,
         photo_bytes: int = 100) -> Path:
    """手搭一个归档目录。``retention=None`` 表示 policy 里干脆没这一项。"""
    stamp = started.strftime("%Y%m%dT%H%M%SZ") + suffix
    run = root / mission / stamp
    (run / "photos").mkdir(parents=True)
    (run / "photos" / "P1__front__x.jpg").write_bytes(b"x" * photo_bytes)
    policy: dict[str, Any] = {}
    if retention is not None:
        policy["retention_days"] = retention
    (run / "manifest.json").write_text(json.dumps({
        "mission": {"mission": mission, "map_id": "m", "waypoints": [],
                    "policy": policy},
        "started_at": stamp,
        "fingerprint": {},
        "summary": {"visited": 1} if done else {},
    }, ensure_ascii=False), encoding="utf-8")
    return run


def test_跑完了就算安定(tmp_path):
    run = _run(tmp_path, "巡检一号", NOW - timedelta(minutes=5), done=True)
    assert is_settled(run, now=NOW) is True


def test_刚开跑还没写完的不算安定(tmp_path):
    # 正在写的那一趟不能删。发件箱不问引擎,只从盘上看(另外还排除引擎正在写的那一趟)。
    run = _run(tmp_path, "巡检一号", NOW - timedelta(minutes=5), done=False)
    assert is_settled(run, now=NOW) is False


def test_崩在半路但已经过了安定期的算安定(tmp_path):
    # 没有 summary 说明崩了。但 24 小时之后已经没人在写它了,不算就永远删不掉。
    run = _run(tmp_path, "巡检一号", NOW - timedelta(hours=SETTLE_HOURS + 1), done=False)
    assert is_settled(run, now=NOW) is True


def test_目录名带撞名序号或随机后缀也认(tmp_path):
    for suffix in ("-2", "-004217", "-0042172"):
        run = _run(tmp_path, f"巡检{suffix}", NOW - timedelta(hours=SETTLE_HOURS + 1),
                   suffix=suffix, done=False)
        assert is_settled(run, now=NOW) is True, suffix


def test_目录名不是时间戳_manifest坏了_都当不安定或按时刻算(tmp_path):
    odd = tmp_path / "巡检一号" / "not-a-stamp"
    odd.mkdir(parents=True)
    assert is_settled(odd, now=NOW) is False, "不是我们建的目录,一律不碰"
    run = _run(tmp_path, "巡检二号", NOW - timedelta(minutes=5), done=True)
    (run / "manifest.json").write_text("{坏的", encoding="utf-8")
    assert is_settled(run, now=NOW) is False, "manifest 坏了、又刚开跑:不安定"
