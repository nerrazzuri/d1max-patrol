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


def _带启动脚本(root: Path, name: str, *, mode: int = 0o755) -> Path:
    """代理那一代的包。启动脚本照真的那样带执行位(git 里是 100755);``mode`` 给坏的用。"""
    where = _pkg(root, name)
    (where / "deploy").mkdir()
    (where / "deploy" / "d1max-agent-start").write_text("#!/bin/sh\n", encoding="utf-8")
    (where / "deploy" / "d1max-agent-start").chmod(mode)
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
    activate(layout, old, now_ms=NOW)
    commit(layout)
    stage(layout, _带启动脚本(tmp_path / "p2", new), now_ms=NOW)
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


def _老与新(tmp_path: Path, *, new_mode: int = 0o755) -> tuple[Layout, str, str]:
    """老服务那一代(没有启动脚本)与代理那一代各落一个槽,链指着老的、坐实。"""
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    old, new = "2026-09-06-a3f9c1", "2026-09-26-77b2de"
    # 照现场的先后:老狗先装着老服务那一代,之后才落代理那一代(盘上已经有代理那一代时,不知道在跑
    # 哪一版就不许切到老服务那一代 —— 见 test_不知道在跑哪一版_…)。
    stage(layout, _pkg(tmp_path / "p1", old), now_ms=NOW)
    activate(layout, old, now_ms=NOW)
    commit(layout)
    stage(layout, _带启动脚本(tmp_path / "p2", new, mode=new_mode), now_ms=NOW)
    return layout, old, new


def test_新版在跑_点名切回老服务那一代的槽_共同入口就拒(tmp_path):
    """W00c5 外审阻断 1:「不退回老服务那一代」原来只在「上一版」和在途退回里查,点名切(站点的
    切版本、命令行 ``release activate``)照样切过去 —— 代理单元找不到启动脚本,狗就没有服务了。
    查放进 ``activate`` 本身:所有换链的路都从这儿过。"""
    layout, old, new = _老与新(tmp_path)
    activate(layout, new, now_ms=NOW)
    commit(layout)
    with pytest.raises(ReleaseError, match="启动脚本"):
        activate(layout, old, now_ms=NOW)
    assert current_name(layout) == new, "链不动"
    assert read_pending(layout) is None, "不留在途标记:守卫下一次开机不该有事可做"


def test_槽里的启动脚本没有执行位_不切(tmp_path):
    """代理单元的 ExecStart 就是这个脚本:没有执行位 systemd 起不来(203/EXEC)。落槽会补执行位
    (见下一条),这里是落槽之后被改掉的。"""
    layout, old, new = _老与新(tmp_path)
    (layout.releases / new / "deploy" / "d1max-agent-start").chmod(0o644)
    with pytest.raises(ReleaseError, match="执行位"):
        activate(layout, new, now_ms=NOW)
    assert current_name(layout) == old and read_pending(layout) is None


def test_落槽补上执行位_U盘拷掉了也能切_重落一遍也补(tmp_path):
    """现场是 U 盘带包(装机清单):FAT/exFAT 不存执行位,包里的启动脚本到狗上是 0644。
    执行位不进指纹,落槽时补上;以前落下的 0644 槽,重跑装机脚本(再落一遍)也补。"""
    layout, old, new = _老与新(tmp_path, new_mode=0o644)
    start = layout.releases / new / "deploy" / "d1max-agent-start"
    assert start.stat().st_mode & 0o111 == 0o111
    activate(layout, new, now_ms=NOW)
    assert current_name(layout) == new
    start.chmod(0o644)
    stage(layout, tmp_path / "p2" / new, now_ms=NOW)
    assert start.stat().st_mode & 0o111 == 0o111


def test_目标版的启动脚本是链接_不切(tmp_path):
    """包里只许普通文件(站点打包、狗上解包都不收链接);落槽之后被换成链接的也不认。"""
    layout, old, new = _老与新(tmp_path)
    start = layout.releases / new / "deploy" / "d1max-agent-start"
    real = tmp_path / "别处的脚本"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    real.chmod(0o755)
    start.unlink()
    start.symlink_to(real)
    with pytest.raises(ReleaseError, match="启动脚本"):
        activate(layout, new, now_ms=NOW)
    assert current_name(layout) == old


def test_老服务那一代之间照旧能切_代理那一代之间照旧能切(tmp_path):
    layout = _两版(tmp_path)
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    assert current_name(layout) == "2026-09-20-77b2de"
    layout2 = Layout(root=tmp_path / "opt2")
    layout2.releases.mkdir(parents=True)
    a, b = "2026-09-25-aaaaaa", "2026-09-26-bbbbbb"
    stage(layout2, _带启动脚本(tmp_path / "q1", a), now_ms=NOW)
    stage(layout2, _带启动脚本(tmp_path / "q2", b), now_ms=NOW)
    activate(layout2, a, now_ms=NOW)
    commit(layout2)
    activate(layout2, b, now_ms=NOW)
    commit(layout2)
    activate(layout2, a, now_ms=NOW)
    assert current_name(layout2) == a


