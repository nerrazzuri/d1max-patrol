"""站点账号的最小版(W00c1):账号 + 口令 + 会话令牌。角色体系(业主/保安/管理员)归 W00c3;
这里只有 ``admin`` 一种,但从第一天起**每条命令都绑在一个已认证的账号上**(总设计 §5)。

- 口令:``hashlib.scrypt``(n=2^14, r=8, p=1),每个账号 16 字节随机盐;比较用常数时间。
  不存在的账号也照算一次哈希,不让响应时间泄露「有没有这个人」。
- 令牌:32 字节随机数,库里只存它的 sha256;闲置 30 分钟、绝对 12 小时过期。
- 限流:同一账号名 5 分钟内错 5 次,锁 5 分钟(内存里记,站点重启清零)。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable

from d1max_site.db import SiteDB

IDLE_MS = 30 * 60_000
ABS_MS = 12 * 3600_000
FAIL_WINDOW_MS = 5 * 60_000
FAIL_LIMIT = 5
LOCK_MS = 5 * 60_000
MIN_PASSWORD = 10
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DUMMY_SALT = b"\0" * 16


class AuthError(RuntimeError):
    pass


class LockedOut(AuthError):
    pass


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


class Accounts:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int], idle_ms: int = IDLE_MS,
                 abs_ms: int = ABS_MS) -> None:
        self.db = db
        self._now = now_ms
        self.idle_ms = idle_ms
        self.abs_ms = abs_ms
        self._fails: dict[str, list[int]] = {}
        self._locked_until: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 账号

    def add(self, name: str, password: str, *, role: str = "admin") -> None:
        if not _NAME.match(name or ""):
            raise AuthError("账号名只许字母、数字、. _ -,1–64 个字符")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"口令至少 {MIN_PASSWORD} 个字符")
        salt = secrets.token_bytes(16)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
                raise AuthError(f"账号 {name} 已存在")
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at) "
                      "VALUES (?,?,?,?,?)", (name, role, salt, _hash(password, salt),
                                             self._now()))

    def names(self) -> list[str]:
        return [r["name"] for r in self.db.query("SELECT name FROM accounts ORDER BY name")]

    # ------------------------------------------------------------ 登录

    def login(self, name: str, password: str) -> str:
        now = self._now()
        with self._lock:
            until = self._locked_until.get(name, 0)
            if now < until:
                raise LockedOut(f"错太多次了,{(until - now) // 1000 + 1} 秒后再试")
        rows = self.db.query("SELECT salt, pw_hash FROM accounts WHERE name=?", (name,))
        salt, stored = (rows[0]["salt"], rows[0]["pw_hash"]) if rows else (_DUMMY_SALT, b"")
        ok = hmac.compare_digest(_hash(password or "", salt), stored) and bool(rows)
        if not ok:
            with self._lock:
                recent = [t for t in self._fails.get(name, []) if now - t < FAIL_WINDOW_MS]
                recent.append(now)
                self._fails[name] = recent
                if len(recent) >= FAIL_LIMIT:
                    self._locked_until[name] = now + LOCK_MS
                    self._fails[name] = []
            raise AuthError("账号或口令不对")
        with self._lock:
            self._fails.pop(name, None)
        token = secrets.token_urlsafe(32)
        with self.db.tx() as c:
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,?,?)", (_token_hash(token), name, now, now))
        return token

    def check(self, token: str | None) -> str | None:
        """令牌有效 → 账号名(并续闲置期);无效或过期 → None(过期的顺手删掉)。"""
        if not token:
            return None
        h = _token_hash(token)
        now = self._now()
        with self.db.tx() as c:
            row = c.execute("SELECT name, created_at, last_used FROM sessions WHERE token_hash=?",
                            (h,)).fetchone()
            if row is None:
                return None
            if now - row["last_used"] > self.idle_ms or now - row["created_at"] > self.abs_ms:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
                return None
            c.execute("UPDATE sessions SET last_used=? WHERE token_hash=?", (now, h))
            return row["name"]

    def logout(self, token: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
