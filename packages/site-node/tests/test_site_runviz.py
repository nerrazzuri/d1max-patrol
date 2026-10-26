"""运营可视化(站点这一半):规划路径、轨迹缓冲(有界)、被挡 / 重规划状态、被挡 P2 告警(只在任务
在跑时、超过门槛才报、不被挡了自动解决)、狗视图里的 ``nav`` 块、``GET /api/runviz``。"""

from __future__ import annotations

import pytest

from d1max_contract.messages import (
    Event,
    MapPose,
    Ready,
    Status,
    TaskState,
    TaskSummary,
    Telemetry,
)
from d1max_site.alert_store import AlertDesk
from d1max_site.db import SiteDB
from d1max_site.runviz import (
    BLOCK_ALERT_S,
    PATH_MAX,
    TRAIL_AGE_MS,
    TRAIL_JUMP_M,
    TRAIL_MAX,
    TRAIL_STEP_M,
    RunViz,
    clean_path,
)

READY = Ready(control=True, motion=True, estop_clear=True, loc_ok=True)


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000_000

    def __call__(self) -> int:
        return self.ms


def _tele(x=None, y=0.0, nav=None, map_v="7", map_id="m"):
    pose = None if x is None else MapPose(map_id=map_id, map_version=map_v, frame_id="map",
                                          x=x, y=y, yaw=0.0)
    return Telemetry(stamp=1, pose=pose, battery_pct=80.0, task_state=None, loc_quality=1.0,
                     nav=nav)


def _status(running: bool, online: bool = True, tid="t1"):
    task = TaskSummary(task_id=tid, kind="patrol", state=TaskState.RUNNING) if running else None
    return Status(online=online, boot_id="b", ready=READY, control_epoch=1, last_seen=1, task=task)


_seq = [0]


def _ev(kind, **data):
    _seq[0] += 1
    return Event(event_id=f"e{_seq[0]}", seq=_seq[0], boot_id="b", stamp=1, kind=kind, data=data)


@pytest.fixture
def 台(tmp_path):
    c = 钟()
    db = SiteDB(tmp_path / "s.db")
    desk = AlertDesk(db, now_ms=c)
    rv = RunViz(now_ms=c, alerts=desk)
    yield c, rv, desk
    db.close()


# ------------------------------------------------------------ 路径


def test_路径_收下_只带序号沿用上一份_没有nav块就清(台):
    c, rv, _ = 台
    rv.on_status("A", _status(True))                    # 在跑任务:轨迹记着,狗一直在视图里
    rv.on_telemetry("A", _tele(1.0, nav={"seq": 1, "path": [[0, 0], [1, 0], [1, 2]]}))
    assert rv.view()["robots"]["A"]["path"] == [[0, 0], [1, 0], [1, 2]]
    rv.on_telemetry("A", _tele(1.0, nav={"seq": 1}))
    assert rv.view()["robots"]["A"]["path"] == [[0, 0], [1, 0], [1, 2]]
    rv.on_telemetry("A", _tele(1.0, nav={"seq": 2}))          # 序号涨了、点还没到:先留着旧的
    assert rv.view()["robots"]["A"]["path"] == [[0, 0], [1, 0], [1, 2]]
    rv.on_telemetry("A", _tele(1.0, nav={"seq": 2, "path": [[5, 5], [6, 6]]}))
    assert rv.view()["robots"]["A"]["path"] == [[5, 5], [6, 6]]
    rv.on_telemetry("A", _tele(1.0))                           # 导航结束
    assert rv.view()["robots"]["A"]["path"] == []


@pytest.mark.parametrize("bad", [
    "xx", [[0, 0]], [[0, 0], [1]], [[0, 0], ["a", 1]], [[0, 0], [float("nan"), 1]],
    [[0, 0], [float("inf"), 1]], [[0, 0], [True, 1]], [[0, 0]] * (PATH_MAX + 1), None, 5])
def test_路径_形状不对_点太多_不是有限数_丢掉_不碰旧的(台, bad):
    c, rv, _ = 台
    assert clean_path(bad) is None
    rv.on_telemetry("A", _tele(1.0, nav={"seq": 1, "path": [[0, 0], [1, 1]]}))
    rv.on_telemetry("A", _tele(1.0, nav={"seq": 2, "path": bad}))
    assert rv.view()["robots"]["A"]["path"] == [[0, 0], [1, 1]]


