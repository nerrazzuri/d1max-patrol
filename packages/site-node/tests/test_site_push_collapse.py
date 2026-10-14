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
    f.send(["t1", "t2"], "站 · P1", "狗看见人了", {"alert_key": "A/x#1", "tier": "2"})
    tags = [_fcm键(m) for m in net.fcm]
    assert tags == [collapse_id("A/x#1", 2)] * 2
    assert all("collapse_key" not in m["android"] for m in net.fcm), \
        "FCM 每台手机只存 4 个折叠键,超了随便丢:离线时会丢掉别的告警"
    assert all(m["data"] == {"alert_key": "A/x#1", "tier": "2"} for m in net.fcm)


def test_极光报文_带apns_collapse_id和厂商notify_id():
    net = _网()
    _通道(net)["jpush"].send(["r1"], "站 · P1", "狗看见人了", {"alert_key": "A/x#1", "tier": "0"})
    apns, ids = _极光键(net.jpush[0])
    assert apns == collapse_id("A/x#1", 0)
    assert ids == dict.fromkeys(JPUSH_NOTIFY_ID_VENDORS, vendor_notify_id("A/x#1", 0))
    assert net.jpush[0]["options"]["time_to_live"] >= 1, "原有的选项照旧"


def test_回执丢了重发_两遍的标记一样_手机上盖掉不叠(tmp_path):
    net = _网()
    net.lose = {"fcm-1", "jp-2"}
    desk, p, clock, db = _台(tmp_path, net)
    desk.raise_alert(kind="dog_sees_person", robot="A", title="狗看见人了")
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
    desk.raise_alert(kind="dog_sees_person", robot="A", title="狗看见人了")
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
    desk.raise_alert(kind="dog_sees_person", robot="A", title="狗看见人了")
    desk.raise_alert(kind="dog_sees_person", robot="B", title="狗看见人了")
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

