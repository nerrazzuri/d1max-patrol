"""机器人自己来挂的绑定申请(W42,``docs/W42-机器人绑定-设计.md`` §5)。

手机扫上装贴纸、给机器人发了现场 WiFi 和站点地址以后,机器人连上站点:

1. ``POST /api/bind/request``(**不用登录**):挂一条申请,回 ``request_id`` 和轮询口令 ``poll_secret``
   (站点只记它的哈希)。同一序列号再来一条,旧的作废;最多同时挂 :data:`MAX_PENDING` 条;
   :data:`TTL_MS` 没人批作废。
2. 有 ``manage`` 权限的人在网页、手机上核对 6 位核对码(两边都从机器人的随机数算),批准或拒绝。
   批准 = 现有的 ``enroll``(签证书、登记,新机器人先只许手动派)。
3. 机器人带着口令轮询 ``GET /api/bind/request/<id>``:批了就给一个开通码(现有的 ``D1MAX1.``,1 小时),
   机器人接着走现有的 ``/api/enroll/claim`` 领证书包。领过了回 ``done``。

另外 ``GET /api/bind/state``:机器人问「我这张证书在你这儿还算不算」(解绑时它不在线,§5.3)。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from d1max_contract.binding import BindingError, check_digits, check_nonce, check_serial

#: 同时最多挂几条。
MAX_PENDING = 10
#: 多久没人批作废(装机当天不一定有人守在控制室)。
TTL_MS = 24 * 3600_000
#: 每个来源地址每分钟最多挂几条。
RATE_PER_MIN = 5
#: 告警种类(给管理员:今天之内批一下)。
ALERT_KIND = "bind_request"
#: 批准以后多久之内机器人还能凭轮询口令(重新)领开通码:领证的回复丢了、机器人写证书写到一半断电,
#: 都靠再领一次(外审 I7)。只领得到批准时那一代证书。
CLAIM_WINDOW_MS = 24 * 3600_000


class BindRefused(Exception):
    """不给挂 / 不给批:``status`` 跟 HTTP 一样。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _fp(text: Any) -> str:
    s = str(text or "").strip().lower()
    return s if s.startswith("sha256:") else "sha256:" + s


