"""归档的分类:多大、多老、安定没有、传过没有。

**这一层只读盘。** 删除计划在 `test_retention_sweep.py`(Task 5)。分开是
因为两件事失败的方式完全不同:这里的坑是"读错了",那里的坑是"删错了"。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from d1max_patrol.engine.mission import MIN_RETENTION_DAYS
from d1max_patrol.engine.retention import (
    DEFAULT_RETENTION_DAYS,
    EXPORTED_REL,
    MIN_NOTICE_DAYS,
    SETTLE_HOURS,
    SWEEP_TARGET_RATIO,
    UPLOADED_REL,
    RunInfo,
    bytes_to_free,
    mark_exported,
    mark_uploaded,
    scan_runs,
)

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


def test_扫出来的字段都对(tmp_path):
    _run(tmp_path, "巡检一号", NOW - timedelta(days=3), retention=30)
    (info,) = scan_runs(tmp_path)
    assert info.mission == "巡检一号"
    assert info.started_at == NOW - timedelta(days=3)
    assert info.retention_days == 30
    assert info.size_bytes >= 100
    assert info.settled is True
    assert info.uploaded is False
    assert info.exported is False


def test_最老的排在最前面(tmp_path):
    _run(tmp_path, "甲", NOW - timedelta(days=1))
    _run(tmp_path, "乙", NOW - timedelta(days=9))
    _run(tmp_path, "丙", NOW - timedelta(days=5))
    assert [i.mission for i in scan_runs(tmp_path)] == ["乙", "丙", "甲"]


def test_目录名带撞名后缀也要认(tmp_path):
    # `archive._make_dir` 撞上同名会追加 -2、-3。不认它的话,一天里跑第二趟
    # 的那些归档会被整个漏掉 —— 而且是**静默**漏掉。
    _run(tmp_path, "巡检一号", NOW - timedelta(days=3), suffix="-2")
    (info,) = scan_runs(tmp_path)
    assert info.started_at == NOW - timedelta(days=3)


def test_目录名根本不是时间戳就跳过(tmp_path):
    (tmp_path / "巡检一号" / "临时目录").mkdir(parents=True)
    _run(tmp_path, "巡检一号", NOW - timedelta(days=3))
    assert len(scan_runs(tmp_path)) == 1


def test_manifest坏了不能让整次扫描炸(tmp_path):
    # `archive.read_manifest` 遇到坏 JSON 是**抛**,不是回 None。一份坏
    # manifest 不能让清扫器从此瘫掉 —— 那台狗的盘会一直满下去。
    bad = _run(tmp_path, "巡检一号", NOW - timedelta(days=3))
    (bad / "manifest.json").write_text("{不是 json", encoding="utf-8")
    (info,) = scan_runs(tmp_path)
    assert info.retention_days == DEFAULT_RETENTION_DAYS
    assert info.mission == "巡检一号"      # 目录结构给的,不靠 manifest


def test_policy里没写保留期就用默认(tmp_path):
    _run(tmp_path, "巡检一号", NOW - timedelta(days=3), retention=None)
    (info,) = scan_runs(tmp_path)
    assert info.retention_days == DEFAULT_RETENTION_DAYS == 90


def test_跑完了就算安定(tmp_path):
    _run(tmp_path, "巡检一号", NOW - timedelta(minutes=5), done=True)
    assert scan_runs(tmp_path)[0].settled is True


def test_刚开跑还没写完的不算安定(tmp_path):
    # 正在写的那一趟不能删。清扫器不认识引擎(单门不变式),只能从盘上看。
    _run(tmp_path, "巡检一号", NOW - timedelta(minutes=5), done=False)
    assert scan_runs(tmp_path, now=NOW)[0].settled is False


def test_崩在半路但已经过了安定期的算安定(tmp_path):
    # 没有 summary 说明崩了。但 24 小时之后已经没人在写它了,不算就永远删不掉。
    _run(tmp_path, "巡检一号", NOW - timedelta(hours=SETTLE_HOURS + 1), done=False)
    assert scan_runs(tmp_path, now=NOW)[0].settled is True


def test_打过上传标记就算已传(tmp_path):
    run = _run(tmp_path, "巡检一号", NOW - timedelta(days=3))
    assert mark_uploaded(run) == run / UPLOADED_REL
    assert scan_runs(tmp_path)[0].uploaded is True


def test_重复打标记不算错(tmp_path):
    run = _run(tmp_path, "巡检一号", NOW - timedelta(days=3))
    mark_uploaded(run)
    mark_uploaded(run)
    assert scan_runs(tmp_path)[0].uploaded is True


def test_打过导出标记就算已导出(tmp_path):
    # 两个标记是**两个文件、两件事**:传到服务器 vs 拉到客户手机上。
    run = _run(tmp_path, "巡检一号", NOW - timedelta(days=3))
    assert mark_exported(run) == run / EXPORTED_REL
    info = scan_runs(tmp_path)[0]
    assert info.exported is True
    assert info.uploaded is False


def test_传走了不等于导出过(tmp_path):
    run = _run(tmp_path, "巡检一号", NOW - timedelta(days=3))
    mark_uploaded(run)
    info = scan_runs(tmp_path)[0]
    assert info.uploaded is True
    assert info.exported is False


def test_线格式带得上两个标记(tmp_path):
    run = _run(tmp_path, "巡检一号", NOW - timedelta(days=3))
    mark_exported(run)
    wire = scan_runs(tmp_path)[0].to_wire()
    assert wire["uploaded"] is False
    assert wire["exported"] is True


def test_runs根目录不存在就是空的(tmp_path):
    assert scan_runs(tmp_path / "从来没有过") == []


# ----------------------------------------------------------- 过期与水位


def _info(days_ago: float, *, retention: int = 90, size: int = 1000,
          settled: bool = True, uploaded: bool = False) -> RunInfo:
    return RunInfo(path=Path("/tmp/x"), mission="巡检一号",
                   started_at=NOW - timedelta(days=days_ago),
                   retention_days=retention, size_bytes=size,
                   settled=settled, uploaded=uploaded)


def test_没到期就是没到期():
    info = _info(89.0)
    assert info.is_expired(NOW) is False
    assert info.days_left(NOW) == pytest.approx(1.0)


def test_到期那一刻就算过期():
    info = _info(90.0)
    assert info.is_expired(NOW) is True
    assert info.days_left(NOW) == pytest.approx(0.0)


def test_保留期短的先过期():
    assert _info(31.0, retention=30).is_expired(NOW) is True
    assert _info(31.0, retention=90).is_expired(NOW) is False


def test_预告期是七天并且不超过保留期下限():
    # 两个 7 不是同一个数,只是恰好相等。但**预告期不能超过保留期下限** ——
    # 超了的话,每一趟归档一落地就已经过了预告期,预告整个失去意义。
    # 这一条钉的就是那个关系,谁改一边都会红。
    assert MIN_NOTICE_DAYS == 7
    assert MIN_NOTICE_DAYS <= MIN_RETENTION_DAYS


def test_已经在水位以下就不用腾地方():
    assert bytes_to_free(used_bytes=60, total_bytes=100) == 0
    assert bytes_to_free(used_bytes=70, total_bytes=100) == 0


def test_超了水位要腾到目标线以下():
    assert SWEEP_TARGET_RATIO == 0.70
    assert bytes_to_free(used_bytes=95, total_bytes=100) == 25


def test_盘是空的不会去除以零():
    assert bytes_to_free(used_bytes=0, total_bytes=0) == 0
