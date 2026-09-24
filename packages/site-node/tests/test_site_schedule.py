"""站点的任务包导入与排程执行器(W00c2a)。台子同 ``test_site_dispatcher.py``:MemoryBroker +
真 AgentRuntime + SimRobot,钟注入。排程时区是吉隆坡;钟拨到 22:00 那一轮。"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_site_dispatcher import SITE, 台子

from d1max_agent.engine.bundle import build_bundle
from d1max_contract.bundle_format import SCHEDULE_NAME
from d1max_site.catalog import CatalogError, active_bundle, import_bundle
from d1max_site.scheduler import SiteScheduler

KL = ZoneInfo("Asia/Kuala_Lumpur")
BUILT = "2026-09-07T14:03:00+08:00"
排程 = """\
timezone: Asia/Kuala_Lumpur
entries:
  - id: nightly
    mission: loop
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 30
    on_missed: skip
"""
任务 = {"mission": "loop", "map_id": "estate-1", "policy": {}, "waypoints": [
    {"name": "a", "pose": {"position": {"x": 0.6, "y": 0.0},
                           "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}},
     "actions": [{"type": "dwell", "seconds": 0.5}]},
    {"name": "b", "pose": {"position": {"x": 0.6, "y": 0.6},
                           "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}]}


def 毫秒(时: int, 分: int, 秒: int = 0) -> int:
    # 台子在 1_800_000_000_000(2027-01-15)登记狗;排程的日子要在那之后。
    return int(datetime(2027, 3, 15, 时, 分, 秒, tzinfo=KL).timestamp() * 1000)


def 打包(tmp_path, version: int, *, schedule: str = 排程, mission=None, tag: str = "") -> object:
    src = tmp_path / f"src-{version}{tag}"
    (src / "missions").mkdir(parents=True)
    (src / "missions" / "loop.json").write_text(json.dumps(mission or 任务), encoding="utf-8")
    (src / SCHEDULE_NAME).write_text(schedule, encoding="utf-8")
    return build_bundle(src, tmp_path / f"staged-{version}{tag}", bundle_id="estate-kl",
                        version=version, built_at=BUILT)


# ------------------------------------------------------------ 导入


def test_导入任务包_成为当前包(tmp_path):
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    got = import_bundle(db, 打包(tmp_path, 1), imported_by="alice", now_ms=1)
    assert got["missions"] == ["loop"] and got["schedule_entries"] == ["nightly"]
    act = active_bundle(db)
    assert (act.bundle_id, act.version) == ("estate-kl", 1)
    assert act.missions["loop"].map_id == "estate-1"
    assert act.schedule.timezone == "Asia/Kuala_Lumpur"
    db.close()


def test_版本不升_被改过_排程指向不存在的任务_都拒(tmp_path):
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    import_bundle(db, 打包(tmp_path, 2), imported_by="alice", now_ms=1)
    with pytest.raises(CatalogError, match="版本号只许往上走"):
        import_bundle(db, 打包(tmp_path, 2, tag="again"), imported_by="alice", now_ms=2)
    改过 = 打包(tmp_path, 3)
    (改过 / "missions" / "loop.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CatalogError, match="校验没过"):
        import_bundle(db, 改过, imported_by="alice", now_ms=3)
    坏 = 排程.replace("mission: loop", "mission: ghost")
    with pytest.raises(CatalogError, match="ghost"):
        import_bundle(db, 打包(tmp_path, 4, schedule=坏), imported_by="alice", now_ms=4)
    assert active_bundle(db).version == 2, "拒掉的不影响当前包"
    db.close()


# ------------------------------------------------------------ 执行器


@pytest.fixture
async def 站(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)           # sim 的电量按钟掉;钟一下拨了两个月,先充满
    await t.start()
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    t.sched = SiteScheduler(t.db, t.site, now_ms=t.clock)
    yield t
    await t.close()


async def _拍(t) -> None:
    await t.send(t.sched.tick())


async def test_到点派patrol_走完回写done(站):
    t = 站
    await _拍(t)
    assert t.sched.runs() == [], "21:59 还没到"
    await t.run(15)                                   # 走到 22:00:00.5 之后
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(1)
    await _拍(t)
    runs = t.sched.runs("nightly")
    assert len(runs) == 1 and runs[0]["outcome"] == "started" and runs[0]["robot_id"] == "A"
    cmd = t.site.commands("A")[0]
    assert cmd["kind"] == "patrol" and cmd["issued_by"] == "schedule:nightly"
    assert cmd["priority"] == 10, "排程巡检的优先级按站点的表(W00c2b),不是条目原样的 0"
    assert cmd["payload"]["map_version"] == "7"
    await t.run(600)
    assert t.sched.runs("nightly")[0]["result"] == "done", t.site.recent_events("A", 20)


async def test_站点重启后同一轮不起第二次(站):
    t = 站
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    await t.run(600)
    again = SiteScheduler(t.db, t.site, now_ms=t.clock)      # 新执行器、同一个库
    t.clock.ms = 毫秒(22, 5)
    await t.run(1)
    await t.send(again.tick())
    assert [r["outcome"] for r in again.runs("nightly")] == ["started"]
    assert len([c for c in t.site.commands("A") if c["kind"] == "patrol"]) == 1


async def test_狗离线到点_记no_robot_窗口过了记skip_不补跑(站):
    t = 站
    await t.agent.close()
    await t.broker.drain()
    t.agent = None
    t.clock.ms = 毫秒(22, 0, 30)
    await _拍(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["no_robot"]
    assert "不在线" in t.sched.runs("nightly")[0]["note"]
    t.clock.ms = 毫秒(22, 45)
    await _拍(t)
    await _拍(t)
    assert sorted(r["outcome"] for r in t.sched.runs("nightly")) == ["no_robot", "skip"]
    assert not [c for c in t.site.commands("A") if c["kind"] == "patrol"]


async def test_钟不可信就不起(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)
    await t.start()
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    t.clock.ms = 毫秒(22, 0, 30)
    s = SiteScheduler(t.db, t.site, now_ms=t.clock,
                      time_reference=lambda: (t.clock() - 3_600_000, "ntp"))
    await t.run(2)
    await t.send(s.tick())
    assert [r["outcome"] for r in s.runs("nightly")] == ["skew"]
    assert not t.site.commands("A")
    await t.close()


async def test_两台都能派_排程没指定_不替人挑(站, tmp_path):
    t = 站
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    await t.site.add_robot("B")
    t.site.clients["B"].status = t.site.clients["A"].status
    t.site.clients["B"].status_live_at = None
    t.site.clients["B"].capabilities = t.site.clients["A"].capabilities
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    t.site.clients["B"].status_live_at = t.clock()
    await _拍(t)
    runs = t.sched.runs("nightly")
    assert [r["outcome"] for r in runs] == ["ambiguous"] and "A, B" in runs[0]["note"]


async def test_排程写了robot就只派那一台(站, tmp_path):
    t = 站
    指定 = 排程.replace("    on_missed: skip\n", "    on_missed: skip\n    robot: B\n")
    import_bundle(t.db, 打包(tmp_path, 2, schedule=指定), imported_by="alice", now_ms=t.clock())
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    await t.site.add_robot("B")
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    runs = t.sched.runs("nightly")
    assert [r["outcome"] for r in runs] == ["no_robot"], "A 在线也不派给它"


async def test_视图有下一轮与最近一次(站):
    t = 站
    v = t.sched.view()
    assert v["bundle"] == {"bundle_id": "estate-kl", "version": 1}
    assert v["entries"][0]["next_run"].startswith("2027-03-15T22:00")
    assert v["entries"][0]["last"] is None
    assert v["clock_checked"] is False, "没接参照就如实说没核对"
    assert SITE == "estate-1"


# ------------------------------------------------------------ 内部评审补的

async def test_回执超时_本轮不再派第二次_结果照样回写(站, monkeypatch):
    """狗其实收到了、回执丢了:下一拍不许再派一趟(狗会排在第一趟后面再跑一遍)。"""
    from d1max_contract.dispatch import DispatchTimeout
    t = 站
    real_send = t.site._send
    calls = {"n": 0}

    async def 发了但回执丢(*a, **k):
        calls["n"] += 1
        r = await real_send(*a, **k)
        if calls["n"] == 1:
            raise DispatchTimeout("回执丢了")
        return r

    monkeypatch.setattr(t.site, "_send", 发了但回执丢)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    runs = t.sched.runs("nightly")
    assert [r["outcome"] for r in runs] == ["started"] and runs[0]["task_id"]
    await t.run(600)                                     # 第一趟走完,还在窗口里
    await _拍(t)
    assert len([c for c in t.site.commands("A") if c["kind"] == "patrol"]) == 1
    assert t.sched.runs("nightly")[0]["result"] == "done"


async def test_一条派不出去不挡别的狗的排程(站, tmp_path):
    t = 站
    两条 = """\
