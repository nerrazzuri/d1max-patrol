"""W72(用户 11-03):状态类告警 —— 情况好了自己解决、只留记录;很短的中断不叫人。

- 跑任务时断联不超过 30 秒:只记一条已解决的记录,不推送;超过才报 P1;连回来稳定在线后自己解决。
- 点位未到:只记录;同一个点连着 3 趟没到才报 P1 要人处理(多半是路被堵死了)。
- 定位变差 / 定位丢失暂停、急停、上装过热、热像镜头被挡、录像中断:恢复了自己解决。
- 事件类(任务中止等)不跟着自动解决。
"""

from __future__ import annotations

import pytest

from nexwar_contract.messages import Event, Ready, Status, TaskState, TaskSummary
from nexwar_site.alert_sources import OFFLINE_ALERT_S, WAYPOINT_MISS_RUNS, SiteAlertSources
from nexwar_site.alert_store import AlertDesk
from nexwar_site.db import SiteDB


class 钟:
    def __init__(self) -> None:
        self.ms = 1_800_000_000_000

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def 台(tmp_path):
    c = 钟()
    db = SiteDB(tmp_path / "site.db")
    desk = AlertDesk(db, now_ms=c)
    desk.push = True
    stale: set[str] = set()
    src = SiteAlertSources(desk, now_ms=c, is_stale=lambda rid: rid in stale)
    yield c, db, desk, src, stale
    db.close()


def _status(*, online=True, task=None, **ready) -> Status:
    r = dict(control=True, motion=True, estop_clear=True, loc_ok=True) | ready
    return Status(online=online, boot_id="b1", ready=Ready(**r), control_epoch=1, last_seen=1,
                  task=task)


def _running(tid="t1") -> TaskSummary:
    return TaskSummary(task_id=tid, kind="patrol", state=TaskState.RUNNING)


_seq = iter(range(1, 10_000))


def _ev(kind: str, **data) -> Event:
    n = next(_seq)
    return Event(event_id=f"e{n}", seq=n, boot_id="b1", stamp=1, kind=kind, data=data)


_NOTE = ("run_start", "run_done")      # P3 通知,不在这里比


def _open(desk, robot="A") -> list[str]:
    return sorted(a.kind for a in desk.book.open() if a.robot == robot and a.kind not in _NOTE)


def _all(desk, robot="A"):
    return sorted((a for a in desk.book.all() if a.robot == robot and a.kind not in _NOTE),
                  key=lambda a: a.first_ms)


def _pushed(db) -> list[str]:
    return [r["alert_key"] for r in db.query("SELECT alert_key FROM push_outbox")]


def test_跑任务时断联不到30秒_只记一条已解决的记录_不推送(台):
    c, db, desk, src, stale = 台
    src.on_status("A", _status(task=_running()))
    src.on_status("A", _status(online=False))          # 遗言:WiFi 闪断
    c.ms += (OFFLINE_ALERT_S - 10) * 1000
    src.tick()
    assert _open(desk) == [], "30 秒以内不报"
    src.on_status("A", _status(task=_running()))       # 回来了
    [a] = _all(desk)
    assert a.kind == "robot_offline" and a.resolved_ms is not None
    assert a.resolved_by.startswith("site:")
    assert "20 秒" in a.title
    assert _pushed(db) == [], "短的闪断不推到手机"


def test_跑任务时断联超过30秒_报P1推送_连回来稳定在线一分钟自己解决(台):
    c, db, desk, src, stale = 台
    src.on_status("A", _status(task=_running()))
    src.on_status("A", _status(online=False))
    c.ms += (OFFLINE_ALERT_S + 1) * 1000
    src.tick()
    assert _open(desk) == ["robot_offline"]
    [a] = _all(desk)
    assert a.level.value == "P1" and _pushed(db) == [a.key]
    src.on_status("A", _status(task=_running()))
    src.tick()
    assert _open(desk) == ["robot_offline"], "刚回来:迟滞里先不解决"
    c.ms += 61_000
    src.tick()
    assert _open(desk) == [], "连着在线一分钟:自己解决"
    [a] = _all(desk)
    assert a.resolved_by == "site:online" and a.verdict == ""


def test_状态过期才发现的掉线_已经静默很久_马上报(台):
    c, db, desk, src, stale = 台
    src.on_status("A", _status(task=_running()))
    stale.add("A")
    src.tick()
    assert _open(desk) == ["robot_offline"]


