"""开机时先跑的那一段。新版本崩到跑不出自检时,靠它把机器捞回来。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_agent.engine.release import (
    MANIFEST_NAME,
    MAX_BOOT_ATTEMPTS,
    GuardAction,
    Layout,
    ReleaseError,
    activate,
    boot_guard,
    commit,
    current_name,
    read_pending,
    stage,
    take_guard_note,
    tree_sha256,
)

NOW = 1_700_000_000_000


def _pkg(root: Path, name: str) -> Path:
    where = root / name
    (where / "bin").mkdir(parents=True, exist_ok=True)
    (where / "bin" / "run.py").write_text("print(1)\n", encoding="utf-8")
    payload = {"name": name, "version": "0.2.0",
               "content_sha256": tree_sha256(where),
               "requires_mission_schema": 1, "built_at": "2026-09-20T03:11:00Z"}
    (where / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return where


def _两版(tmp_path: Path) -> Layout:
    """装好旧版并坐实,再切到新版(在途)。"""
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-06-a3f9c1"), now_ms=NOW)
    stage(layout, _pkg(tmp_path / "p2", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-06-a3f9c1", now_ms=NOW)
    commit(layout)
    return layout


def test_平常开机什么也不做(tmp_path):
    """绝大多数开机走这条。它必须又快又不改任何东西。"""
    layout = _两版(tmp_path)
    assert boot_guard(layout, now_ms=NOW) is GuardAction.OK
    assert current_name(layout) == "2026-09-06-a3f9c1"


def test_升级后第一次开机只数一次(tmp_path):
    layout = _两版(tmp_path)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    assert boot_guard(layout, now_ms=NOW + 1) is GuardAction.COUNTED
    pending = read_pending(layout)
    assert pending is not None and pending.attempts == 1
    # 数归数,链不许动 —— 新版还没被判死刑。
    assert current_name(layout) == "2026-09-20-77b2de"


def test_数够了就退回上一版(tmp_path):
    layout = _两版(tmp_path)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    for _ in range(MAX_BOOT_ATTEMPTS):
        assert boot_guard(layout, now_ms=NOW) is GuardAction.COUNTED
    assert boot_guard(layout, now_ms=NOW) is GuardAction.ROLLED_BACK
    assert current_name(layout) == "2026-09-06-a3f9c1"
    assert read_pending(layout) is None
    # 留一张条子给代理报站点(W00c5d 第三部分内部评审);读一次就没了。
    note = take_guard_note(layout)
    assert note is not None and (note["from"], note["to"]) == ("2026-09-20-77b2de",
                                                             "2026-09-06-a3f9c1")
    assert take_guard_note(layout) is None


def test_退回去之后再开机就安生了(tmp_path):
    """回滚之后标记清了,下一次开机不该再折腾一遍。"""
    layout = _两版(tmp_path)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    for _ in range(MAX_BOOT_ATTEMPTS + 1):
        boot_guard(layout, now_ms=NOW)
    assert boot_guard(layout, now_ms=NOW) is GuardAction.OK
    assert current_name(layout) == "2026-09-06-a3f9c1"


def test_自检过了就再也不数(tmp_path):
    """commit 之后标记没了。这是成功那条路 —— 守卫从此不再管这次升级。"""
    layout = _两版(tmp_path)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    boot_guard(layout, now_ms=NOW)
    commit(layout)
    assert boot_guard(layout, now_ms=NOW) is GuardAction.OK
    assert current_name(layout) == "2026-09-20-77b2de"


def test_装机第一次就起不来的话认输而不是死循环(tmp_path):
    """没有上一版可退。再数下去就是每次开机数一遍,永远数不出结果。"""
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    stage(layout, _pkg(tmp_path / "p1", "2026-09-20-77b2de"), now_ms=NOW)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    for _ in range(MAX_BOOT_ATTEMPTS):
        boot_guard(layout, now_ms=NOW)
    assert boot_guard(layout, now_ms=NOW) is GuardAction.GAVE_UP
    assert read_pending(layout) is None
    assert current_name(layout) == "2026-09-20-77b2de"


def test_链断了按盘上最新的一版修好(tmp_path):
    """换链途中断电的收场。current 指着一个不存在的目录。"""
    layout = _两版(tmp_path)
    layout.current.unlink()
    layout.current.symlink_to(layout.releases / "2026-01-01-000000",
                              target_is_directory=True)
    assert boot_guard(layout, now_ms=NOW) is GuardAction.REPAIRED
    assert current_name(layout) == "2026-09-20-77b2de"


def test_链丢了也按盘上最新的一版修好(tmp_path):
    layout = _两版(tmp_path)
    layout.current.unlink()
    assert boot_guard(layout, now_ms=NOW) is GuardAction.REPAIRED
    assert current_name(layout) == "2026-09-20-77b2de"


def test_有在途标记时修链要照标记修而不是照最新的修(tmp_path):
    """换链换了一半:标记写了,链没换成。这时候「最新的」正是那一版没验过的。"""
    layout = _两版(tmp_path)
    import d1max_agent.engine.release as rel

    def 炸(_layout, _name):
        raise OSError("换到一半断电")

    original, rel._point_current = rel._point_current, 炸
    try:
        try:
            activate(layout, "2026-09-20-77b2de", now_ms=NOW)
        except OSError:
            pass
    finally:
        rel._point_current = original
    layout.current.unlink()

    # 标记在,而且还没数够 —— 该修成 to 那一版,让它有机会证明自己。
    assert boot_guard(layout, now_ms=NOW) is GuardAction.COUNTED
    assert current_name(layout) == "2026-09-20-77b2de"


def test_盘上一版都没有就说清楚而不是装没事(tmp_path):
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    assert boot_guard(layout, now_ms=NOW) is GuardAction.BROKEN


def test_权限拒绝也不会把异常甩给调用方(tmp_path):
    """盘满/只读挂载/权限拒绝这类没法恢复的故障,一样得回一个 GuardAction,
    不能把异常甩给调用方 —— 调用方就是那个「机器起不起得来」的判断点。"""
    layout = _两版(tmp_path)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    for _ in range(MAX_BOOT_ATTEMPTS):
        boot_guard(layout, now_ms=NOW)

    import d1max_agent.engine.release as rel

    def 炸(_layout, _name):
        raise OSError(13, "permission denied")

    original, rel._point_current = rel._point_current, 炸
    try:
        # 数够了,src 非空 —— 该走 ROLLED_BACK,恰好在这一步炸出权限错误。
        assert boot_guard(layout, now_ms=NOW) is GuardAction.BROKEN
    finally:
        rel._point_current = original


def test_标记里的名字不合规也不会把异常甩给调用方(tmp_path):
    """pending.json 里的 to/from 要是不合规的名字(比如带路径穿越的),
    safe_name 会炸 ReleaseError —— 这也得被 boot_guard 兜住,而不是漏出去。"""
    layout = _两版(tmp_path)
    from d1max_agent.engine.release import Pending, write_pending

    write_pending(
        layout,
        Pending(to="2026-09-20-77b2de", src="../evil",
                attempts=MAX_BOOT_ATTEMPTS, at_ms=NOW, auto=False),
    )
    assert boot_guard(layout, now_ms=NOW) is GuardAction.BROKEN


def _带启动脚本(root: Path, name: str) -> Path:
    where = _pkg(root, name)
    (where / "deploy").mkdir()
    (where / "deploy" / "d1max-agent-start").write_text("#!/bin/sh\n", encoding="utf-8")
    payload = json.loads((where / MANIFEST_NAME).read_text(encoding="utf-8"))
    payload["content_sha256"] = tree_sha256(where, skip=MANIFEST_NAME)
    (where / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return where


def test_上一版是老服务那一代_守卫不退回去_留在新版并留条子(tmp_path):
    """W00c5e 内部评审阻断:老狗升上来,上一版的槽里没有代理的启动脚本(老服务那一代),
    老服务的单元也已经被装机脚本删了。新版代理连不上站点、没坐实,守卫数够次数退回老槽 ——
    代理在那一版里起不来,狗就再也没有服务。**退不回去的就不退**:留在新版,清掉在途标记,
    留一张条子说明,等人来看(网络好了代理自己就连上了)。"""
    from d1max_agent.engine.release import rollback, take_guard_note
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    old, new = "2026-09-06-a3f9c1", "2026-09-26-77b2de"
    stage(layout, _pkg(tmp_path / "p1", old), now_ms=NOW)
    stage(layout, _带启动脚本(tmp_path / "p2", new), now_ms=NOW)
    activate(layout, old, now_ms=NOW)
    commit(layout)
    activate(layout, new, now_ms=NOW)
    with pytest.raises(ReleaseError, match="启动脚本"):
        rollback(layout, now_ms=NOW)
    assert current_name(layout) == new and read_pending(layout) is not None
    for _ in range(MAX_BOOT_ATTEMPTS):
        assert boot_guard(layout, now_ms=NOW) is GuardAction.COUNTED
    assert boot_guard(layout, now_ms=NOW) is GuardAction.NO_FALLBACK
    assert current_name(layout) == new, "留在新版"
    assert read_pending(layout) is None
    assert boot_guard(layout, now_ms=NOW) is GuardAction.OK, "下一次开机不再折腾"
    note = take_guard_note(layout)
    assert note is not None and note["no_fallback"] is True and note["from"] == new
