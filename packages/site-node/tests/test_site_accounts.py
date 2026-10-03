"""账号(W00c1):scrypt 口令、令牌只存哈希、闲置与绝对过期、按账号名限流。"""

from __future__ import annotations

import pytest

from d1max_site.accounts import ABS_MS, IDLE_MS, Accounts, AuthError, LockedOut
from d1max_site.db import SiteDB


class 钟:
    ms = 1_800_000_000_000

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def acc(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    c = 钟()
    a = Accounts(db, now_ms=c)
    a.add("alice", "correct-horse-battery", role="admin")
    yield a, c, db
    db.close()


def test_口令不存明文_令牌不存明文(acc):
    a, c, db = acc
    tok = a.login("alice", "correct-horse-battery")
    dump = "\n".join(str(tuple(r)) for t in ("accounts", "sessions")
                     for r in db.query(f"SELECT * FROM {t}"))
    assert "correct-horse-battery" not in dump and tok not in dump
    assert a.check(tok) == "alice"


def test_名字与口令规矩(acc):
    a, _, _ = acc
    with pytest.raises(AuthError):
        a.add("bob", "short", role="admin")
    with pytest.raises(AuthError):
        a.add("bad name", "long-enough-pass", role="admin")
    with pytest.raises(AuthError):
        a.add("alice", "long-enough-pass", role="admin")


def test_闲置过期与绝对过期(acc):
    a, c, _ = acc
    tok = a.login("alice", "correct-horse-battery")
    c.ms += IDLE_MS - 1
    assert a.check(tok) == "alice"                    # 用了一下,闲置计时重来
    c.ms += IDLE_MS + 1
    assert a.check(tok) is None
    tok2 = a.login("alice", "correct-horse-battery")
    for _ in range(ABS_MS // (IDLE_MS // 2) + 1):
        c.ms += IDLE_MS // 2
        a.check(tok2)
    assert a.check(tok2) is None, "一直在用也有绝对期"


def test_不存在的账号与错口令同一个说法_错五次锁(acc):
    a, c, _ = acc
    with pytest.raises(AuthError) as e1:
        a.login("nobody", "whatever-whatever")
    with pytest.raises(AuthError) as e2:
        a.login("alice", "wrong-wrong-wrong")
    assert str(e1.value) == str(e2.value)
    for _ in range(4):
        with pytest.raises(AuthError):
            a.login("alice", "wrong-wrong-wrong")
    with pytest.raises(LockedOut):
        a.login("alice", "correct-horse-battery")
    c.ms += 5 * 60_000 + 1
    assert a.login("alice", "correct-horse-battery")


def test_注销(acc):
    a, _, _ = acc
    tok = a.login("alice", "correct-horse-battery")
    a.logout(tok)
    assert a.check(tok) is None


def test_并发猜口令也只放过限额那么多次(acc):
    """锁要在算哈希之前占位:先算完再记失败的话,40 个并发请求能猜 30 多次。"""
    import threading
    a, _, _ = acc
    got: list[str] = []
    lock = threading.Lock()

    def guess():
        try:
            a.login("alice", "wrong-wrong-wrong")
            r = "ok"
        except LockedOut:
            r = "locked"
        except AuthError:
            r = "wrong"
        with lock:
            got.append(r)

    ts = [threading.Thread(target=guess) for _ in range(40)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert got.count("wrong") <= 5, got.count("wrong")
    assert got.count("locked") >= 35


def test_同时算scrypt的不超过上限(acc, monkeypatch):
    """scrypt 每次约 16 MiB:不限并发的话,未登录的人发一堆并发登录就能把内存吃光。"""
    import threading
    import time

    import d1max_site.accounts as mod
    a, _, _ = acc
    now = {"n": 0, "max": 0}
    lk = threading.Lock()
    real = mod._hash

    def 慢(pw, salt):
        with lk:
            now["n"] += 1
            now["max"] = max(now["max"], now["n"])
        time.sleep(0.05)
        try:
            return real(pw, salt)
        finally:
            with lk:
                now["n"] -= 1

    monkeypatch.setattr(mod, "_hash", 慢)
    ts = [threading.Thread(target=lambda i=i: _try(a, f"user{i}")) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert 1 <= now["max"] <= mod.MAX_CONCURRENT_HASH


def _try(a, name):
    try:
        a.login(name, "whatever-whatever")
    except AuthError:
        pass


def test_W17_值守令牌_30天_没有闲置期_只存哈希_每人最多5个(acc):
    from d1max_site.accounts import WATCH_ABS_MS, WATCH_MAX
    a, c, db = acc
    tok, exp = a.issue_watch_token("alice")
    assert exp == c.ms + WATCH_ABS_MS
    assert tok not in "\n".join(str(tuple(r)) for r in db.query("SELECT * FROM sessions"))
    who = a.check(tok)
    assert who == "alice" and who.scope == "watch" and who.role == "admin"
    c.ms += 3 * IDLE_MS                               # 手机离线一阵:没有闲置期
    assert a.check(tok) == "alice"
    c.ms += ABS_MS * 2                                # 普通会话早过期了,值守令牌还在
    assert a.check(tok) == "alice"
    while c.ms < exp:                                 # 一直连着(流每 30 s 复查一次)也有 30 天的头
        c.ms += IDLE_MS // 2
        a.check(tok)
    c.ms = exp + 1
    assert a.check(tok) is None, "30 天到期"
    toks = [a.issue_watch_token("alice")[0] for _ in range(WATCH_MAX + 2)]
    assert [a.check(t) is not None for t in toks] == [False] * 2 + [True] * WATCH_MAX
    normal = a.login("alice", "correct-horse-battery")
    assert a.check(normal).scope == ""


def test_W17_值守令牌_停用改角色改口令一起作废(acc):
    a, c, _ = acc
    a.add("gina", "guard-pass-12345", role="guard")
    tok, _ = a.issue_watch_token("gina")
    a.set_disabled("gina", True)
    assert a.check(tok) is None
    a.set_disabled("gina", False)
    tok, _ = a.issue_watch_token("gina")
    a.reset_password("gina", "another-pass-123")
    assert a.check(tok) is None
