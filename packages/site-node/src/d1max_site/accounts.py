"""站点账号(W00c1 起;W00c3 加角色、停用、改口令)。**每条命令都绑在一个已认证的账号上**
(总设计 §5)。角色见 ``permissions.py``:admin / guard / owner。

- 工程师临时账号(W44 第二期,``add_engineer``):角色 ``engineer``,4 或 24 小时到点(``expires_ms``),
  到点当停用 —— 令牌下一次请求就失效、登录不了。只能这样开,不能把别的账号改成工程师,也不能把工程师
  改成别的(只能提前关、删)。
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
import json
import re
import secrets
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from d1max_site.db import SiteDB
from d1max_site.permissions import ROLES

IDLE_MS = 30 * 60_000
ABS_MS = 12 * 3600_000
#: 值守令牌(W17,决策 30):手机后台值守用,**只能看告警**(事件流、告警名单、值守汇总、注销自己)。
#: 30 天到期、没有闲置期(后台连着的流每 30 s 复查一次令牌,复查不算「用」);改角色、停用、改口令
#: 时跟会话一起作废。普通会话 12 小时就到期,后台值守用它的话过一夜就断了。
WATCH_ABS_MS = 30 * 86400_000
#: 一个账号最多留几个值守令牌(每次登录手机都会领一个;多出来的删最旧的)。
WATCH_MAX = 5
#: 值守令牌能走的接口:**只有告警流**(``/api/watch/events``:首帧是未解决的告警、之后只有告警帧与心跳)
#: 和注销自己。W17 外审:原先放行通用的 ``/api/events``,首帧是全部狗的视图、之后是状态、任务事件、
#: 回执、
#: 入侵账 —— 偷到一个 30 天的值守令牌就能一直看整个站点。告警名单、值守汇总也不放(汇总里有狗的状态)
#: 。
WATCH_PATHS = frozenset({("GET", "/api/watch/events"), ("POST", "/api/logout"),
                         # 商业化 A6:值守的手机登记、注销推送号(值守令牌就是为收 P1 发的)
                         ("POST", "/api/push/devices"), ("POST", "/api/push/devices/remove")})
FAIL_WINDOW_MS = 5 * 60_000
FAIL_LIMIT = 5
LOCK_MS = 5 * 60_000
MIN_PASSWORD = 10
MAX_CONCURRENT_HASH = 4
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: 工程师临时账号能开多久(小时)。
ENGINEER_HOURS = (4, 24)
ENGINEER = "engineer"
#: 接口路径里占用的名字(``/api/accounts/engineer`` 是开工程师账号)。
RESERVED_NAMES = frozenset({ENGINEER})
_DUMMY_SALT = b"\0" * 16



def _drop_push(c: Any, name: str) -> None:
    """账号的权限变了(停用、改角色、重设口令):它登记的手机都不再推(A 阶段外审 I2)。
    手机下次登录会重新登记。"""
    c.execute("DELETE FROM push_devices WHERE account=?", (name,))

class AuthError(RuntimeError):
    pass


class LockedOut(AuthError):
    pass


class Principal(str):
    """已认证的账号名,带着角色。是 ``str`` 的子类:老调用(当成名字用)不受影响。"""

    role: str
    #: ``""`` 普通会话;``"watch"`` 值守令牌(只能看告警,见 ``WATCH_PATHS``)。
    scope: str

    def __new__(cls, name: str, role: str, scope: str = "") -> Principal:
        obj = super().__new__(cls, name)
        obj.role = role
        obj.scope = scope
        return obj

    def __getnewargs__(self) -> tuple[str, str]:          # copy / pickle 要两个参数
        return (str(self), self.role)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


#: 登录时报的程序信息里认的几样(别的丢掉):哪一类、版本号、接口级别、什么系统。
CLIENT_KINDS = ("app", "web")
_CLIENT_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,31}$")


def clean_client(obj: Any) -> str:
    """登录时报的 ``client`` 收拾成一小段 JSON(存库用);看不懂的回空串。字段:``kind``(app / web,
    必须有)、``version``、``platform``(字母数字和 ``.+_-``,最长 32)、``api_level``(0–9999 的整数)。
    是客户端自己报的,**只拿来给人看**,不拿来做任何判断。"""
    if not isinstance(obj, dict) or obj.get("kind") not in CLIENT_KINDS:
        return ""
    out: dict[str, Any] = {"kind": obj["kind"]}
    for k in ("version", "platform"):
        v = obj.get(k)
        if isinstance(v, str) and _CLIENT_TEXT.match(v):
            out[k] = v
    lv = obj.get("api_level")
    if isinstance(lv, int) and not isinstance(lv, bool) and 0 <= lv <= 9999:
        out["api_level"] = lv
    return json.dumps(out, sort_keys=True)


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

    def add(self, name: str, password: str, *, role: str, display_name: str = "") -> None:
        """``role`` 必须给:不给就默认成 admin 是个等着出事的坑。``display_name`` 是给人看的真名
        (W20 操作人实名:一人一个账号,账号名就是操作人,显示名让审计、告警里看得出是谁)。"""
        if not isinstance(name, str) or not _NAME.match(name):
            raise AuthError("账号名只许字母、数字、. _ -,1–64 个字符")
        if name in RESERVED_NAMES:
            raise AuthError(f"账号名不能叫 {name}")
        self._check_role(role)
        self._check_password(password)
        self._check_display(display_name)
        salt = secrets.token_bytes(16)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
                raise AuthError(f"账号 {name} 已存在")
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, display_name) "
                      "VALUES (?,?,?,?,?,?)", (name, role, salt, _hash(password, salt),
                                               self._now(), display_name.strip()))

    def names(self) -> list[str]:
        return [r["name"] for r in self.db.query("SELECT name FROM accounts ORDER BY name")]

    @staticmethod
    def _check_role(role: str) -> None:
        if role == ENGINEER:
            raise AuthError("工程师账号只能用「给工程师开临时账号」开")
        if not isinstance(role, str) or role not in ROLES:
            roles = ", ".join(sorted(set(ROLES) - {ENGINEER}))
            raise AuthError(f"角色只有 {roles},给的是 {role!r}")

    def add_engineer(self, display_name: Any, hours: Any, *, by: str) -> tuple[str, str, int]:
        """开工程师临时账号 → ``(账号名, 口令, 到期时刻)``。口令站点随机生成,只回这一次。账号名
        ``eng-YYYYMMDD-HHMM``(站点时区),重了加 ``-2``、``-3``。"""
        if isinstance(hours, bool) or not isinstance(hours, int) or hours not in ENGINEER_HOURS:
            raise AuthError("工程师账号只能开 4 小时或 24 小时")
        if not isinstance(display_name, str):
            raise AuthError("要写工程师的名字")
        self._check_display(display_name)
        from d1max_site.sitetime import site_tz
        now = self._now()
        at = datetime.fromtimestamp(now / 1000, site_tz(self.db))
        base = "eng-" + at.strftime("%Y%m%d-%H%M")
        password = secrets.token_urlsafe(12)
        salt = secrets.token_bytes(16)
        digest = _hash(password, salt)
        until = now + hours * 3_600_000
        with self.db.tx() as c:
            name, n = base, 1
            while c.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
                n += 1
                name = f"{base}-{n}"
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, display_name, "
                      "expires_ms) VALUES (?,?,?,?,?,?,?)",
                      (name, ENGINEER, salt, digest, now, display_name.strip() or name, until))
        return name, password, until

    @staticmethod
    def _check_display(display_name: str) -> None:
        if not isinstance(display_name, str) or len(display_name.strip()) > 64 \
                or any(ord(ch) < 0x20 for ch in display_name):
            raise AuthError("显示名最多 64 个字符,不许有控制字符")

    def display_name(self, name: str) -> str:
        """给人看的名字;没设就是账号名。"""
        rows = self.db.query("SELECT display_name FROM accounts WHERE name=?", (name,))
        return (rows[0]["display_name"] if rows else "") or name

    @staticmethod
    def _check_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise AuthError(f"口令至少 {MIN_PASSWORD} 个字符")

    def list(self) -> list[dict]:
        now = self._now()
        return [{"name": r["name"], "role": r["role"], "disabled": bool(r["disabled"]),
                 "display_name": r["display_name"], "expires_ms": r["expires_ms"],
                 "last_login_ms": r["last_login_ms"],
                 "expired": r["expires_ms"] is not None and now >= r["expires_ms"]}
                for r in self.db.query(
                    "SELECT name, role, disabled, display_name, expires_ms, last_login_ms "
                    "FROM accounts ORDER BY name")]

    def _get(self, c, name: str):
        row = c.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        if row is None:
            raise AuthError(f"没有账号 {name}")
        return row

    @staticmethod
    def _other_admins(c, name: str) -> int:
        return c.execute("SELECT count(*) AS n FROM accounts WHERE role='admin' AND disabled=0 "
                         "AND name<>?", (name,)).fetchone()["n"]

    def update(self, name: str, *, role: str | None = None, disabled: bool | None = None,
               password: str | None = None, display_name: str | None = None) -> None:
        """一次改多项:**先全部校验,再在一个事务里一起改**。有一项不合规矩就一项都不改
        (内部评审:原来逐项提交,后面一项 400 了前面的角色已经改了)。"""
        if role is not None:
            self._check_role(role)
        if disabled is not None and not isinstance(disabled, bool):
            raise AuthError("disabled 要是 true/false")
        if password is not None:
            self._check_password(password)
        if display_name is not None:
            self._check_display(display_name)
        salt = secrets.token_bytes(16) if password is not None else None
        digest = _hash(password, salt) if password is not None else None
        with self.db.tx() as c:
            row = self._get(c, name)
            if row["role"] == ENGINEER and (role is not None or password is not None
                                            or disabled is False):
                raise AuthError("工程师临时账号只能提前关或删,不能改角色、口令,也不能再打开")
            new_role = role if role is not None else row["role"]
            new_disabled = disabled if disabled is not None else bool(row["disabled"])
            was_admin = row["role"] == "admin" and not row["disabled"]
            still_admin = new_role == "admin" and not new_disabled
            if was_admin and not still_admin and self._other_admins(c, name) == 0:
                raise AuthError("这是最后一个启用的 admin,不能停用或降级")
            if role is not None:
                c.execute("UPDATE accounts SET role=? WHERE name=?", (role, name))
            if disabled is not None:
                c.execute("UPDATE accounts SET disabled=? WHERE name=?", (int(disabled), name))
            if password is not None:
                c.execute("UPDATE accounts SET salt=?, pw_hash=? WHERE name=?",
                          (salt, digest, name))
            if display_name is not None:
                c.execute("UPDATE accounts SET display_name=? WHERE name=?",
                          (display_name.strip(), name))
            # 改角色、停用、改口令:旧会话作废。只改显示名不动会话(不是权限变化)。
            if role is not None or disabled is not None or password is not None:
                c.execute("DELETE FROM sessions WHERE name=?", (name,))
                _drop_push(c, name)

    def set_role(self, name: str, role: str) -> None:
        self._check_role(role)
        with self.db.tx() as c:
            row = self._get(c, name)
            if row["role"] == ENGINEER:
                raise AuthError("工程师临时账号不能改角色")
            if row["role"] == "admin" and role != "admin" and not row["disabled"] \
                    and self._other_admins(c, name) == 0:
                raise AuthError("这是最后一个启用的 admin,不能降级")
            c.execute("UPDATE accounts SET role=? WHERE name=?", (role, name))
            c.execute("DELETE FROM sessions WHERE name=?", (name,))
            _drop_push(c, name)

    def set_disabled(self, name: str, disabled: bool) -> None:
        with self.db.tx() as c:
            row = self._get(c, name)
            if disabled and row["role"] == "admin" and self._other_admins(c, name) == 0:
                raise AuthError("这是最后一个启用的 admin,不能停用")
            c.execute("UPDATE accounts SET disabled=? WHERE name=?", (int(bool(disabled)), name))
            c.execute("DELETE FROM sessions WHERE name=?", (name,))
            _drop_push(c, name)

    def remove(self, name: str) -> None:
        """删账号(W44 第二期):连它的会话、登记的手机一起删;做过的事还留在审计里。最后一个启用的
        业主删不掉。"""
        with self.db.tx() as c:
            row = self._get(c, name)
            if row["role"] == "admin" and not row["disabled"] and self._other_admins(c, name) == 0:
                raise AuthError("这是最后一个启用的业主账号,不能删")
            c.execute("DELETE FROM sessions WHERE name=?", (name,))
            _drop_push(c, name)
            c.execute("DELETE FROM accounts WHERE name=?", (name,))

    def _attempt(self, name: str) -> None:
        """记一次尝试(先占位再算,见模块说明);锁着抛 ``LockedOut``。"""
        now = self._now()
        with self._lock:
            until = self._locked_until.get(name, 0)
            if now < until:
                raise LockedOut(f"错太多次了,{(until - now) // 1000 + 1} 秒后再试")
            recent = [t for t in self._fails.get(name, []) if now - t < FAIL_WINDOW_MS]
            recent.append(now)
            self._fails[name] = recent
            if len(recent) >= FAIL_LIMIT:
                self._locked_until[name] = now + LOCK_MS
                self._fails[name] = []

    def _clear(self, name: str) -> None:
        with self._lock:
            self._fails.pop(name, None)
            self._locked_until.pop(name, None)

    def verify(self, name: str, password: Any) -> bool:
        """敏感操作再输一次密码(W44 第二期):对不对。跟登录同一个失败计数和锁(拿着偷来的令牌也不能
        靠这条路无限次地猜)。锁着抛 ``LockedOut``。"""
        self._attempt(name)
        rows = self.db.query("SELECT salt, pw_hash FROM accounts WHERE name=?", (name,))
        salt, stored = (rows[0]["salt"], rows[0]["pw_hash"]) if rows else (_DUMMY_SALT, b"")
        with self._hash_slots:
            ok = hmac.compare_digest(_hash(password if isinstance(password, str) else "", salt),
                                     stored) and bool(rows)
        if ok:
            self._clear(name)
        return ok

    def reset_password(self, name: str, password: str) -> None:
        """管理员重设别人的口令。"""
        self._check_password(password)
        salt = secrets.token_bytes(16)
        digest = _hash(password, salt)
        with self.db.tx() as c:
            self._get(c, name)
            c.execute("UPDATE accounts SET salt=?, pw_hash=? WHERE name=?", (salt, digest, name))
            c.execute("DELETE FROM sessions WHERE name=?", (name,))
            _drop_push(c, name)

    def change_password(self, name: str, old: str, new: str) -> None:
        """自己改自己的口令:要旧口令。**猜旧口令跟猜登录口令同一个计数**:不然拿着偷来的
        令牌可以无限次地试,把真口令试出来。"""
        if not isinstance(old, str) or not isinstance(new, str):
            raise AuthError("old 与 new 要是字符串")
        self._check_password(new)
        now = self._now()
        with self._lock:
            until = self._locked_until.get(name, 0)
            if now < until:
                raise LockedOut(f"错太多次了,{(until - now) // 1000 + 1} 秒后再试")
            recent = [t for t in self._fails.get(name, []) if now - t < FAIL_WINDOW_MS]
            recent.append(now)
            self._fails[name] = recent
            if len(recent) >= FAIL_LIMIT:
                self._locked_until[name] = now + LOCK_MS
                self._fails[name] = []
        rows = self.db.query("SELECT salt, pw_hash FROM accounts WHERE name=?", (name,))
        if not rows:
            raise AuthError("账号或口令不对")
        with self._hash_slots:
            ok = hmac.compare_digest(_hash(old, rows[0]["salt"]), rows[0]["pw_hash"])
        if not ok:
            raise AuthError("旧口令不对")
        with self._lock:
            self._fails.pop(name, None)
            self._locked_until.pop(name, None)
        self.reset_password(name, new)

    # ------------------------------------------------------------ 登录

    def login(self, name: str, password: str, client: Any = None) -> str:
        """``client``:登录的是哪个程序、哪一版(C-B4,见 :func:`clean_client`);记在这次会话上,
        管理员在版本一览里看得到谁还在用老 App。看不懂的不记,**不挡登录**。"""
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
        rows = self.db.query("SELECT salt, pw_hash, disabled, expires_ms FROM accounts "
                             "WHERE name=?", (name,))
        salt, stored = (rows[0]["salt"], rows[0]["pw_hash"]) if rows else (_DUMMY_SALT, b"")
        with self._hash_slots:
            digest = _hash(password or "", salt)
        # 停用的账号跟错口令同一个说法:不告诉外人「有这个账号但停用了」。
        ok = hmac.compare_digest(digest, stored) and bool(rows) and not rows[0]["disabled"] \
            and (rows[0]["expires_ms"] is None or now < rows[0]["expires_ms"])
        if not ok:
            raise AuthError("账号或口令不对")
        with self._lock:
            # 成功:这个名字的失败计数与锁都清掉(占位的那次也算不上失败)。
            self._fails.pop(name, None)
            self._locked_until.pop(name, None)
        token = secrets.token_urlsafe(32)
        with self.db.tx() as c:
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used, client) "
                      "VALUES (?,?,?,?,?)",
                      (_token_hash(token), name, now, now, clean_client(client)))
            c.execute("UPDATE accounts SET last_login_ms=? WHERE name=?", (now, name))
        return token

    def clients(self) -> list[dict[str, Any]]:
        """还有效的登录各是哪个程序、哪一版(值守令牌、没报的不列)。最近用过的在前。"""
        now = self._now()
        out = []
        for r in self.db.query(
                "SELECT s.name, s.created_at, s.last_used, s.client FROM sessions s "
                "JOIN accounts a ON a.name=s.name WHERE s.client<>'' AND s.scope='' "
                "AND a.disabled=0 ORDER BY s.last_used DESC, s.name"):
            if now - r["last_used"] > self.idle_ms or now - r["created_at"] > self.abs_ms:
                continue
            try:
                c = json.loads(r["client"])
            except ValueError:
                continue
            out.append({"name": r["name"], "created_at": r["created_at"],
                        "last_used": r["last_used"], **c})
        return out

    def check(self, token: str | None) -> Principal | None:
        """令牌有效 → 账号(``Principal``:名字 + 角色,并续闲置期);无效、过期或账号停用 → None。"""
        if not token:
            return None
        h = _token_hash(token)
        now = self._now()
        with self.db.tx() as c:
            row = c.execute("SELECT s.name, s.created_at, s.last_used, s.scope, a.role, "
                            "a.disabled, a.expires_ms FROM sessions s JOIN accounts a "
                            "ON a.name = s.name WHERE s.token_hash=?", (h,)).fetchone()
            if row is None:
                return None
            watch = row["scope"] == "watch"
            expired = (now - row["created_at"] > WATCH_ABS_MS if watch
                       else now - row["last_used"] > self.idle_ms
                       or now - row["created_at"] > self.abs_ms)
            gone = row["expires_ms"] is not None and now >= row["expires_ms"]   # 工程师到点
            if row["disabled"] or expired or gone:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
                return None
            c.execute("UPDATE sessions SET last_used=? WHERE token_hash=?", (now, h))
            return Principal(row["name"], row["role"], row["scope"])

    def session_alive(self, token_hash: str) -> bool:
        """这个令牌(哈希)现在还有效吗:跟 :meth:`check` 同样的规矩,但不续期、不删(推送名单用,A6)。"""
        if not token_hash:
            return False
        rows = self.db.query("SELECT s.created_at, s.last_used, s.scope, a.disabled, a.expires_ms "
                             "FROM sessions s JOIN accounts a ON a.name = s.name "
                             "WHERE s.token_hash=?",
                             (token_hash,))
        if not rows:
            return False
        r, now = rows[0], self._now()
        if r["disabled"] or (r["expires_ms"] is not None and now >= r["expires_ms"]):
            return False
        if r["scope"] == "watch":
            return now - r["created_at"] <= WATCH_ABS_MS
        return now - r["last_used"] <= self.idle_ms and now - r["created_at"] <= self.abs_ms

    def issue_watch_token(self, name: str) -> tuple[str, int]:
        """给这个账号发一个值守令牌(W17)。回 ``(令牌, 到期时刻)``。令牌只出现这一次,库里只存
        哈希。"""
        token = secrets.token_urlsafe(32)
        now = self._now()
        with self.db.tx() as c:
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used, scope) "
                      "VALUES (?,?,?,?, 'watch')", (_token_hash(token), name, now, now))
            c.execute("DELETE FROM sessions WHERE scope='watch' AND name=? AND token_hash NOT IN "
                      "(SELECT token_hash FROM sessions WHERE scope='watch' AND name=? "
                      "ORDER BY created_at DESC, rowid DESC LIMIT ?)", (name, name, WATCH_MAX))
        return token, now + WATCH_ABS_MS

    def logout(self, token: str) -> None:
        """退出登录。**这次登录登记的手机一并不再推**(A 阶段外审 I2:推送跟着登记它的那次登录的
        「退出」走,不跟着它的「闲置过期」走)。令牌已经过期了也照样删:拿得出令牌就是那部手机。"""
        h = _token_hash(token)
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
            c.execute("DELETE FROM push_devices WHERE session=? AND session != ''", (h,))