def test_路径_任务结束就清(台):
    c, rv, _ = 台
    rv.on_status("A", _status(True))
    rv.on_telemetry("A", _tele(1.0, nav={"seq": 1, "path": [[0, 0], [1, 1]]}))
    rv.on_event("A", _ev("task_done", task_id="t1"))
    assert rv.view()["robots"]["A"]["path"] == []


# ------------------------------------------------------------ 轨迹


def test_轨迹_挪了一步才记_无位姿不记(台):
    c, rv, _ = 台
    rv.on_status("A", _status(True))
    rv.on_telemetry("A", _tele(0.0))
    rv.on_telemetry("A", _tele(TRAIL_STEP_M / 2))               # 没挪够
    rv.on_telemetry("A", _tele(None))                           # 定位不可信:没位姿
    rv.on_telemetry("A", _tele(TRAIL_STEP_M + 0.01))
    assert rv.view()["robots"]["A"]["trail"] == [[0.0, 0.0], [round(TRAIL_STEP_M + 0.01, 2), 0.0]]


def test_轨迹_有界_满了丢最老的(台):
    c, rv, _ = 台
    rv.on_status("A", _status(True))
    for i in range(TRAIL_MAX + 50):
        c.ms += 10
        rv.on_telemetry("A", _tele(i * (TRAIL_STEP_M + 0.05)))
    tr = rv.view()["robots"]["A"]["trail"]
    assert len(tr) == TRAIL_MAX
    assert tr[-1][0] == round((TRAIL_MAX + 49) * (TRAIL_STEP_M + 0.05), 2)
    assert tr[0][0] == round(50 * (TRAIL_STEP_M + 0.05), 2)


def test_轨迹_太老的丢_换图从头记_位置跳了另起一段(台):
    c, rv, _ = 台
    rv.on_status("A", _status(True))
    rv.on_telemetry("A", _tele(0.0))
    c.ms += TRAIL_AGE_MS + 1
    rv.on_telemetry("A", _tele(1.0))
    assert rv.view()["robots"]["A"]["trail"] == [[1.0, 0.0]]
    rv.on_telemetry("A", _tele(5.0, map_v="8"))                 # 换了图版本
    assert rv.view()["robots"]["A"]["trail"] == [[5.0, 0.0]]
    assert rv.view()["robots"]["A"]["map"] == {"map_id": "m", "version": "8"}
    rv.on_telemetry("A", _tele(5.0 + TRAIL_JUMP_M + 1, map_v="8"))   # 重定位跳了
    v = rv.view()["robots"]["A"]
    assert v["trail"] == [[5.0, 0.0], [round(5.0 + TRAIL_JUMP_M + 1, 2), 0.0]]
    assert v["trail_cuts"] == [1], "不连成穿墙的线"


def test_视图_只给有东西可画的狗_按狗筛(台):
    c, rv, _ = 台
    rv.on_status("A", _status(False))
    assert rv.view()["robots"] == {}
    for k in "BC":
        rv.on_status(k, _status(True))
    rv.on_telemetry("B", _tele(0.0))
    rv.on_telemetry("C", _tele(0.0))
    assert set(rv.view()["robots"]) == {"B", "C"}
    assert set(rv.view("B")["robots"]) == {"B"}


# ------------------------------------------------------------ 被挡、重规划


def test_被挡_状态写原因位置等了多久_不被挡了清(台):
    c, rv, _ = 台
    rv.on_status("A", _status(True))
    rv.on_event("A", _ev("nav_blocked", reason="前方 0.4 m 有挡", x=2.5, y=1.0, waited_s=5.0,
                         map_id="m", map_version="7"))
    c.ms += 3000
    s = rv.nav_summary("A")["blocked"]
    assert s["reason"] == "前方 0.4 m 有挡" and (s["x"], s["y"]) == (2.5, 1.0)
    assert s["for_s"] == 8                                      # 狗说已经等了 5 s,又过了 3 s
    rv.on_event("A", _ev("nav_unblocked", waited_s=9.0, detour=False))
    assert rv.nav_summary("A") is None


def test_被挡_事件里的数乱写也不炸(台):
    c, rv, _ = 台
    rv.on_event("A", _ev("nav_blocked", reason=7, x="a", y=None, waited_s=float("nan")))
    b = rv.nav_summary("A")["blocked"]
    assert b["x"] is None and b["for_s"] == 0


