"""断线策略表(总设计 §4.3)。**按当前任务定义**,不是笼统的「照常完成」。"""

from __future__ import annotations

from dataclasses import dataclass

#: 断站点后当前任务的三种去向。
ON_DISCONNECT = ("continue_if_safe", "stop_and_wait", "execute_locally")


@dataclass(frozen=True)
class OfflinePolicy:
    #: 断线时当前任务怎么办。
    on_disconnect: str
    #: 断线期间哪些**新到的**命令排队等重连后执行(其余过期即弃)。
    queue_cmds: frozenset[str]
    #: 断线期间到达的同类新任务是否按过期处理(``True`` = 过期即弃)。
    new_cmd_expires: bool

    def __post_init__(self) -> None:
        if self.on_disconnect not in ON_DISCONNECT:
            raise ValueError(f"on_disconnect 只能是 {ON_DISCONNECT},给的是 {self.on_disconnect!r}")


OFFLINE_POLICY: dict[str, OfflinePolicy] = {
    # 普通移动:定位、电量、安全条件满足 → 继续;否则停并等待。abort 排队。
    "goto": OfflinePolicy(on_disconnect="continue_if_safe", queue_cmds=frozenset({"abort"}),
                          new_cmd_expires=True),
    # abort 本身:本地执行,不排别的队,断线不影响它。
    "abort": OfflinePolicy(on_disconnect="execute_locally", queue_cmds=frozenset(),
                           new_cmd_expires=False),
}


def policy_for(kind: str) -> OfflinePolicy:
    """不认识的任务抛 KeyError,不猜。"""
    return OFFLINE_POLICY[kind]
