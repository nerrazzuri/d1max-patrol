"""同一条告警推了两遍,手机通知栏里只剩一条(推送去重,接 RC 加固外审 I2 的「至少一次」)。

站点照旧至少一次(回执丢了、记账之前崩了都会再发);通知带一个**稳定的折叠标记**,从告警在库里的
主键(``alerts.key``)和档(``tier``)算出来,跟时间、重试次数、推送号、进程都无关:

- FCM:``android.notification.tag`` —— 同一个 tag 的通知,后一条盖掉前一条;**不再带**
  ``android.collapse_key``(FCM 每台手机最多存 4 个不同的折叠键,超了随便丢,离线时会丢掉别的告警)。
- 极光:``options.apns_collapse_id``(iOS),``options.third_party_channel.<厂商>.notify_id``(华为、
  小米、OPPO、vivo、荣耀的厂商通道)。
"""

from __future__ import annotations

import json

from d1max_site.alert_store import AlertDesk
from d1max_site.db import SiteDB
from d1max_site.push import (
    JPUSH_NOTIFY_ID_VENDORS,
    Fcm,
    JPush,
    PushDesk,
    collapse_id,
    vendor_notify_id,
)

NOW = 1_800_000_000_000


class _回:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class _网:
    """记下发往 FCM、极光的每一个请求体;``lose`` 里的推送号:请求到了、回执丢了(超时)。"""

    def __init__(self):
        self.fcm, self.jpush, self.lose = [], [], set()

    def __call__(self, req, timeout):
        if "oauth" in req.full_url:
            return _回({"access_token": "t", "expires_in": 3600})
        body = json.loads(req.data)
        if "jpush" in req.full_url:
            self.jpush.append(body)
            regs = set(body["audience"]["registration_id"])
            reply = {"msg_id": len(self.jpush)}
        else:
            self.fcm.append(body["message"])
            regs = {body["message"]["token"]}
            reply = {"name": f"m{len(self.fcm)}"}
        if regs & self.lose:
            raise TimeoutError("timed out")          # 对方收到了、站点没等到回复
        return _回(reply)


def _通道(net):
    return {"fcm": Fcm("p", "e", "K", opener=net, signer=lambda k, d: b"s", now_s=lambda: 0.0),
            "jpush": JPush("k", "s", opener=net)}


def _台(tmp_path, net):
    clock = [NOW]
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        for i in (1, 2):
            c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                      "VALUES (?,?,?,?,?,0)", (f"u{i}", "guard", b"s", b"h", 0))
            c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                      "VALUES (?,?,0,0)", (f"s{i}", f"u{i}"))
    desk = AlertDesk(db, now_ms=lambda: clock[0])
    desk.push = True
    p = PushDesk(db, now_ms=lambda: clock[0], senders=_通道(net), site_name="站")
    p.alerts = desk
    p.register("u1", "fcm-1", "android", session="s1", provider="fcm")
    p.register("u2", "jp-2", "android", session="s2", provider="jpush")
    return desk, p, clock, db


def _fcm键(msg):
    return msg["android"]["notification"]["tag"]


def _极光键(body):
    o = body["options"]
    return (o["apns_collapse_id"],
            {v: o["third_party_channel"][v]["notify_id"] for v in JPUSH_NOTIFY_ID_VENDORS})


def test_标记_同一条同一档永远一样_不同告警不同档都不一样_长键不截断撞车():
    k = collapse_id("A/dog_sees_person#1", 0)
    assert k == collapse_id("A/dog_sees_person#1", "0") == collapse_id("A/dog_sees_person#1", 0)
    assert len(k.encode()) <= 64, "APNs collapse id 不超过 64 字节"
    assert len({k, collapse_id("A/dog_sees_person#2", 0), collapse_id("B/dog_sees_person#1", 0),
                collapse_id("A/dog_sees_person#1", 1)}) == 4
    long_robot = "R" * 80                          # 原来截到 60 个字:后面的序号截掉了,两条撞成一条
    assert collapse_id(f"{long_robot}/k#1", 0) != collapse_id(f"{long_robot}/k#2", 0)
    n = vendor_notify_id("A/dog_sees_person#1", 0)
    assert isinstance(n, int) and 1 <= n <= 2**31 - 1
    assert n == vendor_notify_id("A/dog_sees_person#1", "0")
    assert n != vendor_notify_id("A/dog_sees_person#2", 0)
    assert set(JPUSH_NOTIFY_ID_VENDORS) == {"huawei", "xiaomi", "oppo", "vivo", "honor"}


