"""2026-10-09 系统审查(跨功能)站点这一头的回归:S04 驱离收场不关受力警报的灯;S05 排程不抢在驱离的狗;
S06 等回执的不挡对账;S07 删过的证据不复活;S08 加密备份不漏同一秒里的改动。审查报告的复现断言的是错的
行为,这里断言对的。"""
# ruff: noqa: F811  (站点、ca 是从 test_site_intake 借来的夹具)

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace

from test_site_intake import ca, 站点  # noqa: F401  (夹具)

from d1max_site.db import SiteDB
from d1max_site.deterrence import DeterrenceDesk, Session
from d1max_site.force import ForceWatch
from d1max_site.modes import ArmingDesk

NOW = 1_800_000_000_000


class 假派遣:
    registry = SimpleNamespace(manual_only=lambda rid: None)

    def __init__(self, outputs):
        self.outputs = outputs
        self.clients = {"A": SimpleNamespace(
            status=SimpleNamespace(online=True, task=None), telemetry=None,
            capabilities=SimpleNamespace(tasks={"force": {"state": "lifted"},
                                                "deter": {"outputs": ["siren", "strobe"]}}))}

    def on_event(self, cb):
        pass

    async def deter(self, rid, payload, **kw):
        self.outputs[payload["output"]] = payload["on"]
        return {"ack": {"result": "accepted"}}

    async def abort(self, *a, **kw):
        return {"ack": {"result": "rejected", "reason": "no_such_task"}}


