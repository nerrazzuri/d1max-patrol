"""W00c5d:运行记录、判读、复核、导出、值守汇总里的盘况与备份,走站点 API(决策 8:证据都在站点)。"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import zipfile
from io import BytesIO
from urllib.parse import quote

import pytest
from test_site_api import PW, _等, 站

from d1max_contract.messages import TaskState, Telemetry
from d1max_contract.storage import StorageFacts

S1 = "20260925T010000Z"


class 假模型:
    def judge(self, prompt, images):
        return {"verdict": "abnormal", "confidence": 0.8, "reason": "门没关", "evidence": ""}


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, runs={"client": 假模型, "backup_dir": tmp_path / "bak"})
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _传一趟(s, robot="A"):
    run = f"巡检一/{S1}"
    photo = os.urandom(900)
    files = {"events.jsonl": b'{"kind":"start"}\n', f"photos/P1__front__{S1}.jpg": photo,
             "manifest.json": json.dumps({"summary": {"result": "done"}}).encode()}
    for rel, data in files.items():
        s.store.put(robot, run, rel, offset=0, data=data, total=len(data))
    return s.store.runs(robot_id=robot)[0]["id"], photo


def _get_raw(s, path, tok):
    host, port = s.api.httpd.server_address[:2]
    req = urllib.request.Request(f"http://{host}:{port}{path}",
                                 headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_业主看得到记录与照片_不能复核不能导出(站点):
    s = 站点
    rid, photo = _传一趟(s)
    olga = _登(s, "olga")
    code, d = s.req("GET", "/api/runs?robot=A", token=olga)
    assert code == 200 and [r["id"] for r in d["runs"]] == [rid]
    code, d = s.req("GET", f"/api/runs/{rid}", token=olga)
    assert code == 200 and d["photos"][0]["waypoint"] == "P1"
    name = d["photos"][0]["name"]
    st, ctype, body = _get_raw(s, f"/api/runs/{rid}/photos/{quote(name)}", olga)
    assert st == 200 and ctype == "image/jpeg" and body == photo
    assert s.req("POST", f"/api/runs/{rid}/review/{quote(name)}",
                 {"verdict": "normal"}, token=olga)[0] == 403
    assert s.req("POST", f"/api/runs/{rid}/judge", {}, token=olga)[0] == 403
    assert s.req("GET", "/api/exports", token=olga)[0] == 403
    assert s.req("GET", "/api/runs/999", token=olga)[0] == 404
    assert s.req("GET", f"/api/runs/{rid}/photos/..%2Fmanifest.json", token=olga)[0] in (400, 404)


def test_保安判读复核_异常出告警_复核进审计(站点):
    s = 站点
    rid, _ = _传一趟(s)
    gina = _登(s, "gina")
    code, d = s.req("POST", f"/api/runs/{rid}/judge", {}, token=gina)
    assert code == 200 and d["findings"][0]["verdict"] == "abnormal"
    kinds = [a["kind"] for a in s.req("GET", "/api/alerts", token=gina)[1]["alerts"]]
    assert "finding" in kinds
    name = s.req("GET", f"/api/runs/{rid}", token=gina)[1]["photos"][0]["name"]
    code, d = s.req("POST", f"/api/runs/{rid}/review/{quote(name)}",
                    {"verdict": "normal", "note": "风吹的"}, token=gina)
    assert code == 200 and d["reviewed"] == 1
    assert s.req("POST", f"/api/runs/{rid}/review/{quote(name)}", {"verdict": "ok"},
                 token=gina)[0] == 400
    p = s.req("GET", f"/api/runs/{rid}", token=gina)[1]["photos"][0]
    assert p["review"] == {"verdict": "normal", "note": "风吹的"}
    acts = [(r["actor"], r["action"]) for r in s.api.audit.list()]
    assert any(a == "gina" and "review" in act for a, act in acts), acts


def test_管理员导出_下载下来哈希对得上(站点):
    s = 站点
    _传一趟(s)
    alice, gina = _登(s, "alice"), _登(s, "gina")
    body = {"since_ms": 0, "until_ms": 2**53}
    assert s.req("POST", "/api/exports", body, token=gina)[0] == 403
    code, meta = s.req("POST", "/api/exports", body, token=alice)
    assert code == 200 and meta["runs"] == 1, meta
    st, ctype, data = _get_raw(s, f"/api/exports/{meta['name']}", alice)
    assert st == 200 and ctype == "application/zip"
    assert hashlib.sha256(data).hexdigest() == meta["sha256"]
    with zipfile.ZipFile(BytesIO(data)) as z:
        assert f"A/巡检一/{S1}/events.jsonl" in z.namelist()
    assert s.req("GET", "/api/exports/..%2Fsite.db", token=alice)[0] in (400, 404)
    assert s.req("POST", "/api/exports", {"since_ms": "x", "until_ms": 1}, token=alice)[0] == 400


def test_值守汇总_狗的盘况与站点的备份(站点):
    s = 站点
    olga = _登(s, "olga")
    _等(lambda: s.disp.telemetry_at.get("A"))
    d = s.req("GET", "/api/watch/summary", token=olga)[1]
    r = d["robots"][0]
    assert r["disk_used_ratio"] is None and "不知道" in r["why"]["disk_used_ratio"]
    assert r["bundle_lag"] is None and "任务包" in r["why"]["bundle_lag"]
    assert r["backup"] is None and "站点" in r["why"]["backup"]
    assert d["site"]["backup"]["configured"] is True and d["site"]["backup"]["last_ok_ms"] is None
    f = StorageFacts(disk_used_ratio=0.85, outbox_bytes=10, outbox_cap_bytes=100,
                     backlog_files=7, backlog_bytes=5000, oldest_backlog_s=2400)
    t = Telemetry(stamp=s.disp.clients["A"].telemetry.stamp, pose=None, battery_pct=80.0,
                  task_state=TaskState.RUNNING, loc_quality=1.0, storage=f)

    async def 来一条():
        s.disp._on_telemetry("A", t)
    s.loop.call(来一条)
    r = s.req("GET", "/api/watch/summary", token=olga)[1]["robots"][0]
    assert (r["disk_used_ratio"], r["upload_backlog"], r["oldest_backlog_s"]) == (0.85, 7, 2400)
    kinds = [a["kind"] for a in s.req("GET", "/api/alerts", token=olga)[1]["alerts"]]
    assert "disk_80" in kinds and "upload_backlog" in kinds
    s.loop.call(来一条)
    counts = {a["kind"]: a["count"] for a in s.req("GET", "/api/alerts", token=olga)[1]["alerts"]}
    assert counts["disk_80"] == 1 and counts["upload_backlog"] == 1, "同一件事不重复报"
    s.backup.run_once()
    d = s.req("GET", "/api/watch/summary", token=olga)[1]
    assert d["site"]["backup"]["last_ok_ms"] is not None