def test_FCM报文_带tag_不带collapse_key():
    net = _网()
    f = _通道(net)["fcm"]
    f.send(["t1", "t2"], "站 · P1", "机器人看见人了", {"alert_key": "A/x#1", "tier": "2"})
    tags = [_fcm键(m) for m in net.fcm]
    assert tags == [collapse_id("A/x#1", 2)] * 2
    assert all("collapse_key" not in m["android"] for m in net.fcm), \
        "FCM 每台手机只存 4 个折叠键,超了随便丢:离线时会丢掉别的告警"
    assert all(m["data"] == {"alert_key": "A/x#1", "tier": "2"} for m in net.fcm)


def test_极光报文_带apns_collapse_id和厂商notify_id():
    net = _网()
    _通道(net)["jpush"].send(["r1"], "站 · P1", "机器人看见人了", {"alert_key": "A/x#1", "tier": "0"})
    apns, ids = _极光键(net.jpush[0])
    assert apns == collapse_id("A/x#1", 0)
    assert ids == dict.fromkeys(JPUSH_NOTIFY_ID_VENDORS, vendor_notify_id("A/x#1", 0))
    assert net.jpush[0]["options"]["time_to_live"] >= 1, "原有的选项照旧"


def test_回执丢了重发_两遍的标记一样_手机上盖掉不叠(tmp_path):
    net = _网()
    net.lose = {"fcm-1", "jp-2"}
    desk, p, clock, db = _台(tmp_path, net)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了")
    p.tick()
    net.lose.clear()
    clock[0] += 61_000                             # 退避以后、时间变了:标记不能跟着时间变
    p.tick()
    assert len(net.fcm) == 2 and len(net.jpush) == 2, "至少一次:两个通道都重发了"
    assert _fcm键(net.fcm[0]) == _fcm键(net.fcm[1])
    assert _极光键(net.jpush[0]) == _极光键(net.jpush[1])
    db.close()


def test_发出去了_记账之前站点崩了_重启再发_标记一样(tmp_path):
    """造成重复的那条路:请求成功了,``push_sent`` 还没写进去站点就断电。重启以后队里这条照旧没发,
    再发一遍 —— 手机会收到两遍,通知栏里要只剩一条。"""
    net = _网()
    desk, p, clock, db = _台(tmp_path, net)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了")
    p.tick()
    with db.tx() as c:                             # 断电:记账那一步没落盘
        c.execute("DELETE FROM push_sent")
        c.execute("UPDATE push_outbox SET sent_ms=NULL, done=''")
    clock[0] += 5_000
    p2 = PushDesk(db, now_ms=lambda: clock[0], senders=_通道(net), site_name="站")   # 重启
    p2.alerts = desk
    p2.tick()
    assert len(net.fcm) == 2 and len(net.jpush) == 2
    assert _fcm键(net.fcm[0]) == _fcm键(net.fcm[1])
    assert _极光键(net.jpush[0]) == _极光键(net.jpush[1])
    db.close()


def test_不同的告警_各自一条_升档也另起一条(tmp_path):
    net = _网()
    desk, p, clock, db = _台(tmp_path, net)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了")
    desk.raise_alert(kind="dog_sees_person", robot="B", title="机器人看见人了")
    p.tick()
    clock[0] += 3 * 60_000
    desk.escalate()
    p.tick()
    keys = [(r["alert_key"], r["tier"]) for r in db.query(
        "SELECT alert_key, tier FROM push_outbox ORDER BY id")]
    assert len(keys) == 4, keys                    # A、B 各一条,各升一档
    assert len(net.fcm) == 4 and len(net.jpush) == 4
    assert len({_fcm键(m) for m in net.fcm}) == 4, "不同的告警、不同的档:互不盖掉"
    assert len({json.dumps(_极光键(b)) for b in net.jpush}) == 4
    assert [_fcm键(m) for m in net.fcm] == [collapse_id(k, t) for k, t in keys]
    db.close()



