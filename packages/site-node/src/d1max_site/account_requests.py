"""App 上申请账号(W52,用户 2026-10-30):不用登录挂一条申请,管理员批准时选权限(默认「只看」)。

- 申请人自己定账号名、密码(规则跟建账号一样);站点只存密码的 scrypt 哈希,批准时原样搬进账号表 ——
  管理员看不到、也不用转交密码。
- 限制:同一来源地址每分钟 :data:`RATE_PER_MIN` 次;站点上最多挂 :data:`MAX_PENDING` 条;
  :data:`TTL_MS`(7 天)没人批就作废;账号名已经有了、或者已经在申请中,回 409。
- 有人申请:记一条 P2 ``account_request``(推给管理员);没有等批准的了就解决掉。
- 申请人不用另外查结果:批了直接登录;登录失败时站点按申请的状态说「还在等管理员批准」
  或「申请被拒了」。
"""

from __future__ import annotations

import secrets
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from d1max_site.accounts import (
    _NAME,
    ENGINEER,
    RESERVED_NAMES,
    Accounts,
    AuthError,
    _hash,
)
from d1max_site.alert_sources import SITE
from d1max_site.permissions import DEFAULT_APPLY_ROLE, ROLES

ALERT_KIND = "account_request"
RATE_PER_MIN = 3
MAX_PENDING = 20
TTL_MS = 7 * 86_400_000


class RequestRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AccountRequests:
    def __init__(self, accounts: Accounts, *, now_ms: Callable[[], int],
                 alerts: Any = None) -> None:
        #: 账号表只由 ``accounts.Accounts`` 写(一张表一个模块);批准时经它建账号。
        self.accounts = accounts
        self.db = accounts.db
        self._now = now_ms
        self.alerts = alerts
        self._lock = threading.Lock()
        self._recent: dict[str, deque[int]] = {}

    def _rate(self, source: str, now: int) -> None:
        with self._lock:
            q = self._recent.setdefault(source, deque())
            while q and now - q[0] > 60_000:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                raise RequestRefused(429, "申请太频繁了,过一分钟再试")
            q.append(now)
            if len(self._recent) > 1000:                    # 别让来源表无限长
                for k in [k for k, v in self._recent.items() if not v]:
                    del self._recent[k]

    def _expire(self, now: int) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE account_requests SET state='expired' WHERE state='pending' "
                      "AND expires_ms<?", (now,))

    def submit(self, d: Any, *, source: str) -> dict[str, Any]:
        if not isinstance(d, dict):
            raise RequestRefused(400, "要 JSON")
        name, display, password = d.get("name"), d.get("display"), d.get("password")
        if not isinstance(name, str) or not _NAME.match(name) or name in RESERVED_NAMES \
                or name.startswith("eng-"):
            raise RequestRefused(400, "账号名只能用字母、数字、点、下划线、横线,最多 64 个")
        if not isinstance(display, str) or not display.strip():
            raise RequestRefused(400, "要写名字(给管理员看是谁)")
        try:
            Accounts._check_display(display)
            Accounts._check_password(password)
        except AuthError as exc:
            raise RequestRefused(400, str(exc)) from exc
        now = self._now()
        self._rate(source or "?", now)
        self._expire(now)
        salt = secrets.token_bytes(16)
        digest = _hash(password, salt)
        rid = secrets.token_hex(8)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
                raise RequestRefused(409, f"账号 {name} 已存在,换一个")
            if c.execute("SELECT 1 FROM account_requests WHERE name=? AND state='pending'",
                         (name,)).fetchone():
                raise RequestRefused(409, f"账号 {name} 已在申请中了,等管理员批准")
            n = c.execute("SELECT COUNT(*) FROM account_requests WHERE state='pending'"
                          ).fetchone()[0]
            if n >= MAX_PENDING:
                raise RequestRefused(429, "等批准的申请太多了,请联系管理员")
            c.execute("INSERT INTO account_requests(id, name, display, salt, pw_hash, source, "
                      "created_ms, expires_ms) VALUES (?,?,?,?,?,?,?,?)",
                      (rid, name, display.strip(), salt, digest, (source or "")[:64], now,
                       now + TTL_MS))
        if self.alerts is not None:
            try:
                self.alerts.raise_alert(kind=ALERT_KIND, robot=SITE,
                                        title=f"{display.strip()} 申请账号,等你批准",
                                        detail=f"账号 {name}")
            except Exception:  # noqa: BLE001 —— 提醒记不下来不挡申请
                pass
        return {"request_id": rid, "expires_ms": now + TTL_MS}

    def pending(self) -> list[dict[str, Any]]:
        self._expire(self._now())
        rows = self.db.query("SELECT id, name, display, created_ms, expires_ms, source "
                             "FROM account_requests WHERE state='pending' ORDER BY created_ms")
        return [dict(r) for r in rows]

    def approve(self, rid: str, *, role: Any, by: str) -> dict[str, Any]:
        role = role or DEFAULT_APPLY_ROLE
        if not isinstance(role, str) or role not in ROLES or role == ENGINEER:
            roles = "、".join(sorted(set(ROLES) - {ENGINEER}))
            raise RequestRefused(400, f"权限只能选 {roles}")
        now = self._now()
        self._expire(now)
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM account_requests WHERE id=?", (str(rid)[:32],)).fetchone()
            if r is None:
                raise RequestRefused(404, "没有此条申请")
            if r["state"] != "pending":
                raise RequestRefused(409, "此条申请已处理或者已过期")
            if c.execute("SELECT 1 FROM accounts WHERE name=?", (r["name"],)).fetchone():
                raise RequestRefused(409, f"账号 {r['name']} 已存在")
            self.accounts.add_hashed(r["name"], role=role, salt=r["salt"], pw_hash=r["pw_hash"],
                                     display_name=r["display"], tx=c)
            c.execute("UPDATE account_requests SET state='approved', role=?, decided_by=?, "
                      "decided_ms=? WHERE id=?", (role, by, now, r["id"]))
        self._settle(by)
        return {"name": r["name"], "role": role}

    def reject(self, rid: str, *, by: str) -> None:
        now = self._now()
        with self.db.tx() as c:
            n = c.execute("UPDATE account_requests SET state='rejected', decided_by=?, "
                          "decided_ms=? WHERE id=? AND state='pending'",
                          (by, now, str(rid)[:32])).rowcount
        if not n:
            raise RequestRefused(404, "没有此条等批准的申请")
        self._settle(by)

    def _settle(self, by: str) -> None:
        if self.alerts is None:
            return
        left = self.db.query("SELECT 1 FROM account_requests WHERE state='pending' LIMIT 1")
        if not left:
            try:
                self.alerts.resolve_all(SITE, ALERT_KIND, who=by)
            except Exception:  # noqa: BLE001
                pass

    def login_hint(self, name: str) -> str:
        """登录失败时补一句(只说申请的状态,不说密码对不对)。"""
        rows = self.db.query("SELECT state FROM account_requests WHERE name=? "
                             "ORDER BY created_ms DESC LIMIT 1", (str(name)[:64],))
        if not rows:
            return ""
        return {"pending": "此账号还在等管理员批准",
                "rejected": "此账号的申请被管理员拒了",
                "expired": "此账号的申请已过期(7 天无人批),重新申请"}.get(rows[0]["state"], "")
