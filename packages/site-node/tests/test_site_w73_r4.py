# 站 是从值守测试借来的 pytest 夹具,各测试按参数名用它
# ruff: noqa: F401, F811
"""W73 外审 r4 的复现(原样搬自外审的 /tmp/w73-r4/test_r4_findings.py,断言按「缺陷已修」写)。"""

from __future__ import annotations

from test_site_w73_guard_duty import MAP, _patrols, _方案, _跑, 站

from d1max_agent.engine.preflight import back_mode, departure_line_pct
from d1max_contract.mission import parse_mission
from d1max_patrol.protocol.nav_types import Pose
from d1max_site.battery_swap import SwapDesk
from d1max_site.standby import StandbyManager


def _待命(t, x, home=True):
    s = StandbyManager(t.db, t.site, now_ms=t.clock)
    s.set("A", "default", map_id=MAP[0], map_version=MAP[1], x=float(x), y=0.0, yaw=0.0,
          default=True)
    if home:
        s.set_home("A", name="home", map_id=MAP[0], map_version=MAP[1], x=0.0, y=0.0, yaw=0.0)
    t.duty.standby = s
    s.hold = t.duty.holds
    return s


def _狗上线(t, info, e, pts=None):
    pts = pts or info["points"]
    by_name = {w["name"]: w for w in info["wire"]["waypoints"]}
    wire = {**info["wire"], "waypoints": [by_name[p["name"]] for p in pts]
            + t.duty._standby_tail(e, info, "A", by_name, len(pts))}
    m = parse_mission(wire)
    return departure_line_pct(m, t.agent.parts.home, start=Pose.from_xy_yaw(t.dog.x, t.dog.y),
                              back=back_mode(t.agent.parts.nav, m))


def _回程(t):
    return [c for c in t.site.commands("A", 500)
            if c["kind"] == "goto" and c["task_id"].startswith("standby-")]


async def test_外审r4_1_换电回程中止被拒又断线_离线的空状态不算停了_不叫人过来(站):
    t = 站
    await t.run(12)
    s = _待命(t, 30)
    await t.send(s.return_to("A", issued_by="site:swap", charge=True))
    await t.run(25)
    d = SwapDesk(t.site, now_ms=t.clock, need=lambda _: 35.0, eta_min=lambda _: None,
                 has_charger=lambda _: False, home=s.swap_point)
    d.standby, d.alerts = s, t.duty.alerts
    t.dog.inject_packs(None, 60.0)
    await t.run(12)
    calls = []

    async def 拒(*a, **kw):
        calls.append(a)
        return {"ack": {"result": "rejected"}}
    t.site.abort = 拒
    await t.send(d.tick())
    assert len(calls) == 1 and not any(a["kind"] == "swap_needed" for a in d.alerts.open)
    t.broker.disconnect("dogA")
    await t.broker.drain()
    await t.send(d.tick())
    await t.run(40)
    assert not [a for a in d.alerts.open if a["kind"] == "swap_needed"], \
        "中止被拒以后离线的空状态不算停了,更不能叫人过来装电池"


async def test_外审r4_2_收队时结果事件先到_状态后到_照样回待命点(站):
    t = 站
    _待命(t, 0)
    _方案(t)
    await _跑(t, 3)
    for z in ("东区", "西区"):
        t.arming.set_zone_home(z, False)
    t.arming.set_mode("home", by="olga")
    await t.send(t.duty.tick())
    assert t.duty.status()["runs"][0]["state"] == "standdown"
    old = t.site.clients["A"].status
    await t.run(15)
    assert t.duty._terminal("A", old.task.task_id)
    t.site.clients["A"].status = old                     # 结果事件到了,状态还是旧的
    await t.send(t.duty.tick())
    await _跑(t, 15)
    assert _回程(t), "停车事件先于空状态到:回待命点不能丢"


async def test_外审r4_2_停这一圈已经停了_还没转状态就全撤防_照样回待命点(站):
    t = 站
    _待命(t, 0)
    _方案(t)
    await _跑(t, 3)
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await t.send(t.duty.tick())
    assert t.duty.status()["runs"][0]["state"] == "stopping"
    await t.run(15)
    assert not t.duty._running("A")
    for z in ("东区", "西区"):
        t.arming.set_zone_home(z, False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 15)
    assert _回程(t), "已经停下、还记着 stopping 时全撤防,也要回待命点"


