"""自动回充,站点这一头(W13,决策 25、45)。派遣器 + 真代理 + 带假充电桩的仿真狗:低电量去桩前、对桩、
充到 90% 出桩、这一轮完;充电中来了入侵:电量够就出桩去、回来接着充,不够就不派;失败报 P1;人中止歇
10 分钟;站点重启接着办。"""

from __future__ import annotations

import pytest
from test_site_dispatcher import MAP, target, 台子

from d1max_adapter_sim.robot import SimRobot
from d1max_site.charging import ChargeDesk, ChargeError

CHARGER = (2.0, 0.0, 0.0)


class 假告警台:
    def __init__(self):
        self.raised = []

    def raise_alert(self, **kw):
        self.raised.append(kw)

    def has_open(self, robot, kind):
        return any(a["robot"] == robot and a["kind"] == kind for a in self.raised)


@pytest.fixture
async def 站(tmp_path):
    t = 台子(tmp_path)
    t.dog = SimRobot(now_ms=t.clock, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2,
                     charger=CHARGER, charge_pct_per_h=3600.0)
    t.dog.inject_battery(29.0)
    await t.start()
    t.charge = ChargeDesk(t.db, t.site, now_ms=t.clock)
    t.charge.alerts = 假告警台()
    t.charge.set_charger("A", map_id=MAP[0], map_version=MAP[1], x=CHARGER[0], y=CHARGER[1],
                         yaw=CHARGER[2], by="alice")
    yield t
    await t.close()


async def _跑(t, secs, *, every=10):
    for i in range(int(secs * 10)):
        await t.run(1)
        if i % every == 0:
            await t.send(t.charge.tick())


def _cmds(t, kind):
    return [c for c in reversed(t.site.commands("A", 200)) if c["kind"] == kind]


async def test_低电量_去桩前_对桩_充到90出桩_这一轮完(站):
    t = 站
    await _跑(t, 30)
    [g] = _cmds(t, "goto")
    assert g["task_id"].startswith("charge-goto-") and g["priority"] == 50
    assert abs(t.dog.x - CHARGER[0]) < 0.3, "走到桩前"
    [d] = _cmds(t, "dock")
    assert d["task_id"].startswith("charge-dock-")
    await _跑(t, 120)
    assert t.dog.docked or t.dog.undock_calls >= 1
    await _跑(t, 60)
    assert not t.dog.docked and t.dog.undock_calls == 1
    assert (await t.dog.battery()).percent >= 90
    assert t.charge.view()["cycles"] == [], "这一轮完"
    await _跑(t, 30)
    assert len(_cmds(t, "goto")) == 1, "充满了不再去,也不自动派回待命点"


async def test_电量够_不去充_狗忙着不去充(站):
    t = 站
    t.dog.inject_battery(60.0)
    await _跑(t, 5)
    assert _cmds(t, "goto") == []
    t.charge.low_pct = 50.0                               # 电量 45%:人派的 goto 过得了出发线
    t.dog.inject_battery(45.0)
    await t.send(t.site.goto("A", target(-3.0), 0.5, issued_by="alice", priority=60))
    await _跑(t, 3)
    charge = [c for c in _cmds(t, "goto") if c["task_id"].startswith("charge-")]
    assert all(c["ack_result"] == "rejected" for c in charge), "狗在跑人派的任务:去充也被拒"
    assert t.dog.x < -0.1, "人派的那一趟照走"
    assert not t.charge.view()["cycles"], "没派成:不记这一轮"


async def test_充电中来了入侵_电量够就出桩去_回来接着充_不够就不派(站):
    t = 站
    await _跑(t, 30)                                      # 在桩上充着了
    assert t.dog.docked
    t.dog.inject_battery(40.0)                            # 不够 50%
    assert "电量不够" in t.charge.refuse("A")
    t.dog.inject_battery(55.0)
    await _跑(t, 2)
    assert t.charge.refuse("A") == ""
    await t.send(t.site.goto("A", target(-2.0), 0.8, issued_by="incident", priority=80,
                             task_id="incident-x"))
    await _跑(t, 1)
    assert t.dog.x > 1.5, "还在桩上:先不走"
    states = []
    for _ in range(20):
        await _跑(t, 1)
        st = t.charge.view()["cycles"][0]["state"]
        if st != "dock":
            states.append(st)
            break
    assert not t.dog.docked and t.dog.undock_calls >= 1, "先出桩再走"
    assert states == ["resume"], "被抢断:记「被打断」"
    await _跑(t, 60)                                      # 入侵那一趟走完,狗空了
    gotos = [c["task_id"] for c in _cmds(t, "goto")]
    assert gotos[-1].startswith("charge-goto-") and len(gotos) == 3, "接着充:再去桩前"


