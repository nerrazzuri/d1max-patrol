"""过渡期的监护租约(W00c6i,W08 决定 11)。

W11 避障真机验收之前,真狗的自主移动是直线、拿运控里程当地图、没有避障 ——
只许在**有人在现场**时动。

- **自主级别**:代理在能力里报 ``tasks.goto.autonomy`` / ``tasks.patrol.autonomy``,
  ``supervised``(要人监护)或 ``autonomous``(可自主)。站点读不到一律按 ``supervised`` 算。
- **监护租约**:手机上「我在现场监护」开着时每 1 s 心跳一次,站点转成一条 ``supervise`` 命令
  (``cmd`` 主题,不是任务):``{"action": "renew"|"release", "ttl_ms", "operator", "session",
  "seq"}``。代理按**收到时刻** + ``ttl_ms``(单调钟)记「这个会话监护到什么时候」;``supervised``
  级别下,没有一个会话在监护就不收 ``goto``/``patrol``、跑着的时候监护过期就当场中止。
- **防补投、防迟到**(W00c6i 内审):命令本身的有效期放宽(站点 30 s,同遥控续租 —— 按狗的墙钟判,
  Orin 的钟不准);旧心跳靠**会话号 + 序号**挡:同一会话里序号不比见过的大的一律不认(断线补投的、
  手机到站点这一段迟到的,都比放租那一条小)。会话号每次打开开关新起一个,谁放只放自己那个会话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

SUPERVISED = "supervised"
AUTONOMOUS = "autonomous"
AUTONOMY_LEVELS = (SUPERVISED, AUTONOMOUS)

#: 监护租约有效期:站点默认 3 s(手机每 1 s 续一次,丢两次还在);代理只认这个范围。
SUPERVISE_TTL_DEFAULT_MS = 3_000
SUPERVISE_TTL_MIN_MS = 1_000
SUPERVISE_TTL_MAX_MS = 10_000
SUPERVISE_ACTIONS = ("renew", "release")


@dataclass(frozen=True)
class Supervise:
    action: str
    ttl_ms: int
    operator: str
    #: 手机每次打开开关新起的会话号(≤64 字);序号在会话里单调递增(≥1)。
    session: str
    seq: int

    def to_payload(self) -> dict[str, Any]:
        return {"action": self.action, "ttl_ms": self.ttl_ms, "operator": self.operator,
                "session": self.session, "seq": self.seq}


def parse_supervise(payload: Any) -> Supervise:
    """读回 ``supervise`` 命令的载荷。形状不对抛 :class:`ContractError`,不猜。"""
    if not isinstance(payload, dict):
        raise ContractError("supervise: 载荷要是对象")
    action = payload.get("action")
    if action not in SUPERVISE_ACTIONS:
        raise ContractError(f"supervise: action 要是 {'/'.join(SUPERVISE_ACTIONS)} 之一")
    ttl = payload.get("ttl_ms", SUPERVISE_TTL_DEFAULT_MS)
    if isinstance(ttl, bool) or not isinstance(ttl, int) \
            or not SUPERVISE_TTL_MIN_MS <= ttl <= SUPERVISE_TTL_MAX_MS:
        raise ContractError(f"supervise: ttl_ms 要在 [{SUPERVISE_TTL_MIN_MS}, "
                            f"{SUPERVISE_TTL_MAX_MS}] 内")
    operator = payload.get("operator", "")
    if not isinstance(operator, str) or len(operator) > 64:
        raise ContractError("supervise: operator 要是 ≤64 字的字符串")
    session = payload.get("session")
    if not isinstance(session, str) or not 1 <= len(session) <= 64:
        raise ContractError("supervise: session 要是 1–64 字的字符串")
    seq = payload.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ContractError("supervise: seq 要是 ≥1 的整数")
    return Supervise(action=action, ttl_ms=ttl, operator=operator, session=session, seq=seq)


def autonomy_of(task_caps: dict[str, Any] | None) -> str:
    """能力里某个任务(``goto``/``patrol``)的自主级别。**读不到按 ``supervised`` 算**
    (老代理、没报)。"""
    level = (task_caps or {}).get("autonomy")
    return level if level in AUTONOMY_LEVELS else SUPERVISED