async def test_外审r4_3_出站线按实际派出去的点序算(站):
    t = 站
    _待命(t, 0)
    pts = [{"name": n, "x": x, "y": 0.0, "yaw": 0.0} for n, x in (("A", 2.0), ("B", 100.0),
                                                                  ("C", 200.0))]
    t.store.put_route("night-guard", {"map_id": MAP[0], "map_version": MAP[1], "kind": "guard",
                                      "waypoints": pts, "base_revision": 1}, by="alice")
    t.areas.set(*MAP, "西区", [[199.0, -2.0], [201.0, -2.0], [201.0, 2.0], [199.0, 2.0]],
                by="alice")
    _方案(t)
    t.dog.inject_battery(60.0)
    await t.run(12)
    t.duty._mark_seen(*MAP, "东区", "A", "point")
    e = t.duty.plan()["entries"][0]
    info = t.duty._route(e["route"])
    ordered = t.duty._lap_points(info, ["东区", "西区"], None, False, False)
    dog = _狗上线(t, info, e, ordered)
    await _跑(t, 8)
    assert dog > 60.0
    assert not _patrols(t), "狗上按实际点序要 64%:60% 站点就不派"
    assert t.duty.topup_line("A") is not None and t.duty.topup_line("A") >= dog - 1e-6


async def test_外审r4_4_站点不知道原点_不拿待命点当原点_满电照样出发(站):
    t = 站
    _待命(t, 800, home=False)
    _方案(t)
    t.dog.inject_battery(100.0)
    await t.run(12)
    e = t.duty.plan()["entries"][0]
    info = t.duty._route(e["route"])
    assert t.duty._lap_need(e, info, "A")[0] < 100
    await _跑(t, 5)
    assert _patrols(t), "狗上要 77%,满电不能被站点拦下"


async def test_外审r4_5_任意一台的方案_挑中了就按它的待命点算预测(站):
    t = 站
    _待命(t, 400)
    _方案(t, robot="")
    await _跑(t, 3)
    info = t.duty._route("night-guard")
    actual = t.duty._lap_s(info, (400.0, 0.0))
    assert t.duty.forecast()["entries"][0]["lap_s"] >= actual


async def test_外审r4_6_重连后第一次拿控制权失败_接着再拿(站, monkeypatch):
    t = 站
    r = t.dog
    await t.run(12)
    calls = []
    real = r.acquire_control

    async def 偶尔失败():
        calls.append(t.clock())
        if len(calls) == 1:
            raise RuntimeError("一次性的 hold 回执失败")
        await real()
    monkeypatch.setattr(r, "acquire_control", 偶尔失败)
    await r.close()
    await t.run(220)
    h = await r.health()
    assert len(calls) >= 2 and h.control, "连上了但拿控制权失败:接着再拿"



async def test_外审r4_回归_重连不接着走_不复位急停(站):
    from test_site_dispatcher import target
    t = 站
    await t.run(12)
    await t.send(t.site.goto("A", target(30.0), 0.8, issued_by="alice", priority=60))
    await t.run(30)
    assert t.dog.x > 0.5
    await t.dog.close()
    await t.dog.emergency_stop(True)
    await t.run(160)
    x = t.dog.x
    await t.run(50)
    h = await t.dog.health()
    assert h.link_ok and h.control and h.estop
    assert abs(t.dog.x - x) < 0.01 and not t.duty._running("A")


async def test_外审r4_回归_方案删了_中止被拒两次_照样收队回待命点(站):
    t = 站
    _待命(t, 0)
    _方案(t)
    await _跑(t, 3)
    real, calls = t.site.abort, []

    async def 拒两次(*a, **kw):
        calls.append(a)
        if len(calls) <= 2:
            return {"ack": {"result": "rejected"}}
        return await real(*a, **kw)
    t.site.abort = 拒两次
    t.duty.put_plan({"entries": [], "base_revision": t.duty.plan()["revision"]}, by="alice")
    await _跑(t, 1)
    assert t.duty.status()["runs"][0]["state"] == "standdown"
    await _跑(t, 20)
    assert len(calls) >= 3 and not t.duty.status()["runs"]
    assert _回程(t)


async def test_外审r4_回归_一圈用时按待命点分开缓存_预测取最长(站):
    t = 站
    s = _待命(t, 400)
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock() - 1,
                 expires_at=t.clock() + 10**10)
    s.set("B", "default", map_id=MAP[0], map_version=MAP[1], x=800.0, y=0.0, yaw=0.0,
          default=True)
    t.duty.put_plan({"entries": [{"route": "night-guard", "robot": "A"},
                                 {"route": "night-guard", "robot": "B"}],
                     "base_revision": 0}, by="alice")
    f = t.duty.forecast()
    info = t.duty._route("night-guard")
    longer, shorter = t.duty._lap_s(info, (800.0, 0.0)), t.duty._lap_s(info, (400.0, 0.0))
    assert longer > shorter and all(e["lap_s"] == longer for e in f["entries"])