def test_被挡超过一分钟且任务在跑_报P2带被挡的位置(台):
    c, rv, desk = 台
    rv.on_status("A", _status(True, tid="patrol-9"))
    rv.on_event("A", _ev("nav_blocked", reason="r", x=2.5, y=1.0, waited_s=5.0, map_id="m",
                         map_version="7"))
    rv.tick()
    assert desk.open() == [], "刚被挡 5 s:不报"
    c.ms += (BLOCK_ALERT_S - 5) * 1000 - 1
    rv.tick()
    assert desk.open() == [], "不到一分钟:不报"
    c.ms += 2
    rv.tick()
    [a] = desk.open()
    assert a["kind"] == "nav_blocked" and a["level"] == "P2" and a["robot"] == "A"
    assert a["context"]["pose"]["x"] == 2.5 and a["context"]["pose"]["map_version"] == "7"
    assert a["context"]["task_id"] == "patrol-9"
    rv.tick()
    assert len(desk.open()) == 1 and desk.open()[0]["count"] == 1, "同一次被挡只报一次"


def test_被挡的P2_动起来了自动解决_任务结束也解决_掉线也解决(台):
    c, rv, desk = 台
    for how in ("unblocked", "done", "offline", "idle"):
        rv.on_status("A", _status(True))
        rv.on_event("A", _ev("nav_blocked", reason="r", x=1.0, y=1.0, waited_s=5.0))
        c.ms += BLOCK_ALERT_S * 1000
        rv.tick()
        assert [a["kind"] for a in desk.open()] == ["nav_blocked"], how
        if how == "unblocked":
            rv.on_event("A", _ev("nav_unblocked", waited_s=70.0, detour=False))
        elif how == "done":
            rv.on_event("A", _ev("task_failed", task_id="t1", reason="被挡"))
        elif how == "offline":
            rv.on_status("A", _status(False, online=False))
        else:
            rv.on_status("A", _status(False))
        assert desk.open() == [], how
        assert rv.nav_summary("A") is None, how


def test_没任务在跑_被挡不报_狗空闲时的残留被挡清掉(台):
    c, rv, desk = 台
    rv.on_status("A", _status(False))
    rv.on_event("A", _ev("nav_blocked", reason="r", x=1.0, y=1.0, waited_s=100.0))
    rv.tick()
    assert desk.open() == [] and rv.nav_summary("A") is None


def test_解决不成_下一拍再来(台):
    c, rv, desk = 台
    rv.on_status("A", _status(True))
    rv.on_event("A", _ev("nav_blocked", reason="r", x=1.0, y=1.0, waited_s=5.0))
    c.ms += BLOCK_ALERT_S * 1000
    rv.tick()
    real = desk.resolve_all

    def 炸(*a, **k):
        raise RuntimeError("库忙")
    desk.resolve_all = 炸
    rv.on_event("A", _ev("nav_unblocked", waited_s=70.0, detour=False))
    assert len(desk.open()) == 1
    desk.resolve_all = real
    rv.tick()
    assert desk.open() == []


def test_报不出去_下一拍再报(台):
    c, rv, desk = 台
    rv.on_status("A", _status(True))
    rv.on_event("A", _ev("nav_blocked", reason="r", x=1.0, y=1.0, waited_s=5.0))
    c.ms += BLOCK_ALERT_S * 1000
    real = desk.raise_alert

    def 炸(**k):
        raise RuntimeError("库忙")
    desk.raise_alert = 炸
    rv.tick()
    desk.raise_alert = real
    rv.tick()
    assert [a["kind"] for a in desk.open()] == ["nav_blocked"]


def test_重规划_记原因和次数_不认识的原因归成偏离_任务结束清(台):
    c, rv, _ = 台
    rv.on_status("A", _status(True))
    rv.on_event("A", _ev("nav_replan", why="zones", n=2))
    r = rv.nav_summary("A")["replan"]
    assert r["why"] == "zones" and r["n"] == 2
    rv.on_event("A", _ev("nav_replan", why="???", n="x"))
    r = rv.nav_summary("A")["replan"]
    assert r["why"] == "off_route" and r["n"] == 0
    rv.on_status("A", _status(False))
    assert rv.nav_summary("A") is None


