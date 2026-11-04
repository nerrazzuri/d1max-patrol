"""断线策略表(总设计 §4.3)。**按当前任务定义**,不是笼统的「照常完成」。

W00 里代理只用 ``on_disconnect``(runtime 在断线那一拍按它处理当前任务)。
``queue_cmds``/``new_cmd_expires`` 在 W00 **只登记数据**:排队实际靠 broker 的
``clean_session=False`` 离线收件箱,过期靠命令自己的 ``expires_at``;这两个字段的消费者
是 W00c 的派遣器(给哪类命令多长的 ttl、断线时要不要撤回)。
"""

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
    # 巡检:同 goto —— 安全就走完这一趟,不安全就停下等;断线期间 abort 排队。
    "patrol": OfflinePolicy(on_disconnect="continue_if_safe", queue_cmds=frozenset({"abort"}),
                            new_cmd_expires=True),
    # abort 本身:本地执行,不排别的队,断线不影响它。
    "abort": OfflinePolicy(on_disconnect="execute_locally", queue_cmds=frozenset(),
                           new_cmd_expires=False),
    # W00c5c:遥控。断线 = 停车、结束,**不续**(决策 7:不在操作者断线后延续运动)。
    "teleop": OfflinePolicy(on_disconnect="stop_and_wait", queue_cmds=frozenset(),
                            new_cmd_expires=True),
}


def policy_for(kind: str) -> OfflinePolicy:
    """不认识的任务抛 KeyError,不猜。"""
    return OFFLINE_POLICY[kind]
