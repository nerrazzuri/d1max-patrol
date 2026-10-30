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
from d1max_site.push import JPush, PushDesk, PushError, collapse_id, load_sender

NOW = 1_800_000_000_000


class 假极光:
    def __init__(self):
        self.sent, self.fail = [], None

    def send(self, reg, title, body, extras, **kw):
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
    desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了:2 个人")
    desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了:2 个人")
    desk.raise_alert(kind="evidence_plain", robot="A", title="机器人上没加密")
    assert [(r["tier"], r["title"]) for r in _队(db)] == [(0, "机器人看见人了:2 个人")]
    clock[0] += 3 * 60_000
    desk.escalate()
    assert [r["tier"] for r in _队(db)] == [0, 1], "没人确认、升档:再推一次"


def test_只推标题给登记了的手机_停用账号不推(台):
    desk, p, j, clock, db = 台
    desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了",
                     context={"pose": {"x": 3.0, "y": 4.0}})
    p.tick()
    [(reg, title, body, extras)] = j.sent
    assert reg == ["rid-gina"] and title == "翠湖庄园 · P1" and body == "机器人看见人了"
    assert set(extras) == {"alert_key", "tier"}, "不带照片、位置"
    assert _队(db)[0]["sent_ms"] == NOW
    p.tick()
    assert len(j.sent) == 1, "推过的不再推"


def test_W42外审I11_等批准加入_换WiFi等确认_也推_只推给管理员和工程师_标题不写P1(台):
    desk, p, j, clock, db = 台
    with db.tx() as c:
        c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                  "VALUES ('ada', 'admin', x'00', x'00', 0, 0)")
        c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                  "VALUES ('s-ada', 'ada', 0, 0)")
    p.register("ada", "rid-ada", "android", session="s-ada")
    desk.raise_alert(kind="bind_request", robot="D1M-7", title="有一台机器人等批准加入站点")
    desk.raise_alert(kind="wifi_change", robot="A", title="机器人要换 WiFi,等确认")
    p.tick()
    assert sorted((tuple(reg), title) for reg, title, _, _ in j.sent) == [
        (("rid-ada",), "翠湖庄园 · 等你批准"), (("rid-ada",), "翠湖庄园 · 等你批准")]


def test_已经确认或解决的不推_一小时没发出去作废_没有手机作废(台):
    desk, p, j, clock, db = 台
    a = desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    desk.ack(a.key, who="gina")
    p.tick()
    assert not j.sent and "确认" in _队(db)[0]["dropped"]
    desk.raise_alert(kind="force_flipped", robot="B", title="机器人翻倒了")
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
    assert jp.send(["r1"], "站 · P1", "机器人翻倒了", {"alert_key": "k"}) == "123"
    req = seen["req"]
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"key1:sec1").decode()
    body = json.loads(req.data)
    assert body["audience"] == {"registration_id": ["r1"]}
    assert body["notification"]["android"]["title"] == "站 · P1"
    assert body["notification"]["android"]["alert"] == "机器人翻倒了"

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

    def 慢(*a, **kw):
        clock[0] += 1000                                  # 这一条等网络等了 1 秒
        return real(*a, **kw)
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

    def 发着确认(*args, **kw):
        out = real(*args, **kw)
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
    assert p._devices() == {"jpush": ["rid-1"]}, "闲置过期不影响推送"
    acc.logout(tok)                                         # 过期了的令牌照样能退出
    assert p._devices() == {}
    assert not p.register("gina", "rid-1", "android", session=_token_hash(tok)), "迟到的登记不复活"
    assert p._devices() == {}
    for 撤 in (lambda: acc.set_disabled("gina", True), lambda: acc.set_role("gina", "owner"),
              lambda: acc.reset_password("gina", PW + "x")):
        acc.set_disabled("gina", False)
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES ('h1','gina',?,?)", (clock[0], clock[0]))
        assert p.register("gina", "rid-2", "android", session="h1")
        assert p._devices() == {"jpush": ["rid-2"]}
        撤()
        assert p._devices() == {}
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
        assert p._devices() == {"jpush": ["rid-1"]}
        assert s.req("POST", "/api/logout", {}, token=tok)[0] == 200
        assert p._devices() == {}, "退出了:登记还在也不推"
    finally:
        s.close()


