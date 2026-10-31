"""设备健康(W40,上装 V1.2):狗上每台设备一条 ``{"id", "state", "detail"}``,随遥测每
:data:`DEVICES_EVERY_MS` 带一次(变了也带)。

- ``state``:``online`` 在线、``offline`` 离线(连不上、没数据)、``degraded`` 降级(通着但不全好:
  RTK 只有浮点解、盘快满了)、``error`` 故障(通着但坏了:继电器板没确认、播放器退出码不对)。
- **没有反馈的设备不报「在线」**:聚光灯、警灯没有灯具状态传感器,报的是继电器板(命令写进去了没有),
  不是灯亮没亮(硬件交接 V1.2 第四条)。
- ``id``:``relay`` 继电器板、``speaker`` 喇叭(声卡)、``siren`` 喇叭警笛、``rtk``、``camera:<名>``、
  ``disk`` 发件箱那块盘、``link:<名>`` 网络上的设备(路由器、热像这类连得上连不上)。
"""

from __future__ import annotations

import re
from typing import Any

from d1max_contract.errors import ContractError

STATES = ("online", "offline", "degraded", "error")
#: 多久带一次。
DEVICES_EVERY_MS = 10_000
MAX_DEVICES = 32
_ID = re.compile(r"[a-z][a-z0-9_]{0,15}(?::[A-Za-z0-9_.-]{1,32})?")


def device(id: str, state: str, detail: str = "") -> dict[str, Any]:
    """一条设备健康(构造时就校验)。"""
    if not _ID.fullmatch(id):
        raise ContractError(f"设备名错误:{id!r}")
    if state not in STATES:
        raise ContractError(f"设备状态只认 {'/'.join(STATES)}:{state!r}")
    return {"id": id, "state": state, "detail": str(detail)[:200]}


def parse_devices(v: Any) -> list[dict[str, Any]] | None:
    """遥测里的 ``devices``;不对的条目扔掉(老站点 / 新狗混跑时不因为一条坏的丢整份遥测)。"""
    if not isinstance(v, list):
        return None
    out = []
    for d in v[:MAX_DEVICES]:
        if not isinstance(d, dict):
            continue
        try:
            out.append(device(d.get("id", ""), d.get("state", ""), d.get("detail", "")
                              if isinstance(d.get("detail", ""), str) else ""))
        except (ContractError, TypeError):
            continue
    return out
