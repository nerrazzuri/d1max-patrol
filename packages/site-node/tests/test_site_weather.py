"""全天候(W29,决策 41):天气怎么判(天气码、降水)、手动以手动为准且到点回自动、联网的旧了算不知道、
查不到照用最后一次;雷暴撤在跑的排程巡检(每趟 15 秒最多一次)、不撤别的;下雨、雷暴每台狗限速对账
(不一样就发、快到点续、15 秒最多一次、不认的狗不发、不新鲜不发);接口:保安、业主都能切、记审计。
排程雷暴不起跑见 ``test_site_schedule``;雷暴中出动、定位变差先 P2 见 ``test_site_alerts``。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from d1max_site.db import SiteDB
from d1max_site.weather import (
    AUTO_STALE_S,
    CAP_TTL_S,
    POLL_S,
    WeatherDesk,
    WeatherError,
    classify,
    parse_latlon,
)


class 钟:
    def __init__(self):
        self.ms = 1_000_000_000

    def __call__(self):
        return self.ms

    def go(self, s):
        self.ms += int(s * 1000)


def _狗(task=None, cap=None, *, fresh=True):
    tasks = {} if cap is False else {"speed_cap": cap or {"max_speed_mps": None}}
    st = None if task is None else SimpleNamespace(
        task_id=task, state=SimpleNamespace(value="running"))
    return SimpleNamespace(capabilities=SimpleNamespace(tasks=tasks),
                           status=SimpleNamespace(task=st), fresh=fresh)


class 假派遣:
    def __init__(self, **dogs):
        self.clients = dogs
        self.aborts, self.caps = [], []
        self.fail = False

    def _fresh(self, c):
        return c.fresh

    async def abort(self, rid, task_id, *, issued_by):
        if self.fail:
            raise RuntimeError("狗不在线")
        self.aborts.append((rid, task_id))

    async def speed_cap(self, rid, cap, *, issued_by):
        if self.fail:
            raise RuntimeError("狗不在线")
        self.caps.append((rid, cap.max_speed_mps))
        self.clients[rid].capabilities.tasks["speed_cap"] = (
            {"max_speed_mps": cap.max_speed_mps, "left_s": cap.ttl_s}
            if cap.max_speed_mps is not None else {"max_speed_mps": None})


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    c = 钟()
    got = {"cur": {"weather_code": 3, "precipitation": 0.0}, "n": 0, "fail": False}

    def fetch(lat, lon):
        got["n"] += 1
        got["at"] = (lat, lon)
        if got["fail"]:
            raise OSError("没网")
        return got["cur"]
    disp = 假派遣(A=_狗(), B=_狗())
    pushed = []
    desk = WeatherDesk(db, disp, now_ms=c, latlon=(3.14, 101.69), fetch=fetch,
                       publish=pushed.append)
    yield SimpleNamespace(db=db, clock=c, got=got, disp=disp, desk=desk, pushed=pushed)
    db.close()


def test_天气码和降水怎么判():
    assert classify(95, 0) == "storm" and classify(99, 0) == "storm"
    assert classify(65, 0) == "storm" and classify(82, 0) == "storm", "大雨按雷暴"
    assert classify(3, 8.0) == "storm", "每小时 7.6 mm 以上按雷暴"
    assert classify(61, 0) == "rain" and classify(80, 0) == "rain" and classify(3, 0.2) == "rain"
    assert classify(0, 0) == "normal" and classify(None, None) == "normal"
    assert parse_latlon("3.14159,101.6869") == (3.14, 101.69), "只留两位小数(约 1 km)"
    assert parse_latlon("") is None and parse_latlon("x,y") is None and parse_latlon("91,0") is None


async def test_联网查_每10分钟一次_只发两位小数(台):
    t = 台
    await t.desk.tick()
    assert t.got["n"] == 1 and t.got["at"] == (3.14, 101.69)
    assert t.desk.current() == "normal" and t.desk.view()["source"] == "auto"
    t.got["cur"] = {"weather_code": 61, "precipitation": 1.0}
    t.clock.go(60)
    await t.desk.tick()
    assert t.got["n"] == 1 and t.desk.current() == "normal", "10 分钟内不再查"
    t.clock.go(POLL_S)
    await t.desk.tick()
    assert t.desk.current() == "rain" and t.pushed[-1]["weather"]["condition"] == "rain"


async def test_查不到_照用最后一次_旧了算不知道(台):
    t = 台
    t.got["cur"] = {"weather_code": 95, "precipitation": 0}
    await t.desk.tick()
    t.got["fail"] = True
    t.clock.go(POLL_S)
    await t.desk.tick()
    assert t.desk.current() == "storm" and "没网" in t.desk.view()["auto"]["error"]
    t.clock.go(AUTO_STALE_S)
    assert t.desk.current() == "unknown" and t.desk.view()["label"] == "不知道"
    assert t.desk.speed_cap() is None and not t.desk.storm(), "不知道按正常管"


async def test_手动以手动为准_到点回自动_能直接切回自动(台):
    t = 台
    await t.desk.tick()
    v = t.desk.set_manual("storm", by="gina", hours=2)
    assert v["condition"] == "storm" and v["source"] == "manual" and v["manual"]["by"] == "gina"
    t.clock.go(2 * 3600 - 1)
    assert t.desk.storm()
    t.clock.go(2)
    t.got["fail"] = True
    assert t.desk.current() == "unknown", "手动到点:回到联网查的(这里已经旧了)"
    t.desk.set_manual("rain", by="gina")
    assert t.desk.current() == "rain"
    t.desk.set_manual("auto", by="gina")
    assert t.desk.view()["source"] != "manual"
    for bad in ({"condition": "snow"}, {"condition": "rain", "hours": 0},
                {"condition": "rain", "hours": 25}, {"condition": "rain", "hours": True}):
        with pytest.raises(WeatherError):
            t.desk.set_manual(bad["condition"], by="gina", hours=bad.get("hours"))


async def test_没配坐标_不联网_只能手动(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    n = []
    desk = WeatherDesk(db, 假派遣(), now_ms=钟(), latlon=None, fetch=lambda a, b: n.append(1))
    await desk.tick()
    assert n == [] and desk.current() == "unknown" and desk.view()["auto"]["enabled"] is False
    db.close()


async def test_雷暴_撤在跑的排程巡检_别的不撤_15秒最多撤一次(台):
    t = 台
    t.disp.clients = {"A": _狗("sched-1"), "B": _狗("incident-2"), "C": _狗("manual-3")}
    t.desk.set_manual("storm", by="gina")
    await t.desk.tick()
    assert t.disp.aborts == [("A", "sched-1")], "入侵、人手动派的不撤"
    await t.desk.tick()
    assert len(t.disp.aborts) == 1
    t.clock.go(15)
    await t.desk.tick()
    assert len(t.disp.aborts) == 2, "还在跑:接着撤"
    t.desk.set_manual("rain", by="gina")
    t.clock.go(15)
    await t.desk.tick()
    assert len(t.disp.aborts) == 2, "下雨不撤巡检"


async def test_限速对账_不一样就发_快到点续_雨停了取消_不认的不发_不新鲜不发(台):
    t = 台
    t.disp.clients = {"A": _狗(), "B": _狗(cap=False), "C": _狗(fresh=False)}
    await t.desk.tick()
    assert t.disp.caps == [], "正常天:都是不限,不用发"
    t.desk.set_manual("rain", by="gina")
    await t.desk.tick()
    assert t.disp.caps == [("A", 0.3)]
    await t.desk.tick()
    assert len(t.disp.caps) == 1, "一样了:不发"
    t.disp.clients["A"].capabilities.tasks["speed_cap"]["left_s"] = CAP_TTL_S // 2 - 1
    t.clock.go(15)
    await t.desk.tick()
    assert t.disp.caps == [("A", 0.3), ("A", 0.3)], "快到点了:续"
    t.desk.set_manual("normal", by="gina")
    t.clock.go(15)
    await t.desk.tick()
    assert t.disp.caps[-1] == ("A", None), "雨停了:取消"


async def test_限速发不出去_15秒后再发(台):
    t = 台
    t.desk.set_manual("storm", by="gina")
    t.disp.fail = True
    await t.desk.tick()
    t.disp.fail = False
    await t.desk.tick()
    assert t.disp.caps == []
    t.clock.go(15)
    await t.desk.tick()
    assert sorted(t.disp.caps) == [("A", 0.3), ("B", 0.3)]


async def test_重启_手动切的还在(台):
    t = 台
    t.desk.set_manual("storm", by="gina", hours=5)
    desk2 = WeatherDesk(t.db, t.disp, now_ms=t.clock, latlon=None)
    assert desk2.storm() and desk2.view()["manual"]["by"] == "gina"


def test_接口_保安业主都能切_切成雷暴记审计_不合规矩400_没登录401(tmp_path):
    from test_site_api import PW, 站
    s = 站(tmp_path)
    try:
        s.accounts.add("gina", PW, role="guard")
        s.accounts.add("olga", PW, role="owner")
        s.api.weather = WeatherDesk(s.db, s.disp, now_ms=s.api._now, latlon=None,
                                    publish=s.disp.feed.publish)
        tok = {n: s.req("POST", "/api/login", {"name": n, "password": PW})[1]["token"]
               for n in ("gina", "olga")}
        code, v = s.req("GET", "/api/weather", token=tok["olga"])
        assert code == 200 and v["condition"] == "unknown" and v["auto"]["enabled"] is False
        code, v = s.req("POST", "/api/weather", {"condition": "storm", "hours": 2},
                        token=tok["gina"])
        assert code == 200 and v["condition"] == "storm" and v["patrols_paused"] is True
        assert v["speed_cap_mps"] == 0.3 and v["manual"]["by"] == "gina"
        code, v = s.req("POST", "/api/weather", {"condition": "auto"}, token=tok["olga"])
        assert code == 200 and v["source"] != "manual"
        assert s.req("POST", "/api/weather", {"condition": "hail"}, token=tok["gina"])[0] == 400
        assert s.req("GET", "/api/weather")[0] == 401
        acts = [(a["actor"], a["action"], a["target"]) for a in s.api.audit.list()]
        assert ("gina", "POST /api/weather", "storm") in acts
        assert ("olga", "POST /api/weather", "auto") in acts
    finally:
        s.close()