# ---------------------------------------------------------------- RC 加固 H9:多通道、FCM


class 假通道:
    def __init__(self, batch=None):
        self.sent, self.fail, self.dead = [], None, []
        if batch:
            self.batch = batch

    def send(self, reg, title, body, extras, **kw):
        if self.fail:
            raise PushError(self.fail)
        self.sent.append(list(reg))
        return "ok"

    def take_dead(self):
        dead, self.dead = self.dead, []
        return dead


@pytest.fixture
def 双通道(tmp_path):
    clock = [NOW]
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        for name in ("gina", "omar", "lee"):
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                      "VALUES (?,?,?,?,?,0)", (name, "guard", b"s", b"h", 0))
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,0,0)", ("s-" + name, name))
    desk = AlertDesk(db, now_ms=lambda: clock[0])
    desk.push = True
    j, f = 假通道(), 假通道()
    p = PushDesk(db, now_ms=lambda: clock[0], senders={"jpush": j, "fcm": f}, site_name="站")
    p.alerts = desk
    p.register("gina", "jp-gina", "android", session="s-gina")                    # 国内手机
    p.register("omar", "fcm-omar", "android", session="s-omar", provider="fcm")   # 带 Google 服务
    yield desk, p, j, f, clock, db
    db.close()


def test_H9_各走各的通道_同一部手机两个都登记了只走FCM(双通道):
    desk, p, j, f, clock, db = 双通道
    p.register("lee", "jp-lee", "android", session="s-lee")
    p.register("lee", "fcm-lee", "android", session="s-lee", provider="fcm")
    assert p._devices() == {"jpush": ["jp-gina"], "fcm": ["fcm-lee", "fcm-omar"]}
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    assert j.sent == [["jp-gina"]] and f.sent == [["fcm-lee", "fcm-omar"]]
    row = _队(db)[0]
    assert row["sent_ms"] == NOW and set(row["done"].split(",")) == {"fcm", "jpush"}


def test_H9_一个通道没发出去_另一个照发_重试只补没发的那个(双通道):
    desk, p, j, f, clock, db = 双通道
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    f.fail = "连不上 FCM"
    p.tick()
    row = _队(db)[0]
    assert j.sent == [["jp-gina"]] and f.sent == []
    assert row["sent_ms"] is None and row["done"] == "jpush" and row["attempts"] == 1
    assert "连不上 FCM" in row["error"]
    f.fail = None
    clock[0] += 11_000
    p.tick()
    assert j.sent == [["jp-gina"]], "极光那边发过了,不重发"
    assert f.sent == [["fcm-omar"]] and _队(db)[0]["sent_ms"] == NOW + 11_000


def test_H9_两个通道都不通_连续10分钟报P2_通了一个还剩一个仍算没发完(双通道):
    desk, p, j, f, clock, db = 双通道
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    j.fail = f.fail = "没外网"
    for _ in range(12):
        p.tick()
        clock[0] += 60_000
    assert [a for a in desk.book.open() if a.kind == "push_failed"]
    j.fail = None
    clock[0] += 300_000
    p.tick()
    assert _队(db)[0]["sent_ms"] is None and _队(db)[0]["done"] == "jpush"
    assert [a for a in desk.book.open() if a.kind == "push_failed"], "FCM 的手机还没收到"


def test_H9_手机登记的通道站点没配_不推它_体检说得出有几台收不到(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                  "VALUES ('omar','guard',?,?,0,0)", (b"s", b"h"))
        c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                  "VALUES ('s-omar','omar',0,0)")
    j = 假通道()
    p = PushDesk(db, now_ms=lambda: NOW, senders={"jpush": j})
    p.register("omar", "fcm-omar", "android", session="s-omar", provider="fcm")
    assert p._devices() == {}
    v = p.view()
    assert v["providers"] == ["jpush"] and v["devices_unreachable"] == 1
    assert v["devices_by_provider"] == {"fcm": 1}
    from d1max_site.health import HealthDesk
    assert "1 台登记的通道站点没配,收不到" in HealthDesk._push_detail(v)
    db.close()


