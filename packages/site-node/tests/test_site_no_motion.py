"""站点不自己产生运动(W15;决策 5、决策 7 的守门测试)。

决策 5:站点「可下发任务级指令,不可遥控」。决策 7 定稿:「允许站点转发经过认证、持有控制租约的人类实时
操作输入;禁止站点自主生成、重放或在操作者断线后延续运动指令」。这里扫站点的源码,卡住三件事,以后谁改代码
越了界这里先红:

1. 站点发给狗的命令种类是一个**封闭的任务级清单**:没有 ``vel``、``walk`` 这类直接速度命令;
   新加一种命令
   要来这里登记(登记的时候想一想它是不是任务级)。
2. 遥控帧(``TeleopFrame``)只在遥控模块的 ``_send_frame`` 里造;``_send_frame`` 只被「手机来的杆量」
   (``_drive``)和「结束时的那一帧零速」(``close``)调用;**结束时那一帧必须是零速**。
3. 站点不碰旁路进程的速度协议(``agent_frames``、``sidecar``)。

扫的是语法树,不是字符串:注释、文档里提到这些名字不算。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "d1max_site"

#: 站点会发给狗的命令(都是任务级或维护级)。``teleop`` 是授予遥控租约,不是速度;杆量走单独的遥控帧。
ALLOWED_KINDS = frozenset({
    "goto", "patrol", "abort", "halt", "teleop", "map_activate", "map_build", "mapping",
    "outbox_retry", "release", "release_activate", "release_install", "release_rollback",
    "snapshot", "video", "mark_home", "relocalize", "supervise", "resume", "zones_set",
    "teleop_lease", "proc_log", "release_precheck", "mapping_trail",
    # W21:上装的声光(警灯、警笛、聚光灯、喇叭),带最长时间、到点狗上自己关;不动腿
    "deter",
})
#: 直接速度、运动原语:站点永远不许发。
FORBIDDEN_KINDS = frozenset({"vel", "walk", "move", "stand", "lie", "estop_off", "gait"})


def _trees() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"), filename=str(p)))
            for p in sorted(SRC.rglob("*.py"))]


def _command_kinds() -> dict[str, list[str]]:
    """写死的命令种类 → 出现在哪些文件。认这几种写法:``_send(c, robot_id, "<kind>", …)``、
    ``map_command(robot_id, "<kind>", …)``、``kind = "<kind>"``、
    ``kind, payload = "<kind>", …``。"""
    found: dict[str, list[str]] = {}

    def add(v: object, where: str) -> None:
        if isinstance(v, str):
            found.setdefault(v, []).append(where)

    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name, args = node.func.attr, node.args
                if name == "_send" and len(args) >= 3 and isinstance(args[2], ast.Constant):
                    add(args[2].value, path.name)
                if name == "map_command" and len(args) >= 2 and isinstance(args[1], ast.Constant):
                    add(args[1].value, path.name)
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "kind" \
                            and isinstance(node.value, ast.Constant):
                        add(node.value.value, path.name)
                    # 只认 ``kind, payload = …``(发命令的写法);告警的 ``kind, robot = …`` 不是命令
                    if isinstance(tgt, ast.Tuple) and len(tgt.elts) >= 2 \
                            and isinstance(tgt.elts[0], ast.Name) and tgt.elts[0].id == "kind" \
                            and isinstance(tgt.elts[1], ast.Name) and tgt.elts[1].id == "payload" \
                            and isinstance(node.value, ast.Tuple) and node.value.elts \
                            and isinstance(node.value.elts[0], ast.Constant):
                        add(node.value.elts[0].value, path.name)
    return found


def test_站点发的命令种类是封闭的任务级清单_没有直接速度命令():
    kinds = _command_kinds()
    assert kinds, "一个都没扫到:扫描本身坏了"
    assert {"goto", "patrol", "halt", "teleop"} <= set(kinds), sorted(kinds)
    bad = set(kinds) & FORBIDDEN_KINDS
    assert not bad, f"站点发直接速度命令:{ {k: kinds[k] for k in bad} }"
    unknown = set(kinds) - ALLOWED_KINDS
    assert not unknown, (f"新命令种类 { {k: kinds[k] for k in unknown} }:是任务级的就登记进 "
                         "ALLOWED_KINDS;不是任务级的不许加(决策 5、7)")


def test_遥控帧只在遥控模块里转发人的杆量_结束那一帧是零速():
    makers, callers = [], {}
    for path, tree in _trees():
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if name == "TeleopFrame":
                    makers.append((path.name, fn.name))
                if name == "_send_frame":
                    callers.setdefault((path.name, fn.name), []).append(node)
    assert makers == [("teleop.py", "_send_frame")], makers
    assert set(callers) == {("teleop.py", "_drive"), ("teleop.py", "close")}, sorted(callers)
    for call in callers[("teleop.py", "close")]:
        vx, wz = call.args[1:3]
        assert isinstance(vx, ast.Constant) and vx.value == 0.0, "结束时只许发零速"
        assert isinstance(wz, ast.Constant) and wz.value == 0.0, "结束时只许发零速"
        assert any(k.arg == "final" for k in call.keywords)
    # _drive 里的 _send_frame 用的是手机来的杆量(参数 vx、wz)或零速,不是站点算出来的
    for call in callers[("teleop.py", "_drive")]:
        got = [a.id if isinstance(a, ast.Name) else getattr(a, "value", None)
               for a in call.args[1:3]]
        assert got in (["vx", "wz"], [0.0, 0.0]), got


def test_站点不碰旁路进程的速度协议():
    for path, tree in _trees():
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for m in mods:
                assert "agent_frames" not in m and "sidecar" not in m, f"{path.name} 导入了 {m}"
