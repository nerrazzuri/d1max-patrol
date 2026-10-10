"""P1 推到手机(商业化 A6,决策 53):没确认的 P1 新起、升档跟告警同一个事务排队;推给登记了的
手机(停用的账号不推);只推标题;已经确认、解决的不推;一小时没发出去作废;发不出去退避、连续 10 分钟
报 P2、发出去自动解决;极光的报文;手机(含值守令牌)登记、注销推送号。"""

from __future__ import annotations

import base64
import json
import urllib.error

import pytest
from test_site_alerts_api import _登
from test_site_api import PW, 站

from d1max_site.alert_store import AlertDesk
from d1max_site.db import SiteDB
from d1max_site.push import JPush, PushDesk, PushError, load_sender

NOW = 1_800_000_000_000


class 假极光:
    def __init__(self):
        self.sent, self.fail = [], None

    def send(self, reg, title, body, extras):
        if self.fail:
            raise PushError(self.fail)
        self.sent.append((list(reg), title, body, extras))
        return "m1"


@pytest.fixture
def 台(tmp_path):
    clock = [NOW]
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        for name, dis in (("gina", 0), ("bob", 1)):
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                      "VALUES (?,?,?,?,?,?)", (name, "guard", b"s", b"h", 0, dis))
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,0,0)", ("s-" + name, name))
    desk = AlertDesk(db, now_ms=lambda: clock[0])
    desk.push = True
    j = 假极光()
    p = PushDesk(db, now_ms=lambda: clock[0], sender=j, site_name="翠湖庄园")
    p.alerts = desk
    p.register("gina", "rid-gina", "android", session="s-gina")
    p.register("bob", "rid-bob", "android", session="s-bob")
    yield desk, p, j, clock, db
    db.close()


def _队(db):
    return [dict(r) for r in db.query("SELECT * FROM push_outbox ORDER BY id")]


def test_P1新起排一条_同一条再来不重排_升档再排一条_P2不排(台):
    desk, p, j, clock, db = 台
    desk.raise_alert(kind="dog_sees_person", robot="A", title="狗看见人了:2 个人")
    desk.raise_alert(kind="dog_sees_person", robot="A", title="狗看见人了:2 个人")
    desk.raise_alert(kind="evidence_plain", robot="A", title="狗上没加密")
    assert [(r["tier"], r["title"]) for r in _队(db)] == [(0, "狗看见人了:2 个人")]
    clock[0] += 3 * 60_000
    desk.escalate()
    assert [r["tier"] for r in _队(db)] == [0, 1], "没人确认、升档:再推一次"


def test_只推标题给登记了的手机_停用账号不推(台):
    desk, p, j, clock, db = 台
    desk.raise_alert(kind="dog_sees_person", robot="A", title="狗看见人了",
                     context={"pose": {"x": 3.0, "y": 4.0}})
    p.tick()
    [(reg, title, body, extras)] = j.sent
    assert reg == ["rid-gina"] and title == "翠湖庄园 · P1" and body == "狗看见人了"
    assert set(extras) == {"alert_key"}, "不带照片、位置"
    assert _队(db)[0]["sent_ms"] == NOW
    p.tick()
    assert len(j.sent) == 1, "推过的不再推"


def test_已经确认或解决的不推_一小时没发出去作废_没有手机作废(台):
    desk, p, j, clock, db = 台
    a = desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    desk.ack(a.key, who="gina")
    p.tick()
    assert not j.sent and "确认" in _队(db)[0]["dropped"]
    desk.raise_alert(kind="force_flipped", robot="B", title="狗翻倒了")
    j.fail = "没外网"
    p.tick()
    clock[0] += 3601_000
    p.tick()
    assert "作废" in _队(db)[1]["dropped"]
    j.fail = None
    with db.tx() as c:
        c.execute("DELETE FROM push_devices")
    desk.raise_alert(kind="force_lifted", robot="C", title="被抱起来了")
    p.tick()
    assert _队(db)[-1]["dropped"] == "没有登记的手机"


def test_发不出去退避_连续10分钟报P2_发出去了自动解决_P2自己不推(台):
    desk, p, j, clock, db = 台
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    j.fail = "连不上极光"
    p.tick()
    row = _队(db)[0]
    assert row["attempts"] == 1 and row["next_ms"] == NOW + 10_000
    clock[0] += 5_000
    p.tick()
    assert _队(db)[0]["attempts"] == 1, "退避期内不重发"
    for _ in range(12):
        clock[0] += 60_000
        p.tick()
    stuck = [a for a in desk.book.open() if a.kind == "push_failed"]
    assert len(stuck) == 1 and stuck[0].level.name == "P2"
    assert all(r["robot"] != "site" for r in _队(db)), "P2 自己不推"
    j.fail = None
    clock[0] += 300_000
    p.tick()
    assert j.sent and not [a for a in desk.book.open() if a.kind == "push_failed"]