def test_H9_通道说推送号没了_从名单上删掉_发送失败时也删(双通道):
    desk, p, j, f, clock, db = 双通道
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    f.dead, f.fail = ["fcm-omar"], "5xx"
    p.tick()
    assert [r["reg_id"] for r in db.query("SELECT reg_id FROM push_devices")] == ["jp-gina"]


def test_H9_有人确认了_后面的通道不再发(双通道, monkeypatch):
    desk, p, j, f, clock, db = 双通道
    a = desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    real = f.send

    def 发着的时候确认(reg, *rest, **kw):
        out = real(reg, *rest, **kw)
        desk.ack(a.key, who="gina")
        return out
    monkeypatch.setattr(f, "send", 发着的时候确认)
    p.tick()
    assert f.sent == [["fcm-omar"]] and j.sent == [], "FCM 发完有人确认了,极光那批不发"
    row = _队(db)[0]
    assert row["sent_ms"] is not None and "后面的没发" in row["error"] and row["done"] == "fcm"


def test_H9_登记_通道名不认识_令牌太长_拒(双通道):
    _, p, *_ = 双通道
    with pytest.raises(ValueError, match="provider"):
        p.register("gina", "x", "android", session="s-gina", provider="apns")
    p.register("gina", "f" * 400, "android", session="s-gina", provider="fcm")
    with pytest.raises(ValueError):
        p.register("gina", "f" * 400, "android", session="s-gina")       # 极光的号没这么长
    with pytest.raises(ValueError):
        p.register("gina", "f" * 513, "android", session="s-gina", provider="fcm")


def test_H9_配置_老写法是极光_新写法两个并列_一个配坏了另一个照用并说原因(tmp_path):
    from d1max_site.push import Fcm, load_senders
    (tmp_path / "jp").write_text("sec\n", "utf-8")
    acct = tmp_path / "fcm.json"
    acct.write_text(json.dumps({"project_id": "proj", "client_email": "a@b", "private_key": "PEM"}),
                    "utf-8")
    jp = {"app_key": "k", "secret_file": str(tmp_path / "jp")}
    old, why = load_senders({"push": jp})
    assert list(old) == ["jpush"] and why == ""
    both, why = load_senders({"push": {"jpush": jp, "fcm": {"service_account_file": str(acct)}}})
    assert set(both) == {"jpush", "fcm"} and why == "" and isinstance(both["fcm"], Fcm)
    assert both["fcm"].url.endswith("/v1/projects/proj/messages:send")
    half, why = load_senders({"push": {"jpush": jp,
                                       "fcm": {"service_account_file": str(tmp_path / "无")}}})
    assert list(half) == ["jpush"] and "读不了 FCM 服务账号" in why
    acct.write_text('{"project_id": "p"}', "utf-8")
    bad = load_senders({"push": {"fcm": {"service_account_file": str(acct)}}})
    assert bad[0] == {} and "不是服务账号文件" in bad[1]
    assert "不认识的推送通道" in load_senders({"push": {"apns": {}}})[1]
    assert load_sender({"push": {"fcm": {"service_account_file": str(tmp_path / "无")}}})[0] is None


class _回:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http错(code, body):
    import io
    import urllib.error
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


