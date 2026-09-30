"""时间权威(W09h,决策 19):哪些命令要求站点知道狗的钟差、且钟差在命令有效期的一半以内。

命令的有效期由狗按**自己的墙钟**判(W09d):钟差未知 = 有效期对不对未知。按种类**和动作**分三类:

- ``gated``:会让狗动、建新任务、起新的运行状态,或者依赖有效期正确的命令 —— 钟差必须已知且合格;
  **没列出的新种类一律算这类**(默认收紧);
- ``safe``:停下、撤销、放掉、退回 —— 钟差未知或超限都放行。停下、撤销、放掉晚到或重投最多让狗停下;
  退版本、发件箱重试是外审指定的恢复手段(站点这头有命令账,狗那头有幂等记录),钟不对的时候正要靠它们;
- ``read``:只读查询 —— 放行。

W09d 原来是「没有估计就放行」、遥控续租 / 视频 / 监护不过闸;W09h 用户重新拍板改成这样(决策 19)。
"""

from __future__ import annotations

from typing import Any

GATED, SAFE, READ = "gated", "safe", "read"

#: 始终放行的(停下、撤销、退回)。
_SAFE_KINDS = frozenset({"halt", "abort", "release_rollback", "outbox_retry"})
#: 只读查询。
_READ_KINDS = frozenset({"proc_log", "mapping_trail", "mapping_preview", "release_precheck"})


def temporal_class(kind: str, payload: dict[str, Any] | None = None) -> str:
    p = payload or {}
    if kind in _SAFE_KINDS:
        return SAFE
    if kind in _READ_KINDS:
        return READ
    if kind in ("teleop_lease", "supervise"):
        return SAFE if p.get("action") == "release" else GATED
    if kind == "video":
        return SAFE if p.get("stop") is True else GATED
    if kind == "mapping":
        return SAFE if p.get("action") == "stop" else GATED
    if kind == "zones_set":
        # W10(内审应修 8):区域一律照发 —— 修订号挡住旧的重放(狗上旧修订拒),放宽的狗等空闲才换
        # (狗上自己判,不信站点的标);钟差未知时补发不出去的话,收紧的禁行区就一直到不了狗上
        return SAFE
    return GATED