def test_极光报文_鉴权_出错抛PushError():
    seen = {}

    class 回:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def 开(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        return 回(b'{"sendno":"0","msg_id":"123"}')
    jp = JPush("key1", "sec1", opener=开)
    assert jp.send(["r1"], "站 · P1", "狗翻倒了", {"alert_key": "k"}) == "123"
    req = seen["req"]
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"key1:sec1").decode()
    body = json.loads(req.data)
    assert body["audience"] == {"registration_id": ["r1"]}
    assert body["notification"]["android"]["title"] == "站 · P1"
    assert body["notification"]["android"]["alert"] == "狗翻倒了"

    def 坏(req, timeout):
        raise urllib.error.URLError("没网")
    with pytest.raises(PushError, match="连不上"):
        JPush("k", "s", opener=坏).send(["r"], "t", "b", {})

    def 拒(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    with pytest.raises(PushError, match="401"):
        JPush("k", "s", opener=拒).send(["r"], "t", "b", {})


def test_配置_没配_没密钥文件_配好了(tmp_path):
    assert load_sender({})[0] is None and "没配" in load_sender({})[1]
    s, why = load_sender({"push": {"app_key": "k", "secret_file": str(tmp_path / "无")}})
    assert s is None and "读不了" in why
    (tmp_path / "jpush.secret").write_text("sec\n")
    s, why = load_sender({"push": {"app_key": "k", "secret_file": str(tmp_path / "jpush.secret")}})
    assert isinstance(s, JPush) and why == ""


def test_没配推送_告警不排队_值守汇总说没配(tmp_path):
    from types import SimpleNamespace

    from d1max_site.watch import watch_summary
    db = SiteDB(tmp_path / "s.db")
    desk = AlertDesk(db, now_ms=lambda: NOW)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    assert not db.query("SELECT 1 FROM push_outbox")
    p = PushDesk(db, now_ms=lambda: NOW, why_off="没配推送(site.json 的 push)")
    out = watch_summary(SimpleNamespace(clients={}), desk, now_ms=NOW, push=p)
    assert out["site"]["push"]["configured"] is False and "没配" in out["site"]["why"]["push"]
    db.close()


def test_手机登记注销推送号_值守令牌也能_只能登记给自己(tmp_path):
    s = 站(tmp_path, alerts=True)
    try:
        s.accounts.add("gina", PW, role="guard")
        s.api.push = PushDesk(s.db, now_ms=lambda: NOW, sender=假极光())
        tok = _登(s, "gina")
        code, _ = s.req("POST", "/api/push/devices",
                        {"registration_id": "rid-1", "platform": "android"}, token=tok)
        assert code == 200
        code, d = s.req("POST", "/api/watch/token", {}, token=tok)
        w = d["token"]
        assert s.req("POST", "/api/push/devices", {"registration_id": "rid-2"}, token=w)[0] == 200
        rows = {r["reg_id"]: r["account"] for r in s.db.query("SELECT * FROM push_devices")}
        assert rows == {"rid-1": "gina", "rid-2": "gina"}
        assert s.req("POST", "/api/push/devices", {"registration_id": ""}, token=tok)[0] == 400
        assert s.req("POST", "/api/push/devices/remove", {"registration_id": "rid-1"},
                     token=tok)[0] == 200
        assert [r["reg_id"] for r in s.db.query("SELECT * FROM push_devices")] == ["rid-2"]
        assert s.req("POST", "/api/push/devices", {"registration_id": "x"})[0] == 401
    finally:
        s.close()


async def test_推送那条道在线程里跑_卡住不挡事件循环(monkeypatch):
    import asyncio
    import threading
    import time as _t
    from types import SimpleNamespace

    import d1max_site.main as main
    cls = next(v for v in vars(main).values() if isinstance(v, type) and "_lane" in vars(v))
    where, gate = [], threading.Event()

    def 卡住的推送():
        where.append(threading.current_thread() is threading.main_thread())
        gate.wait(5)
    rt = SimpleNamespace()
    cls._lane(rt, "推送", 卡住的推送, thread=True)
    t0 = _t.monotonic()
    for _ in range(20):
        await asyncio.sleep(0.01)                         # 事件循环照转
    assert _t.monotonic() - t0 < 1 and where == [False], "在线程里、不卡循环"
    gate.set()
    await asyncio.wait(list(rt._lanes.values()), timeout=5)


# ------------------------------------------------------------ A6 外审(F3、F4)


def test_F4_前一条等网络跨过了一小时_后一条不再发(台):
    desk, p, j, clock, db = 台
    desk.raise_alert(kind="dog_sees_person", robot="A", title="一")
    desk.raise_alert(kind="force_flipped", robot="B", title="二")
    clock[0] += 3600_000 - 500
    real = j.send

    def 慢(*a):
        clock[0] += 1000                                  # 这一条等网络等了 1 秒
        return real(*a)
    j.send = 慢
    p.tick()
    q = {r["title"]: r for r in _队(db)}
    assert [b for _, _, b, _ in j.sent] == ["一"], "第二条发之前已经过期:不发"
    assert "作废" in q["二"]["dropped"] and q["一"]["sent_ms"] == clock[0], "记实际发完的时刻"


def test_F4_分批发_第一批发着的时候有人确认了_后面的批次不发(台, monkeypatch):
    import d1max_site.push as push
    desk, p, j, clock, db = 台
    monkeypatch.setattr(push, "BATCH", 1)
    p.register("gina", "rid-gina-2", "android", session="s-gina")
    a = desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    real = j.send

    def 发着确认(*args):
        out = real(*args)
        desk.ack(a.key, who="gina")
        return out
    j.send = 发着确认
    p.tick()
    assert len(j.sent) == 1, "确认以后后面的批次不发"
    row = _队(db)[0]
    assert row["sent_ms"] is not None and "后面的没发" in row["error"]


def test_外审I2_推送跟着退出和撤权走_不跟着闲置过期走(tmp_path):
    """App 被系统清掉、半小时没请求、站点重启了:照样推。明确退出、停用、改角色、重设口令:不推。
    退出以后迟到的登记不能把它加回来。没绑登录的老数据不推。"""
    from d1max_site.accounts import Accounts, _token_hash
    clock = [NOW]
    db = SiteDB(tmp_path / "s.db")
    acc = Accounts(db, now_ms=lambda: clock[0])
    acc.add("gina", PW, role="guard")
    tok = acc.login("gina", PW)
    p = PushDesk(db, now_ms=lambda: clock[0], sender=假极光())
    assert p.register("gina", "rid-1", "android", session=_token_hash(tok))
    p.register("gina", "rid-old", "android")                # 老数据:没绑登录
    clock[0] += 13 * 3600_000                               # 闲置、到期都过了
    assert not acc.session_alive(_token_hash(tok))
    db.close()
    db = SiteDB(tmp_path / "s.db")                          # 站点重启
    acc = Accounts(db, now_ms=lambda: clock[0])
    p = PushDesk(db, now_ms=lambda: clock[0], sender=假极光())
    assert p._devices() == ["rid-1"], "闲置过期不影响推送"
    acc.logout(tok)                                         # 过期了的令牌照样能退出
    assert p._devices() == []
    assert not p.register("gina", "rid-1", "android", session=_token_hash(tok)), "迟到的登记不复活"
    assert p._devices() == []
    for 撤 in (lambda: acc.set_disabled("gina", True), lambda: acc.set_role("gina", "owner"),
              lambda: acc.reset_password("gina", PW + "x")):
        acc.set_disabled("gina", False)
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES ('h1','gina',?,?)", (clock[0], clock[0]))
        assert p.register("gina", "rid-2", "android", session="h1") and p._devices() == ["rid-2"]
        撤()
        assert p._devices() == []
    db.close()


def test_F3_真接口_登记以后退出登录_站点不再推给这台手机(tmp_path):
    s = 站(tmp_path, alerts=True)
    try:
        s.accounts.add("gina", PW, role="guard")
        j = 假极光()
        p = PushDesk(s.db, now_ms=lambda: NOW, sender=j)
        s.api.push = p
        tok = _登(s, "gina")
        assert s.req("POST", "/api/push/devices", {"registration_id": "rid-1"}, token=tok)[0] \
            == 200
        assert p._devices() == ["rid-1"]
        assert s.req("POST", "/api/logout", {}, token=tok)[0] == 200
        assert p._devices() == [], "退出了:登记还在也不推"
    finally:
        s.close()
