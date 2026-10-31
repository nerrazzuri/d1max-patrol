"""远程运维(W55,站点这边):证书自动续签、App 与网页的错误记录。

机器人上的运维命令(日志、诊断、重启……)由 API 直接转给机器人(``Dispatcher.map_command``),这里不管;
这里管的是站点自己要定期做的事。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: 证书剩这么多天以内就自动续(机器人上的证书一年一签)。要早于体检的「快到期」告警(30 天):
#: 告警只在自动续签一直不成时才出现。
RENEW_WITHIN_DAYS = 45
CERT_DAYS = 365
DAY_MS = 86_400_000
#: 同一台失败了,隔多久再试。
RETRY_MS = DAY_MS
#: 机器人正忙(巡逻、遥控、建图)没换:隔多久再试(外审 W55-1 第 16 条:换证书要重启代理,只在空闲时换)。
BUSY_RETRY_MS = 3_600_000
#: 多久扫一次。
SCAN_MS = 3_600_000


class RenewError(Exception):
    pass


class RenewBusy(RenewError):
    """机器人正忙,这次没换(不算失败、不告警,一小时后再试)。"""


class CertRenewer:
    """证书自动续签:机器人生成新私钥和申请 → 站点签 → 机器人核对后换上(代理随后重启)。

    在线、报了 ``cert_csr``、证书 ``RENEW_WITHIN_DAYS`` 天内到期的才续;
    失败报 P2「证书自动续签失败」,
    隔一天再试;机器人正忙一小时后再试。人也可以在网页上点「立即续签」(:meth:`renew`)。

    **新证书先挂着,不马上换登记表**(外审 W55-1 第 6 条):签好的记进 ``cert_pending``,旧证书照常认;
    机器人回执说换上了才换登记表;回执丢了的话,机器人拿新证书连上接收口时由 intake 换。机器人明确拒绝
    就删掉挂着的那张。"""

    def __init__(self, *, dispatcher: Any, registry: Any, ca: Any, alerts: Any | None,
                 now_ms: Callable[[], int]) -> None:
        self.dispatcher = dispatcher
        self.registry = registry
        self.ca = ca
        self.alerts = alerts
        self._now = now_ms
        self._scanned = 0
        self._tried: dict[str, int] = {}
        self._wait: dict[str, int] = {}
        self._inflight: set[str] = set()

    def due(self) -> list[str]:
        now = self._now()
        out = []
        for rid, c in list(getattr(self.dispatcher, "clients", {}).items()):
            caps = getattr(c, "capabilities", None)
            if caps is None or "cert_csr" not in getattr(caps, "tasks", {}):
                continue
            row = self.registry.get(rid)
            if row is None or row.revoked or row.expires_at - now > RENEW_WITHIN_DAYS * DAY_MS:
                continue
            pend = self.registry.pending(rid)
            if pend is not None and now - pend["created_ms"] < RETRY_MS:
                continue                # 挂着一张没回执的:等机器人拿它连上来,别再签一张顶掉它
            out.append(rid)
        return out

    def tick(self) -> None:
        """事件循环里每拍调一次(同步):到点扫一遍,要续的起后台任务。"""
        now = self._now()
        if now - self._scanned < SCAN_MS:
            return
        self._scanned = now
        for rid in self.due():
            wait = self._wait.get(rid, RETRY_MS)
            if rid in self._inflight or now - self._tried.get(rid, -wait) < wait:
                continue
            self._tried[rid] = now
            asyncio.ensure_future(self._auto(rid))

    async def _auto(self, rid: str) -> None:
        try:
            await self.renew(rid, by="site:cert")
            self._wait.pop(rid, None)
        except RenewBusy as exc:
            self._wait[rid] = BUSY_RETRY_MS
            log.info("%s 证书续签等空闲:%s", rid, exc)
        except Exception as exc:  # noqa: BLE001 —— 后台任务:记下、告警,不往外抛
            self._wait[rid] = RETRY_MS
            log.warning("%s 证书自动续签失败:%s", rid, exc)

    async def renew(self, rid: str, *, by: str) -> dict[str, Any]:
        if rid in self._inflight:
            raise RenewError(f"{rid} 正在续签")
        self._inflight.add(rid)
        try:
            out = await self._renew(rid, by)
        except RenewBusy:
            raise
        except Exception as exc:
            self._alert(rid, str(exc))
            raise
        finally:
            self._inflight.discard(rid)
        if self.alerts is not None and self.alerts.has_open(rid, "cert_renew_failed"):
            self.alerts.resolve_all(rid, "cert_renew_failed", who="site:cert")
        return out

    async def _renew(self, rid: str, by: str) -> dict[str, Any]:
        got = await self.dispatcher.map_command(rid, "cert_csr", {}, issued_by=by)
        ack = got.get("ack", {})
        if ack.get("result") != "accepted":
            raise RenewError(f"机器人未生成证书申请:{ack.get('reason') or ack.get('result')}")
        csr = (ack.get("data") or {}).get("csr")
        now = self._now()
        cert, reg, fp = await asyncio.to_thread(self.ca.sign_robot_csr, rid, csr, days=CERT_DAYS,
                                                now_ms=now)
        old = self.registry.get(rid)
        self.registry.stage(rid, fingerprint=fp, issued_at=reg.issued_at,
                            expires_at=reg.expires_at, now_ms=now,
                            old_fingerprint=old.fingerprint if old is not None else "")
        try:
            got = await self.dispatcher.map_command(
                rid, "cert_install", {"cert": cert, "registration": reg.to_wire()}, issued_by=by)
        except Exception as exc:
            # 没回执:可能已经换上了(回执丢了)。新证书挂着,机器人拿它连上来时自动生效
            raise RenewError(f"机器人未回执(可能已换上,连上来时自动生效):{exc}") from exc
        ack = got.get("ack", {})
        if ack.get("result") != "accepted":
            reason = str(ack.get("reason") or ack.get("result"))
            if reason.startswith("priv_pending"):
                # 机器人那边执行器结果未知(外审 W55-2 C3):可能已经换上了 —— 挂着的新证书留着,
                # 机器人拿它连上来时自动生效
                raise RenewError("机器人上的执行器未及时回复(可能已换上,连上来时自动生效)")
            self.registry.drop_pending(rid, fp)
            if reason.startswith("busy"):
                raise RenewBusy(f"机器人正忙,未换证书:{reason}")
            raise RenewError(f"机器人未换上新证书:{reason}")
        self.registry.promote(rid, fp)
        log.info("%s 证书已续签,到期 %s", rid, reg.expires_at)
        return {"robot_id": rid, "expires_at": reg.expires_at, "fingerprint": fp}

    def _alert(self, rid: str, why: str) -> None:
        if self.alerts is None:
            return
        try:
            self.alerts.raise_alert(kind="cert_renew_failed", robot=rid,
                                    title="证书自动续签失败",
                                    detail=f"{why[:300]};明天自动重试,也可在网页机器人页点「立即续签」",
                                    now_ms=self._now())
        except Exception:
            log.exception("%s 续签失败的告警未能记录", rid)


#: App、网页的错误记录:每条最多这么长;一次最多传这么多条;留多少天。
CLIENT_ERR_CHARS = 2000
CLIENT_ERR_BATCH = 200
CLIENT_ERR_KEEP_MS = 14 * DAY_MS
_FIELDS = ("at_ms", "kind", "page", "message", "detail", "app_version", "platform")


class ClientErrors:
    """App、网页报上来的错误(W55):站点存 14 天,进诊断包。不收画面、不收密码(客户端也不传)。"""

    def __init__(self, db: Any, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def add(self, account: str, client: str, items: Any) -> int:
        if not isinstance(items, list) or len(items) > CLIENT_ERR_BATCH:
            raise ValueError(f"items 要是不超过 {CLIENT_ERR_BATCH} 条的列表")
        now = self._now()
        rows = []
        for it in items:
            if not isinstance(it, dict):
                raise ValueError("每一条要是对象")
            at = it.get("at_ms")
            at = at if isinstance(at, int) and not isinstance(at, bool) else now
            rows.append((min(at, now), account, client[:16],
                         *(str(it.get(k) or "")[:CLIENT_ERR_CHARS] for k in _FIELDS[1:]), now))
        with self.db.tx() as c:
            c.executemany("INSERT INTO client_errors(at_ms, account, client, kind, page, message, "
                          "detail, app_version, platform, received_ms) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def prune(self) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM client_errors WHERE received_ms < ?",
                      (self._now() - CLIENT_ERR_KEEP_MS,))

    def recent(self, since_ms: int, limit: int = 5000) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT at_ms, account, client, kind, page, message, detail, app_version, platform "
            "FROM client_errors WHERE at_ms >= ? ORDER BY at_ms DESC LIMIT ?", (since_ms, limit))]


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str).encode()