async def test_对不上桩_报P1_歇10分钟再试(站, monkeypatch):
    t = 站
    import d1max_agent.tasks.dock as dock_mod  # noqa: F401 - 狗上的超时用请求里的
    t.charge.resume_pct = 90

    async def 对不上():
        return None
    t.dog.recharge_start = 对不上                          # 厂家回充不报失败,就是一直充不上
    t.charge.dock_timeout_s = 20
    await _跑(t, 30)
    await _跑(t, 30)
    [a] = t.charge.alerts.raised
    assert a["kind"] == "charge_failed" and "没对上桩" in a["detail"]
    assert t.charge.view()["cycles"][0]["state"] == "cooldown"
    await _跑(t, 30)
    assert len(_cmds(t, "dock")) == 1, "歇着:不再试"


async def test_人中止回充_歇10分钟_不马上又去(站):
    t = 站
    await _跑(t, 3)
    [g] = _cmds(t, "goto")
    await t.send(t.site.abort("A", g["task_id"], issued_by="gina"))
    await _跑(t, 20)
    assert t.charge.view()["cycles"][0]["state"] == "cooldown"
    assert len(_cmds(t, "goto")) == 1
    t.clock.advance(601)
    await _跑(t, 5)
    assert len(_cmds(t, "goto")) == 2, "歇够了电量还低:再去"


async def test_站点重启_接着办(站):
    t = 站
    await _跑(t, 30)
    assert _cmds(t, "dock")
    t.charge = ChargeDesk(t.db, t.site, now_ms=t.clock)    # 站点重启
    t.charge.alerts = 假告警台()
    await _跑(t, 180)
    assert t.charge.view()["cycles"] == [] and t.dog.undock_calls == 1
    assert (await t.dog.battery()).percent >= 90


def test_登记桩不成形就拒(tmp_path):
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    desk = ChargeDesk(db, None, now_ms=lambda: 1)
    with pytest.raises(ChargeError):
        desk.set_charger("A", map_id="", map_version="1", x=0, y=0, yaw=0, by="a")
    assert desk.set_charger("A", map_id="m", map_version="1", x=1, y=2, yaw=0.5,
                            by="a")["x"] == 1.0
    assert desk.remove_charger("A") and not desk.remove_charger("A")
    db.close()


async def test_入侵派遣挑狗_在充电电量不够就不挑_够了照挑(站):
    from d1max_site.incidents import IncidentDesk
    t = 站
    await _跑(t, 30)
    assert t.dog.docked
    desk = IncidentDesk(t.db, t.site, now_ms=t.clock)
    desk.charging = t.charge.refuse
    point = {"map_id": MAP[0], "map_version": MAP[1], "x": -2.0, "y": 0.0}
    t.dog.inject_battery(40.0)
    await _跑(t, 2)
    rid, why = desk.pick_robot(point)
    assert rid is None and "在充电、电量不够" in why
    t.dog.inject_battery(60.0)
    await _跑(t, 2)
    assert desk.pick_robot(point) == ("A", "")


def test_接口_看桩_管理员在这儿设桩_删桩_保安只能看(tmp_path):
    from test_site_api import PW, _等
    from test_site_api import 站 as 真站
    s = 真站(tmp_path)
    try:
        s.accounts.add("gina", PW, role="guard")
        s.api.charge = ChargeDesk(s.db, s.disp, now_ms=s.api._now)
        tok = {n: s.req("POST", "/api/login", {"name": n, "password": PW})[1]["token"]
               for n in ("gina", "alice")}
        code, v = s.req("GET", "/api/chargers", token=tok["gina"])
        assert code == 200 and v["chargers"] == [] and v["low_pct"] == 30
        assert s.req("POST", "/api/chargers/A", {"here": True}, token=tok["gina"])[0] == 403
        _等(lambda: s.disp.clients["A"].telemetry is not None)
        code, d = s.req("POST", "/api/chargers/A", {"here": True}, token=tok["alice"])
        assert code == 200 and d["robot_id"] == "A" and d["set_by"] == "alice", d
        code, d = s.req("POST", "/api/chargers/A",
                        {"map_id": "m", "map_version": "1", "x": 1, "y": 2, "yaw": 0.5},
                        token=tok["alice"])
        assert code == 200 and (d["x"], d["y"]) == (1.0, 2.0)
        assert s.req("POST", "/api/chargers/A", {"x": "a"}, token=tok["alice"])[0] == 400
        assert s.req("POST", "/api/chargers/A", {"remove": True},
                     token=tok["alice"])[1] == {"removed": True}
        assert s.req("POST", "/api/chargers/nobody", {"here": True},
                     token=tok["alice"])[0] == 409
    finally:
        s.close()


async def test_电量在返航线下面_去桩前的路上不掉头回原点(站):
    t = 站
    t.dog.inject_battery(26.5)                            # 高于中止线 25%,低于返航线(25% + 回家)
    await _跑(t, 40)
    assert abs(t.dog.x - CHARGER[0]) < 0.3, "去充是回家的路:接着走到桩前"
    assert _cmds(t, "dock")


