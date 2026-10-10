"""站点看各只狗的版本配不配(商业化 A7)。规矩见 :mod:`d1max_contract.compat`。

- **对账**(:meth:`CompatWatch.tick`,告警循环每拍):狗报的代理兼容级别(能力
  ``tasks.compat.level``;老代理不报算 1)配不上站点 —— 太老报 P2「狗上的代理太老」(要给狗
  升级),太新报 P2「站点比狗老」(先升站点)。配上了自动解决。按当前状态对账,报、解决失败下一拍
  再来。
- **版本一览**(:func:`versions`,``GET /api/versions``、``d1max-site versions``):站点自己、每只
  狗的版本、兼容级别、旁路进程协议号、配不配。
"""

from __future__ import annotations

import logging
from typing import Any

from d1max_contract import SCHEMA
from d1max_contract.compat import (
    MIN_APP_API_LEVEL,
    SITE_API_LEVEL,
    SITE_MAX_AGENT_LEVEL,
    SITE_MIN_AGENT_LEVEL,
    agent_verdict,
)

log = logging.getLogger(__name__)

_TITLES = {"agent_too_old": "狗上的代理太老:要给这只狗升级",
           "site_too_old": "站点比狗上的代理老:先升站点"}
_DETAILS = {"agent_too_old": "站点要的代理兼容级别 ≥{min};这只狗报的是 {lv}。配不上的地方:"
                             "站点删过的证据再传上来会被老代理一直重传。在站点上给这只狗下发新版本",
            "site_too_old": "这只狗的代理兼容级别是 {lv},站点只认到 {max}。先升站点"
                            "(docs/版本兼容与升级顺序.md),狗不用动"}


def _robot(rid: str, c: Any) -> dict[str, Any]:
    caps = getattr(c, "capabilities", None)
    comp = (caps.tasks.get("compat") if caps is not None else None) or {}
    lv = comp.get("level") if isinstance(comp, dict) else None
    lv = lv if isinstance(lv, int) and not isinstance(lv, bool) else None
    return {"robot_id": rid, "online": bool(getattr(c, "status", None) and c.status.online),
            "agent": getattr(caps, "agent", None), "adapter": getattr(caps, "adapter", None),
            "level": lv if lv is not None else (1 if caps is not None else None),
            "sidecar_proto": comp.get("sidecar_proto") if isinstance(comp, dict) else None,
            "verdict": agent_verdict(lv) if caps is not None else "unknown"}


def versions(dispatcher: Any) -> dict[str, Any]:
    from d1max_site.health import site_version
    robots = [_robot(rid, c) for rid, c in sorted(getattr(dispatcher, "clients", {}).items())]
    return {"site": {"version": site_version(), "schema": SCHEMA, "api_level": SITE_API_LEVEL,
                     "agent_level_min": SITE_MIN_AGENT_LEVEL,
                     "agent_level_max": SITE_MAX_AGENT_LEVEL,
                     "min_app_level": MIN_APP_API_LEVEL},
            "robots": robots}


class CompatWatch:
    def __init__(self, dispatcher: Any) -> None:
        self.dispatcher = dispatcher
        self.alerts: Any = None

    def tick(self) -> None:
        if self.alerts is None:
            return
        for rid, c in list(getattr(self.dispatcher, "clients", {}).items()):
            r = _robot(rid, c)
            if r["verdict"] == "unknown":
                continue                                   # 还没报能力:说不清,不动
            for kind in ("agent_too_old", "site_too_old"):
                try:
                    open_ = self.alerts.has_open(rid, kind)
                    if r["verdict"] == kind and not open_:
                        self.alerts.raise_alert(
                            kind=kind, robot=rid, title=_TITLES[kind],
                            detail=_DETAILS[kind].format(lv=r["level"], min=SITE_MIN_AGENT_LEVEL,
                                                         max=SITE_MAX_AGENT_LEVEL))
                    elif r["verdict"] != kind and open_:
                        self.alerts.resolve_all(rid, kind, who="site:compat")
                except Exception:
                    log.exception("%s 的版本告警没对上", rid)
