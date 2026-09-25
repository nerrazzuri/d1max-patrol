"""遥控经站点的契约(W00c5c,受决策 7 约束)。

「允许站点转发经过认证、持有控制租约的人类实时操作输入;禁止站点自主生成、重放或在操作者断线后
延续运动指令。」

- **帧**(:class:`TeleopFrame`)走专用主题 ``…/teleop``,QoS 0、不保留 —— **绝不经 ``cmd``**:``cmd``
  是持久会话,断线重连会补投旧帧,那就是被禁止的重放。每帧带租约代次、单调序号、站点发出时刻、
  很短的有效期;规矩由代理执行(见 ``d1max_agent.teleop_frames``)。
- **租约**:授予是一条任务命令 ``teleop``(优先级最高,抢占自动任务,先停稳再移交);续租、放租是
  ``teleop_lease``(不是任务);停车是 ``halt``(不是任务、不走遥控连接)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

#: 帧的有效期:站点默认 300 ms;代理只认这个范围。
FRAME_TTL_DEFAULT_MS = 300
FRAME_TTL_MIN_MS = 50
FRAME_TTL_MAX_MS = 1000
#: 帧里速度的合理范围(代理还会按 HAL 能力的一半再夹一次)。
MAX_ABS_SPEED = 5.0
#: 租约有效期范围。站点连接活着时每 1 s 续一次。
LEASE_TTL_MIN_MS = 1_000
LEASE_TTL_MAX_MS = 30_000
LEASE_TTL_DEFAULT_MS = 5_000
#: 遥控任务的优先级:高于一切自动任务(事件 80)。
TELEOP_PRIORITY = 100


def _pos_int(d: dict, k: str, *, lo: int = 1) -> int:
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ContractError(f"teleop: {k} 要是 ≥{lo} 的整数")
    return v


def _speed(d: dict, k: str) -> float:
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or abs(v) > MAX_ABS_SPEED:
        raise ContractError(f"teleop: {k} 要是 ±{MAX_ABS_SPEED} 内的有限数")
    return float(v)


@dataclass(frozen=True)
class TeleopFrame:
    lease_epoch: int
    seq: int
    #: 站点发出时刻(站点的钟,毫秒)。代理不拿它跟自己的钟直接比,只看在途时间有没有比基线多。
    sent_at: int
    ttl_ms: int
    vx: float
    wz: float

    def to_wire(self) -> dict[str, Any]:
        return {"lease_epoch": self.lease_epoch, "seq": self.seq, "sent_at": self.sent_at,
                "ttl_ms": self.ttl_ms, "vx": self.vx, "wz": self.wz}

    @classmethod
    def from_wire(cls, d: Any) -> TeleopFrame:
        if not isinstance(d, dict):
            raise ContractError("teleop: 帧要是对象")
        ttl = _pos_int(d, "ttl_ms")
        if not FRAME_TTL_MIN_MS <= ttl <= FRAME_TTL_MAX_MS:
            raise ContractError(f"teleop: ttl_ms 要在 [{FRAME_TTL_MIN_MS}, {FRAME_TTL_MAX_MS}]")
        return cls(lease_epoch=_pos_int(d, "lease_epoch"), seq=_pos_int(d, "seq"),
                   sent_at=_pos_int(d, "sent_at", lo=0), ttl_ms=ttl,
                   vx=_speed(d, "vx"), wz=_speed(d, "wz"))


def teleop_grant_payload(*, lease_epoch: int, operator: str, lease_ttl_ms: int) -> dict[str, Any]:
    """``teleop`` 命令(授予租约、起遥控任务)的载荷。"""
    return {"lease_epoch": lease_epoch, "operator": operator, "lease_ttl_ms": lease_ttl_ms}


def _lease_ttl(d: dict, *, default: int | None = None) -> int:
    if default is not None and "lease_ttl_ms" not in d:
        return default
    v = _pos_int(d, "lease_ttl_ms")
    if not LEASE_TTL_MIN_MS <= v <= LEASE_TTL_MAX_MS:
        raise ContractError(f"teleop: lease_ttl_ms 要在 [{LEASE_TTL_MIN_MS}, {LEASE_TTL_MAX_MS}]")
    return v


def parse_teleop_grant(p: Any) -> tuple[int, str, int]:
    if not isinstance(p, dict):
        raise ContractError("teleop: 载荷要是对象")
    op = p.get("operator")
    if not isinstance(op, str) or not 1 <= len(op) <= 64:
        raise ContractError("teleop: operator 要是 1–64 个字符")
    return _pos_int(p, "lease_epoch"), op, _lease_ttl(p)


@dataclass(frozen=True)
class TeleopLease:
    """``teleop_lease`` 命令:``renew``(续租)或 ``release``(放租)。不是任务。"""

    action: str
    lease_epoch: int
    lease_ttl_ms: int = LEASE_TTL_DEFAULT_MS

    def to_payload(self) -> dict[str, Any]:
        return {"action": self.action, "lease_epoch": self.lease_epoch,
                "lease_ttl_ms": self.lease_ttl_ms}


def parse_teleop_lease(p: Any) -> TeleopLease:
    if not isinstance(p, dict):
        raise ContractError("teleop_lease: 载荷要是对象")
    action = p.get("action")
    if action not in ("renew", "release"):
        raise ContractError("teleop_lease: action 只认 renew/release")
    return TeleopLease(action=action, lease_epoch=_pos_int(p, "lease_epoch"),
                       lease_ttl_ms=_lease_ttl(p, default=LEASE_TTL_DEFAULT_MS))
