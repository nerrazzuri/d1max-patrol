"""外部事件派遣(W00c2c)。台子同 ``test_site_dispatcher.py``。"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from test_site_dispatcher import 台子

from d1max_site.incidents import IncidentAuthError, IncidentDesk
from d1max_site.priorities import EVENT
from d1max_site.standby import StandbyManager


def 签(secret: str, ts: int, body: bytes) -> str:
    return hmac.new(bytes.fromhex(secret), str(ts).encode() + b"." + body,
                    hashlib.sha256).hexdigest()


@pytest.fixture
async def 站(tmp_path):
    t = 台子(tmp_path)
    await t.start()
    t.desk = IncidentDesk(t.db, t.site, now_ms=t.clock)
    t.secret = t.desk.add_source("nvr-1")
    t.desk.set_intercept("gate", map_id="estate-1", map_version="7", x=1.0, y=0.0, yaw=0.0)
    t.desk.map_zone("front-yard", "gate")
    yield t
    await t.close()


async def _报(t, event_id="e1", zone="front-yard", typ="intrusion", ts=None, secret=None):
    body = json.dumps({"event_id": event_id, "type": typ, "zone": zone,
                       "occurred_at": t.clock()}).encode()
    ts = t.clock() if ts is None else ts
    sig = 签(secret or t.secret, ts, body)
    t.desk.verify("nvr-1", str(ts), sig, body)
    return await t.send(t.desk.handle("nvr-1", json.loads(body)))


def _gotos(t):
    return [c for c in reversed(t.site.commands("A", 100)) if c["kind"] == "goto"]


async def test_签名对的入侵事件_派最近的狗去拦截点_优先级80(站):
    t = 站
    await t.run(12)                                  # 让遥测上来
    r = await _报(t)
    assert r["outcome"] == "dispatched" and r["robot_id"] == "A" and r["intercept"] == "gate"
    g = _gotos(t)
    assert len(g) == 1 and g[0]["priority"] == EVENT and g[0]["issued_by"] == "incident:nvr-1"
    assert g[0]["task_id"].startswith("incident-")
    await t.run(200)
    o = await t.dog.odometry()
    assert abs(o.x - 1.0) < 0.2
    assert t.desk.list()[0]["result"] == "done"


async def test_签名错_时间戳过期_源没登记_都拒(站):
    t = 站
    body = b'{"event_id":"x","type":"intrusion","zone":"front-yard"}'
    ts = t.clock()
    with pytest.raises(IncidentAuthError):
        t.desk.verify("nvr-1", str(ts), "00" * 32, body)
    with pytest.raises(IncidentAuthError):
        t.desk.verify("nvr-1", str(ts - 10 * 60_000), 签(t.secret, ts - 10 * 60_000, body), body)
    with pytest.raises(IncidentAuthError):
        t.desk.verify("ghost", str(ts), 签(t.secret, ts, body), body)
    with pytest.raises(IncidentAuthError):
        t.desk.verify("nvr-1", "not-a-number", "x", body)
    assert not _gotos(t)


async def test_同一事件重复_同一防区60秒内合并_都不再派(站):
    t = 站
    await _报(t, "e1")
    again = await _报(t, "e1")
    assert again["outcome"] == "duplicate"
    t.clock.advance(20)
    merged = await _报(t, "e2")
    assert merged["outcome"] == "merged" and merged["merged_into"]
    assert len(_gotos(t)) == 1
    t.clock.advance(61)
    await t.run(1)
    later = await _报(t, "e3")
    assert later["outcome"] != "merged", "窗口过了不再合并"


async def test_防区没映射_类型不认_都记账不派(站):
    t = 站
    assert (await _报(t, "u1", zone="back"))["outcome"] == "unmapped"
    assert (await _报(t, "u2", typ="loitering"))["outcome"] == "ignored_type"
    assert not _gotos(t)
    assert len(t.desk.list()) == 2


async def test_巡检中来事件_抢占巡检去拦截点_到了回待命点(站):
    t = 站
    StandbyManager(t.db, t.site, now_ms=t.clock).set(
        "A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    from test_site_dispatcher import target
    await t.send(t.site.patrol("A", {"mission": "loop", "map_id": "estate-1", "policy": {},
                                     "waypoints": [{"name": "far", "pose": {
                                         "position": {"x": -6.0, "y": 0.0},
                                         "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}]},
                               issued_by="schedule:x", priority=20))
    await t.run(20)
    r = await _报(t)
    assert r["outcome"] == "dispatched"
    await t.run(400)
    kinds = [e["kind"] for e in reversed(t.site.recent_events("A", 200))]
    assert "task_preempted" in kinds
    g = _gotos(t)
    assert [x["task_id"].split("-")[0] for x in g] == ["incident", "standby"]
    assert target


async def test_没有能派的狗_记no_robot(站):
    t = 站
    await t.agent.close()
    await t.broker.drain()
    t.agent = None
    r = await _报(t)
    assert r["outcome"] == "no_robot" and "不在线" in r["note"]


async def test_两台狗_派离拦截点近的那台(站):
    from d1max_contract.messages import MapPose, Telemetry
    t = 站
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    await t.site.add_robot("B")
    a, b = t.site.clients["A"], t.site.clients["B"]
    await t.run(12)
    b.status, b.status_live_at, b.capabilities = a.status, t.clock(), a.capabilities
    far = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=50.0, y=0.0, yaw=0.0)
    near = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=1.1, y=0.0, yaw=0.0)
    a.telemetry = Telemetry(stamp=1, pose=far, battery_pct=90, task_state=None, loc_quality=1)
    b.telemetry = Telemetry(stamp=1, pose=near, battery_pct=90, task_state=None, loc_quality=1)
    picked = t.desk.pick_robot(t.desk.intercept("gate"))
    assert picked[0] == "B"


async def test_拦截点的地图版本对不上_不派(站):
    t = 站
    t.desk.set_intercept("gate", map_id="estate-1", map_version="6", x=1.0, y=0.0, yaw=0.0)
    r = await _报(t)
    assert r["outcome"] == "no_robot" and "estate-1:6" in r["note"], r
    assert not _gotos(t)


def test_登记类的输入校验(tmp_path):
    from d1max_site.db import SiteDB
    from d1max_site.incidents import IncidentError
    db = SiteDB(tmp_path / "s.db")

    class 假派遣:
        def on_event(self, cb):
            pass

    desk = IncidentDesk(db, 假派遣(), now_ms=lambda: 0)
    for bad in (dict(name="a b"), dict(x=float("inf")), dict(x=10**400), dict(map_version="")):
        kw = dict(name="p", map_id="m", map_version="1", x=0.0, y=0.0, yaw=0.0) | bad
        with pytest.raises(IncidentError):
            desk.set_intercept(kw.pop("name"), **kw)
    with pytest.raises(IncidentError):
        desk.map_zone("z", "nope")
    desk.add_source("nvr")
    with pytest.raises(IncidentError):
        desk.add_source("nvr")
    with pytest.raises(IncidentError):
        IncidentDesk.parse({"event_id": "", "type": "intrusion", "zone": "z"})
    db.close()


async def test_狗正在处理另一个事件_不再派它(站):
    """同是事件优先级,狗会回 busy;站点先挑开,记 no_robot 并说明原因。"""
    t = 站
    t.desk.set_intercept("pond", map_id="estate-1", map_version="7", x=-1.0, y=0.0, yaw=0.0)
    t.desk.map_zone("back-yard", "pond")
    r1 = await _报(t, "e1")
    assert r1["outcome"] == "dispatched"
    await t.run(3)
    r2 = await _报(t, "e2", zone="back-yard")
    assert r2["outcome"] == "no_robot" and "另一个事件" in r2["note"], r2



# ------------------------------------------------------------ 内部评审补的

async def test_两个事件同时到_不派给同一台狗(站):
    import asyncio
    t = 站
    t.desk.set_intercept("pond", map_id="estate-1", map_version="7", x=-1.0, y=0.0, yaw=0.0)
    t.desk.map_zone("back-yard", "pond")

    def 签好(eid, zone):
        body = json.dumps({"event_id": eid, "type": "intrusion", "zone": zone}).encode()
        t.desk.verify("nvr-1", str(t.clock()), 签(t.secret, t.clock(), body), body)
        return json.loads(body)

    a, b = 签好("e1", "front-yard"), 签好("e2", "back-yard")
    r1, r2 = await t.send(asyncio.gather(t.desk.handle("nvr-1", a), t.desk.handle("nvr-1", b)))
    outs = sorted([r1["outcome"], r2["outcome"]])
    assert outs == ["dispatched", "no_robot"], (r1, r2)
    assert len(_gotos(t)) == 1


def test_验签的边角(tmp_path):
    from d1max_site.db import SiteDB

    class 假派遣:
        def on_event(self, cb):
            pass

    db = SiteDB(tmp_path / "s.db")
    desk = IncidentDesk(db, 假派遣(), now_ms=lambda: 1_800_000_000_000)
    secret = desk.add_source("nvr")
    body = b'{"event_id":"e","type":"intrusion","zone":"z"}'
    ts = 1_800_000_000_000
    good = 签(secret, ts, body)
    desk.verify("nvr", str(ts), good, body)
    for src, stamp, sig in (("nvr", str(ts), "é" * 64),              # 非 ASCII:不许 500
                            ("nvr", f" +{ts:_}", good),               # 时间戳只认纯数字
                            ("nvr", "0" + str(ts), good),
                            ("nvr", str(ts), good[:-1]),
                            # 按怪写法签好的也不收:时间戳只认十进制纯数字
                            ("nvr", f" +{ts:_}", 签(secret, 0, b"") and hmac.new(
                                bytes.fromhex(secret), f" +{ts:_}".encode() + b"." + body,
                                hashlib.sha256).hexdigest())):
        with pytest.raises(IncidentAuthError):
            desk.verify(src, stamp, sig, body)
    from d1max_site.incidents import IncidentError
    with pytest.raises(IncidentError):
        IncidentDesk.parse({"event_id": "e", "type": "intrusion", "zone": "z",
                            "occurred_at": 10**30})
    with pytest.raises(IncidentError):
        desk.map_zone("z", ["gate"])
    ev = IncidentDesk.parse({"event_id": "e", "type": "intrusion", "zone": "z",
                             "detail": {"blob": "x" * 10_000}})
    assert ev["detail"] == {"truncated": True}
    db.close()


async def test_派单被拒的那条不吸收后续事件(站, monkeypatch):
    t = 站
    real = t.site.goto
    calls = {"n": 0}

    async def 第一次被拒(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            from d1max_site.dispatcher import DispatchRefused
            raise DispatchRefused("模拟:狗不收")
        return await real(*a, **k)

    monkeypatch.setattr(t.site, "goto", 第一次被拒)
    r1 = await _报(t, "e1")
    assert r1["outcome"] == "dispatch_failed"
    r2 = await _报(t, "e2")
    assert r2["outcome"] == "dispatched", "失败的那条不算已出动,不能把后面的并进去"


async def test_意外异常也记dispatch_failed_不留半截(站, monkeypatch):
    t = 站

    async def 炸(*a, **k):
        raise RuntimeError("transport 炸了")

    monkeypatch.setattr(t.site, "goto", 炸)
    r = await _报(t, "e1")
    assert r["outcome"] == "dispatch_failed" and "transport" in r["note"]


async def test_每种去向都推SSE_结果回写也推(站):
    t = 站
    sub = t.site.feed.subscribe()
    await _报(t, "u1", zone="nowhere")
    await _报(t, "e1")
    await _报(t, "e1")                       # duplicate
    await t.run(200)
    got = []
    while (item := sub.get(0)) is not None:
        if item["kind"] == "incident":
            got.append((item["incident"]["outcome"], item["incident"].get("result")))
    outs = [o for o, _ in got]
    assert "unmapped" in outs and "dispatched" in outs and "duplicate" in outs
    assert ("dispatched", "done") in got, got
