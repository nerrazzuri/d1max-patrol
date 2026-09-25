"""主题形状 ``site/<site_id>/robot/<robot_id>/<kind>`` 与代理侧 ACL(总设计 §3、§3.6)。

W00 决定 4:broker 侧的 mTLS/ACL 归 W00c;这里先把「一台狗只能发自己的主题、只能订
自己的 cmd」做成代理自己就守的规则 —— 代理的 Transport 包装层用 :class:`TopicAcl`
拒绝越界,不等 broker 来拒。
"""

from __future__ import annotations

from dataclasses import dataclass

from d1max_contract.errors import ContractError

KINDS = ("capabilities", "status", "cmd", "cmd/ack", "event", "reconcile", "telemetry", "teleop")
#: 狗可以发的六种;``cmd`` 与 ``teleop``(W00c5c,遥控帧)只有站点能发、狗只订自己的。
PUBLISH_KINDS = ("capabilities", "status", "cmd/ack", "event", "reconcile", "telemetry")


def _check_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"Topics: {name} 不许为空")
    if any(ch in value for ch in "/+# "):
        raise ContractError(f"Topics: {name} 里不许有 / + # 或空格: {value!r}")


@dataclass(frozen=True)
class Topics:
    site_id: str
    robot_id: str

    def __post_init__(self) -> None:
        _check_id("site_id", self.site_id)
        _check_id("robot_id", self.robot_id)

    @property
    def prefix(self) -> str:
        return f"site/{self.site_id}/robot/{self.robot_id}"

    def of(self, kind: str) -> str:
        if kind not in KINDS:
            raise ContractError(f"Topics: 不认识的 kind {kind!r}")
        return f"{self.prefix}/{kind}"

    @property
    def capabilities(self) -> str:
        return self.of("capabilities")

    @property
    def status(self) -> str:
        return self.of("status")

    @property
    def cmd(self) -> str:
        return self.of("cmd")

    @property
    def ack(self) -> str:
        return self.of("cmd/ack")

    @property
    def event(self) -> str:
        return self.of("event")

    @property
    def reconcile(self) -> str:
        return self.of("reconcile")

    @property
    def telemetry(self) -> str:
        return self.of("telemetry")

    @property
    def teleop(self) -> str:
        """遥控帧(W00c5c):QoS 0、不保留,**绝不经 cmd**(持久会话会补投旧帧)。"""
        return self.of("teleop")

    @staticmethod
    def parse(topic: str) -> tuple[str, str, str]:
        """``site/<s>/robot/<r>/<kind>`` → ``(s, r, kind)``。不成形抛 ContractError。"""
        parts = topic.split("/") if isinstance(topic, str) else []
        if len(parts) < 5 or parts[0] != "site" or parts[2] != "robot":
            raise ContractError(f"Topics: 主题不成形: {topic!r}")
        site_id, robot_id = parts[1], parts[3]
        kind = "/".join(parts[4:])
        _check_id("site_id", site_id)
        _check_id("robot_id", robot_id)
        if kind not in KINDS:
            raise ContractError(f"Topics: 不认识的 kind {kind!r}(主题 {topic!r})")
        return site_id, robot_id, kind


class TopicAcl:
    """代理侧的越界守卫。**精确匹配,不认通配** —— 狗没有任何理由订通配主题。"""

    def __init__(self, topics: Topics) -> None:
        self._t = topics
        self._publish = frozenset(topics.of(k) for k in PUBLISH_KINDS)
        self._subscribe = frozenset({topics.cmd, topics.teleop})

    def may_publish(self, topic: str) -> bool:
        return topic in self._publish

    def may_subscribe(self, topic_filter: str) -> bool:
        return topic_filter in self._subscribe