def test_上一版的启动脚本坏了_守卫也不退回去(tmp_path):
    """守卫退回走的是同一道判据:上一版的启动脚本没有执行位,退过去一样起不来。"""
    from d1max_agent.engine.release import rollback
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    a, b = "2026-09-25-aaaaaa", "2026-09-26-bbbbbb"
    stage(layout, _带启动脚本(tmp_path / "q1", a), now_ms=NOW)
    stage(layout, _带启动脚本(tmp_path / "q2", b), now_ms=NOW)
    activate(layout, a, now_ms=NOW)
    commit(layout)
    activate(layout, b, now_ms=NOW)
    (layout.releases / a / "deploy" / "d1max-agent-start").chmod(0o644)
    with pytest.raises(ReleaseError, match="启动脚本"):
        rollback(layout, now_ms=NOW)
    for _ in range(MAX_BOOT_ATTEMPTS):
        assert boot_guard(layout, now_ms=NOW) is GuardAction.COUNTED
    assert boot_guard(layout, now_ms=NOW) is GuardAction.NO_FALLBACK
    assert current_name(layout) == b


def test_重落时槽里的启动脚本是链接_不跟过去补执行位_照样不切(tmp_path):
    """补执行位只动槽里的普通文件:跟着链接 chmod 会改到槽外面的文件。"""
    layout, old, new = _老与新(tmp_path)
    start = layout.releases / new / "deploy" / "d1max-agent-start"
    outside = tmp_path / "槽外面的文件"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o644)
    start.unlink()
    start.symlink_to(outside)
    stage(layout, tmp_path / "p2" / new, now_ms=NOW)
    assert outside.stat().st_mode & 0o777 == 0o644, "槽外面的文件不许被改"
    with pytest.raises(ReleaseError, match="启动脚本"):
        activate(layout, new, now_ms=NOW)


def test_链丢了守卫修链_修到起得来的最新一版_不挑排最后的老槽(tmp_path):
    """W00c5 修复内部评审(应修 1):同一天打的包按哈希排序,老服务那一代的槽可能排在最后。
    链丢了、没有在途标记,守卫以前按名字取最后一个 —— 修到老槽,下次开机说 OK,代理却起不来。"""
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    agent, legacy = "2026-09-26-1aaaaa", "2026-09-26-fbbbbb"
    stage(layout, _带启动脚本(tmp_path / "q1", agent), now_ms=NOW)
    stage(layout, _pkg(tmp_path / "q2", legacy), now_ms=NOW)
    activate(layout, agent, now_ms=NOW)
    commit(layout)
    layout.current.unlink()
    assert boot_guard(layout, now_ms=NOW) is GuardAction.REPAIRED
    assert current_name(layout) == agent


def test_守卫修链_排最后的那版启动脚本坏了_修到旁边好的那版(tmp_path):
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    good, bad = "2026-09-25-aaaaaa", "2026-09-26-bbbbbb"
    stage(layout, _带启动脚本(tmp_path / "q1", good), now_ms=NOW)
    stage(layout, _带启动脚本(tmp_path / "q2", bad), now_ms=NOW)
    (layout.releases / bad / "deploy" / "d1max-agent-start").chmod(0o644)
    assert boot_guard(layout, now_ms=NOW) is GuardAction.REPAIRED
    assert current_name(layout) == good


def test_不知道在跑哪一版_盘上有代理那一代_就不许切到老服务那一代(tmp_path):
    """W00c5 修复内部评审(小问题 a):没有链、链指着已经删掉的槽时,以前按「老服务那一代」放行。"""
    layout, old, new = _老与新(tmp_path)
    activate(layout, new, now_ms=NOW)
    commit(layout)
    layout.current.unlink()                                  # 没有链
    with pytest.raises(ReleaseError, match="启动脚本"):
        activate(layout, old, now_ms=NOW)
    gone = "2026-09-27-cccccc"                               # 链指着一个已经没了的槽
    layout.current.symlink_to(layout.releases / gone, target_is_directory=True)
    with pytest.raises(ReleaseError, match="启动脚本"):
        activate(layout, old, now_ms=NOW)
    activate(layout, new, now_ms=NOW)                        # 切到起得来的照旧能切
    assert current_name(layout) == new


def test_执行位不全_不算起得来(tmp_path):
    """只看「有没有哪个执行位」的话 0o010、0o001 也算过,服务账号其实执行不了。"""
    layout, old, new = _老与新(tmp_path)
    start = layout.releases / new / "deploy" / "d1max-agent-start"
    for mode in (0o710, 0o010, 0o001):
        start.chmod(mode)
        with pytest.raises(ReleaseError, match="执行位"):
            activate(layout, new, now_ms=NOW)
    assert current_name(layout) == old


def test_包里的deploy是个文件_落槽不因补执行位而失败(tmp_path):
    """补执行位补不上不拦落槽:``deploy`` 是个普通文件时,找启动脚本是 NotADirectoryError。"""
    layout = Layout(root=tmp_path / "opt")
    layout.releases.mkdir(parents=True)
    where = _pkg(tmp_path / "q", "2026-09-26-dddddd")
    (where / "deploy").write_text("不是目录\n", encoding="utf-8")
    payload = json.loads((where / MANIFEST_NAME).read_text(encoding="utf-8"))
    payload["content_sha256"] = tree_sha256(where, skip=MANIFEST_NAME)
    (where / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    stage(layout, where, now_ms=NOW)
    assert (layout.releases / "2026-09-26-dddddd" / "deploy").is_file()