class BindDesk:
    """``enroll(serial)`` 签证书、登记;``issue(serial, host, cert_fp)`` 给 ``cert_fp`` 那一代
    出 1 小时的开通码
    (都是站点现有的函数)。"""

    def __init__(self, db: Any, *, now_ms: Callable[[], int], enroll: Callable[[str], None],
                 issue: Callable[[str, str, str], str], alerts: Any = None,
                 set_name: Callable[[str, str], None] | None = None) -> None:
        self.db = db
        self._now = now_ms
        self._enroll = enroll
        self._issue = issue
        self._set_name = set_name
        self.alerts = alerts
        self._lock = threading.Lock()
        self._recent: dict[str, deque[int]] = {}

    # ------------------------------------------------------------ 机器人这头(不用登录)

    def request(self, d: Any, *, source: str, host: str) -> dict[str, Any]:
        if not isinstance(d, dict):
            raise BindRefused(400, "要 JSON")
        try:
            serial = check_serial(d.get("serial"))
            nonce = check_nonce(d.get("nonce"))
        except BindingError as exc:
            raise BindRefused(400, str(exc)) from exc
        model = str(d.get("model") or "")[:64]
        version = str(d.get("version") or "")[:64]
        now = self._now()
        self._rate(source, now)
        self._expire(now)
        rid, secret = secrets.token_hex(8), secrets.token_urlsafe(24)
        with self.db.tx() as c:
            row = c.execute("SELECT revoked FROM robots WHERE robot_id=?", (serial,)).fetchone()
            if row is not None and not row["revoked"]:
                raise BindRefused(409, f"{serial} 已经在这个站点了:要重新绑定,先在站点上解绑")
            c.execute("UPDATE bind_requests SET state='replaced' "
                      "WHERE serial=? AND state IN ('pending', 'approved')", (serial,))
            n = c.execute("SELECT COUNT(*) FROM bind_requests WHERE state='pending'").fetchone()[0]
            if n >= MAX_PENDING:
                raise BindRefused(429, f"已经有 {MAX_PENDING} 台在等批准了:先在站点上处理掉一些")
            c.execute("INSERT INTO bind_requests(id, serial, nonce, model, version, source, host, "
                      "secret_hash, created_ms, expires_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rid, serial, nonce, model, version, source[:64], host[:200], _h(secret),
                       now, now + TTL_MS))
        digits = check_digits(nonce)
        if self.alerts is not None:
            self.alerts.raise_alert(kind=ALERT_KIND, robot=serial,
                                    title="有一台机器人等批准加入站点",
                                    detail=f"序列号 {serial},核对码 {digits}")
        return {"request_id": rid, "poll_secret": secret, "check": digits,
                "expires_ms": now + TTL_MS}

    def poll(self, rid: str, secret: str) -> dict[str, Any]:
        """``pending`` / ``rejected`` / ``expired`` / ``replaced`` / ``approved``(带开通码)。

        批了以后 :data:`CLAIM_WINDOW_MS` 之内,每问一次出一个新开通码(旧的作废):领证的回复丢了、
        写到一半断电,机器人再问一次就是。**只出批准时那一代**(外审 C1):这台后来吊销了、重新登记了,
        旧申请回 ``replaced``,领不到新私钥。"""
        now = self._now()
        rows = self.db.query("SELECT * FROM bind_requests WHERE id=?", (str(rid)[:32],))
        if not rows or not isinstance(secret, str) \
                or not hmac.compare_digest(rows[0]["secret_hash"], _h(secret)):
            raise BindRefused(404, "没有这条申请")
        r = rows[0]
        state = r["state"]
        if state == "pending" and now > r["expires_ms"]:
            self._expire(now)
            state = "expired"
        if state != "approved":
            return {"state": state}
        reg = self.db.query("SELECT fingerprint, revoked FROM robots WHERE robot_id=?",
                            (r["serial"],))
        if not reg or reg[0]["revoked"] or not r["cert_fp"] \
                or not hmac.compare_digest(str(reg[0]["fingerprint"]), str(r["cert_fp"])):
            return {"state": "replaced"}
        if now > (r["decided_ms"] or 0) + CLAIM_WINDOW_MS:
            return {"state": "expired"}
        try:
            code = self._issue(r["serial"], r["host"], r["cert_fp"])
        except Exception as exc:  # 原因原样告诉机器人(它会记日志、再问)
            raise BindRefused(409, f"批了,但开通码出不来:{exc}") from exc
        return {"state": "approved", "code": code}

    def cert_state(self, serial: Any, cert: Any) -> str:
        """``active``:这张证书在这儿还算;``revoked``:这台在这儿**明确吊销了**(解绑);
        ``unknown``:这儿没这台(站点重装了)、或者登记的是另一张证书(续签了、机器人还没换上):
        机器人别动(外审 C2:续签不能被当成解绑去清机)。"""
        try:
            check_serial(serial)
        except BindingError:
            return "unknown"
        rows = self.db.query("SELECT fingerprint, revoked FROM robots WHERE robot_id=?", (serial,))
        if not rows:
            return "unknown"
        r = rows[0]
        if r["revoked"]:
            return "revoked"
        if not hmac.compare_digest(str(r["fingerprint"]).lower(), _fp(cert)):
            return "unknown"
        return "active"

    # ------------------------------------------------------------ 站点上的人(manage)

    def pending(self) -> list[dict[str, Any]]:
        now = self._now()
        self._expire(now)
        rows = self.db.query("SELECT * FROM bind_requests WHERE state='pending' "
                             "ORDER BY created_ms")
        return [{"id": r["id"], "serial": r["serial"], "check": check_digits(r["nonce"]),
                 "model": r["model"], "version": r["version"], "source": r["source"],
                 "created_ms": r["created_ms"], "expires_ms": r["expires_ms"]} for r in rows]

    def approve(self, rid: str, *, by: str, name: Any = None) -> dict[str, Any]:
        r = self._pending_row(rid)
        serial = r["serial"]
        self._enroll(serial)                       # 失败抛出去(站点那头的原因原样给人看)
        with self.db.tx() as c:
            fp = c.execute("SELECT fingerprint FROM robots WHERE robot_id=?", (serial,)).fetchone()
            c.execute("UPDATE bind_requests SET state='approved', decided_by=?, decided_ms=?, "
                      "cert_fp=? WHERE id=?", (by, self._now(), fp["fingerprint"] if fp else "",
                                               rid))
        if isinstance(name, str) and name.strip() and self._set_name is not None:
            try:
                self._set_name(serial, name.strip()[:64])
            except Exception:  # noqa: BLE001 - 名字起不上不挡批准(以后在机器人页改)
                pass
        self._resolve(serial, by)
        return {"id": rid, "serial": serial, "state": "approved"}

    def reject(self, rid: str, *, by: str) -> dict[str, Any]:
        r = self._pending_row(rid)
        with self.db.tx() as c:
            c.execute("UPDATE bind_requests SET state='rejected', decided_by=?, decided_ms=? "
                      "WHERE id=?", (by, self._now(), rid))
        self._resolve(r["serial"], by)
        return {"id": rid, "serial": r["serial"], "state": "rejected"}

    # ------------------------------------------------------------ 内部

    def _pending_row(self, rid: str) -> Any:
        self._expire(self._now())
        rows = self.db.query("SELECT * FROM bind_requests WHERE id=? AND state='pending'",
                             (str(rid)[:32],))
        if not rows:
            raise BindRefused(404, "没有这条申请(可能已经处理过、过期了,或者机器人又重新申请了)")
        return rows[0]

    def _expire(self, now: int) -> None:
        with self.db.tx() as c:
            gone = c.execute("SELECT serial FROM bind_requests WHERE state='pending' "
                             "AND expires_ms < ?", (now,)).fetchall()
            c.execute("UPDATE bind_requests SET state='expired' WHERE state='pending' "
                      "AND expires_ms < ?", (now,))
        for g in gone:
            self._resolve(g["serial"], "system")

    def _resolve(self, serial: str, by: str) -> None:
        if self.alerts is None:
            return
        still = self.db.query("SELECT 1 FROM bind_requests WHERE serial=? AND state='pending'",
                              (serial,))
        if not still:
            try:
                self.alerts.resolve_all(serial, ALERT_KIND, who=by)
            except Exception:  # noqa: BLE001 - 告警收不掉不挡批准
                pass

    def _rate(self, source: str, now: int) -> None:
        with self._lock:
            q = self._recent.setdefault(source, deque())
            while q and now - q[0] > 60_000:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                raise BindRefused(429, "申请太频繁了,过一分钟再试")
            q.append(now)
            if len(self._recent) > 1000:                # 别让乱来的地址把内存撑大
                for k in [k for k, v in self._recent.items() if not v or now - v[-1] > 60_000]:
                    del self._recent[k]