def test_H9_FCM_报文_鉴权换一次令牌反复用_快到期再换():
    from d1max_site.push import Fcm
    calls, clock = [], [1000.0]

    def opener(req, timeout):
        calls.append(req)
        if "oauth" in req.full_url:
            return _回({"access_token": f"tok{len(calls)}", "expires_in": 3600})
        return _回({"name": "projects/p/messages/9"})
    signed = []
    f = Fcm("p", "svc@p.iam", "KEY", opener=opener, now_s=lambda: clock[0],
            signer=lambda key, data: signed.append((key, data)) or b"SIG")
    assert f.send(["t1", "t2"], "站 · P1", "机器人翻倒了", {"alert_key": "A/x#1", "tier": "2"}) \
        == "projects/p/messages/9"
    assert [("oauth" in c.full_url) for c in calls] == [True, False, False], "令牌只换一次"
    key, signing = signed[0]
    head, claims = (json.loads(base64.urlsafe_b64decode(x + b"==")) for x in signing.split(b"."))
    assert key == "KEY" and head == {"alg": "RS256", "typ": "JWT"}
    assert claims == {"iss": "svc@p.iam", "aud": "https://oauth2.googleapis.com/token",
                      "scope": "https://www.googleapis.com/auth/firebase.messaging",
                      "iat": 1000, "exp": 4600}
    assert calls[0].data.decode().endswith("." + "U0lH")           # base64url("SIG")
    msg = json.loads(calls[1].data)["message"]
    assert calls[1].full_url == "https://fcm.googleapis.com/v1/projects/p/messages:send"
    assert calls[1].get_header("Authorization") == "Bearer tok1"
    assert msg["token"] == "t1" and msg["notification"] == {"title": "站 · "
                                                                     "P1", "body": "机器人翻倒了"}
    assert msg["data"] == {"alert_key": "A/x#1", "tier": "2"}
    assert msg["android"]["priority"] == "HIGH" and msg["android"]["ttl"] == "3600s"
    assert msg["android"]["notification"]["tag"] == collapse_id("A/x#1", 2), "重发盖掉原来那条,不叠"
    clock[0] += 3500
    f.send(["t1"], "t", "b", {"alert_key": "k", "tier": "0"})
    assert sum("oauth" in c.full_url for c in calls) == 1, "还有一百秒才到期:接着用"
    clock[0] += 50
    f.send(["t1"], "t", "b", {"alert_key": "k", "tier": "0"})
    assert sum("oauth" in c.full_url for c in calls) == 2, "不到一分钟就到期:换新的"


def test_H9_FCM_令牌失效记下不算失败_别的错算失败并发完其余的_鉴权错了下次重新换():
    from d1max_site.push import Fcm
    seen, auth = [], [0]

    def opener(req, timeout):
        if "oauth" in req.full_url:
            auth[0] += 1
            return _回({"access_token": "tok", "expires_in": 3600})
        tok = json.loads(req.data)["message"]["token"]
        seen.append(tok)
        if tok == "gone":
            raise _http错(404, {"error": {"status": "NOT_FOUND", "details": [
                {"errorCode": "UNREGISTERED"}]}})
        if tok == "busy":
            raise _http错(503, {"error": {"status": "UNAVAILABLE"}})
        if tok == "auth":
            raise _http错(401, {"error": {"status": "UNAUTHENTICATED"}})
        return _回({"name": "m"})
    f = Fcm("p", "e", "K", opener=opener, signer=lambda k, d: b"s", now_s=lambda: 0.0)
    assert f.send(["gone", "ok"], "t", "b", {}) == "m" and f.take_dead() == ["gone"]
    assert f.take_dead() == []
    with pytest.raises(PushError, match=r"FCM 回 503.*1 / 3 台没发出去"):
        f.send(["ok", "busy", "ok2"], "t", "b", {})
    assert seen[-3:] == ["ok", "busy", "ok2"], "一台没发出去不耽误后面的"
    with pytest.raises(PushError, match="401"):
        f.send(["auth"], "t", "b", {})
    f.send(["ok"], "t", "b", {})
    assert auth[0] == 2, "401 之后把令牌扔了重新换"


def test_H9_FCM_鉴权失败_连不上_都是PushError():
    import urllib.error

    from d1max_site.push import Fcm

    def 拒(req, timeout):
        raise _http错(400, {"error": "invalid_grant"})

    def 断(req, timeout):
        raise urllib.error.URLError("no route")
    for opener, why in ((拒, "FCM 鉴权回 400"), (断, "连不上 Google 鉴权")):
        f = Fcm("p", "e", "K", opener=opener, signer=lambda k, d: b"s")
        with pytest.raises(PushError, match=why):
            f.send(["t"], "t", "b", {})
    f = Fcm("p", "e", "K", opener=lambda r, timeout: _回({}), signer=lambda k, d: b"s")
    with pytest.raises(PushError, match="没有访问令牌"):
        f.send(["t"], "t", "b", {})


