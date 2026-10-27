"""运营可视化的接口(跟 ``api.py`` 分开,只在那边挂一个入口):

- ``GET  /api/runviz[?robot=]``:每台狗的规划路径、最近轨迹、被挡 / 重规划状态(``runviz.RunViz``)。
- ``GET  /api/loc-episodes?robot=&kind=&since_ms=&until_ms=&map_id=&map_version=&limit=``:定位历史。
- ``GET  /api/admin/cameras``:摄像头配置(管理员;**不回口令**,地址里的账号口令也抹掉)
  和每台的防区检查。
- ``POST /api/admin/cameras``:登记或改(``edit: true`` 是改:没给 ``password`` 就保留原来的)。
- ``POST /api/admin/cameras/<名字>/remove``:删。

摄像头的校验用 ``cctv_admin.check_camera``(命令行的规矩 —— 名字、防区 —— 加上地址形状的检查)。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs

from d1max_site.permissions import MANAGE, VIEW

_CAM_ADMIN = re.compile(r"^/api/admin/cameras(?:/([A-Za-z0-9._-]{1,64})/remove)?$")


def handles(path: str) -> bool:
    return path in ("/api/runviz", "/api/loc-episodes") or _CAM_ADMIN.match(path) is not None


def route(h: Any, method: str, path: str, user: Any) -> None:
    """``h`` 是 ``api.py`` 的请求处理器(用它的 ``_need``、``_send_json``、``_body``、``site``)。"""
    from d1max_site.api import HttpError
    q = parse_qs(h.path.split("?", 1)[1] if "?" in h.path else "")

    def one(k: str) -> str | None:
        v = (q.get(k) or [None])[0]
        return v if v else None

    def num(k: str) -> int | None:
        v = one(k)
        if v is None:
            return None
        try:
            return int(v)
        except ValueError as exc:
            raise HttpError(400, f"{k} 要是整数") from exc

    site = h.site
    if path == "/api/runviz":
        if method != "GET":
            raise HttpError(405, "只支持 GET")
        h._need(user, VIEW)
        rv = getattr(site, "runviz", None)
        return h._send_json(200, rv.view(one("robot")) if rv is not None else {"robots": {}})
    if path == "/api/loc-episodes":
        if method != "GET":
            raise HttpError(405, "只支持 GET")
        h._need(user, VIEW)
        hist = getattr(site, "loc_history", None)
        if hist is None:
            return h._send_json(200, {"episodes": []})
        kind = one("kind")
        if kind is not None and kind not in ("lost", "degraded"):
            raise HttpError(400, "kind 要是 lost 或 degraded")
        lim = num("limit")
        return h._send_json(200, {"episodes": hist.list(
            robot=one("robot"), kind=kind, since_ms=num("since_ms"), until_ms=num("until_ms"),
            map_id=one("map_id"), map_version=one("map_version"),
            limit=200 if lim is None else lim)})
    # 摄像头登记(管理员)
    from d1max_site import cctv_admin
    h._need(user, MANAGE)
    db = site.dispatcher.db
    m = _CAM_ADMIN.match(path)
    assert m is not None
    if m.group(1) is None and method == "GET":
        return h._send_json(200, {"cameras": cctv_admin.list_cameras(db)})
    if method != "POST":
        raise HttpError(405, "只支持 GET/POST")
    if m.group(1) is not None:
        h._audit_target = f"camera/{m.group(1)}"
        if not cctv_admin.remove(db, m.group(1)):
            raise HttpError(404, f"没有摄像头 {m.group(1)}")
        h._body()
        h._hist("cameras", "", "", "", user)                     # W44 第三期:历史版本
        _sync_now(site)
        return h._send_json(200, {"ok": True})
    d = h._body()
    name = str(d.get("name", ""))[:64]
    h._audit_target = f"camera/{name}"
    try:
        got = cctv_admin.save(db, d, now_ms=site._now())
    except cctv_admin.CameraConflict as exc:
        raise HttpError(409, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc
    # 审计只记改了什么(不记口令、不记带账号的地址)
    h._audit_detail = {"zone": got["camera"]["zone"], "motion": got["camera"]["motion"],
                       "edit": bool(d.get("edit")), "password_changed": got["password_changed"],
                       "warnings": [w["code"] for w in got["warnings"]]}
    h._hist("cameras", "", "", "", user)                         # W44 第三期:历史版本
    _sync_now(site)
    return h._send_json(200, {"ok": True, "camera": got["camera"], "warnings": got["warnings"]})


def _sync_now(site) -> None:
    """网页加、改、删了摄像头:当场同步订阅(演示站点实测:原来等下一拍,最多 30 秒,删了的摄像头这几秒里
    报的入侵照样报警)。在后台线程里做:停旧订阅要等它退干净,不挡这个请求。"""
    mgr = getattr(site, "cctv", None)
    if mgr is None:
        return
    import threading
    threading.Thread(target=mgr.sync, daemon=True, name="cctv-sync-now").start()
