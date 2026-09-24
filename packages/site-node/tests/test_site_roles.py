"""账号与角色(W00c3)。夹具同 ``test_site_api.py``(真 HTTP、真线程)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, target, wall, 站

from d1max_site.accounts import Accounts, AuthError


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def test_业主只能看和叫停_保安能派不能管_管理员都行(站点):
    s = 站点
    olga, gina, alice = _登(s, "olga"), _登(s, "gina"), _登(s, "alice")
    _等(lambda: s.req("GET", "/api/robots/A", token=alice)[1].get("fresh"))
    assert s.req("GET", "/api/robots", token=olga)[0] == 200
    assert s.req("POST", "/api/robots/A/goto", {"target": target(0.3)}, token=olga)[0] == 403
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.3)}, token=gina)
    assert code == 200, d
    assert s.req("POST", "/api/robots/A/abort", {"task_id": d["task_id"]}, token=olga)[0] == 200
    assert s.req("POST", "/api/robots/A/standby", {"name": "x"}, token=gina)[0] == 403
    assert s.req("POST", "/api/bundles", {"path": "/nope"}, token=gina)[0] == 403
    assert s.req("GET", "/api/accounts", token=gina)[0] == 403
    assert s.req("GET", "/api/audit", token=gina)[0] == 403
    assert s.req("GET", "/api/accounts", token=alice)[0] == 200


def test_管理员加账号_改角色_停用_令牌立刻作废(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    code, d = s.req("POST", "/api/accounts", {"name": "ben", "password": PW, "role": "owner"},
                    token=alice)
    assert code == 200 and {"name": "ben", "role": "owner", "disabled": False} in d["accounts"]
    assert s.req("POST", "/api/accounts", {"name": "x", "password": PW, "role": "god"},
                 token=alice)[0] == 400
    assert s.req("POST", "/api/accounts/gina", {"role": "owner"}, token=alice)[0] == 200
    assert s.req("GET", "/api/robots", token=gina)[0] == 401, "改角色吊销旧会话"
    gina = _登(s, "gina")
    assert s.req("POST", "/api/accounts/gina", {"disabled": True}, token=alice)[0] == 200
    assert s.req("GET", "/api/robots", token=gina)[0] == 401
    assert s.req("POST", "/api/login", {"name": "gina", "password": PW})[0] == 401
    assert s.req("POST", "/api/accounts/gina", {"disabled": "yes"}, token=alice)[0] == 400
    assert s.req("POST", "/api/accounts/nobody", {"role": "owner"}, token=alice)[0] == 404


def test_最后一个管理员不能停用也不能降级(站点):
    s = 站点
    alice = _登(s, "alice")
    assert s.req("POST", "/api/accounts/alice", {"disabled": True}, token=alice)[0] == 400
    assert s.req("POST", "/api/accounts/alice", {"role": "guard"}, token=alice)[0] == 400
    s.req("POST", "/api/accounts", {"name": "root2", "password": PW, "role": "admin"},
          token=alice)
    assert s.req("POST", "/api/accounts/alice", {"role": "guard"}, token=alice)[0] == 200


def test_自己改口令要旧口令_改完要重新登录(站点):
    s = 站点
    gina = _登(s, "gina")
    assert s.req("POST", "/api/me/password", {"old": "wrong-wrong-x", "new": "brand-new-pass"},
                 token=gina)[0] == 400
    assert s.req("POST", "/api/me/password", {"old": PW, "new": "brand-new-pass"},
                 token=gina)[0] == 200
    assert s.req("GET", "/api/robots", token=gina)[0] == 401
    assert s.req("POST", "/api/login", {"name": "gina", "password": "brand-new-pass"})[0] == 200


def test_审计记下失败登录_派单_改角色(站点):
    s = 站点
    s.req("POST", "/api/login", {"name": "alice", "password": "wrong-wrong-x"})
    alice = _登(s, "alice")
    _等(lambda: s.req("GET", "/api/robots/A", token=alice)[1].get("fresh"))
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.3)}, token=alice)
    s.req("POST", "/api/accounts/gina", {"role": "owner"}, token=alice)
    code, a = s.req("GET", "/api/audit", token=alice)
    rows = a["audit"]
    assert any(r["actor"] == "login:alice" and r["status"] == 401 for r in rows)
    assert any(r["action"] == "POST /api/robots/A/goto" and r["actor"] == "alice"
               and r["detail"].get("command_id") == d["command_id"] for r in rows)
    assert any(r["action"] == "POST /api/accounts/gina" and r["status"] == 200 for r in rows)
    assert not any(r["action"].startswith("GET") for r in rows), "看不进审计"


def test_账号层的规矩(tmp_path):
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    acc = Accounts(db, now_ms=wall)
    acc.add("a", PW, role="admin")
    with pytest.raises(AuthError):
        acc.add("b", PW, role="root")
    acc.add("b", PW, role="owner")
    tok = acc.login("b", PW)
    who = acc.check(tok)
    assert who == "b" and who.role == "owner"
    acc.set_disabled("b", True)
    assert acc.check(tok) is None
    with pytest.raises(AuthError):
        acc.login("b", PW)
    with pytest.raises(AuthError):
        acc.set_disabled("a", True)
    # 第二道:哪怕会话没被删(直接改库、或将来别的路径停用),停用的账号令牌也不认。
    acc.set_disabled("b", False)
    tok2 = acc.login("b", PW)
    with db.tx() as c:
        c.execute("UPDATE accounts SET disabled=1 WHERE name='b'")
    assert acc.check(tok2) is None
    db.close()
