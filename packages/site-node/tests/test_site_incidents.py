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


# ------------------------------------------------------------ 外审补的(W00 系列外审阻断 2)

def _第二台狗(t):
    """B 照抄 A 的在线状态与能力(没有真代理):派单由 ``_假派单`` 接住。"""
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)


async def _两台狗(t):
    _第二台狗(t)
    await t.site.add_robot("B")
    await t.run(12)
    a, b = t.site.clients["A"], t.site.clients["B"]
    b.status, b.status_live_at, b.capabilities = a.status, t.clock(), a.capabilities


class _假派单:
    """替 ``dispatcher.goto``:回执由测试放行(``release``),结果按顺序给。"""

    def __init__(self, results=("accepted",)):
        import asyncio
        self.calls: list[tuple[str, str]] = []
        self.gate = asyncio.Event()
        self.results = list(results)

    async def __call__(self, rid, target, max_speed, *, issued_by, priority, task_id, photo=None):
        self.calls.append((rid, task_id))
        await self.gate.wait()
        res = self.results.pop(0) if self.results else "accepted"
        return {"ack": {"result": res, "reason": "测试给的"}}


def _签好(t, eid, zone="front-yard"):
    body = json.dumps({"event_id": eid, "type": "intrusion", "zone": zone}).encode()
    t.desk.verify("nvr-1", str(t.clock()), 签(t.secret, t.clock(), body), body)
    return json.loads(body)


async def test_两台狗_同防区两条事件并发_只出动一台(站, monkeypatch):
    """第一条还在等回执(dispatching)时同防区第二条到了:并进第一条,不派第二台狗。"""
    import asyncio
    t = 站
    await _两台狗(t)
    fake = _假派单()
    monkeypatch.setattr(t.site, "goto", fake)
    h1 = asyncio.ensure_future(t.desk.handle("nvr-1", _签好(t, "e1")))
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(fake.calls) == 1, "第一条已经在等回执"
    r2 = await asyncio.wait_for(t.desk.handle("nvr-1", _签好(t, "e2")), 2)
    assert r2["outcome"] == "merged", r2
    fake.gate.set()
    r1 = await asyncio.wait_for(h1, 2)
    assert r1["outcome"] == "dispatched" and r2["merged_into"] == r1["id"]
    assert len(fake.calls) == 1, "同一防区只出动一台"


async def test_首条派遣失败_并进来的后续事件接着派(站, monkeypatch):
    import asyncio
    t = 站
    await _两台狗(t)
    fake = _假派单(results=("rejected", "accepted"))
    monkeypatch.setattr(t.site, "goto", fake)
    h1 = asyncio.ensure_future(t.desk.handle("nvr-1", _签好(t, "e1")))
    for _ in range(5):
        await asyncio.sleep(0)
    r2 = await asyncio.wait_for(t.desk.handle("nvr-1", _签好(t, "e2")), 2)
    r3 = await asyncio.wait_for(t.desk.handle("nvr-1", _签好(t, "e3")), 2)
    assert r2["outcome"] == r3["outcome"] == "merged"
    fake.gate.set()
    r1 = await asyncio.wait_for(h1, 2)
    assert r1["outcome"] == "dispatch_failed"
    await asyncio.wait_for(t.desk.drain(), 2)
    rows = {r["event_id"]: r for r in t.desk.list()}
    assert rows["e2"]["outcome"] == "dispatched", rows["e2"]
    assert rows["e3"]["outcome"] == "merged" and rows["e3"]["merged_into"] == rows["e2"]["id"]
    assert len(fake.calls) == 2 and fake.calls[1][1] == rows["e2"]["task_id"]


async def test_相同事件并发提交_一条正常一条duplicate_不500(站, monkeypatch):
    import asyncio
    t = 站
    fake = _假派单()
    monkeypatch.setattr(t.site, "goto", fake)
    ev = _签好(t, "same")
    h1 = asyncio.ensure_future(t.desk.handle("nvr-1", dict(ev)))
    h2 = asyncio.ensure_future(t.desk.handle("nvr-1", dict(ev)))
    for _ in range(5):
        await asyncio.sleep(0)
    fake.gate.set()
    outs = sorted(r["outcome"] for r in await asyncio.wait_for(asyncio.gather(h1, h2), 2))
    assert outs == ["dispatched", "duplicate"], outs
    assert len(fake.calls) == 1