def test_H9_用系统openssl签RS256_验得过_私钥不对说得出(tmp_path):
    import shutil
    import subprocess

    from d1max_site.push import openssl_rs256
    if shutil.which("openssl") is None:
        pytest.skip("没有 openssl")
    key = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    pub = tmp_path / "p.pem"
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)], check=True,
                   capture_output=True)
    sig = openssl_rs256(key.read_text(), b"head.claims")
    (tmp_path / "sig").write_bytes(sig)
    (tmp_path / "data").write_bytes(b"head.claims")
    ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature",
                         str(tmp_path / "sig"), str(tmp_path / "data")], capture_output=True)
    assert ok.returncode == 0 and len(sig) == 256
    with pytest.raises(PushError, match="私钥不对"):
        openssl_rs256("not a key", b"x")


def test_H9_库从54升上来_老的推送号算极光_队里的照发(tmp_path):
    import sqlite3
    path = tmp_path / "s.db"
    SiteDB(path).close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE push_devices DROP COLUMN provider")
    con.execute("ALTER TABLE push_outbox DROP COLUMN done")
    con.execute("UPDATE meta SET value='54' WHERE key='schema'")
    con.execute("INSERT INTO push_devices(reg_id, account, platform, updated_ms, session) "
                "VALUES ('old','gina','android',0,'s')")
    con.execute("INSERT INTO push_outbox(alert_key, tier, robot, title, created_ms, next_ms) "
                "VALUES ('k',0,'A','t',0,0)")
    con.commit()
    con.close()
    db = SiteDB(path)
    assert db.query("SELECT provider FROM push_devices")[0]["provider"] == "jpush"
    assert db.query("SELECT done FROM push_outbox")[0]["done"] == ""
    from d1max_site.db import SCHEMA_VERSION
    assert db.query("SELECT value FROM meta WHERE key='schema'")[0]["value"] == str(SCHEMA_VERSION)
    db.close()


def test_H9_真接口_登记带通道名_回站点配了哪几个(tmp_path):
    s = 站(tmp_path, alerts=True)
    try:
        s.accounts.add("gina", PW, role="guard")
        s.api.push = PushDesk(s.db, now_ms=lambda: NOW, senders={"jpush": 假通道()})
        tok = _登(s, "gina")
        code, got = s.req("POST", "/api/push/devices",
                          {"registration_id": "fcm-1", "provider": "fcm"}, token=tok)
        assert code == 200 and got == {"ok": True, "providers": ["jpush"]}
        assert s.db.query("SELECT provider FROM push_devices")[0]["provider"] == "fcm"
        assert s.req("POST", "/api/push/devices", {"registration_id": "x", "provider": "apns"},
                     token=tok)[0] == 400
    finally:
        s.close()


# ---------------------------------------------------------------- RC 加固外审(2026-10-13)I1、I2


def _真FCM(opener):
    from d1max_site.push import Fcm
    return Fcm("p", "e", "K", opener=opener, signer=lambda k, d: b"s", now_s=lambda: 0.0)


def _FCM台(tmp_path, opener, n=3):
    clock = [NOW]
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        for i in range(n):
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                      "VALUES (?,?,?,?,?,0)", (f"u{i}", "guard", b"s", b"h", 0))
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,0,0)", (f"s{i}", f"u{i}"))
    desk = AlertDesk(db, now_ms=lambda: clock[0])
    desk.push = True
    p = PushDesk(db, now_ms=lambda: clock[0], senders={"fcm": _真FCM(opener)}, site_name="站")
    p.alerts = desk
    for i in range(n):
        p.register(f"u{i}", f"reg{i}", "android", session=f"s{i}", provider="fcm")
    return desk, p, clock, db


def _tok(req):
    return json.loads(req.data)["message"]["token"]