def test_断联闪来闪去_挂着的那条不重复记录_最后一直不回来照样报(台):
    c, db, desk, src, stale = 台
    src.on_status("A", _status(task=_running()))
    src.on_status("A", _status(online=False))
    c.ms += 5_000
    src.on_status("A", _status(task=_running()))       # 闪断一次:一条记录
    c.ms += 10_000
    src.on_status("A", _status(online=False))          # 回来不到一分钟又掉,这回不回来了
    c.ms += (OFFLINE_ALERT_S + 1) * 1000
    src.tick()
    assert _open(desk) == ["robot_offline"], "第二次掉了不回来:要报"
    assert len(_all(desk)) == 2


def test_空闲时断联也一样_不到30秒只记录(台):
    c, db, desk, src, stale = 台
    src.on_status("B", _status())
    src.on_status("B", _status(online=False))
    c.ms += 3_000
    src.on_status("B", _status())
    [a] = _all(desk, "B")
    assert a.kind == "robot_offline_idle" and a.resolved_ms is not None


def test_点位未到_只记录不推送_同一个点连着3趟没到才报P1(台):
    c, db, desk, src, stale = 台
    for i in range(WAYPOINT_MISS_RUNS - 1):
        src.on_event("A", _ev("patrol_waypoint", task_id=f"t{i}", name="gate", ok=False,
                              note="被挡"))
        c.ms += 1000
    assert _open(desk) == [] and _pushed(db) == []
    assert all(a.kind == "stuck" and a.resolved_ms is not None for a in _all(desk))
    src.on_event("A", _ev("patrol_waypoint", task_id="t9", name="gate", ok=False, note="被挡"))
    assert _open(desk) == ["stuck"]
    [a] = [x for x in _all(desk) if x.resolved_ms is None]
    assert "连续 3 趟" in a.title and "gate" in a.title and _pushed(db) == [a.key]


def test_点位未到_中间到过一次就从头数_同一趟重复报只算一趟(台):
    c, db, desk, src, stale = 台
    src.on_event("A", _ev("patrol_waypoint", task_id="t1", name="gate", ok=False))
    src.on_event("A", _ev("patrol_waypoint", task_id="t1", name="gate", ok=False))
    src.on_event("A", _ev("patrol_waypoint", task_id="t2", name="gate", ok=True))
    src.on_event("A", _ev("patrol_waypoint", task_id="t3", name="gate", ok=False))
    src.on_event("A", _ev("patrol_waypoint", task_id="t4", name="gate", ok=False))
    src.on_event("A", _ev("patrol_waypoint", task_id="t4", name="pond", ok=False))
    assert _open(desk) == []


def test_定位变差和定位丢失暂停_定位恢复了自己解决(台):
    c, db, desk, src, stale = 台
    src.on_status("A", _status(task=_running(), loc_ok=False))
    c.ms += 31_000
    src.tick()
    assert _open(desk) == ["loc_degraded", "loc_lost_paused"]
    src.on_status("A", _status(task=_running()))
    assert _open(desk) == []


def test_急停松开_上装凉下来_镜头擦干净_录像恢复_各自解决(台):
    c, db, desk, src, stale = 台
    src.on_status("A", _status(estop_clear=False))
    src.on_event("A", _ev("board_thermal", level="shed", reason="SoC 88 °C"))
    src.on_event("A", _ev("thermal_lens_blocked", camera="thermal"))
    src.on_event("A", _ev("recording_failed", camera="front", reason="相机不通"))
    src.on_event("A", _ev("recording_failed", camera="rear", reason="相机不通"))
    assert _open(desk) == ["board_hot", "estop_pressed", "lens_blocked", "recording_failed"]
    src.on_status("A", _status())
    src.on_event("A", _ev("board_thermal", level="ok", reason=""))
    src.on_event("A", _ev("thermal_lens_clear", camera="thermal"))
    src.on_event("A", _ev("recording_ok", camera="front"))
    assert _open(desk) == ["recording_failed"], "还有一路没录回来"
    src.on_event("A", _ev("recording_ok", camera="rear"))
    assert _open(desk) == []


def test_事件类不跟着自动解决_任务中止还要人确认(台):
    c, db, desk, src, stale = 台
    desk.raise_alert(kind="run_abort", robot="A", title="任务中止")
    src.on_status("A", _status(task=_running()))
    src.on_status("A", _status(online=False))
    c.ms += 2_000
    src.on_status("A", _status())
    c.ms += 61_000
    src.tick()
    assert _open(desk) == ["run_abort"]