timezone: Asia/Kuala_Lumpur
entries:
  - id: vip
    mission: loop
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 30
    on_missed: skip
    priority: 5
    robot: B
  - id: nightly
    mission: loop
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 30
    on_missed: skip
    robot: A
"""
    import_bundle(t.db, 打包(tmp_path, 2, schedule=两条), imported_by="alice",
                  now_ms=t.clock())
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    got = {r["entry_id"]: r["outcome"] for r in t.sched.runs()}
    assert got == {"vip": "no_robot", "nightly": "started"}, got


async def test_同一台狗两条同时到点_优先的先派_另一条记displaced(站, tmp_path):
    t = 站
    两条 = 排程 + """\
  - id: early
    mission: loop
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 30
    on_missed: skip
    priority: 9
"""
    import_bundle(t.db, 打包(tmp_path, 2, schedule=两条), imported_by="alice",
                  now_ms=t.clock())
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    got = {r["entry_id"]: r["outcome"] for r in t.sched.runs()}
    assert got == {"early": "started", "nightly": "displaced"}, got


async def test_导入之前就到点的那一轮不跑也不记账(tmp_path):
    """22:00 导入一个带 08:00 run_late 的包:不许立刻补跑一趟迟了 14 小时的。"""
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(22, 0)
    t.dog.inject_battery(100.0)
    await t.start()
    早 = 排程.replace('at: "22:00"', 'at: "08:00"').replace("on_missed: skip",
                                                              "on_missed: run_late")
    import_bundle(t.db, 打包(tmp_path, 1, schedule=早), imported_by="alice", now_ms=t.clock())
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    await t.run(2)
    await t.send(s.tick())
    assert s.runs() == [] and not t.site.commands("A")
    await t.close()


async def test_跨午夜那一轮在站点上照跑(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(23, 50)
    t.dog.inject_battery(100.0)
    await t.start()
    晚 = 排程.replace('at: "22:00"', 'at: "23:55"').replace("window_min: 30", "window_min: 20")
    import_bundle(t.db, 打包(tmp_path, 1, schedule=晚), imported_by="alice", now_ms=t.clock())
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    t.clock.ms = 毫秒(23, 55) + 10 * 60_000                 # 第二天 00:05
    await t.run(2)
    await t.send(s.tick())
    assert [r["outcome"] for r in s.runs()] == ["started"]
    await t.close()


async def test_中途abort_结果回写aborted(站):
    t = 站
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    tid = t.sched.runs("nightly")[0]["task_id"]
    await t.run(5)
    await t.send(t.site.abort("A", tid, issued_by="bob"))
    await t.run(60)
    assert t.sched.runs("nightly")[0]["result"] == "aborted"


async def test_狗明确拒收_本轮记dispatch_failed_不每拍重发(站, tmp_path):
    t = 站
    别的图 = dict(任务, map_id="other-map")
    import_bundle(t.db, 打包(tmp_path, 2, mission=别的图), imported_by="alice",
                  now_ms=t.clock())
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await _拍(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["no_robot"], \
        "站点先看地图:对不上的不发"


async def test_狗明确拒收_记dispatch_failed_这一轮不再重发(tmp_path):
    """用一台假狗:在线、就绪、报了地图,但对 patrol 一律回 rejected。"""
    from d1max_contract.memory_broker import MemoryTransport
    from d1max_contract.messages import (
        Ack,
        AckResult,
        Capabilities,
        Command,
        Ready,
        Status,
    )
    from d1max_contract.topics import Topics
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    await t.site.start()
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    ta = Topics(site_id=SITE, robot_id="A")
    fake = MemoryTransport(t.broker, "fakeA")
    got: list[str] = []

    async def 回拒(m):
        cmd = Command.from_wire(json.loads(m.payload))
        got.append(cmd.kind)
        await fake.publish(ta.ack, json.dumps(Ack(cmd.command_id, cmd.task_id,
                                                  AckResult.REJECTED, "busy").to_wire()).encode())

    await fake.subscribe(ta.cmd, 回拒)
    await fake.connect()
    caps = Capabilities(robot_id="A", agent="x", adapter="y",
                        tasks={"patrol": {"map_id": "estate-1", "map_version": "7"}},
                        actuators={}, sensing={})
    await fake.publish(ta.capabilities, json.dumps(caps.to_wire()).encode(), retain=True)
    t.clock.ms = 毫秒(22, 0, 30)
    st = Status(online=True, boot_id="b", ready=Ready(True, True, True, True), control_epoch=1,
                last_seen=t.clock(), task=None)
    await fake.publish(ta.status, json.dumps(st.to_wire()).encode())
    await t.broker.drain()
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    await t.send(s.tick())
    assert [r["outcome"] for r in s.runs("nightly")] == ["dispatch_failed"]
    assert "rejected" in s.runs("nightly")[0]["note"]
    t.clock.ms = 毫秒(22, 1)
    await fake.publish(ta.status, json.dumps(st.to_wire()).encode())
    await t.broker.drain()
    await t.send(s.tick())
    assert got == ["patrol"], "被拒之后这一轮不再每拍重发"
    await t.site.close()
    t.db.close()