def test_外审I1_FCM同一批里_发着第一台的时候有人确认了_后面的不发(tmp_path):
    seen, hook = [], [None]

    def opener(req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        seen.append(_tok(req))
        if hook[0]:
            hook[0]()
        return _回({"name": "m"})
    desk, p, clock, db = _FCM台(tmp_path, opener)
    a = desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    hook[0] = lambda: desk.ack(a.key, who="gina") if len(seen) == 1 else None
    p.tick()
    assert seen == ["reg0"], "第一台发着的时候确认了:第二、三台不发"
    row = _队(db)[0]
    assert row["sent_ms"] is not None and "确认" in row["error"]
    db.close()


def test_外审I1_FCM同一批里_发着第一台跨过了一小时_后面的不发_有效期按剩下的给(tmp_path):
    seen, ttl, hook = [], [], [None]

    def opener(req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        seen.append(_tok(req))
        ttl.append(json.loads(req.data)["message"]["android"]["ttl"])
        hook[0]()
        return _回({"name": "m"})
    desk, p, clock, db = _FCM台(tmp_path, opener)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    clock[0] += 50 * 60_000                                   # 排队 50 分钟才轮到(站点刚上外网)
    hook[0] = lambda: clock.__setitem__(0, clock[0] + 6 * 60_000)   # 每台等网络 6 分钟
    p.tick()
    assert seen == ["reg0", "reg1"], "发第三台之前已经过了一小时"
    assert ttl == ["600s", "240s"], "手机那头的有效期按此刻还剩多少给,不是再给一小时"
    db.close()


def test_外审I2_FCM一台成功一台503_重试只补没成功的那台_重启也一样(tmp_path):
    seen, busy = [], {"reg1"}

    def opener(req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        seen.append(_tok(req))
        if _tok(req) in busy:
            raise _http错(503, {"error": {"status": "UNAVAILABLE"}})
        return _回({"name": "m"})
    desk, p, clock, db = _FCM台(tmp_path, opener)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    assert seen == ["reg0", "reg1", "reg2"], "一台没发出去不耽误后面的"
    row = _队(db)[0]
    assert row["sent_ms"] is None and row["attempts"] == 1 and "503" in row["error"]
    busy.clear()
    clock[0] += 11_000
    p2 = PushDesk(db, now_ms=lambda: clock[0], senders={"fcm": _真FCM(opener)}, site_name="站")
    p2.tick()                                                   # 站点重启过:换了一个 PushDesk
    assert seen == ["reg0", "reg1", "reg2", "reg1"], "发成功过的 reg0、reg2 不再发"
    assert _队(db)[0]["sent_ms"] == NOW + 11_000
    db.close()


def test_外审I2_回执丢了_超时_算没成功_下一轮再发这一台_至少一次(tmp_path):
    """手机可能已经收到、站点没等到回复:分不清,按没发成功重发(至少一次投递;文档里明说)。"""
    seen, lost = [], {"reg0"}

    def opener(req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        seen.append(_tok(req))
        if _tok(req) in lost:
            raise TimeoutError("timed out")
        return _回({"name": "m"})
    desk, p, clock, db = _FCM台(tmp_path, opener, n=2)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    lost.clear()
    clock[0] += 11_000
    p.tick()
    assert seen == ["reg0", "reg1", "reg0"]
    db.close()


def test_外审I2_整个通道不通_连着几台都失败就先停_不把每一台都等一遍(tmp_path):
    seen = []

    def opener(req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        seen.append(_tok(req))
        raise OSError("Network is unreachable")
    desk, p, clock, db = _FCM台(tmp_path, opener, n=12)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    assert len(seen) == 3, "一台最多等 10 秒:没外网时 500 台要等一个多小时"
    db.close()


def test_外审I2_极光一批发成功记下这一批_下一批失败只补下一批(台, monkeypatch):
    desk, p, j, clock, db = 台
    monkeypatch.setattr("d1max_site.push.BATCH", 2)
    with db.tx() as c:
        for i in range(3):
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                      "VALUES (?,?,?,?,?,0)", (f"u{i}", "guard", b"s", b"h", 0))
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,0,0)", (f"s{i}", f"u{i}"))
    for i in range(3):
        p.register(f"u{i}", f"rid-u{i}", "android", session=f"s{i}")
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    calls = []
    real = j.send

    def send(reg, title, body, extras, **kw):
        calls.append(list(reg))
        if len(calls) == 2:
            raise PushError("极光回 503")
        return real(reg, title, body, extras, **kw)
    j.send = send
    p.tick()
    assert calls == [["rid-gina", "rid-u0"], ["rid-u1", "rid-u2"]]
    clock[0] += 11_000
    p.tick()
    assert calls[2:] == [["rid-u1", "rid-u2"]], "第一批发成功了:不重发"
    assert _队(db)[0]["sent_ms"] == NOW + 11_000


def test_外审I2_库从55升上来_发了一半的那条_已经发完的通道不重发(tmp_path):
    import sqlite3
    from pathlib import Path
    snap = Path(__file__).parent / "fixtures" / "schema" / "site-v55.sql"
    path = tmp_path / "s.db"
    con = sqlite3.connect(path)
    con.executescript(snap.read_text(encoding="utf-8"))
    con.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                "VALUES ('gina','guard',x'00',x'00',0,0)")
    con.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                "VALUES ('s','gina',0,0)")
    con.execute("INSERT INTO push_devices(reg_id, account, platform, updated_ms, session, provider)"
                " VALUES ('jp','gina','android',0,'s','jpush')")
    con.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                "VALUES ('omar','guard',x'00',x'00',0,0)")
    con.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                "VALUES ('s2','omar',0,0)")
    con.execute("INSERT INTO push_devices(reg_id, account, platform, updated_ms, session, provider)"
                " VALUES ('fc','omar','android',0,'s2','fcm')")
    con.execute("INSERT INTO push_outbox(alert_key, tier, robot, title, created_ms, next_ms, done) "
                "VALUES ('A/x#1',0,'A','x',?,?,'jpush')", (NOW, NOW))
    con.commit()
    con.close()
    db = SiteDB(path)
    j, f = 假通道(), 假通道()
    p = PushDesk(db, now_ms=lambda: NOW, senders={"jpush": j, "fcm": f}, site_name="站")
    p.tick()
    assert j.sent == [], "升级前极光那一路发完了:不重发"
    assert f.sent == [["fc"]], "FCM 那一路当时没发完:照发"
    assert _队(db)[0]["sent_ms"] == NOW
    db.close()