async def test_派了的回充丢了_狗上没在跑也没终态_过一阵当被打断接着充(站):
    t = 站
    t.dog.inject_battery(60.0)
    with t.db.tx() as c:
        c.execute("INSERT INTO charge_cycles(robot_id, state, task_id, sent_ms, started_ms) "
                  "VALUES ('A','dock','charge-dock-ghost',?,?)", (t.clock.ms, t.clock.ms))
    await _跑(t, 30)
    assert t.charge.view()["cycles"][0]["state"] == "dock", "还没到时候"
    await _跑(t, 70)
    gotos = _cmds(t, "goto")
    assert gotos and gotos[-1]["task_id"].startswith("charge-goto-"), "当被打断:接着充(不看电量线)"


def test_回充那几趟结束_待命点管理器不派回程(tmp_path):
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    desk = ChargeDesk(db, None, now_ms=lambda: 1)
    assert desk.holds("A", "charge-dock-1") and desk.holds("A", "charge-goto-1")
    assert not desk.holds("A", "incident-1") and not desk.holds("A", "sched-1")
    db.close()


# ------------------------------------------------------------ W13 外审 4、桩上危险


async def test_外审4_对桩任务号先落库_写不进去就不派_库好了按记着的派(站, monkeypatch):
    t = 站
    await _跑(t, 3)                                       # 还在去桩前的路上
    assert _cmds(t, "goto") and not _cmds(t, "dock")
    real_db = t.charge.db
    calls = []

    class 写不进:
        def __getattr__(self, k):
            return getattr(real_db, k)

        def tx(self):
            calls.append(1)
            raise RuntimeError("库锁住了")
    t.charge.db = 写不进()                                 # 只坏回充台的写(站点别的照常)
    await _跑(t, 20)
    t.charge.db = real_db
    assert calls and _cmds(t, "dock") == [], "写不进去:不派"
    await _跑(t, 10)
    [d] = _cmds(t, "dock")
    assert t.charge.view()["cycles"][0]["task_id"] == d["task_id"], "派的就是记着的那个"


async def test_外审4_对桩发出去回执没到_狗其实收了_它失败了照样报P1歇着_不另派(站, monkeypatch):
    t = 站
    t.charge.dock_timeout_s = 20
    await _跑(t, 3)
    real = t.site.dock

    async def 回执没到(rid, tid, req, *, issued_by):
        await real(rid, tid, req, issued_by=issued_by)
        raise TimeoutError("回执没到")

    async def 对不上():
        return None
    t.dog.recharge_start = 对不上
    monkeypatch.setattr(t.site, "dock", 回执没到)
    await _跑(t, 60)
    assert len(_cmds(t, "dock")) == 1, "说不清收没收:按记着的任务号对账,不另派"
    [a] = t.charge.alerts.raised
    assert a["kind"] == "charge_failed" and "没对上桩" in a["detail"]
    assert a["context"]["task_id"] == _cmds(t, "dock")[0]["task_id"]
    t.charge = ChargeDesk(t.db, t.site, now_ms=t.clock)    # 站点重启:歇着照歇,不另派、不重报
    t.charge.alerts = 假告警台()
    await _跑(t, 30)
    assert t.charge.alerts.raised == []
    assert t.charge.view()["cycles"][0]["state"] == "cooldown" and len(_cmds(t, "dock")) == 1


async def test_外审4_对桩被拒_退回走到了_过一阵再派(站, monkeypatch):
    t = 站
    await _跑(t, 3)
    real = t.site.dock
    n = []

    async def 拒(rid, tid, req, *, issued_by):
        n.append(tid)
        return {"ack": {"result": "rejected", "reason": "busy"}}
    monkeypatch.setattr(t.site, "dock", 拒)
    await _跑(t, 10)
    assert n and t.charge.view()["cycles"][0]["state"] == "arrived"
    monkeypatch.setattr(t.site, "dock", real)
    await _跑(t, 40)
    assert _cmds(t, "dock") and t.charge.view()["cycles"][0]["state"] == "dock"


async def test_桩上危险_报P1一次_摘了清_再挂再报(站):
    t = 站
    caps = t.site.clients["A"].capabilities.tasks
    caps["dock"] = {"mode": "vendor_dock", "on_dock": True, "hazard": "出不了桩(120 秒)"}
    await t.send(t.charge.tick())
    await t.send(t.charge.tick())
    [a] = t.charge.alerts.raised
    assert a["kind"] == "dock_stuck" and "出不了桩" in a["detail"]
    caps["dock"] = {"mode": "vendor_dock", "on_dock": False}
    await t.send(t.charge.tick())
    assert not t.db.query("SELECT 1 FROM dock_hazards")
    t.charge.alerts.raised.clear()
    caps["dock"] = {"mode": "vendor_dock", "on_dock": True, "hazard": "停不住对桩"}
    await t.send(t.charge.tick())
    assert [x["kind"] for x in t.charge.alerts.raised] == ["dock_stuck"]
