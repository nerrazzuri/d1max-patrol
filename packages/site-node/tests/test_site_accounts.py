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
    a.add("alice", "correct-horse-battery")
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
        a.add("bob", "short")
    with pytest.raises(AuthError):
        a.add("bad name", "long-enough-pass")
    with pytest.raises(AuthError):
        a.add("alice", "long-enough-pass")


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