def test_外审I2_隔一台失败一台_不算整个通道不通_每一台都发到(tmp_path):
    seen = []

    def opener(req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        seen.append(_tok(req))
        if int(_tok(req)[3:]) % 2:
            raise _http错(503, {"error": {"status": "UNAVAILABLE"}})
        return _回({"name": "m"})
    desk, p, clock, db = _FCM台(tmp_path, opener, n=8)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    assert len(seen) == 8, "失败的没连着:不停"
    db.close()


def test_外审I1_极光那头替离线手机存多久_也按还剩多少给():
    got = []

    def opener(req, timeout):
        got.append(json.loads(req.data))
        return _回({"msg_id": 1})
    JPush("k", "s", opener=opener).send(["r"], "t", "b", {}, ttl_s=125)
    assert got[0]["options"]["time_to_live"] == 125


# ---------------------------------------------------------------- RC 加固复查 R1


def _双通道真FCM(tmp_path, respond):
    """手机 A(同一次登录登记了 FCM 和极光)、手机 B(只有 FCM);两个通道都配了。"""
    seen = []

    def opener(req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        seen.append(_tok(req))
        return respond(_tok(req))
    clock = [NOW]
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        for name in ("a", "b"):
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                      "VALUES (?,?,?,?,?,0)", (name, "guard", b"s", b"h", 0))
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,0,0)", ("s-" + name, name))
    desk = AlertDesk(db, now_ms=lambda: clock[0])
    desk.push = True
    j = 假通道()

    def mk():
        p = PushDesk(db, now_ms=lambda: clock[0],
                     senders={"jpush": j, "fcm": _真FCM(opener)}, site_name="站")
        p.alerts = desk
        return p
    p = mk()
    p.register("a", "fcm-a", "android", session="s-a", provider="fcm")
    p.register("a", "jp-a", "android", session="s-a")
    p.register("b", "fcm-b", "android", session="s-b", provider="fcm")
    return desk, p, mk, j, seen, clock, db


def _没了():
    return _http错(404, {"error": {"status": "NOT_FOUND",
                                  "details": [{"errorCode": "UNREGISTERED"}]}})


