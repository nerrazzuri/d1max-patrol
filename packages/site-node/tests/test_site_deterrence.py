"""分级驱离(W22,决策 37)。站点这一层:到了拦截点开一场 → 每 30 秒自动升到 L3 → 按级开、续、关声光、
轮放话术 → 人跳级、往回退之后不再自动升 → 解除、10 分钟到点收(全关、回待命点)→ 狗被派去干别的就收 →
站点重启接着管。假派遣器 + 可控的钟;最后两条走真 HTTP → 真代理 → 仿真狗。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from d1max_contract.messages import Event
from d1max_site.db import SiteDB
from d1max_site.deterrence import CAP_S, KEEP_S, DeterrenceDesk, DeterrenceError
from d1max_site.priorities import EVENT

CAPS = {"outputs": ["strobe", "siren", "spotlight", "speaker"], "max_s": 600.0,
        "clips": ["warn-zh", "warn-en", "warn-ms", "notified-zh", "notified-en"], "tts": False}


class 钟:
    def __init__(self):
        self.ms = 1_000_000

    def __call__(self):
        return self.ms

    def go(self, s):
        self.ms += int(s * 1000)


class 假派遣:
    def __init__(self, caps=CAPS):
        self.clients = {"A": SimpleNamespace(
            capabilities=SimpleNamespace(tasks={"deter": caps} if caps else {}),
            status=SimpleNamespace(task=None))}
        self.cbs = []
        self.sent: list[tuple[str, dict, int]] = []
        self.fail = False
        self.feed = SimpleNamespace(publish=lambda item: self.pushed.append(item))
        self.pushed: list[dict] = []

    def on_event(self, cb):
        self.cbs.append(cb)

    async def deter(self, rid, payload, *, issued_by, priority):
        if self.fail:
            raise RuntimeError("狗不在线")
        self.sent.append((rid, payload, priority))
        return {"ack": {"result": "accepted"}}

    def on(self):
        """每一路最后一次是开还是关。"""
        last: dict[str, bool] = {}
        for _, p, _ in self.sent:
            last[p["output"]] = p["on"]
        return {k for k, v in last.items() if v}


class 假待命:
    def __init__(self):
        self.back: list[str] = []

    async def return_to(self, rid, *, issued_by):
        self.back.append(rid)


def _到了(desk_or_disp, task_id="incident-abc", rid="A"):
    disp = desk_or_disp
    e = Event(event_id="e1", seq=1, boot_id="b", stamp=1, kind="task_done",
              data={"task_id": task_id})
    for cb in disp.cbs:
        cb(rid, e)


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    db.query("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome, robot_id, "
             "task_id, note, detail) VALUES ('nvr','e1','intrusion','front',1,'dispatched','A',"
             "'incident-abc','','{}')")
    clock, disp, stb = 钟(), 假派遣(), 假待命()
    desk = DeterrenceDesk(db, disp, now_ms=clock, standby=stb)
    yield SimpleNamespace(db=db, clock=clock, disp=disp, stb=stb, desk=desk)
    db.close()


async def test_到了拦截点开一场_L0什么都不开_待命点先别回_事件派遣不选它(台):
    t = 台
    assert t.desk.holds("A", "incident-abc") and not t.desk.holds("A", "sched-x")
    _到了(t.disp)
    [s] = t.desk.view()
    assert (s["robot_id"], s["level"], s["zone"], s["auto"]) == ("A", 0, "front", True)
    assert s["next_in_s"] == 30 and s["ends_in_s"] == CAP_S
    await t.desk.tick()
    assert t.disp.on() == set() and t.desk.busy() == {"A"}
    assert {p["output"] for _, p, _ in t.disp.sent} == {"strobe", "siren", "spotlight",
                                                         "speaker"}, "L0:没确认关过的都先关一遍"
    assert t.disp.pushed[-1]["kind"] == "deterrence"


async def test_每30秒自动升_最多到L3_按级开声光_话术轮放_优先级是事件档(台):
    t = 台
    _到了(t.disp)
    t.clock.go(30)
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 1 and t.disp.on() == {"strobe", "spotlight"}
    assert all(p["max_s"] == KEEP_S for _, p, _ in t.disp.sent if p["on"])
    t.clock.go(30)
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 2
    clips = [p["clip"] for _, p, _ in t.disp.sent if p["output"] == "speaker" and p["on"]]
    assert clips == ["warn-zh"]
    assert {pr for _, p, pr in t.disp.sent if p["output"] == "speaker"} == {EVENT}
    for _ in range(2):
        t.clock.go(10)
        await t.desk.tick()
    clips = [p["clip"] for _, p, _ in t.disp.sent if p["output"] == "speaker" and p["on"]]
    assert clips == ["warn-zh", "warn-en", "warn-ms"], "中文、英文、马来文轮放"
    t.clock.go(10)                                       # 到 L2 之后 30 秒:升 L3,话术从头
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 3 and "siren" in t.disp.on()
    for _ in range(2):
        t.clock.go(10)
        await t.desk.tick()
    clips = [p["clip"] for _, p, _ in t.disp.sent if p["output"] == "speaker" and p["on"]]
    assert clips[3:] == ["notified-zh", "notified-en", "notified-zh"], \
        "L3 放「已通知保安」;没录马来文的就跳过"
    t.clock.go(120)
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 3, "自动最多到 L3,L4 只能人进"


async def test_声光每20秒续一次_不多发(台):
    t = 台
    _到了(t.disp)
    t.clock.go(30)
    await t.desk.tick()
    n = len(t.disp.sent)
    t.clock.go(5)
    await t.desk.tick()
    assert len(t.disp.sent) == n, "没到 20 秒不续"
    t.clock.go(16)
    await t.desk.tick()
    renewed = [p["output"] for _, p, _ in t.disp.sent[n:] if p["on"]]
    assert sorted(renewed) == ["spotlight", "strobe"]


async def test_人跳级往回退_不再自动升_不该开的关掉(台):
    t = 台
    _到了(t.disp)
    await t.desk.set_level("A", 3, by="gina")
    assert {"strobe", "spotlight", "siren"} <= t.disp.on()
    v = await t.desk.set_level("A", 1, by="gina")
    assert v["auto"] is False and v["by"] == "gina" and v["next_in_s"] is None
    assert t.disp.on() == {"strobe", "spotlight"}, "退回 L1:警笛、喇叭关"
    t.clock.go(120)
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 1, "人动过就不再自动升"
    await t.desk.set_level("A", 4, by="gina")
    assert t.desk.sessions["A"].level == 4
    for bad in (5, -1, "2", True):
        with pytest.raises(DeterrenceError):
            await t.desk.set_level("A", bad, by="gina")
    with pytest.raises(DeterrenceError, match="没在驱离"):
        await t.desk.set_level("B", 1, by="gina")


async def test_解除_全关_回待命点_库里删掉(台):
    t = 台
    _到了(t.disp)
    await t.desk.set_level("A", 3, by="gina")
    await t.desk.release("A", by="olga")
    assert t.disp.on() == set() and t.stb.back == ["A"]
    assert t.desk.view() == [] and not t.db.query("SELECT 1 FROM deter_sessions")
    assert t.disp.pushed[-1]["session"] is None and "olga" in t.disp.pushed[-1]["ended"]
    with pytest.raises(DeterrenceError):
        await t.desk.release("A", by="olga")


async def test_10分钟到点自动收_人动过从那时重算(台):
    t = 台
    _到了(t.disp)
    t.clock.go(CAP_S - 60)
    await t.desk.set_level("A", 2, by="gina")          # 人动了:从现在再算 10 分钟
    t.clock.go(CAP_S - 1)
    await t.desk.tick()
    assert "A" in t.desk.sessions
    t.clock.go(1)
    await t.desk.tick()
    assert "A" not in t.desk.sessions and t.disp.on() == set() and t.stb.back == ["A"]


async def test_狗被派去干别的_收掉_不派回程(台):
    t = 台
    _到了(t.disp)
    t.clock.go(30)
    await t.desk.tick()
    t.disp.clients["A"].status.task = SimpleNamespace(task_id="incident-other",
                                                       state=SimpleNamespace(value="running"))
    await t.desk.tick()
    assert "A" not in t.desk.sessions and t.disp.on() == set() and t.stb.back == []


async def test_站点重启接着管(台, tmp_path):
    t = 台
    _到了(t.disp)
    await t.desk.set_level("A", 2, by="gina")
    desk2 = DeterrenceDesk(t.db, t.disp, now_ms=t.clock, standby=t.stb)
    [s] = desk2.view()
    assert (s["level"], s["auto"], s["by"], s["zone"]) == (2, False, "gina", "front")
    n = len(t.disp.sent)
    await desk2.tick()
    assert {p["output"] for _, p, _ in t.disp.sent[n:] if p["on"]} >= {"strobe", "spotlight",
                                                                       "speaker"}, \
        "重启后的第一拍把该开的重新开上(新的台没记着发过什么)"


async def test_发不出去_下一拍再发(台):
    t = 台
    _到了(t.disp)
    t.clock.go(30)
    t.disp.fail = True
    await t.desk.tick()
    assert t.disp.sent == []
    t.disp.fail = False
    t.clock.go(5)
    await t.desk.tick()
    assert t.disp.on() == {"strobe", "spotlight"}


async def test_没装上装的狗_不开场_照常回待命点(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    disp = 假派遣(caps=None)
    desk = DeterrenceDesk(db, disp, now_ms=钟(), standby=假待命())
    assert not desk.holds("A", "incident-abc")
    _到了(disp)
    assert desk.view() == []
    db.close()


async def test_不是事件任务_不开场_同一台不开第二场(台):
    t = 台
    _到了(t.disp, task_id="sched-1")
    assert t.desk.view() == []
    _到了(t.disp)
    first = t.desk.sessions["A"].started_ms
    t.clock.go(5)
    _到了(t.disp)
    assert t.desk.sessions["A"].started_ms == first


async def test_只有部分路_只开有的_没话术不放(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    db.query("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome, robot_id, "
             "task_id, note, detail) VALUES ('nvr','e1','intrusion','f',1,'dispatched','A',"
             "'incident-abc','','{}')")
    clock = 钟()
    disp = 假派遣(caps={"outputs": ["strobe", "speaker"], "clips": [], "tts": False})
    desk = DeterrenceDesk(db, disp, now_ms=clock, standby=假待命())
    _到了(disp)
    await desk.set_level("A", 3, by="gina")
    assert {p["output"] for _, p, _ in disp.sent if p["on"]} == {"strobe"}
    db.close()


# ------------------------------------------------------------ 接口(真 HTTP → 真代理 → 仿真狗)


def test_接口_保安跳级_狗上真开了_业主能解除不能跳级_没在驱离404(tmp_path):
    from test_site_api import PW, _等, 站

    from d1max_site.standby import StandbyManager
    s = 站(tmp_path, payload=True)
    try:
        s.accounts.add("gina", PW, role="guard")
        s.accounts.add("olga", PW, role="owner")
        stb = StandbyManager(s.db, s.disp, now_ms=s.api._now)
        desk = s.loop.call(lambda: _建(s, stb))
        s.api.deterrence = desk

        def 登(n):
            return s.req("POST", "/api/login", {"name": n, "password": PW})[1]["token"]
        gina, olga = 登("gina"), 登("olga")
        _等(lambda: "deter" in ((s.req("GET", "/api/robots/A", token=gina)[1].get("capabilities")
                                 or {}).get("tasks") or {}))
        s.db.query("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome, "
                   "robot_id, task_id, note, detail) VALUES ('nvr','e1','intrusion','front',1,"
                   "'dispatched','A','incident-abc','','{}')")
        s.loop.call(lambda: _开场(desk))
        assert s.req("GET", "/api/deterrence", token=olga)[1]["sessions"][0]["level"] == 0
        assert s.req("POST", "/api/deterrence/A/level", {"level": 1}, token=olga)[0] == 403
        code, d = s.req("POST", "/api/deterrence/A/level", {"level": 1}, token=gina)
        assert code == 200 and d["session"]["level"] == 1 and d["session"]["auto"] is False
        assert s.dog.deter_on("strobe") and s.dog.deter_on("spotlight")
        assert s.req("POST", "/api/deterrence/A/level", {"level": 9}, token=gina)[0] == 400
        assert s.req("POST", "/api/deterrence/A/release", {}, token=olga)[0] == 200
        assert not s.dog.deter_on("strobe") and not s.dog.deter_on("spotlight")
        assert s.req("POST", "/api/deterrence/A/release", {}, token=olga)[0] == 404
        acts = [(r["actor"], r["action"]) for r in s.api.audit.list()]
        assert ("gina", "POST /api/deterrence/A/level") in acts
        assert ("olga", "POST /api/deterrence/A/release") in acts
    finally:
        s.close()


async def _建(s, stb):
    return DeterrenceDesk(s.db, s.disp, now_ms=s.api._now, standby=stb)


async def _开场(desk):
    e = Event(event_id="x", seq=1, boot_id="b", stamp=1, kind="task_done",
              data={"task_id": "incident-abc"})
    desk._on_event("A", e)


def test_接口_没开驱离的站点404(tmp_path):
    from test_site_api import 站
    s = 站(tmp_path)
    try:
        assert s.req("GET", "/api/deterrence", token=s.login())[0] == 404
    finally:
        s.close()


# ------------------------------------------------------------ W22 外审:串行、该关的关、落库顺序


class 慢派遣(假派遣):
    """「开」的回执卡在闸上,测试放行。"""

    def __init__(self):
        super().__init__()
        import asyncio
        self.gate = asyncio.Event()
        self.gate.set()

    async def deter(self, rid, payload, *, issued_by, priority):
        if payload["on"]:
            await self.gate.wait()
        return await super().deter(rid, payload, issued_by=issued_by, priority=priority)


async def test_外审1_升级等回执时解除_解除等它_结束之后不再有开(tmp_path):
    import asyncio
    db = SiteDB(tmp_path / "site.db")
    db.query("INSERT INTO incidents(source, event_id, type, zone, received_at, outcome, robot_id, "
             "task_id, note, detail) VALUES ('nvr','e1','intrusion','front',1,'dispatched','A',"
             "'incident-abc','','{}')")
    disp, stb = 慢派遣(), 假待命()
    desk = DeterrenceDesk(db, disp, now_ms=钟(), standby=stb)
    _到了(disp)
    disp.gate.clear()
    up = asyncio.ensure_future(desk.set_level("A", 3, by="gina"))
    await asyncio.sleep(0.05)                             # 升级卡在等「开」的回执
    rel = asyncio.ensure_future(desk.release("A", by="olga"))
    await asyncio.sleep(0.05)
    assert not rel.done(), "解除等在途的那条命令"
    disp.gate.set()
    await asyncio.gather(up, rel)
    last_on = max(i for i, (_, p, _) in enumerate(disp.sent) if p["on"])
    offs = {p["output"] for _, p, _ in disp.sent[last_on + 1:] if not p["on"]}
    assert offs == {"strobe", "siren", "spotlight", "speaker"}, "最后一条「开」之后每一路都关了"
    assert disp.on() == set() and desk.view() == [] and stb.back == ["A"]
    await desk.tick()
    assert disp.on() == set(), "结束之后不再有开"
    db.close()


async def test_外审2_重启后马上降到L0_该关的都关(台):
    t = 台
    _到了(t.disp)
    await t.desk.set_level("A", 3, by="gina")
    assert {"strobe", "spotlight", "siren", "speaker"} <= t.disp.on()
    desk2 = DeterrenceDesk(t.db, t.disp, now_ms=t.clock, standby=t.stb)   # 重启:不记得发过什么
    n = len(t.disp.sent)
    await desk2.set_level("A", 0, by="gina")
    offs = {p["output"] for _, p, _ in t.disp.sent[n:] if not p["on"]}
    assert offs == {"strobe", "spotlight", "siren", "speaker"}
    assert t.disp.on() == set()


async def test_外审2_开的回执丢了_降级照样关(台):
    t = 台
    _到了(t.disp)
    await t.desk.tick()                                   # L0:先确认都关了
    real = t.disp.deter

    async def 开了但回执丢了(rid, payload, *, issued_by, priority):
        await real(rid, payload, issued_by=issued_by, priority=priority)
        if payload["on"]:
            raise TimeoutError("回执超时")
        return {"ack": {"result": "accepted"}}
    t.disp.deter = 开了但回执丢了
    await t.desk.set_level("A", 1, by="gina")
    assert t.disp.on() == {"strobe", "spotlight"}, "狗上其实开了"
    t.disp.deter = real
    await t.desk.set_level("A", 0, by="gina")
    assert t.disp.on() == set(), "没记到成了,不等于没开"


async def test_外审3_解除时库整个写不进_先全关_标收尾中_回报错_下一拍接着收(台, monkeypatch):
    t = 台
    _到了(t.disp)
    await t.desk.set_level("A", 3, by="gina")
    real = t.db.tx
    bad = {"on": True}

    def tx():
        if bad["on"]:
            raise RuntimeError("库锁住了")
        return real()
    monkeypatch.setattr(t.db, "tx", tx)
    with pytest.raises(DeterrenceError, match="没落进库"):
        await t.desk.release("A", by="olga")              # 解除回报错:让人知道没落进库
    assert t.disp.on() == set(), "删库没成也先全关"
    assert t.desk.view() == [] and t.desk.busy() == {"A"}, "收尾中:不显示、也不派别的活"
    assert t.stb.back == [] and t.db.query("SELECT level FROM deter_sessions")[0]["level"] == 3
    with pytest.raises(DeterrenceError):
        await t.desk.set_level("A", 2, by="gina")
    t.clock.go(30)
    n = len(t.disp.sent)
    await t.desk.tick()
    assert not [p for _, p, _ in t.disp.sent[n:] if p["on"]], "收尾中一条「开」都不发"
    bad["on"] = False
    await t.desk.tick()
    assert t.desk.busy() == set() and t.stb.back == ["A"]
    assert not t.db.query("SELECT 1 FROM deter_sessions")


def _删不掉(monkeypatch, db, bad):
    """库能写(标收尾中成),就是删 ``deter_sessions`` 那一句没成。"""
    from contextlib import contextmanager
    real = db.tx

    class 连接:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a):
            if bad["on"] and sql.startswith("DELETE FROM deter_sessions"):
                raise RuntimeError("盘忙")
            return self._c.execute(sql, *a)

    @contextmanager
    def tx():
        with real() as c:
            yield 连接(c)
    monkeypatch.setattr(db, "tx", tx)


async def test_复查_解除时删库没成_马上重启_只接着收尾_不再开(台, monkeypatch):
    t = 台
    _到了(t.disp)
    await t.desk.set_level("A", 3, by="gina")
    bad = {"on": True}
    _删不掉(monkeypatch, t.db, bad)
    await t.desk.release("A", by="olga")                  # 「收尾中」落进库了:不报错
    assert t.disp.on() == set() and t.desk.view() == []
    assert t.db.query("SELECT ending FROM deter_sessions")[0]["ending"] == "olga 解除"
    # 站点马上重启(库删不掉的毛病也还在)
    desk2 = DeterrenceDesk(t.db, t.disp, now_ms=t.clock, standby=t.stb)
    assert desk2.view() == [] and desk2.busy() == {"A"}, "收尾中:不显示,仍不派别的活"
    n = len(t.disp.sent)
    t.clock.go(30)
    await desk2.tick()
    assert not [p for _, p, _ in t.disp.sent[n:] if p["on"]], "重启之后一条「开」都不发"
    assert t.disp.on() == set()
    with pytest.raises(DeterrenceError):
        await desk2.set_level("A", 3, by="gina")
    bad["on"] = False
    await desk2.tick()
    assert desk2.busy() == set() and t.stb.back == ["A"], "库好了:收完、派回程"
    assert not t.db.query("SELECT 1 FROM deter_sessions")


async def test_复查_到点收也先落收尾_删库没成重启照样只收尾(台, monkeypatch):
    t = 台
    _到了(t.disp)
    await t.desk.set_level("A", 2, by="gina")
    bad = {"on": True}
    _删不掉(monkeypatch, t.db, bad)
    t.clock.go(CAP_S)
    await t.desk.tick()                                   # 10 分钟到点:收尾,删库没成
    desk2 = DeterrenceDesk(t.db, t.disp, now_ms=t.clock, standby=t.stb)
    n = len(t.disp.sent)
    await desk2.tick()
    assert not [p for _, p, _ in t.disp.sent[n:] if p["on"]]


def test_复查_接口_解除没落进库回503_切级落库没成也是503(tmp_path):
    from test_site_api import 站

    class 库坏了的台:
        async def release(self, rid, *, by):
            raise DeterrenceError("声光已全关,但「解除」没落进库(站点重启可能恢复这一场):"
                                  "稍后再按一次")

        async def set_level(self, rid, level, *, by):
            raise DeterrenceError("落库没成,级别没改: 库锁住了")
    s = 站(tmp_path)
    try:
        s.api.deterrence = 库坏了的台()
        tok = s.login()
        code, d = s.req("POST", "/api/deterrence/A/release", {}, token=tok)
        assert code == 503 and "稍后再按一次" in d["error"]
        assert s.req("POST", "/api/deterrence/A/level", {"level": 1}, token=tok)[0] == 503
    finally:
        s.close()


async def test_外审3_切级落库没成_不改_自动升落库没成_下一拍再升(台, monkeypatch):
    t = 台
    _到了(t.disp)
    real = t.db.tx
    bad = {"on": True}

    def tx():
        if bad["on"]:
            raise RuntimeError("库锁住了")
        return real()
    monkeypatch.setattr(t.db, "tx", tx)
    with pytest.raises(DeterrenceError, match="落库没成"):
        await t.desk.set_level("A", 3, by="gina")
    assert t.desk.sessions["A"].level == 0 and t.desk.sessions["A"].auto is True
    t.clock.go(30)
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 0, "落库没成就不升"
    bad["on"] = False
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 1
    assert t.db.query("SELECT level FROM deter_sessions")[0]["level"] == 1


async def test_外审3_开场落不了库_不开_照常回待命点(台, monkeypatch):
    import asyncio
    t = 台
    monkeypatch.setattr(t.db, "tx", lambda: (_ for _ in ()).throw(RuntimeError("库锁住了")))
    _到了(t.disp)
    await asyncio.sleep(0.05)
    assert t.desk.busy() == set() and t.stb.back == ["A"], "待命点那头没派回程:这里派"


async def test_外审1_解除时每一路都发关_不信记录_保安手动开过的也关(台):
    t = 台
    _到了(t.disp)
    await t.desk.tick()                                   # L0:都确认关过了
    await t.disp.deter("A", {"output": "siren", "on": True, "max_s": 60},
                       issued_by="gina", priority=60)      # 保安在「上装…」里手动开了警笛
    await t.desk.release("A", by="gina")
    assert t.disp.on() == set(), "解除:每一路都关,不信「确认关过」的记录"


# ------------------------------------------------------------ 跟待命点、事件派遣接上


async def test_接线_到了拦截点_待命点不自动回_驱离结束才回(tmp_path):
    """真派遣器 + 真代理 + 仿真狗(装了上装):事件任务到了拦截点,待命点管理器不派回程。"""
    from test_site_dispatcher import 台子

    from d1max_site.incidents import IncidentDesk
    from d1max_site.standby import StandbyManager
    t = 台子(tmp_path)
    t.dog.payload = True                                  # 上装(仿真狗)
    await t.start()
    try:
        stb = StandbyManager(t.db, t.site, now_ms=t.clock)
        stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0,
                default=True)
        desk = DeterrenceDesk(t.db, t.site, now_ms=t.clock, standby=stb)
        stb.hold = desk.holds
        inc = IncidentDesk(t.db, t.site, now_ms=t.clock)
        inc.busy = desk.busy
        inc.set_intercept("gate", map_id="estate-1", map_version="7", x=1.0, y=0.0, yaw=0.0)
        inc.map_zone("front", "gate")
        await t.run(12)
        r = await t.send(inc.handle("nvr", {"event_id": "e1", "type": "intrusion",
                                            "zone": "front"}))
        assert r["outcome"] == "dispatched"
        await t.run(200)
        assert desk.busy() == {"A"}, "到了拦截点:开驱离"
        gotos = [c for c in t.site.commands("A", 50) if c["kind"] == "goto"]
        assert not any(c["task_id"].startswith("standby-") for c in gotos), "不自动回待命点"
        # 驱离中:同一台狗不去别的防区
        inc.map_zone("back", "gate")
        r2 = await t.send(inc.handle("nvr", {"event_id": "e2", "type": "intrusion",
                                             "zone": "back"}))
        assert r2["outcome"] == "no_robot" and "正在驱离" in r2["note"]
        await t.send(desk.release("A", by="gina"))
        await t.run(5)
        gotos = [c for c in t.site.commands("A", 50) if c["kind"] == "goto"]
        assert any(c["task_id"].startswith("standby-") for c in gotos), "解除之后回待命点"
    finally:
        await t.close()


# ------------------------------------------------------------ W24:人员检测联动(按当前人员状态对账)


class 假告警台:
    def __init__(self, fail=0):
        self.raised = []
        self.fail = fail

    def raise_alert(self, **kw):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("告警库写不进")
        self.raised.append(kw)

    def has_open(self, robot, kind):
        return any(a["robot"] == robot and a["kind"] == kind for a in self.raised)


def _人员(t, present, *, near=False, count=1, nearest_m=7.5, state="ok"):
    p = {"state": state, "present": present, "near": near}
    if present:
        p |= {"count": count, "nearest_m": nearest_m}
    t.disp.clients["A"].capabilities.tasks["persons"] = p


async def _到场(t):
    import asyncio
    _到了(t.disp)
    for _ in range(5):
        await asyncio.sleep(0)


async def test_W24_有人_报告警带人数距离_现场归事件任务(台):
    t = 台
    t.desk.alerts = 假告警台()
    await _到场(t)
    _人员(t, True, count=2, nearest_m=7.5)
    await t.desk.tick()
    [a] = t.desk.alerts.raised
    assert a["kind"] == "intrusion_person" and "2 个人" in a["title"] and "7.5 m" in a["title"]
    assert a["context"]["task_id"] == "incident-abc"
    await t.desk.tick()
    assert len(t.desk.alerts.raised) == 1, "报过了不重报"
    assert t.desk.view()[0]["persons"]["count"] == 2
    assert t.db.query("SELECT person_seen, person_alerted FROM deter_sessions")[0][0] == 1


async def test_W24外审3_到之前就看到人_开场马上对上_近就直接L3(台):
    t = 台
    t.desk.alerts = 假告警台()
    _人员(t, True, near=True, nearest_m=3.0)               # 到之前就有人、3 米
    await _到场(t)
    s = t.desk.sessions["A"]
    assert s.level == 3 and s.seen_person and len(t.desk.alerts.raised) == 1
    assert "siren" in t.disp.on()


async def test_W24外审4_没有事件能控制驱离_补投的老走了不收场(台):
    t = 台
    await _到场(t)
    _人员(t, True)
    await t.desk.tick()
    e = Event(event_id="old", seq=1, boot_id="b", stamp=1, kind="person_gone", data={})
    for cb in t.disp.cbs:
        cb("A", e)                                        # 很早以前的「走了」补投过来
    await t.desk.tick()
    assert "A" in t.desk.sessions and t.stb.back == []


async def test_W24_看到过人_确认没人_收场回待命点(台):
    t = 台
    await _到场(t)
    _人员(t, True)
    await t.desk.tick()
    _人员(t, False)
    await t.desk.tick()
    assert "A" not in t.desk.sessions and t.stb.back == ["A"] and t.disp.on() == set()
    assert "人走了" in t.disp.pushed[-1]["ended"]


async def test_W24_没看到过人_确认没人也不收_不知道也不收(台):
    t = 台
    await _到场(t)
    _人员(t, False)
    await t.desk.tick()
    assert "A" in t.desk.sessions
    _人员(t, True)
    await t.desk.tick()
    _人员(t, None, state="stale")                          # 检测不在
    await t.desk.tick()
    _人员(t, None)                                        # 不知道
    await t.desk.tick()
    assert "A" in t.desk.sessions, "不知道不收"


async def test_W24_人接手了_近不升_没人不收_只显示(台):
    t = 台
    await _到场(t)
    await t.desk.set_level("A", 1, by="gina")
    _人员(t, True, near=True, nearest_m=3.0)
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 1 and t.desk.view()[0]["persons"]["near"] is True
    _人员(t, False)
    await t.desk.tick()
    assert "A" in t.desk.sessions


async def test_W24外审6_告警报不出去_下一拍接着报_重启后也接着报(台):
    t = 台
    t.desk.alerts = 假告警台(fail=2)
    await _到场(t)
    _人员(t, True)
    await t.desk.tick()
    await t.desk.tick()
    assert t.desk.alerts.raised == []
    assert len(t.db.query("SELECT 1 FROM pending_alerts")) == 1, "要报的意图落了库"
    desk2 = DeterrenceDesk(t.db, t.disp, now_ms=t.clock, standby=t.stb)   # 站点重启
    desk2.alerts = t.desk.alerts
    await desk2.tick()
    assert len(t.desk.alerts.raised) == 1 and not t.db.query("SELECT 1 FROM pending_alerts")


async def test_W24复查3_告警没报成_人马上走了_会话收了_告警照样报出去(台):
    t = 台
    t.desk.alerts = 假告警台(fail=1)
    await _到场(t)
    _人员(t, True)
    await t.desk.tick()                                   # 看到人,报不出去
    _人员(t, False)
    await t.desk.tick()                                   # 确认没人:收场
    assert "A" not in t.desk.sessions
    assert len(t.desk.alerts.raised) == 1, "会话收了,没报成的告警照样报"
    assert t.desk.alerts.raised[0]["context"]["task_id"] == "incident-abc"
    assert not t.db.query("SELECT 1 FROM pending_alerts")


async def test_W24复查3_看到过人和要报的告警一起落库_落不了都不记(台, monkeypatch):
    t = 台
    t.desk.alerts = 假告警台()
    await _到场(t)
    real = t.db.tx
    monkeypatch.setattr(t.db, "tx", lambda: (_ for _ in ()).throw(RuntimeError("库锁住了")))
    _人员(t, True)
    await t.desk.tick()
    assert not t.desk.sessions["A"].seen_person and t.desk.alerts.raised == []
    monkeypatch.setattr(t.db, "tx", real)
    await t.desk.tick()
    assert t.desk.sessions["A"].seen_person and len(t.desk.alerts.raised) == 1


async def test_W24_狗不新鲜_人员状态不算(台):
    t = 台
    await _到场(t)
    t.disp._fresh = lambda c: False
    _人员(t, True, near=True, nearest_m=2.0)
    await t.desk.tick()
    assert t.desk.sessions["A"].level == 0 and not t.desk.sessions["A"].seen_person


async def test_W24_不在驱离的狗_看到人只记不报(台):
    t = 台
    t.desk.alerts = 假告警台()
    _人员(t, True)
    await t.desk.tick()
    assert t.desk.alerts.raised == [] and t.desk.view() == []


async def test_W24外审6_保安处理掉告警之后站点重启_不再报一次(台):
    t = 台
    t.desk.alerts = 假告警台()
    await _到场(t)
    _人员(t, True)
    await t.desk.tick()
    assert len(t.desk.alerts.raised) == 1
    t.desk.alerts.raised.clear()                          # 保安处理掉了(告警簿里不挂着了)
    desk2 = DeterrenceDesk(t.db, t.disp, now_ms=t.clock, standby=t.stb)
    desk2.alerts = t.desk.alerts
    await desk2.tick()
    assert t.desk.alerts.raised == [], "报成过就记在库里,重启不再报"