async def test_S04_驱离收场不关被抱起来的警笛警灯_过了45秒才归还(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    now, outputs = [NOW], {}
    d = 假派遣(outputs)
    arming = ArmingDesk(db, now_ms=lambda: now[0])
    arming.set_mode("armed", by="guard")
    force = ForceWatch(db, d, now_ms=lambda: now[0], arming=arming)
    deter = DeterrenceDesk(db, d, now_ms=lambda: now[0])
    deter.held_by_others = force.held
    s = Session(robot_id="A", incident_id=0, zone="here", task_id="incident-a", level=1,
                started_ms=now[0], level_ms=now[0])
    deter.sessions["A"] = s
    deter._save(s)
    await force.tick()
    assert outputs == {"siren": True, "strobe": True}
    now[0] += 5000
    await deter.release("A", by="guard")
    await force.tick()
    assert outputs == {"siren": True, "strobe": True}, "驱离只撤自己的,受力警报照响"
    now[0] += 41_000
    assert force.held("A") == set(), "45 秒过了:归还"
    db.close()


def test_S05_排程不抢在驱离的狗_选狗和发之前都看(tmp_path):
    from d1max_contract.mission import parse_mission
    from d1max_contract.schedule import parse_schedule
    from d1max_site.catalog import ActiveBundle
    from d1max_site.dispatcher import Dispatcher
    from d1max_site.scheduler import SiteScheduler
    db = SiteDB(tmp_path / "db")
    mission = parse_mission({"mission": "loop", "map_id": "m", "policy": {}, "waypoints": [
        {"name": "p", "pose": {"position": {"x": 1, "y": 0},
                               "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}]})
    schedule = parse_schedule({"timezone": "UTC", "entries": [
        {"id": "s", "mission": "loop", "at": "22:00", "days": ["mon"], "window_min": 30,
         "on_missed": "skip"}]})
    d = SimpleNamespace(
        clients={"A": SimpleNamespace(
            status=SimpleNamespace(task=None),
            capabilities=SimpleNamespace(tasks={"patrol": {"map_id": "m"}, "deter": {}}))},
        registry=SimpleNamespace(list=lambda: [SimpleNamespace(robot_id="A", revoked=False)]),
        dispatchable=lambda rid, kind: "", autonomy=lambda rid: "autonomous",
        on_event=lambda cb: None, on_ack=lambda cb: None)
    d.busy = lambda rid: Dispatcher.busy(d, rid)
    s = Session(robot_id="A", incident_id=1, zone="z", task_id="incident-x", level=2,
                started_ms=NOW, level_ms=NOW)
    deter = DeterrenceDesk(db, d, now_ms=lambda: NOW)
    deter.sessions["A"] = s
    deter._save(s)
    sched = SiteScheduler(db, d, now_ms=lambda: NOW)
    sched.site_busy = deter.busy
    act = ActiveBundle("b", 1, NOW, schedule, {"loop": mission})
    ok, _, why, kinds = sched._candidates(act, schedule.entries[0], {})
    assert ok == [] and "正在驱离" in why and kinds == {"busy"}
    db.close()


async def test_S06_驱离等回执的时候_受力对账照样每拍跑(monkeypatch):
    import d1max_site.main as main
    cls = next(v for v in vars(main).values() if isinstance(v, type) and "_alert_loop" in vars(v))
    entered, unblock, calls, stop = asyncio.Event(), asyncio.Event(), [], threading.Event()

    async def slow():
        entered.set()
        await unblock.wait()

    async def noop():
        pass
    rt = SimpleNamespace(
        _stop=stop, alert_sources=SimpleNamespace(step=lambda: calls.append("离线")),
        incidents=SimpleNamespace(retell=lambda: None),
        deterrence=SimpleNamespace(tick=slow), charge=SimpleNamespace(tick=noop),
        sightings=SimpleNamespace(tick=lambda: None),
        force=SimpleNamespace(reconcile=lambda: calls.append("受力"), sound=noop),
        evidence_watch=SimpleNamespace(tick=lambda: None),
        weather=SimpleNamespace(tick=lambda: calls.append("天气") or noop()),
        arming=SimpleNamespace(tick=lambda: None))
    rt._lane = lambda name, fn, **kw: cls._lane(rt, name, fn, **kw)
    real_sleep = asyncio.sleep

    async def fast(_n):
        await real_sleep(0)
    monkeypatch.setattr(main.asyncio, "sleep", fast)
    job = asyncio.create_task(cls._alert_loop(rt))
    await entered.wait()
    for _ in range(20):
        await real_sleep(0)
    assert calls.count("受力") >= 3 and calls.count("离线") >= 3 and calls.count("天气") >= 3, calls
    stop.set()
    unblock.set()
    await job


def test_S07_删过的照片_又传上来照收照回执_不复活(tmp_path):
    from d1max_site.evidence import EvidenceStore
    from d1max_site.privacy import PrivacyDesk
    db = SiteDB(tmp_path / "db")
    now = [NOW]
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=lambda: now[0])
    run, rel, data = "mission/20261009T120000Z", "photos/P1__front__20261009T120000Z.jpg", \
        b"private-image"
    store.put("A", run, rel, offset=0, data=data, total=len(data))
    privacy = PrivacyDesk(db, store, now_ms=lambda: now[0])
    assert privacy.purge(since_ms=now[0] - 1, until_ms=now[0] + 1)["runs"] == 1
    now[0] += 1000
    got = store.put("A", run, rel, offset=0, data=data[:5], total=len(data))
    assert got.discarded, "回执叫狗当传完(PR #87 复查 R3:一个字节都不收)"
    assert not db.query("SELECT * FROM runs") and not db.query("SELECT * FROM run_photos")
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix == ".jpg"]
    db.close()


def test_S07_传到一半的时候删了_收齐也不登记(tmp_path):
    from d1max_site.evidence import EvidenceStore
    from d1max_site.privacy import PrivacyDesk
    db = SiteDB(tmp_path / "db")
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=lambda: NOW)
    run = "mission/20261009T120000Z"
    store.put("A", run, "events.jsonl", offset=0, data=b"{}\n", total=3)
    rel, data = "photos/P1__front__20261009T120000Z.jpg", b"0123456789"
    store.put("A", run, rel, offset=0, data=data[:4], total=len(data))
    PrivacyDesk(db, store, now_ms=lambda: NOW).purge(since_ms=NOW - 1, until_ms=NOW + 1)
    store.put("A", run, rel, offset=4, data=data[4:], total=len(data))
    assert not db.query("SELECT * FROM runs") and not db.query("SELECT * FROM run_photos")
    db.close()


def test_S07_删过的录像段_又传上来不复活(tmp_path):
    from d1max_site.recordings import RecordingStore, stamp_ms
    db = SiteDB(tmp_path / "db")
    rec = RecordingStore(db, tmp_path / "rec", now_ms=lambda: NOW)
    stamp = "20261009T010000Z"
    rec.put("A", f"front/{stamp}", "video.mp4", offset=0, data=b"x" * 100, total=100)
    [row] = rec.list(robot_id="A")
    assert rec._delete(row)
    rec.put("A", f"front/{stamp}", "video.mp4", offset=0, data=b"x" * 100, total=100)
    assert rec.list(robot_id="A") == [] and stamp_ms(stamp)
    db.close()


def test_S08_加密备份_同一秒里追加的后半截下一轮进备份(tmp_path):
    from d1max_site.backup import SiteBackup
    from d1max_site.sealbox import SealBox
    db = SiteDB(tmp_path / "db")
    root = tmp_path / "evidence"
    root.mkdir()
    dest = tmp_path / "backup"
    src = root / "events.jsonl"
    src.write_bytes(b"first\n")
    os.utime(src, (1000.1, 1000.1))
    box = SealBox(b"k" * 32)
    backup = SiteBackup(db, root, dest, now_ms=lambda: NOW, box=box)
    backup._mirror()
    calls = []
    real = box.seal_file
    box.seal_file = lambda s, d: (calls.append(s), real(s, d))
    backup._mirror()
    assert calls == [], "没变:不重加密(原文长度按加密的固定开销换算对得上)"
    src.write_bytes(b"first\nsecond\n")
    os.utime(src, (1000.9, 1000.9))
    backup._mirror()
    opened = tmp_path / "opened"
    box.open_file(dest / "evidence/events.jsonl.d1seal", opened)
    assert opened.read_bytes() == b"first\nsecond\n"
    src.write_bytes(b"FIRST\nSECOND\n")                   # 同长度覆盖、同一秒
    os.utime(src, (1000.95, 1000.95))
    backup._mirror()
    box.open_file(dest / "evidence/events.jsonl.d1seal", opened)
    assert opened.read_bytes() == b"FIRST\nSECOND\n"
    db.close()


def test_S08_拷的时候源还在变_标成要重拷_下一轮补上(tmp_path, monkeypatch):
    from d1max_site.backup import SiteBackup
    db = SiteDB(tmp_path / "db")
    root = tmp_path / "evidence"
    root.mkdir()
    dest = tmp_path / "backup"
    src = root / "p.jpg"
    src.write_bytes(b"half")
    backup = SiteBackup(db, root, dest, now_ms=lambda: NOW)
    import shutil
    real = shutil.copy2

    def 边拷边写(a, b, **kw):
        out = real(a, b, **kw)
        with open(src, "ab") as f:                          # 拷完那一下,后半截到了
            f.write(b"+rest")
        return out
    monkeypatch.setattr(shutil, "copy2", 边拷边写)
    backup._mirror()
    monkeypatch.setattr(shutil, "copy2", real)
    backup._mirror()
    assert (dest / "evidence" / "p.jpg").read_bytes() == b"half+rest"
    db.close()


def test_S08_加密开销_跟真的加密文件对得上(tmp_path):
    from d1max_site.sealbox import FILE_OVERHEAD, SealBox
    for n in (0, 1, 100, 70_000):
        (tmp_path / "a").write_bytes(os.urandom(n))
        SealBox(b"k" * 32).seal_file(tmp_path / "a", tmp_path / "b")
        assert (tmp_path / "b").stat().st_size == n + FILE_OVERHEAD


def test_S07_删过的又传上来_半截也不落进证据库(tmp_path):
    from d1max_site.evidence import EvidenceStore
    from d1max_site.privacy import PrivacyDesk
    db = SiteDB(tmp_path / "db")
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=lambda: NOW)
    run, rel = "mission/20261009T120000Z", "photos/P1__front__20261009T120000Z.jpg"
    store.put("A", run, rel, offset=0, data=b"abc", total=3)
    PrivacyDesk(db, store, now_ms=lambda: NOW).purge(since_ms=NOW - 1, until_ms=NOW + 1)
    store.put("A", run, rel, offset=0, data=b"a", total=3)
    assert not [p for p in (tmp_path / "evidence").rglob("*") if p.is_file()], \
        "删过的:收的时候就放到证据库外"
    db.close()


def test_S07_收的时候还没删_登记之前删了_登记那一步挡住(tmp_path, monkeypatch):
    import d1max_site.evidence as ev
    from d1max_site.evidence import EvidenceStore
    from d1max_site.privacy import PrivacyDesk
    db = SiteDB(tmp_path / "db")
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=lambda: NOW)
    run, rel = "mission/20261009T120000Z", "photos/P1__front__20261009T120000Z.jpg"
    store.put("A", run, "events.jsonl", offset=0, data=b"{}\n", total=3)
    PrivacyDesk(db, store, now_ms=lambda: NOW).purge(since_ms=NOW - 1, until_ms=NOW + 1)
    monkeypatch.setattr(ev, "is_purged", lambda *a: False)   # 收的那一刻还没删(交错)
    store.put("A", run, rel, offset=0, data=b"abc", total=3)
    assert not db.query("SELECT * FROM runs") and not db.query("SELECT * FROM run_photos")
    assert not [p for p in (tmp_path / "evidence").rglob("*") if p.is_file()]
    db.close()


# ------------------------------------------------------------ PR #87 复查(R1–R4)

RUN87, REL87, DATA87 = "mission/20261009T120000Z", "photos/P1__front__20261009T120000Z.jpg", \
    b"private image"


def _证据87(tmp_path, now):
    from d1max_site.evidence import EvidenceStore
    from d1max_site.privacy import PrivacyDesk
    db = SiteDB(tmp_path / "db")
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=lambda: now[0])
    store.put("A", RUN87, REL87, offset=0, data=DATA87, total=len(DATA87))
    return db, store, PrivacyDesk(db, store, now_ms=lambda: now[0])


