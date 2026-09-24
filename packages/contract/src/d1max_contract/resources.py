"""资源表(总设计 §4.1)。**数据,不是代码分支。**

代理内六种资源;每种任务声明自己占哪些。两个占同一资源的任务互斥;不占的并行。
不认识的任务**不许默认**——既不默认占 motion,也不默认什么都不占:那正是
「所有命令共用一个任务槽」的两种坏法。各任务在自己的工单里往表里加一行。
"""

from __future__ import annotations

RESOURCES: tuple[str, ...] = ("motion", "light", "sound", "spotlight", "head", "camera")

#: W00 只登记两种。其余任务类型(patrol/standoff/deter/…)各自工单再加。
TASK_RESOURCES: dict[str, frozenset[str]] = {
    "goto": frozenset({"motion"}),
    # W00c2a:整趟巡检。走、拍、开灯、转头;跟 goto 互斥,跟 abort 不冲突。
    "patrol": frozenset({"motion", "camera", "light", "head"}),
    "abort": frozenset(),
}


def resources_for(kind: str) -> frozenset[str]:
    """不认识的任务抛 KeyError —— 上层据此回 ``rejected(unsupported)``。"""
    return TASK_RESOURCES[kind]


def conflicts(kind_a: str, kind_b: str) -> bool:
    return bool(resources_for(kind_a) & resources_for(kind_b))
