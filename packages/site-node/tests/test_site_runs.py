"""W00c5d:运行记录台(看、判读、复核、导出)与站点自己的备份。判读用假模型,不上网。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile

import pytest

from d1max_site.backup import STALE_MS, SiteBackup
from d1max_site.db import SiteDB
from d1max_site.evidence import EvidenceStore
from d1max_site.runs import QUIET_MS, RunDesk, RunError

S1, S2 = "20260925T010000Z", "20260926T010000Z"


class 钟:
    def __init__(self) -> None:
        self.ms = 1_790_000_000_000

    def __call__(self) -> int:
        return self.ms


class 假模型:
    def __init__(self, verdict="normal") -> None:
        self.verdict = verdict
        self.calls: list[int] = []                    # 每次送了几张图

    def judge(self, prompt, images):
        self.calls.append(len(images))
        return {"verdict": self.verdict, "confidence": 0.9, "reason": "看过了", "evidence": ""}


class 假告警:
    def __init__(self) -> None:
        self.got = []

    def raise_alert(self, **kw):
        self.got.append(kw)


def _传(store, robot, stamp, photos=("P1__front", "P2__front"), finished=True):
    run = f"巡检一/{stamp}"
    files = {"events.jsonl": b'{"kind":"start"}\n'}
    for p in photos:
        files[f"photos/{p}__{stamp}.jpg"] = os.urandom(800)
    files["manifest.json"] = json.dumps(
        {"summary": {"result": "done"} if finished else {},
         "mission": {"waypoints": [{"name": "P1", "check": "门关好了没有"}]}}).encode()
    for rel, data in files.items():
        store.put(robot, run, rel, offset=0, data=data, total=len(data))
    return files


@pytest.fixture
def 台(tmp_path):
    c = 钟()
    db = SiteDB(tmp_path / "site.db")
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=c)
    model = {"m": 假模型()}
    alerts = 假告警()
    desk = RunDesk(store, home=tmp_path, now_ms=c, client_factory=lambda: model["m"],
                   alerts=alerts)
    return c, store, desk, model, alerts


def test_看一趟_照片带判读与复核(台):
    c, store, desk, model, _ = 台
    _传(store, "A", S1)
    rid = store.runs(robot_id="A")[0]["id"]
    d = desk.detail(rid)
    assert d["run"]["photos"] == 2 and d["run"]["finished"] is True
    assert [p["waypoint"] for p in d["photos"]] == ["P1", "P2"]
    assert all(p["finding"] is None and p["review"] is None for p in d["photos"])
    assert desk.photo(rid, d["photos"][0]["name"]).is_file()
    for bad in ("../manifest.json", "nope.jpg", ""):
        with pytest.raises(RunError):
            desk.photo(rid, bad)


def test_跑完而且一分钟没新文件才自动判读_判出异常出告警(台):
    c, store, desk, model, alerts = 台
    _传(store, "A", S1)
    _传(store, "A", S2, finished=False)
    assert desk.step() == 0, "刚收完:照片可能还在路上"
    c.ms += QUIET_MS + 1
    model["m"] = 假模型("abnormal")
    assert desk.step() == 1, "没跑完的那一趟不判"
    r = store.runs(robot_id="A")
    judged = [x for x in r if x["stamp"] == S1][0]
    assert judged["verdicts"] == {"abnormal": 2} and judged["judged_ms"] is not None
    assert [a["kind"] for a in alerts.got] == ["finding", "finding"]
    assert desk.step() == 0, "判过了、之后没新文件,不重判"


def test_没配模型密钥就不自动判(台, tmp_path):
    c, store, _, _, _ = 台
    desk = RunDesk(store, home=tmp_path, now_ms=c, client_factory=lambda: None)
    _传(store, "A", S1)
    c.ms += QUIET_MS + 1
    assert desk.step() == 0


def test_同一个点位第二趟_带上一趟或基线一起判(台):
    c, store, desk, model, _ = 台
    _传(store, "A", S1)
    id1 = store.runs(robot_id="A")[0]["id"]
    desk.judge(id1)
    assert model["m"].calls == [1, 1], "第一趟没有可比的"
    _传(store, "A", S2)
    id2 = [x for x in store.runs(robot_id="A") if x["stamp"] == S2][0]["id"]
    model["m"].calls.clear()
    desk.judge(id2)
    assert model["m"].calls == [2, 2], "判正常的那张成了基线,第二趟带着它比"
    assert (desk.baselines / "A" / "P1__front.jpg").is_file()


def test_复核写得进_重判不动它_坏结论拒(台):
    c, store, desk, model, _ = 台
    _传(store, "A", S1)
    rid = store.runs(robot_id="A")[0]["id"]
    name = desk.detail(rid)["photos"][0]["name"]
    assert desk.review(rid, name, verdict="abnormal", note="门没关")["reviewed"] == 1
    desk.judge(rid)
    p = [x for x in desk.detail(rid)["photos"] if x["name"] == name][0]
    assert p["review"] == {"verdict": "abnormal", "note": "门没关"}
    assert p["finding"]["verdict"] == "normal"
    for bad in (dict(photo=name, verdict="ok", note=""), dict(photo="x.jpg", verdict="normal",
                                                            note="")):
        with pytest.raises(RunError):
            desk.review(rid, bad["photo"], verdict=bad["verdict"], note=bad["note"])
    with pytest.raises(RunError):
        desk.detail(99999)


def test_导出_文件原样_清单与哈希对得上(台):
    c, store, desk, model, _ = 台
    files = _传(store, "A", S1)
    _传(store, "B", S1)
    meta = desk.export(since_ms=c.ms - 1000, until_ms=c.ms + 1000, robot_id="A")
    assert meta["runs"] == 1 and meta["files"] == len(files)
    path = desk.export_path(meta["name"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == meta["sha256"]
    with zipfile.ZipFile(path) as z:
        listing = json.loads(z.read("清单.json"))
        for f in listing["files"]:
            data = z.read(f["path"])
            assert hashlib.sha256(data).hexdigest() == f["sha256"]
            assert f["path"].startswith("A/巡检一/")
        assert z.read(f"A/巡检一/{S1}/events.jsonl") == files["events.jsonl"]
    assert [e["name"] for e in desk.exports()] == [meta["name"]]
    with pytest.raises(RunError):
        desk.export(since_ms=c.ms + 10_000, until_ms=c.ms + 20_000)
    for bad in ("../site.db", "x.zip", meta["name"].replace(".zip", ".json")):
        with pytest.raises(RunError):
            desk.export_path(bad)


def test_备份_库一份快照_证据增量镜像_只留7份(台, tmp_path):
    c, store, desk, model, _ = 台
    _传(store, "A", S1)
    b = SiteBackup(store.db, store.root, tmp_path / "bak", now_ms=c)
    for _ in range(9):
        assert b.run_once()
        c.ms += 1000
    snaps = sorted((tmp_path / "bak" / "db").glob("site-*.db"))
    assert len(snaps) == 7
    con = sqlite3.connect(snaps[-1])
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    con.close()
    src = store.run_dir("A", "巡检一", S1) / "events.jsonl"
    dst = tmp_path / "bak" / "evidence" / "A" / "巡检一" / S1 / "events.jsonl"
    assert dst.read_bytes() == src.read_bytes()
    src.unlink()
    b.run_once()
    assert dst.exists(), "站点上删了的,备份里不跟着删"
    assert b.status()["configured"] and b.status()["last_ok_ms"] == c.ms


def test_备份过期出一条告警_没配就明说(台, tmp_path):
    c, store, desk, model, alerts = 台
    bad = tmp_path / "bak-is-a-file"
    bad.write_text("x")
    b = SiteBackup(store.db, store.root, bad, now_ms=c, alerts=alerts)
    b.step()
    assert not b.status()["last_ok_ms"] and b.status()["error"]
    c.ms += STALE_MS + 1
    b.step()
    b.step()
    assert [a["kind"] for a in alerts.got] == ["backup_stale"]
    none = SiteBackup(store.db, store.root, None, now_ms=c)
    assert none.status() == {"configured": False, "dest": "", "last_ok_ms": None, "error": "",
                             "stale": False}
