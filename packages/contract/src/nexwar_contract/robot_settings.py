"""不靠 SSH(11-06,用户定):站点能改的机器人设置。

原来要 SSH 改 ``/etc/nexwar/env`` 的几项。站点发 ``ops_settings``,代理交给特权执行器只改这几个键、
只认这几个值(``deploy/nexwar_ops_runner`` 里的 ``ENV_SETTINGS`` 同一份,测试核一致);文件里别的行
(设备 PIN 等)不读出、不改。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

#: 键 → 能取的值。
SETTINGS: dict[str, tuple[str, ...]] = {
    "NEXWAR_PERSONS": ("bridge", "none"),
    "NEXWAR_OBS_PERMIT": ("0", "1"),
    "NEXWAR_MAX_FRACTION": ("0.5", "0.7", "1.0"),
    "NEXWAR_VENDOR_PAUSE": ("0", "1"),
    #: 厂家用不上的服务开机自启(第 32 条,用户 11-06:先默认关,直到我们 V2 上装有了)。
    "NEXWAR_VENDOR_AUTOSTART": ("0", "1"),
}

_MAX_ARG = re.compile(r"--max-fraction(?:=|\s+)([0-9.]+)")


def effective(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` 里这几项此刻算什么(没写站点设置的,按老写法推:``NEXWAR_OBS_SIDECAR`` 有值算开了许可,
    ``NEXWAR_AGENT_ARGS`` 里的 ``--max-fraction`` 算上限)。只回这几个键,别的一概不碰。"""
    def get(k: str) -> str:
        return (env.get(k) or "").strip()
    persons = get("NEXWAR_PERSONS") or "none"
    permit = get("NEXWAR_OBS_PERMIT") or ("1" if get("NEXWAR_OBS_SIDECAR") else "0")
    frac = get("NEXWAR_MAX_FRACTION")
    if not frac:
        m = _MAX_ARG.findall(get("NEXWAR_AGENT_ARGS"))
        frac = m[-1] if m else "0.5"
    return {"NEXWAR_PERSONS": persons, "NEXWAR_OBS_PERMIT": permit,
            "NEXWAR_MAX_FRACTION": frac, "NEXWAR_VENDOR_PAUSE": get("NEXWAR_VENDOR_PAUSE") or "0",
            "NEXWAR_VENDOR_AUTOSTART": get("NEXWAR_VENDOR_AUTOSTART") or "0"}


def check(changes: object) -> dict[str, str]:
    """站点给的改动:白名单的键、能取的值,一项都不能错;前进上限超过 0.5 要同时开着许可。"""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("要改哪几项:非空的对象")
    out: dict[str, str] = {}
    for k, v in changes.items():
        if k not in SETTINGS:
            raise ValueError(f"这一项不能在这里改:{str(k)[:40]}")
        if not isinstance(v, str) or v not in SETTINGS[k]:
            raise ValueError(f"{k} 只能是 {'/'.join(SETTINGS[k])}")
        out[k] = v
    return out


def conflict(now: Mapping[str, str], changes: Mapping[str, str]) -> str:
    """改完以后自相矛盾的说法(空 = 没问题):前进上限超过 0.5 要开着障碍许可(W47 设计)。"""
    after = {**now, **changes}
    if after.get("NEXWAR_MAX_FRACTION", "0.5") != "0.5" and after.get("NEXWAR_OBS_PERMIT") != "1":
        return "前进上限超过 0.5 要先开障碍净空许可"
    return ""