def _文件(root):
    return [p for p in root.rglob("*") if p.is_file()]


def test_R1_删文件和落记号之间来了重传_不留没人管的文件(tmp_path, monkeypatch):
    import shutil
    import threading
    db, store, privacy = _证据87(tmp_path, [NOW])
    real, raced = shutil.rmtree, []

    def 交错(path, *a, **kw):
        out = real(path, *a, **kw)
        if not raced:                                     # 上传口在别的线程,正好这时候来
            t = threading.Thread(target=lambda: raced.append(store.put(
                "A", RUN87, REL87, offset=0, data=DATA87, total=len(DATA87))))
            t.start()
            raced.append(t)
        return out
    monkeypatch.setattr(shutil, "rmtree", 交错)
    assert privacy.purge(since_ms=NOW - 1, until_ms=NOW + 1)["runs"] == 1
    raced[0].join(5)
    got = raced[1]
    assert got.discarded, "记号先落了、删的时候拿着锁:交错进来的重传等删完、不收"
    assert not db.query("SELECT * FROM runs") and not _文件(store.root)
    db.close()


def test_R1_上传拿着锁的时候_删除等它写完再删(tmp_path):
    import threading
    db, store, privacy = _证据87(tmp_path, [NOW])
    lock = store.locks("run", "A", RUN87)
    lock.acquire()
    done = []
    t = threading.Thread(target=lambda: done.append(
        privacy.purge(since_ms=NOW - 1, until_ms=NOW + 1)))
    t.start()
    t.join(0.3)
    assert not done, "删除等着同一趟的收"
    lock.release()
    t.join(5)
    assert done and done[0]["runs"] == 1 and not _文件(store.root)
    db.close()


