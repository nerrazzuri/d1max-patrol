"""手机端(Dart)读的站点报文夹具(W00c4)。夹具由这里**从真的站点 API** 取、落在
``mobile/test/fixtures/site_*.json``;站点改了报文形状,这里先红,提示
``D1MAX_WRITE_FIXTURES=1`` 重生成,Dart 那侧跟着红 —— 沿用「Python 夹具先红、Dart 跟着红」。

比的是**形状**(键与值的类型,递归),不是值:令牌、时刻每次都不一样。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from test_site_api import PW, _等, target, wall, 站

FIXTURES = Path(__file__).resolve().parents[3] / "mobile" / "test" / "fixtures"


def _shape(v):
    """键与值的类型,递归。**列表取所有元素形状的并集**:只看第一个的话,同一份列表(比如有的告警
    确认过、有的没有)按顺序不同会比出不同的形状 —— 时过时不过,而且真变了形也可能照样过。"""
    if isinstance(v, dict):
        # 事件的 ``data`` 按种类各不相同(契约里就是自由对象):哪几种事件赶上了这一趟,不该让形状变。
        return {k: ("obj" if k == "data" and isinstance(x, dict) else _shape(x))
                for k, x in sorted(v.items())}
    if isinstance(v, list):
        merged = None
        for x in v:
            merged = _merge(merged, _shape(x))
        return [merged] if v else []
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if v is None:
        return "null"
    return "str"


def _merge(a, b):
    if a is None:
        return b
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: _merge(a.get(k), b.get(k)) if k in a and k in b else (a.get(k) or b.get(k))
                for k in sorted(set(a) | set(b))}
    if isinstance(a, list) and isinstance(b, list):
        return [_merge(a[0] if a else None, b[0] if b else None)] if (a or b) else []
    if isinstance(a, str) and isinstance(b, str):
        return "|".join(sorted(set(a.split("|")) | set(b.split("|"))))
    return f"{a!r}|{b!r}"


class _假发布:
    def current(self):
        return "2026-09-20-aaaaaa"

    def commit_if_pending(self):
        return None


def _collect(tmp_path) -> dict[str, object]:
    from test_site_schedule import 打包

    from d1max_site.incidents import IncidentDesk
    from d1max_site.scheduler import SiteScheduler
    class _模型:
        n = 0

        def judge(self, prompt, images):
            _模型.n += 1
            return {"verdict": "abnormal" if _模型.n % 2 else "normal", "confidence": 0.7,
                    "reason": "看过了", "evidence": "左下角"}
    s = 站(tmp_path, alerts=True, video={}, agent_video=False,
          runs={"client": _模型, "backup_dir": tmp_path / "bak"}, maps={},
          releases={"ops": _假发布()})
    try:
        s.api.scheduler = SiteScheduler(s.db, s.disp, now_ms=wall)
        s.api.incidents = IncidentDesk(s.db, s.disp, now_ms=wall)
        code, login = s.req("POST", "/api/login", {"name": "alice", "password": PW})
        tok = login["token"]
        _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
        s.req("POST", "/api/bundles", {"path": str(打包(tmp_path, 1))}, token=tok)
        sub = s.disp.feed.subscribe()
        code, dispatch = s.req("POST", "/api/robots/A/goto", {"target": target(4.0)}, token=tok)
        busy = _等(lambda: (lambda v: v if (v["status"] or {}).get("task") else None)(
            s.req("GET", "/api/robots/A", token=tok)[1]))
        frame = _等(lambda: (lambda f: f if f and f["kind"] == "status" else None)(sub.get(1.0)))
        s.req("POST", "/api/robots/A/abort", {"task_id": dispatch["task_id"]}, token=tok)
        _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1]["events"])
        s.api.incidents.set_intercept("gate", map_id="estate-1", map_version="7", x=0.2, y=0.0,
                                      yaw=0.0)
        s.api.incidents.map_zone("yard", "gate")
        s.api.incidents.add_source("nvr")
        s.loop.call(lambda: s.api.incidents.handle("nvr", {"event_id": "e1",
                                                           "type": "intrusion", "zone": "yard"}))
        # W00c5a:告警与值守。狗急停 → 站点出 P1,SSE 推一帧,保安确认。
        s.loop.call(lambda: s.dog.emergency_stop(True))

        def _alert_frame():
            f = sub.get(1.0)
            return f if f and f["kind"] == "alert" and f["alert"]["kind"] == "estop_pressed" \
                else None
        alert_frame = _等(_alert_frame)
        alerts = _等(lambda: (lambda d: d if any(a["kind"] == "estop_pressed"
                                                  for a in d["alerts"]) else None)(
            s.req("GET", "/api/alerts", token=tok)[1]))
        from urllib.parse import quote
        estop = next(a for a in alerts["alerts"] if a["kind"] == "estop_pressed")
        key = quote(estop["key"], safe="")
        ack = s.req("POST", f"/api/alerts/{key}/ack", {}, token=tok)[1]
        # 交接班那一张(?all=1)要同时有:没人管且升到顶的、有人确认的、聚合两次且已解决的。
        def _seed():
            async def go():
                b = s.desk.book
                b.raise_alert(kind="fallen", robot="A", title="狗跌倒了",
                              now_ms=wall() - 6 * 60_000)
                b.due_escalations(now_ms=wall())
                b.raise_alert(kind="clock_skew", robot="A", title="狗的钟不准",
                              now_ms=wall() - 60_000)
                lag = b.raise_alert(kind="clock_skew", robot="A", title="狗的钟不准",
                                    now_ms=wall())
                b.resolve(lag.key, who="alice", now_ms=wall())
            return go()
        s.loop.call(_seed)
        alerts_all = s.req("GET", "/api/alerts?all=1", token=tok)[1]
        _等(lambda: s.disp.telemetry_at.get("A"))
        # W00c5d:一趟运行记录(两张照片,判过、复核过一张);狗报过盘况;站点备份过一次。
        import json as _json

        from d1max_contract.messages import TaskState, Telemetry
        from d1max_contract.storage import StorageFacts
        st = "20260925T010000Z"
        for rel, data in ((f"photos/P1__front__{st}.jpg", b"\xff\xd8x\xff\xd9"),
                          (f"photos/P2__front__{st}.jpg", b"\xff\xd8y\xff\xd9"),
                          ("events.jsonl", b'{"kind":"start"}\n'),
                          ("manifest.json", _json.dumps({"summary": {"result": "done"}}).encode())):
            s.store.put("A", f"巡检一/{st}", rel, offset=0, data=data, total=len(data))
        rid = s.store.runs(robot_id="A")[0]["id"]
        s.req("POST", f"/api/runs/{rid}/judge", {}, token=tok)
        s.req("POST", f"/api/runs/{rid}/review/P1__front__{st}.jpg",
              {"verdict": "normal", "note": "风吹的"}, token=tok)
        facts = StorageFacts(disk_used_ratio=0.42, outbox_bytes=10, outbox_cap_bytes=100,
                             backlog_files=3, backlog_bytes=900, oldest_backlog_s=95)

        async def _盘况():
            s.disp._on_telemetry("A", Telemetry(stamp=wall(), pose=None, battery_pct=80.0,
                                                task_state=TaskState.RUNNING, loc_quality=1.0,
                                                storage=facts))
        s.loop.call(_盘况)
        s.backup.run_once()
        # W00c5d 第二部分:一张导入的图、一个狗传上来的录包。
        mdir = tmp_path / "vendor-map"
        mdir.mkdir()
        (mdir / "estate-1.pgm").write_bytes(b"P5")
        (mdir / "estate-1.yaml").write_text("resolution: 0.05")
        s.maps.import_dir(mdir, map_id="estate-1", version="8", note="厂商图")
        s.maps.bag_done("A", "yard-0925", 5000)
        from test_site_releases import 做包
        s.rel_catalog.add(做包(tmp_path), note="夜巡修了一个 bug")
        return {
            "site_alerts": alerts,
            "site_alert_frame": alert_frame,
            "site_alert_ack": ack,
            "site_alerts_all": alerts_all,
            "site_watch_summary": s.req("GET", "/api/watch/summary", token=tok)[1],
            "site_video_health": s.req("GET", "/api/robots/A/video/health", token=tok)[1],
            "site_login": login,
            "site_robots": s.req("GET", "/api/robots", token=tok)[1],
            "site_robot": s.req("GET", "/api/robots/A", token=tok)[1],
            "site_robot_busy": busy,
            "site_dispatch": dispatch,
            "site_event_frame": frame,
            "site_schedule": s.req("GET", "/api/schedule", token=tok)[1],
            "site_incidents": s.req("GET", "/api/incidents", token=tok)[1],
            "site_error": s.req("POST", "/api/robots/A/goto", {"target": target(0.3)},
                                token="nope")[1],
            "site_runs": s.req("GET", "/api/runs", token=tok)[1],
            "site_run": s.req("GET", f"/api/runs/{rid}", token=tok)[1],
            "site_maps": s.req("GET", "/api/maps", token=tok)[1],
            "site_releases": s.req("GET", "/api/releases", token=tok)[1],
        }
    finally:
        s.close()


def test_手机读的站点夹具跟真站点同形(tmp_path):
    got = _collect(tmp_path)
    if os.environ.get("D1MAX_WRITE_FIXTURES") == "1":
        for k, v in got.items():
            (FIXTURES / f"{k}.json").write_text(
                json.dumps(v, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
    for k, v in got.items():
        f = FIXTURES / f"{k}.json"
        assert f.is_file(), f"{f.name} 没有:D1MAX_WRITE_FIXTURES=1 跑一次这条测试"
        assert _shape(json.loads(f.read_text(encoding="utf-8"))) == _shape(v), \
            f"{f.name} 跟站点现在的报文不同形:D1MAX_WRITE_FIXTURES=1 重生成,再修 Dart 那侧"
