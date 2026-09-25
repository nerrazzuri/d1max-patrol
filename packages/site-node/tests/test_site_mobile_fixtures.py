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
    if isinstance(v, dict):
        return {k: _shape(x) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return [_shape(v[0])] if v else []
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if v is None:
        return "null"
    return "str"


def _collect(tmp_path) -> dict[str, object]:
    from test_site_schedule import 打包

    from d1max_site.incidents import IncidentDesk
    from d1max_site.scheduler import SiteScheduler
    s = 站(tmp_path)
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
        return {
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