def test_R2_删过的记号不过期_过了一年再传也不复活(tmp_path):
    now = [NOW]
    db, store, privacy = _证据87(tmp_path, now)
    privacy.purge(since_ms=NOW - 1, until_ms=NOW + 1)
    now[0] += 400 * 86_400_000
    privacy.prune()
    assert db.query("SELECT * FROM purged")
    assert store.put("A", RUN87, REL87, offset=0, data=DATA87, total=len(DATA87)).discarded
    assert not db.query("SELECT * FROM runs") and not _文件(store.root)
    db.close()


def test_R3_删过的再传_一个字节都不收_没有暂存区_以前的暂存区起来就清(tmp_path):
    from d1max_site.evidence import EvidenceStore
    now = [NOW]
    db, store, privacy = _证据87(tmp_path, now)
    privacy.purge(since_ms=NOW - 1, until_ms=NOW + 1)
    got = store.put("A", RUN87, REL87, offset=0, data=DATA87[:5], total=len(DATA87))
    assert got.discarded and got.size == 0
    assert not _文件(tmp_path / "evidence")
    old = tmp_path / "evidence-purged-incoming" / "A" / "x.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"priva")
    EvidenceStore(tmp_path / "evidence", db, now_ms=lambda: now[0])
    assert not old.exists() and not (tmp_path / "evidence-purged-incoming").exists()
    db.close()


def test_R3_删过的录像段再传_不收(tmp_path):
    from d1max_site.recordings import RecordingStore
    db = SiteDB(tmp_path / "db")
    rec = RecordingStore(db, tmp_path / "rec", now_ms=lambda: NOW)
    stamp = "20261009T010000Z"
    rec.put("A", f"front/{stamp}", "video.mp4", offset=0, data=b"x" * 100, total=100)
    assert rec._delete(rec.list(robot_id="A")[0])
    got = rec.put("A", f"front/{stamp}", "video.mp4", offset=0, data=b"x" * 40, total=100)
    assert got.discarded and not _文件(tmp_path / "rec")
    db.close()