def test_复查R1_手机的FCM令牌失效了_另一台要重试_极光那一路不能记成发完_下一轮补上(tmp_path):
    """A 的 FCM 令牌失效被删、B 回 503。极光这一轮本来没有要发的(A 先走 FCM),却被记成「发完了」;
    下一轮 B 成功,极光被跳过,整条记成发完 —— A 两个通道都没收到这条 P1。"""
    state = {"b": 503}

    def respond(tok):
        if tok == "fcm-a":
            raise _没了()
        if state["b"]:
            raise _http错(state["b"], {"error": {"status": "UNAVAILABLE"}})
        return _回({"name": "m"})
    desk, p, mk, j, seen, clock, db = _双通道真FCM(tmp_path, respond)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    assert _队(db)[0]["sent_ms"] is None
    state["b"] = 0
    clock[0] += 11_000
    mk().tick()                                                 # 站点重启过也一样
    assert j.sent == [["jp-a"]], "A 的 FCM 没了:它登记的极光号要补上"
    assert seen.count("fcm-b") == 2 and _队(db)[0]["sent_ms"] is not None
    db.close()


def test_复查R1_这一轮FCM别的都发成了_只有一台令牌失效_当轮就改走它的极光_再记发完(tmp_path):
    def respond(tok):
        if tok == "fcm-a":
            raise _没了()
        return _回({"name": "m"})
    desk, p, mk, j, seen, clock, db = _双通道真FCM(tmp_path, respond)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    assert j.sent == [["jp-a"]] and seen == ["fcm-a", "fcm-b"]
    assert _队(db)[0]["sent_ms"] == NOW
    p.tick()
    assert j.sent == [["jp-a"]] and seen == ["fcm-a", "fcm-b"], "发完了:不再发"
    db.close()


def test_复查R1_极光先发完了_之后那台手机的FCM令牌才失效_极光照样补给它(tmp_path):
    """极光这一路有别的手机、先发完了;这不等于以后新落到极光上的手机不用发。"""
    state = {"a": 503}

    def respond(tok):
        if tok == "fcm-a":
            if state["a"] == 503:
                raise _http错(503, {"error": {"status": "UNAVAILABLE"}})
            raise _没了()
        return _回({"name": "m"})
    desk, p, mk, j, seen, clock, db = _双通道真FCM(tmp_path, respond)
    with db.tx() as c:
        c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                  "VALUES ('c','guard',x'00',x'00',0,0)")
        c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                  "VALUES ('s-c','c',0,0)")
    p.register("c", "jp-c", "android", session="s-c")           # 只有极光的手机
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    assert j.sent == [["jp-c"]] and _队(db)[0]["sent_ms"] is None
    state["a"] = 404
    clock[0] += 11_000
    p.tick()
    assert j.sent == [["jp-c"], ["jp-a"]], "发过的 jp-c 不重发;新落到极光上的 jp-a 补上"
    assert _队(db)[0]["sent_ms"] is not None
    db.close()


def test_复查R1_手机全没了_发过的算发完_一台都没发成的作废(tmp_path):
    def respond(tok):
        raise _没了()
    desk, p, mk, j, seen, clock, db = _双通道真FCM(tmp_path, respond)
    with db.tx() as c:
        c.execute("DELETE FROM push_devices WHERE reg_id='jp-a'")
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    p.tick()
    row = _队(db)[0]
    assert row["dropped"] == "没有登记的手机" and db.query("SELECT * FROM push_devices") == []
    db.close()


def test_复查R1_通道一次回了失败又说有推送号没了_当轮也重算名单(双通道):
    """一批一个请求的通道(极光那种):这一批没发成,同时告诉站点哪几个号没了。"""
    desk, p, j, f, clock, db = 双通道
    p.register("lee", "jp-lee", "android", session="s-lee")
    p.register("lee", "fcm-lee", "android", session="s-lee", provider="fcm")
    desk.raise_alert(kind="dog_sees_person", robot="A", title="x")
    f.fail, f.dead = "FCM 回 503", ["fcm-lee"]
    p.tick()
    assert ["jp-lee"] in j.sent, "lee 的 FCM 号没了:当轮就走它的极光"
    assert _队(db)[0]["sent_ms"] is None, "fcm-omar 还没发成"
