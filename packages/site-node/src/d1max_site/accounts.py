"""站点账号(W00c1 起;W00c3 加角色、停用、改口令)。**每条命令都绑在一个已认证的账号上**
(总设计 §5)。角色见 ``permissions.py``:admin / guard / owner。

- 停用、改角色、重设口令都**吊销这个账号的全部会话**(权限变了,旧令牌不能继续按旧权限用)。
- **最后一个启用的 admin 不许停用、不许降级**:不然站点上就没人能管账号了。

- 口令:``hashlib.scrypt``(n=2^14, r=8, p=1),每个账号 16 字节随机盐;比较用常数时间。
  不存在的账号也照算一次哈希,不让响应时间泄露「有没有这个人」。
- 令牌:32 字节随机数,库里只存它的 sha256;闲置 30 分钟、绝对 12 小时过期。
- 限流:同一账号名 5 分钟内试 5 次没成,锁 5 分钟(内存里记,站点重启清零)。**每次尝试在算哈希
  之前就在锁里占位**:先算完再记失败的话,并发请求能在计数追上之前猜几十次。
- 同时在算的 scrypt 不超过 ``MAX_CONCURRENT_HASH`` 个:每次约 16 MiB,不限的话未登录的人
  发一堆并发登录就能吃光内存。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable

from d1max_site.db import SiteDB
from d1max_site.permissions import ROLES

IDLE_MS = 30 * 60_000
ABS_MS = 12 * 3600_000
FAIL_WINDOW_MS = 5 * 60_000
FAIL_LIMIT = 5
LOCK_MS = 5 * 60_000
MIN_PASSWORD = 10
MAX_CONCURRENT_HASH = 4
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DUMMY_SALT = b"\0" * 16


class AuthError(RuntimeError):
    pass


class LockedOut(AuthError):
    pass


class Principal(str):
    """已认证的账号名,带着角色。是 ``str`` 的子类:老调用(当成名字用)不受影响。"""

    role: str

    def __new__(cls, name: str, role: str) -> Principal:
        obj = super().__new__(cls, name)
        obj.role = role
        return obj


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
        self._hash_slots = threading.BoundedSemaphore(MAX_CONCURRENT_HASH)

    # ------------------------------------------------------------ 账号

    def add(self, name: str, password: str, *, role: str = "admin") -> None:
        if not isinstance(name, str) or not _NAME.match(name):
            raise AuthError("账号名只许字母、数字、. _ -,1–64 个字符")
        self._check_role(role)
        self._check_password(password)
        salt = secrets.token_bytes(16)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
                raise AuthError(f"账号 {name} 已存在")
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at) "
                      "VALUES (?,?,?,?,?)", (name, role, salt, _hash(password, salt),
                                             self._now()))

    def names(self) -> list[str]:
        return [r["name"] for r in self.db.query("SELECT name FROM accounts ORDER BY name")]

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ROLES:
            raise AuthError(f"角色只有 {', '.join(sorted(ROLES))},给的是 {role!r}")

    @staticmethod
    def _check_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise AuthError(f"口令至少 {MIN_PASSWORD} 个字符")

    def list(self) -> list[dict]:
        return [{"name": r["name"], "role": r["role"], "disabled": bool(r["disabled"])}
                for r in self.db.query("SELECT name, role, disabled FROM accounts ORDER BY name")]

    def _get(self, c, name: str):
        row = c.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        if row is None:
            raise AuthError(f"没有账号 {name}")
        return row

    @staticmethod
    def _other_admins(c, name: str) -> int:
        return c.execute("SELECT count(*) AS n FROM accounts WHERE role='admin' AND disabled=0 "
                         "AND name<>?", (name,)).fetchone()["n"]

    def set_role(self, name: str, role: str) -> None:
        self._check_role(role)
        with self.db.tx() as c:
            row = self._get(c, name)
            if row["role"] == "admin" and role != "admin" and not row["disabled"] \
                    and self._other_admins(c, name) == 0:
                raise AuthError("这是最后一个启用的 admin,不能降级")
            c.execute("UPDATE accounts SET role=? WHERE name=?", (role, name))
            c.execute("DELETE FROM sessions WHERE name=?", (name,))

    def set_disabled(self, name: str, disabled: bool) -> None:
        with self.db.tx() as c:
            row = self._get(c, name)
            if disabled and row["role"] == "admin" and self._other_admins(c, name) == 0:
                raise AuthError("这是最后一个启用的 admin,不能停用")
            c.execute("UPDATE accounts SET disabled=? WHERE name=?", (int(bool(disabled)), name))
            c.execute("DELETE FROM sessions WHERE name=?", (name,))

    def reset_password(self, name: str, password: str) -> None:
        """管理员重设别人的口令。"""
        self._check_password(password)
        salt = secrets.token_bytes(16)
        digest = _hash(password, salt)
        with self.db.tx() as c:
            self._get(c, name)
            c.execute("UPDATE accounts SET salt=?, pw_hash=? WHERE name=?", (salt, digest, name))
            c.execute("DELETE FROM sessions WHERE name=?", (name,))

    def change_password(self, name: str, old: str, new: str) -> None:
        """自己改自己的口令:要旧口令。"""
        rows = self.db.query("SELECT salt, pw_hash FROM accounts WHERE name=?", (name,))
        if not rows:
            raise AuthError("账号或口令不对")
        with self._hash_slots:
            ok = hmac.compare_digest(_hash(old or "", rows[0]["salt"]), rows[0]["pw_hash"])
        if not ok:
            raise AuthError("旧口令不对")
        self.reset_password(name, new)

    # ------------------------------------------------------------ 登录

    def login(self, name: str, password: str) -> str:
        now = self._now()
        with self._lock:
            until = self._locked_until.get(name, 0)
            if now < until:
                raise LockedOut(f"错太多次了,{(until - now) // 1000 + 1} 秒后再试")
            # 先占位再算:这次尝试立刻计数,并发的第 FAIL_LIMIT+1 个请求直接被锁。
            recent = [t for t in self._fails.get(name, []) if now - t < FAIL_WINDOW_MS]
            recent.append(now)
            self._fails[name] = recent
            if len(recent) >= FAIL_LIMIT:
                self._locked_until[name] = now + LOCK_MS
                self._fails[name] = []
        rows = self.db.query("SELECT salt, pw_hash, disabled FROM accounts WHERE name=?",
                             (name,))
        salt, stored = (rows[0]["salt"], rows[0]["pw_hash"]) if rows else (_DUMMY_SALT, b"")
        with self._hash_slots:
            digest = _hash(password or "", salt)
        # 停用的账号跟错口令同一个说法:不告诉外人「有这个账号但停用了」。
        ok = hmac.compare_digest(digest, stored) and bool(rows) and not rows[0]["disabled"]
        if not ok:
            raise AuthError("账号或口令不对")
        with self._lock:
            # 成功:这个名字的失败计数与锁都清掉(占位的那次也算不上失败)。
            self._fails.pop(name, None)
            self._locked_until.pop(name, None)
        token = secrets.token_urlsafe(32)
        with self.db.tx() as c:
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,?,?)", (_token_hash(token), name, now, now))
        return token

    def check(self, token: str | None) -> Principal | None:
        """令牌有效 → 账号(``Principal``:名字 + 角色,并续闲置期);无效、过期或账号停用 → None。"""
        if not token:
            return None
        h = _token_hash(token)
        now = self._now()
        with self.db.tx() as c:
            row = c.execute("SELECT s.name, s.created_at, s.last_used, a.role, a.disabled "
                            "FROM sessions s JOIN accounts a ON a.name = s.name "
                            "WHERE s.token_hash=?", (h,)).fetchone()
            if row is None:
                return None
            if (row["disabled"] or now - row["last_used"] > self.idle_ms
                    or now - row["created_at"] > self.abs_ms):
                c.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
                return None
            c.execute("UPDATE sessions SET last_used=? WHERE token_hash=?", (now, h))
            return Principal(row["name"], row["role"])

    def logout(self, token: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