def test_相同事件两个线程同时提交_一条正常一条duplicate(tmp_path):
    """站点 API 的请求可能落在不同线程:占位要在库的锁里一步做完,唯一约束冲突转 duplicate。"""
    import asyncio
    import threading

    from d1max_site.db import SiteDB

    class 假派遣:
        def on_event(self, cb):
            pass

    db = SiteDB(tmp_path / "s.db")
    desk = IncidentDesk(db, 假派遣(), now_ms=lambda: 1_800_000_000_000)
    desk.add_source("nvr")
    ev = {"event_id": "same", "type": "intrusion", "zone": "nowhere"}   # 没映射:不派单
    barrier = threading.Barrier(8)
    outs, errs = [], []

    def 提交():
        barrier.wait()
        try:
            outs.append(asyncio.run(desk.handle("nvr", dict(ev)))["outcome"])
        except Exception as exc:  # noqa: BLE001 - 断言里要看到是什么炸了
            errs.append(exc)

    class _feed:
        @staticmethod
        def publish(_):
            pass
    desk.dispatcher.feed = _feed
    ts = [threading.Thread(target=提交) for _ in range(8)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    assert not errs, errs
    assert sorted(outs) == ["duplicate"] * 7 + ["unmapped"], outs
    db.close()


async def test_收尾时收掉还在等回执的提升派单(站, monkeypatch):
    import asyncio
    t = 站
    await _两台狗(t)
    fake = _假派单(results=("rejected",))
    monkeypatch.setattr(t.site, "goto", fake)
    h1 = asyncio.ensure_future(t.desk.handle("nvr-1", _签好(t, "e1")))
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.wait_for(t.desk.handle("nvr-1", _签好(t, "e2")), 2)
    first, fake.gate = fake.gate, asyncio.Event()     # 提升出来的那条卡在新闸上等回执
    first.set()
    await asyncio.wait_for(h1, 2)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(fake.calls) == 2 and t.desk._followups
    [pending] = list(t.desk._followups)
    import time
    t0 = time.monotonic()
    await asyncio.wait_for(t.desk.close(), 2)
    assert time.monotonic() - t0 < 0.5, "收尾是取消,不是干等回执"
    assert pending.cancelled() and not t.desk._followups
    rows = {r["event_id"]: r for r in t.desk.list()}
    assert rows["e2"]["outcome"] == "dispatch_failed" and "收尾" in rows["e2"]["note"], \
        "被取消的派单不许一直挂在 dispatching(会占住狗与防区)"


def test_站点重启_遗留的dispatching落成失败_不占住狗和防区(tmp_path):
    from d1max_site.db import SiteDB

    class 假派遣:
        def on_event(self, cb):
            pass

    db = SiteDB(tmp_path / "s.db")
    desk = IncidentDesk(db, 假派遣(), now_ms=lambda: 1_800_000_000_000)
    with db.tx() as c:
        c.execute("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome, "
                  "robot_id, task_id, note, detail) VALUES ('nvr','e1','intrusion','z',"
                  "1800000000000,'dispatching','A','incident-x','发送中','{}')")
    IncidentDesk(db, 假派遣(), now_ms=lambda: 1_800_000_000_000)     # 站点重启
    row = desk.list()[0]
    assert row["outcome"] == "dispatch_failed" and "重启" in row["note"], row
    assert desk._open_incident_robots() == set()
    db.close()


async def test_要人监护的狗不接事件派遣(站):
    """W00c6i:CCTV 事件夜里没人在场,要人监护的真狗不派。"""
    t = 站
    await t.run(12)
    for task in ("goto", "patrol"):
        t.site.clients["A"].capabilities.tasks[task]["autonomy"] = "supervised"
    r = await _报(t)
    assert r["outcome"] == "no_robot" and "监护" in r["note"]
    assert not _gotos(t)


# ------------------------------------------------------------ W16:告诉值守的人、事件源管理、限流


@pytest.fixture
async def 带告警(站):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    t = 站
    t.alerts = AlertDesk(t.db, now_ms=t.clock, publish=lambda _: None)
    t.src = SiteAlertSources(t.alerts, now_ms=t.clock)
    t.desk.on_outcome = t.src.on_incident
    t.desk.on_throttled = t.src.on_incident_throttled
    return t


def _开着的(t):
    return t.alerts.open()


async def test_W16_入侵派出去了_报P1有入侵谁去了_合并进来的同一条(带告警):
    t = 带告警
    await t.run(12)
    r = await _报(t, "e1")
    assert r["outcome"] == "dispatched"
    [a] = _开着的(t)
    assert a["kind"] == "intrusion" and a["level"] == "P1" and a["robot"] == "A"
    assert "front-yard" in a["title"] and "A 已出动" in a["title"]
    m = await _报(t, "e2")
    assert m["outcome"] == "merged"
    assert len(_开着的(t)) == 1, "合并进已出动的那条,不另起"
    assert _开着的(t)[0]["count"] == 2, "并进来的那条算一次"
    await t.run(200)                                      # 狗到了:结果回写、这两条又推一遍
    assert t.desk.list()[-1]["result"] == "done"
    assert _开着的(t)[0]["count"] == 2, "同一条事件只报一次,结果回写不再报"


async def test_W16_入侵没狗去_报P1没狗去_防区合在标题里(带告警):
    t = 带告警
    t.desk.set_intercept("back", map_id="estate-1", map_version="99", x=0.0, y=0.0, yaw=0.0)
    t.desk.map_zone("back-yard", "back")                    # 版本对不上:没狗可派
    r = await _报(t, "e1", zone="back-yard")
    assert r["outcome"] == "no_robot"
    r2 = await _报(t, "e2", zone="pond")                    # 没映射
    assert r2["outcome"] == "unmapped"
    [a] = _开着的(t)
    assert a["kind"] == "intrusion_unanswered" and a["level"] == "P1" and a["robot"] == "site"
    assert "back-yard" in a["title"] and "pond" in a["title"] and "没狗去" in a["title"]


async def test_W16_重复的_不认的类型_不报(站):
    t = 站
    told = []
    t.desk.on_outcome = told.append
    await t.run(12)
    await _报(t, "e1")
    assert [r["outcome"] for r in told] == ["dispatched"]
    assert (await _报(t, "e1"))["outcome"] == "duplicate"
    assert (await _报(t, "e3", typ="tamper"))["outcome"] == "ignored_type"
    assert [r["outcome"] for r in told] == ["dispatched"], "重复的、不认的类型都不报"


async def test_W16_告警报不出去_不带走派遣(站):
    t = 站
    await t.run(12)

    def 炸(_row):
        raise RuntimeError("告警台坏了")
    t.desk.on_outcome = 炸
    r = await _报(t, "e1")
    assert r["outcome"] == "dispatched" and len(_gotos(t)) == 1


async def test_W16_每个事件源每分钟有上限_超了不收_报一次P2(带告警):
    from d1max_site.incidents import RATE_PER_MIN
    t = 带告警
    assert all(t.desk.allow("nvr-1") for _ in range(RATE_PER_MIN))
    assert not t.desk.allow("nvr-1") and not t.desk.allow("nvr-1")
    flood = [a for a in _开着的(t) if a["kind"] == "incident_flood"]
    assert len(flood) == 1 and flood[0]["level"] == "P2" and "nvr-1" in flood[0]["title"]
    assert t.desk.allow("nvr-2"), "别的事件源不受影响"
    t.clock.ms += 61_000
    assert t.desk.allow("nvr-1"), "过了一分钟又收"


async def test_W16_事件源换密钥_旧的当场作废_删了验签不过(站):
    t = 站
    body = b'{"event_id":"x","type":"intrusion","zone":"front-yard"}'
    ts = t.clock()
    new = t.desk.rotate_secret("nvr-1")
    assert new != t.secret
    with pytest.raises(IncidentAuthError):
        t.desk.verify("nvr-1", str(ts), 签(t.secret, ts, body), body)
    t.desk.verify("nvr-1", str(ts), 签(new, ts, body), body)
    assert [s["name"] for s in t.desk.sources()] == ["nvr-1"]
    assert "secret" not in t.desk.sources()[0]
    t.desk.remove_source("nvr-1")
    with pytest.raises(IncidentAuthError):
        t.desk.verify("nvr-1", str(ts), 签(new, ts, body), body)
    from d1max_site.incidents import IncidentError
    with pytest.raises(IncidentError):
        t.desk.rotate_secret("nvr-1")
    with pytest.raises(IncidentError):
        t.desk.remove_source("nvr-1")


async def test_W16_拦截点与防区_列出_有防区指着不许删_防区取消映射(站):
    from d1max_site.incidents import IncidentError
    t = 站
    got = t.desk.intercepts()
    assert [i["name"] for i in got["intercepts"]] == ["gate"]
    assert got["zones"] == [{"zone": "front-yard", "intercept": "gate"}]
    with pytest.raises(IncidentError, match="front-yard"):
        t.desk.remove_intercept("gate")
    t.desk.unmap_zone("front-yard")
    t.desk.remove_intercept("gate")
    assert t.desk.intercepts() == {"intercepts": [], "zones": []}
    with pytest.raises(IncidentError):
        t.desk.unmap_zone("front-yard")


# ------------------------------------------------------------ W16 外审:限流并发、告警报不出去要补


def test_W16外审_40个线程同时进来_严格只放30个_只报一次(tmp_path):
    """接口是多线程的。让「读出最近一分钟」与「写回」之间让出 CPU(字典的 get 睡一下),把并发逼出来:
    不加锁 40 个全放行(外审复现)。"""
    import threading
    import time

    from d1max_site.db import SiteDB
    from d1max_site.incidents import RATE_PER_MIN

    class _慢(dict):
        def get(self, *a):
            got = super().get(*a)
            time.sleep(0.002)
            return got

    class _D:
        feed = None

        def on_event(self, _):
            pass
    desk = IncidentDesk(SiteDB(tmp_path / "s.db"), _D(), now_ms=lambda: 1_000_000)
    desk._recent = _慢()
    told: list[str] = []
    desk.on_throttled = told.append
    barrier = threading.Barrier(40)
    got: list[bool] = []

    def go():
        barrier.wait()
        got.append(desk.allow("nvr-1"))
    ts = [threading.Thread(target=go) for _ in range(40)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert got.count(True) == RATE_PER_MIN and got.count(False) == 40 - RATE_PER_MIN
    assert told == ["nvr-1"]


async def test_W16外审_告警第一次报不出去_派遣照常_下一拍补报_只报一次(带告警):
    t = 带告警
    await t.run(12)
    real = t.desk.on_outcome
    tries = []

    def 头一回炸(row):
        tries.append(row["id"])
        if len(tries) == 1:
            raise RuntimeError("告警库一时写不进")
        real(row)
    t.desk.on_outcome = 头一回炸
    r = await _报(t, "e1")
    assert r["outcome"] == "dispatched" and len(_gotos(t)) == 1, "派遣照常"
    assert not _开着的(t) and t.desk.list()[0]["told_ms"] is None, "没报成:不记说过"
    assert t.desk.retell() == 1
    [a] = _开着的(t)
    assert a["kind"] == "intrusion" and a["count"] == 1
    assert t.desk.list()[0]["told_ms"] is not None
    assert t.desk.retell() == 0
    await t.run(200)                                  # 狗到了:结果回写又推一遍,不再报
    assert t.desk.list()[0]["result"] == "done"
    assert _开着的(t)[0]["count"] == 1 and len(tries) == 2


async def test_W16外审_站点重启_没报成的入侵补报_一天以前的不补(带告警):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    t = 带告警
    t.desk.on_outcome = None                          # 「重启前」:告警还没接上就停了
    await _报(t, "old", zone="pond")
    t.clock.ms += 25 * 3600_000
    await _报(t, "e1", zone="pond")
    await _报(t, "e2", zone="lake")
    assert [r["told_ms"] for r in t.desk.list()] == [None, None, None]
    desk2 = IncidentDesk(t.db, t.site, now_ms=t.clock)        # 重启
    alerts2 = AlertDesk(t.db, now_ms=t.clock, publish=lambda _: None)
    desk2.on_outcome = SiteAlertSources(alerts2, now_ms=t.clock).on_incident
    assert desk2.retell() == 2
    [a] = alerts2.open()
    assert a["kind"] == "intrusion_unanswered" and "pond" in a["title"] and "lake" in a["title"]
    old = [r for r in desk2.list() if r["event_id"] == "old"][0]
    assert old["told_ms"] is None, "一天以前的不补"
    assert desk2.retell() == 0


def test_W16外审_老库升级_历史入侵当说过了_不补报(tmp_path):
    import sqlite3

    from d1max_site.db import SiteDB
    p = tmp_path / "s.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, "
              "event_id TEXT NOT NULL, type TEXT NOT NULL, zone TEXT NOT NULL, intercept TEXT, "
              "received_at INTEGER NOT NULL, occurred_at INTEGER, outcome TEXT NOT NULL, "
              "robot_id TEXT, task_id TEXT, result TEXT, merged_into INTEGER, "
              "note TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}', "
              "UNIQUE (source, event_id))")
    c.execute("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome) "
              "VALUES ('nvr-1', 'e0', 'intrusion', 'yard', 1, 'no_robot')")
    c.commit()
    c.close()
    db = SiteDB(p)
    assert db.query("SELECT told_ms FROM incidents")[0]["told_ms"] == 0


async def test_W16外审_站点告警循环每拍都补报_补报炸了不带走告警(monkeypatch):
    import asyncio

    import d1max_site.alert_sources as als
    from d1max_site import main as site_main
    monkeypatch.setattr(als, "STEP_S", 0.01)
    calls: list[str] = []

    class _Stop:
        def is_set(self):
            return len(calls) >= 6

    class _Src:
        def step(self):
            calls.append("step")

    class _Desk:
        def retell(self):
            calls.append("retell")
            raise RuntimeError("库锁住了")
    fake = type("F", (), {})()
    fake._stop, fake.alert_sources, fake.incidents = _Stop(), _Src(), _Desk()
    await asyncio.wait_for(site_main.Server._alert_loop(fake), 5)
    assert calls == ["step", "retell"] * 3


# ------------------------------------------------------------ W17:入侵一来就响、带现场、到了拍照


async def test_W17_入侵一来就到最高档响铃_确认之后再来一条照样响(带告警):
    t = 带告警
    await t.run(12)
    await _报(t, "e1")
    [a] = _开着的(t)
    assert a["channel"] == "sound" and a["escalated"] == 2, "入侵不等 5 分钟"
    t.alerts.ack(a["key"], who="gina")
    t.clock.ms += 120_000
    await t.run(200)                                   # 狗到了、回来
    await _报(t, "e2", zone="front-yard")
    new = [x for x in _开着的(t) if x["acked_ms"] is None and x["kind"].startswith("intrusion")]
    assert len(new) == 1 and new[0]["key"] != a["key"] and new[0]["channel"] == "sound"
    t.alerts.raise_alert(kind="stuck", robot="A", title="别的 P1")
    stuck = [x for x in _开着的(t) if x["kind"] == "stuck"][0]
    assert stuck["channel"] == "screen", "别的 P1 照旧从屏幕开始升档"


async def test_W17_入侵告警带现场_防区_拦截点_哪一趟(带告警):
    t = 带告警
    await t.run(12)
    r = await _报(t, "e1")
    [a] = _开着的(t)
    ctx = a["context"]
    assert ctx["zone"] == "front-yard" and ctx["task_id"] == r["task_id"]
    assert ctx["intercept"] == {"name": "gate", "map_id": "estate-1", "map_version": "7",
                                "x": 1.0, "y": 0.0, "yaw": 0.0}
    t.desk.set_intercept("back", map_id="estate-1", map_version="99", x=0.0, y=0.0, yaw=0.0)
    t.desk.map_zone("back-yard", "back")
    await _报(t, "e2", zone="back-yard")               # 没狗去:有拦截点、没有哪一趟
    u = [x for x in _开着的(t) if x["kind"] == "intrusion_unanswered"][0]
    assert u["context"]["intercept"]["name"] == "back" and "task_id" not in u["context"]


async def test_W17_狗报能拍_派去拦截的goto带photo_不报就不带(站):
    from d1max_agent.bridges.sim_media import sim_media
    t = 站
    await t.run(12)
    await _报(t, "e1")
    assert "photo" not in _gotos(t)[0]["payload"], "狗没报 goto_photo(老代理、真狗还没取流)"
    await t.run(200)
    t.agent.parts.engine._media = sim_media(t.clock)     # 给这只仿真狗装上相机、重报能力
    await t.agent._publish_caps()
    await t.run(10)
    assert t.site.clients["A"].capabilities.tasks["goto_photo"]["cameras"] == ["front"]
    t.clock.ms += 120_000                                # 出了合并窗口;再让遥测新鲜起来
    await t.run(12)
    r = await _报(t, "e2")
    assert r["outcome"] == "dispatched", r
    assert _gotos(t)[-1]["payload"]["photo"] == "front", _gotos(t)
    await t.run(200)
    assert t.desk.list()[0]["result"] == "done"
    shots = list((t.tmp / "agent").rglob("*.jpg")) + list(t.tmp.rglob("runs/**/*.jpg"))
    assert shots, "到了拍的那张在这一趟的归档里"
