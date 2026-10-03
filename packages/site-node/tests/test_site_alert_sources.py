

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