async def test_R4_受力警报开着_狗重启上装全关了_剩下的时间里补开_过了点不再开(tmp_path):
    from d1max_site.force import RENEW_S
    db = SiteDB(tmp_path / "s.db")
    now, outputs, sent = [NOW], {}, []
    d = 假派遣(outputs)
    real = d.deter

    async def deter(rid, payload, **kw):
        sent.append(dict(payload))
        return await real(rid, payload, **kw)
    d.deter = deter
    arming = ArmingDesk(db, now_ms=lambda: now[0])
    arming.set_mode("armed", by="guard")
    force = ForceWatch(db, d, now_ms=lambda: now[0], arming=arming)
    await force.tick()
    assert outputs == {"siren": True, "strobe": True}
    outputs.clear()                                       # 狗重启:上装全关
    now[0] += RENEW_S * 1000
    await force.tick()
    assert outputs == {"siren": True, "strobe": True}, "定时续:补回来了"
    assert sent[-1]["max_s"] == 45 - RENEW_S, "按剩下的时长,不重新算 45 秒"
    n = len(sent)
    now[0] = NOW + 46_000
    await force.tick()
    assert len(sent) == n and force.held("A") == set(), "过了 45 秒不再开、不再归它"
    db.close()


def test_R3_端到端_狗传站点已经删过的那一趟_站点不收_狗当传完删掉(站点, ca, tmp_path):
    from test_site_intake import STAMP, _sink, _一趟, _跑

    from d1max_agent.outbox import Outbox
    from d1max_site.evidence import mark_purged
    with 站点.store.db.tx() as tx:                       # 站点上这一趟删过了(狗断网时)
        mark_purged(tx, "run", "A", f"巡检一/{STAMP}", NOW)
    box = Outbox(tmp_path / "dogA", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    run = _一趟(box.root)
    _跑(box, 3)
    assert not run.exists(), "狗当传完:整趟删掉,不无限重试"
    assert not 站点.store.runs(robot_id="A")
    assert not [p for p in 站点.store.root.rglob("*") if p.is_file()]
    box.close()


# ------------------------------------------------------------ PR #88 复查


def test_PR88_旧暂存区删不掉_不吞错误_报P2_杂事里再删_删掉了自动解决(tmp_path, monkeypatch):
    import shutil

    from d1max_site import main as site_main
    from d1max_site.alert_store import AlertDesk
    from d1max_site.evidence import EvidenceStore
    from d1max_site.recordings import RecordingStore
    db = SiteDB(tmp_path / "db")
    old = tmp_path / "evidence-purged-incoming" / "A" / "x.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"priva")
    real = shutil.rmtree

    def 删不掉(path, *a, **kw):
        if "purged-incoming" in str(path):
            if kw.get("ignore_errors"):
                return None                               # 跟真的一样:吞了错误、什么都没删
            raise PermissionError("只读")
        return real(path, *a, **kw)
    monkeypatch.setattr(shutil, "rmtree", 删不掉)
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=lambda: NOW)
    assert "只读" in store.incoming_error and old.exists()
    rt = SimpleNamespace(evidence=store, recordings=RecordingStore(db, tmp_path / "rec",
                                                                   now_ms=lambda: NOW),
                         alerts=AlertDesk(db, now_ms=lambda: NOW))
    site_main.Server._retry_old_incoming(rt)
    site_main.Server._retry_old_incoming(rt)
    [a] = [a for a in rt.alerts.book.open() if a.kind == "purged_incoming_stuck"]
    assert a.level.name == "P2"
    monkeypatch.setattr(shutil, "rmtree", real)            # 权限修好了
    site_main.Server._retry_old_incoming(rt)
    assert not old.exists() and store.incoming_error == ""
    assert not [a for a in rt.alerts.book.open() if a.kind == "purged_incoming_stuck"]
    db.close()


async def test_PR88_续发时前一路等回执跨过了截止_后一路不再发开(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    now, outputs, sent = [NOW], {}, []
    d = 假派遣(outputs)
    real = d.deter

    async def 慢(rid, payload, **kw):
        sent.append((now[0], dict(payload)))
        if now[0] - NOW >= 40_000:
            now[0] += 10_000                              # 这一路等回执等了 10 秒
        return await real(rid, payload, **kw)
    d.deter = 慢
    arming = ArmingDesk(db, now_ms=lambda: now[0])
    arming.set_mode("armed", by="guard")
    force = ForceWatch(db, d, now_ms=lambda: now[0], arming=arming)
    await force.tick()
    sent.clear()
    now[0] = NOW + 40_000
    await force.tick()
    assert [p["output"] for _, p in sent] == ["siren"], "第二路发之前过了 45 秒:不发"
    db.close()
