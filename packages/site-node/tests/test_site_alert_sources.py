

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


def _落库(db, rid, seq, *codes, at=1):
    """像派遣器那样先把事件存进库(告警源回调没成也在库里)。"""
    import json

    from d1max_contract.hal import Fault
    from d1max_contract.messages import fault_event_data
    db.query("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, received_at) "
             "VALUES (?,?,?,?,?,?,?,?)",
             (rid, "b", seq, f"e{seq}", "robot_fault",
              json.dumps(fault_event_data(tuple(Fault(code=c, fatal=False, text=c)
                                                for c in codes))), 1, at))


def _台(tmp_path):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    desk = AlertDesk(db, now_ms=lambda: 1_000)
    return db, desk, SiteAlertSources(desk, now_ms=lambda: 1_000)


def test_W21复查2_第一次报告警没成_下一拍补上(tmp_path):
    db, desk, src = _台(tmp_path)
    real, calls = desk.raise_alert, []

    def 炸一次(**kw):
        calls.append(kw["kind"])
        if len(calls) == 1:
            raise RuntimeError("库锁住了")
        return real(**kw)
    desk.raise_alert = 炸一次
    _落库(db, "A", 1, "payload_siren")
    src.on_event("A", _故障事件(1, "payload_siren"))     # 回调里没成(不往外抛)
    assert not _开着的(desk, "payload_siren")
    src.on_event("A", _故障事件(1, "payload_siren"))     # 重投同一条:派遣器那头会去重,这里也补不上
    src.step()                                            # 下一拍对账
    assert len(_开着的(desk, "payload_siren")) == 1
    src.step()
    assert len(_开着的(desk, "payload_siren")) == 1, "对账不重复报"


def test_W21复查2_好了_解决没成_下一拍接着解决(tmp_path):
    db, desk, src = _台(tmp_path)
    src.on_event("A", _故障事件(1, "payload_spotlight"))
    assert _开着的(desk, "payload_spotlight")
    real = desk.resolve_all
    desk.resolve_all = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("库锁住了"))
    src.on_event("A", _故障事件(2))
    assert _开着的(desk, "payload_spotlight"), "这一下没解决成"
    desk.resolve_all = real
    src.step()
    assert not _开着的(desk, "payload_spotlight")


def test_W21复查2_事件落了库告警没做成_站点重启后对账补上(tmp_path):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    db, desk, src = _台(tmp_path)
    _落库(db, "A", 1, "payload_siren", "payload_strobe", at=10)
    _落库(db, "A", 2, "payload_siren", at=20)                # A 最近一条:只剩警笛
    _落库(db, "B", 1, "payload_spotlight", at=15)
    _落库(db, "B", 2, at=30)                                 # B 最近一条:都好了
    desk.raise_alert(kind="payload_spotlight", robot="B", title="老的", detail="")
    # 告警源一次都没成功处理过这几条(或者站点在那之前停了):重启
    desk2 = AlertDesk(db, now_ms=lambda: 2_000)
    src2 = SiteAlertSources(desk2, now_ms=lambda: 2_000)
    src2.step()
    assert [a["robot"] for a in _开着的(desk2, "payload_siren")] == ["A"]
    assert not _开着的(desk2, "payload_strobe"), "按最近一条算,警灯早好了"
    assert not _开着的(desk2, "payload_spotlight"), "B 最近一条好了:老告警解决"


def test_W21复查2_读库失败_下一拍再读(tmp_path):
    db, desk, src = _台(tmp_path)
    _落库(db, "A", 1, "payload_siren")
    real = db.query
    db.query = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("盘忙"))
    src.step()
    assert not _开着的(desk, "payload_siren")
    db.query = real
    src.step()
    assert len(_开着的(desk, "payload_siren")) == 1


def test_W21复查2_一台对账炸了_不挡别的台(tmp_path):
    db, desk, src = _台(tmp_path)
    real = desk.raise_alert

    def A总炸(**kw):
        if kw["robot"] == "A":
            raise RuntimeError("A 的这条写不进去")
        return real(**kw)
    desk.raise_alert = A总炸
    _落库(db, "A", 1, "payload_siren", at=1)
    _落库(db, "B", 1, "payload_siren", at=2)
    src.step()
    assert [a["robot"] for a in _开着的(desk, "payload_siren")] == ["B"]