def test_极光不认去重字段回400_通道自己不重发_交回外层_之后不再带():
    """外审 WRAP-I1(2026-10-15):原来 400 在 ``JPush.send`` 里递归再发一次,第二次没过外层「发之前
    按此刻再核」(确认了、过期了照发,TTL 也是旧的)。现在通道只关掉去重字段、报「马上重试」,由外层
    再核后重发。"""
    import io
    import urllib.error

    import pytest

    from d1max_site.push import JPush, PushRetryNow
    sent = []

    def opener(req, timeout):
        body = json.loads(req.data)
        sent.append(body)
        if "third_party_channel" in body["options"] or "apns_collapse_id" in body["options"]:
            raise urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b'{"error":{"code":1003}}'))
        return io.BytesIO(json.dumps({"msg_id": str(len(sent))}).encode())

    j = JPush("k", "s", opener=opener)
    extras = {"alert_key": "A/intrusion#1", "tier": "1"}
    with pytest.raises(PushRetryNow):
        j.send(["r"], "t", "b", extras)
    assert len(sent) == 1, "通道自己不能再发:再发要外层先核"
    assert j.send(["r"], "t", "b", extras) == "2"          # 之后直接不带,不再先挨一次 400
    assert "third_party_channel" not in sent[1]["options"]
    assert "apns_collapse_id" not in sent[1]["options"]


def _极光400台(tmp_path, change):
    """真 AlertDesk、PushDesk、JPush,假网络:第一次请求期间 ``change`` 发生(确认 / 解决 / 过期 /
    只过了一会儿),然后回 400。"""
    import io
    import urllib.error

    from d1max_site.push import EXPIRE_MS
    clock = [NOW]
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        c.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at, disabled) "
                  "VALUES ('u','guard',X'00',X'00',0,0)")
        c.execute("INSERT INTO sessions(token_hash, name, created_at, last_used) "
                  "VALUES ('s','u',0,0)")
    desk = AlertDesk(db, now_ms=lambda: clock[0])
    desk.push = True
    calls = []

    def opener(req, timeout):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            if change == "ack":
                with db.tx() as c:
                    c.execute("UPDATE alerts SET acked_ms=?", (clock[0],))
            elif change == "resolve":
                with db.tx() as c:
                    c.execute("UPDATE alerts SET resolved_ms=?", (clock[0],))
            elif change == "expire":
                clock[0] += EXPIRE_MS + 1
            elif change == "wait":
                clock[0] += 600_000                      # 第一次等了十分钟才回 400
            raise urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b"{}"))
        return _回({"msg_id": str(len(calls))})

    p = PushDesk(db, now_ms=lambda: clock[0], senders={"jpush": JPush("k", "s", opener=opener)})
    p.alerts = desk
    p.register("u", "jp-1", "android", session="s", provider="jpush")
    desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了")
    return p, db, calls


def test_极光400重发之前_告警被确认_解决_过期了就不再发(tmp_path):
    for change in ("ack", "resolve", "expire"):
        p, db, calls = _极光400台(tmp_path / change, change)
        p.tick()
        assert len(calls) == 1, f"{change}:400 之后告警已经{change},不能再发"
        assert db.query("SELECT sent_ms FROM push_outbox")[0]["sent_ms"] is None
        assert not db.query("SELECT 1 FROM push_sent")
        db.close()


def test_极光400重发_同一拍马上发_TTL按此刻重算_记成发了(tmp_path):
    p, db, calls = _极光400台(tmp_path, "wait")
    p.tick()
    assert len(calls) == 2, "去掉去重字段马上重发,不等退避"
    assert "third_party_channel" not in calls[1]["options"]
    assert calls[1]["options"]["time_to_live"] == calls[0]["options"]["time_to_live"] - 600, \
        "第二次的 TTL 要按此刻剩下的算"
    assert db.query("SELECT reg_id FROM push_sent")[0]["reg_id"] == "jp-1"
    row = db.query("SELECT sent_ms, attempts FROM push_outbox")[0]
    assert row["sent_ms"] is not None and row["attempts"] == 0
    db.close()


def test_极光回别的错_不重发():
    import io
    import urllib.error

    import pytest

    from d1max_site.push import JPush, PushError
    sent = []

    def opener(req, timeout):
        sent.append(req)
        raise urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"{}"))

    with pytest.raises(PushError, match="401"):
        JPush("k", "s", opener=opener).send(["r"], "t", "b", {"alert_key": "A/x#1", "tier": "1"})
    assert len(sent) == 1