def test_告警种类登记了_有英文中文标题_是P2():
    import json
    from pathlib import Path

    from d1max_site.alerts import LEVEL_OF, Level
    assert LEVEL_OF["nav_blocked"] is Level.P2
    cat = json.loads((Path(__file__).resolve().parents[3] / "design" / "i18n" / "alerts.json")
                     .read_text(encoding="utf-8"))
    assert cat["nav_blocked"]["en"].isascii() and cat["nav_blocked"]["zh"]


# ------------------------------------------------------------ 接口


def test_接口_runviz_要登录_狗视图带nav块(tmp_path):
    import time

    from test_site_api import 站
    s = 站(tmp_path)
    try:
        rv = RunViz(now_ms=lambda: int(time.time() * 1000))
        s.api.runviz = rv
        s.disp.runviz = rv
        assert s.req("GET", "/api/runviz")[0] == 401
        tok = s.login()
        rv.on_telemetry("A", _tele(1.0, nav={"seq": 1, "path": [[0, 0], [1, 1]]}))
        rv.on_event("A", _ev("nav_blocked", reason="前面有箱子", x=1.0, y=1.0, waited_s=6.0))
        code, d = s.req("GET", "/api/runviz", token=tok)
        assert code == 200 and d["robots"]["A"]["path"] == [[0, 0], [1, 1]]
        assert d["robots"]["A"]["blocked"]["reason"] == "前面有箱子"
        assert s.req("GET", "/api/runviz?robot=Z", token=tok)[1]["robots"] == {}
        assert s.req("POST", "/api/runviz", {}, token=tok)[0] == 405
        code, v = s.req("GET", "/api/robots", token=tok)
        assert code == 200 and v["robots"][0]["nav"]["blocked"]["reason"] == "前面有箱子"
    finally:
        s.close()


# ------------------------------------------------------------ W41:只画这一趟、遥控器那段不画


def _st(running=True, tid="t1", control=True):
    task = TaskSummary(task_id=tid, kind="patrol", state=TaskState.RUNNING) if running else None
    return Status(online=True, boot_id="b", ready=Ready(control, True, True, True),
                  control_epoch=1, last_seen=1, task=task)


def test_W41_轨迹_新任务开始清掉上一趟_没任务时不记_结束后留着(台):
    c, rv, _ = 台
    rv.on_status("A", _st(running=False))
    rv.on_telemetry("A", _tele(0.0))
    rv.on_telemetry("A", _tele(1.0))
    assert "A" not in rv.view()["robots"], "没在跑任务:不记"
    rv.on_status("A", _st(tid="t1"))
    for x in (2.0, 3.0, 4.0):
        rv.on_telemetry("A", _tele(x))
    rv.on_status("A", _st(running=False))
    rv.on_telemetry("A", _tele(9.0))                              # 收工以后挪的不记
    assert rv.view()["robots"]["A"]["trail"] == [[2.0, 0.0], [3.0, 0.0], [4.0, 0.0]], \
        "上一趟收工后留着"
    rv.on_status("A", _st(tid="t2"))
    rv.on_telemetry("A", _tele(5.0))
    rv.on_status("A", _st(tid="t2"))                              # 同一趟的状态再来不清
    rv.on_telemetry("A", _tele(6.0))
    assert rv.view()["robots"]["A"]["trail"] == [[5.0, 0.0], [6.0, 0.0]]


def test_W41_轨迹_遥控器拿走控制权那段不记_拿回来另起一段(台):
    c, rv, _ = 台
    rv.on_status("A", _st())
    rv.on_telemetry("A", _tele(0.0))
    rv.on_telemetry("A", _tele(1.0))
    rv.on_status("A", _st(control=False))                         # 遥控器拿走了
    rv.on_telemetry("A", _tele(1.5))
    rv.on_telemetry("A", _tele(2.5))
    rv.on_status("A", _st(control=True))                          # 拿回来,同一趟
    rv.on_telemetry("A", _tele(2.6))
    rv.on_telemetry("A", _tele(3.0))
    v = rv.view()["robots"]["A"]
    assert v["trail"] == [[0.0, 0.0], [1.0, 0.0], [2.6, 0.0], [3.0, 0.0]]
    assert v["trail_cuts"] == [2]
