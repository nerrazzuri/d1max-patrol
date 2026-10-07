

def test_W17_狗的告警带现场_最后在哪_在跑哪一趟_重启读回来还在(tmp_path):
    from d1max_contract.messages import MapPose, Telemetry
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    now = [1_000_000]
    db = SiteDB(tmp_path / "s.db")
    desk = AlertDesk(db, now_ms=lambda: now[0])
    src = SiteAlertSources(desk, now_ms=lambda: now[0])
    pose = MapPose(map_id="estate-1", map_version="7", frame_id="map", x=3.0, y=-1.0, yaw=0.5)
    src.on_telemetry("A", Telemetry(stamp=now[0], pose=pose, battery_pct=80.0, task_state=None,
                                    loc_quality=1.0))
    src._m("A").running, src._m("A").started = True, "patrol-7"
    a = desk.raise_alert(kind="stuck", robot="A", title="卡住了")
    assert a.context["pose"] == pose.to_wire() | {"at_ms": 1_000_000}
    assert a.context["task_id"] == "patrol-7"
    assert desk.raise_alert(kind="schedule_died", robot="site", title="x").context == {}
    again = AlertDesk(db, now_ms=lambda: now[0])
    [b] = [x for x in again.open() if x["kind"] == "stuck"]
    assert b["context"]["pose"]["x"] == 3.0 and b["context"]["task_id"] == "patrol-7"


def test_W17_现场拿不到不挡告警(tmp_path):
    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    desk = AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1)

    def 炸(_):
        raise RuntimeError("x")
    desk.context_for = 炸
    assert desk.raise_alert(kind="stuck", robot="A", title="卡住了", context={"k": 1}).context \
        == {"k": 1}


def test_W17_老库升级_告警表补上现场列(tmp_path):
    import sqlite3

    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    p = tmp_path / "s.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE alerts (key TEXT PRIMARY KEY, level TEXT NOT NULL, kind TEXT NOT NULL, "
              "robot TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL, "
              "first_ms INTEGER NOT NULL, last_ms INTEGER NOT NULL, count INTEGER NOT NULL, "
              "acked_by TEXT NOT NULL, acked_ms INTEGER, resolved_by TEXT NOT NULL, "
              "resolved_ms INTEGER, escalated INTEGER NOT NULL)")
    c.execute("INSERT INTO alerts VALUES ('A/stuck#1','P1','stuck','A','t','',1,1,1,'',NULL,'',"
              "NULL,0)")
    c.commit()
    c.close()
    [a] = AlertDesk(SiteDB(p), now_ms=lambda: 2).open()
    assert a["context"] == {}


def _故障事件(seq, *codes):
    from d1max_contract.hal import Fault
    from d1max_contract.messages import Event, fault_event_data
    return Event(event_id=f"e{seq}", seq=seq, boot_id="b", stamp=1, kind="robot_fault",
                 data=fault_event_data(tuple(Fault(code=c, fatal=False, text=f"{c} 关不上")
                                             for c in codes)))


def _开着的(desk, kind):
    return [a for a in desk.open() if a["kind"] == kind]


def test_W21复查_上装关不上_每一路一条P1_重复上报不重复_好了解决_站点重启也对得上(tmp_path):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    desk = AlertDesk(db, now_ms=lambda: 1_000)
    src = SiteAlertSources(desk, now_ms=lambda: 1_000)
    src.on_event("A", _故障事件(1, "payload_siren", "payload_spotlight", "7"))
    [siren] = _开着的(desk, "payload_siren")
    assert siren["level"] == "P1" and "警笛" in siren["title"] and siren["robot"] == "A"
    assert len(_开着的(desk, "payload_spotlight")) == 1 and not _开着的(desk, "payload_strobe")
    src.on_event("A", _故障事件(2, "payload_siren", "payload_spotlight"))   # 重复上报
    assert len(_开着的(desk, "payload_siren")) == 1
    assert _开着的(desk, "payload_siren")[0]["count"] == 1, "不重复报(不聚合计数)"
    src.on_event("A", _故障事件(3, "payload_siren"))                          # 聚光灯关上了
    assert not _开着的(desk, "payload_spotlight") and _开着的(desk, "payload_siren")
    # 站点重启:内存没了,告警簿还在;重复上报照样不重复,好了照样解决
    desk2 = AlertDesk(db, now_ms=lambda: 2_000)
    src2 = SiteAlertSources(desk2, now_ms=lambda: 2_000)
    src2.on_event("A", _故障事件(4, "payload_siren"))
    [again] = _开着的(desk2, "payload_siren")
    assert again["count"] == 1
    src2.on_event("A", _故障事件(5))                                          # 全好了
    assert not _开着的(desk2, "payload_siren")
    assert not [a for a in desk2.open() if a["kind"].startswith("payload_")]


def test_W21复查_别的狗的上装故障不串(tmp_path):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    desk = AlertDesk(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1)
    src = SiteAlertSources(desk, now_ms=lambda: 1)
    src.on_event("A", _故障事件(1, "payload_strobe"))
    src.on_event("B", _故障事件(1))
    assert [a["robot"] for a in _开着的(desk, "payload_strobe")] == ["A"]
