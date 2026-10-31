"""地图管理页的 HTTP 接口(管理员,``/api/admin/maps…``)。逻辑在 :mod:`d1max_site.maps_admin`,
这里只管路由、参数、审计、把 :class:`AdminError` 翻成 HTTP。由 ``api._Handler._maps_admin`` 调。

- ``GET  /api/admin/maps``                         总览:每张图每个版本 + 每只狗的换图状态
- ``POST /api/admin/maps/<图>/<版本>/note``        ``{note}`` 改备注(审计)
- ``POST /api/admin/maps/<图>/<版本>/publish``     ``{robots: [...], without_home?}`` 发布到几只狗,
  每只狗一个结果;结果的 ``state``:``pending`` 狗收下了(后台换,看总览里它的换图状态)/ ``refused``
- ``POST /api/admin/maps/rollback``                ``{robot_id}`` 把这只狗换回它上一张
- ``POST /api/admin/maps/<图>/<版本>/delete``      删旧版本;有狗在用、最新一版 → 409
- ``GET  /api/admin/maps/<图>/<版本>/diff?with=<另一版>``      对比的统计(JSON,带必读的 ``caveat``)
- ``GET  /api/admin/maps/<图>/<版本>/diff.png?with=<另一版>``  对比图
- ``GET  /api/admin/maps/<图>/<版本>/noise``       零星噪点的候选(W41;编号、位置、几格,带栅格摘要)
- ``GET  /api/admin/maps/<图>/<版本>/quality``     建图结论(W54:能用 / 有问题 + 原因 / 说不清)
- ``POST /api/admin/maps/<图>/<版本>/denoise``     ``{digest, skip: [编号]}`` 清掉、另存成新的一版
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from d1max_site.api import HttpError
from d1max_site.ca import SAFE_ID
from d1max_site.maps_admin import AdminError, MapAdmin
from d1max_site.permissions import MANAGE

_ROUTE = re.compile(r"^/api/admin/maps(?:/(rollback)|/([^/]{1,64})/([^/]{1,64})/"
                    r"(note|publish|delete|diff|diff\.png|noise|denoise|edit|display|basemap|quality))?$")
MAX_ROBOTS = 32


def _admin(h: Any) -> MapAdmin:
    adm = getattr(h.site, "_map_admin", None)
    if adm is None:
        adm = h.site._map_admin = MapAdmin(h.site)
    return adm


def handle(h: Any, method: str, path: str, user: Any) -> None:
    m = _ROUTE.match(path)
    if m is None:
        raise HttpError(404, f"没有 {method} {path}")
    h._need(user, MANAGE)
    adm = _admin(h)
    try:
        if m.group(1) == "rollback":
            if method != "POST":
                raise HttpError(404, f"没有 {method} {path}")
            return _rollback(h, adm, user)
        if m.group(2) is None:
            if method != "GET":
                raise HttpError(404, f"没有 {method} {path}")
            return h._send_json(200, adm.overview())
        map_id, version, what = unquote(m.group(2)), unquote(m.group(3)), m.group(4)
        if what in ("diff", "diff.png"):
            if method != "GET":
                raise HttpError(404, f"没有 {method} {path}")
            return _diff(h, adm, map_id, version, what)
        if what == "quality":                     # W54:建图结论(能用 / 有问题 / 说不清)
            if method != "GET":
                raise HttpError(404, f"没有 {method} {path}")
            from d1max_site.map_wizard import read_quality
            adm.ref(map_id, version)
            return h._send_json(200, read_quality(adm.cat, map_id, version))
        if what == "noise":
            if method != "GET":
                raise HttpError(404, f"没有 {method} {path}")
            return h._send_json(200, adm.noise(map_id, version))
        if what == "display":
            return _display(h, adm, user, method, map_id, version)
        if what == "basemap":
            return _basemap(h, adm, method, map_id, version)
        if method != "POST":
            raise HttpError(404, f"没有 {method} {path}")
        h._audit_target = f"{map_id}:{version}"
        d = h._body()
        if what == "note":
            note = adm.set_note(map_id, version, d.get("note"))
            h._audit_detail = {"note": note}
            return h._send_json(200, {"ok": True, "note": note})
        if what == "delete":
            out = adm.delete(map_id, version)
            h._audit_detail = out
            return h._send_json(200, out)
        if what == "edit":
            out = adm.edit(map_id, version, digest=d.get("digest"), ops=d.get("ops"),
                           note=d.get("note"), by=str(user))
            h._audit_detail = {k: out[k] for k in ("version", "erased_m2", "obstacle_m2")} | {
                "ops": len(d.get("ops") or [])}
            return h._send_json(200, out)
        if what == "denoise":
            out = adm.denoise(map_id, version, digest=d.get("digest"), skip=d.get("skip", []),
                              by=str(user))
            h._audit_detail = out
            return h._send_json(200, out)
        return _publish(h, adm, user, map_id, version, d)
    except AdminError as exc:
        raise HttpError(exc.status, exc.text, extra=exc.extra or None) from exc


def _robots(adm: MapAdmin, raw: Any) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ROBOTS \
            or not all(isinstance(x, str) and SAFE_ID.match(x) for x in raw):
        raise AdminError(400, f"robots 要是 1–{MAX_ROBOTS} 个机器人的号")
    out = list(dict.fromkeys(raw))                    # 去重、保序
    return out


def _publish(h: Any, adm: MapAdmin, user: Any, map_id: str, version: str, d: dict) -> None:
    if h._via_cookie:
        # W44 第四期(设计 §5.2 第 4 条):网页上换图要再输一次密码(手机这一期不改)
        h._confirm(user, d)
    ref = adm.ref(map_id, version)
    robots = _robots(adm, d.get("robots"))
    known = set(adm.robot_ids())
    jobs: list[tuple[str, dict[str, Any]]] = []
    results: dict[str, dict[str, Any]] = {}
    for rid in robots:
        if rid not in known:
            results[rid] = {"robot_id": rid, "ok": False, "state": "refused",
                            "reason": "机器人不存在"}
            continue
        try:
            jobs.append((rid, h._map_activate_payload(rid, ref, d.get("without_home", False))))
        except HttpError as exc:
            results[rid] = {"robot_id": rid, "ok": False, "state": "refused", "reason": str(exc),
                            "code": (exc.extra or {}).get("reason")}
    if jobs:
        for r in h.site.dispatch(lambda: adm.send_all(jobs, issued_by=str(user))):
            results[r["robot_id"]] = r
    out = [results[rid] for rid in robots]
    h._audit_detail = {"robots": robots, "results": {r["robot_id"]: r["state"] for r in out}}
    h._send_json(200, {"map_id": map_id, "version": version, "results": out})


def _display(h: Any, adm: MapAdmin, user: Any, method: str, map_id: str, version: str) -> None:
    """``GET`` 这一版的显示设置;``POST`` 整份存。特殊地面要放慢的那几块落成限速区(``t-`` 开头;
    走 ``NavZones.put``:收紧当场生效,删了是放宽、照样要确认),发给机器人。"""
    from d1max_site.map_display import TERRAIN_PREFIX, DisplayError, slow_zones
    from d1max_site.nav_zones import ZonesConflict, ZonesError
    adm.ref(map_id, version)
    disp = h.site.map_display
    if method == "GET":
        return h._send_json(200, disp.get(map_id, version))
    if method != "POST":
        raise HttpError(405, "只支持 GET/POST")
    d = h._body()
    h._audit_target = f"{map_id}:{version}"
    try:
        body = disp.put(map_id, version, d, by=str(user))
    except DisplayError as exc:
        raise HttpError(400, str(exc)) from exc
    zones = h.site.zones
    if zones is not None:
        cur = zones.current(map_id, version)
        keep = [z.to_wire() for z in cur.zones if not z.id.startswith(TERRAIN_PREFIX)]
        want = keep + slow_zones(body)
        if want != [z.to_wire() for z in cur.zones]:
            try:
                zs, tighten = zones.put(map_id, version, want, base_revision=cur.revision,
                                        by=str(user))
            except ZonesConflict as exc:
                raise HttpError(409, str(exc)) from exc
            except ZonesError as exc:
                raise HttpError(400, str(exc)) from exc
            h.site.dispatch(lambda: h.site.dispatcher.push_zones(
                zs, tighten=tighten, issued_by=str(user)))
    h._audit_detail = {"regions": len(body["regions"]), "terrain": len(body["terrain"]),
                       "rotate_deg": body["rotate_deg"]}
    return h._send_json(200, body)


#: 底图最大多少(W44 第四期)。
MAX_BASEMAP = 8 * 1024 * 1024
_IMG = {"image/png": "png", "image/jpeg": "jpg"}


def _basemap_dir(h: Any, map_id: str, version: str) -> Any:
    if h.site.home is None:
        raise HttpError(404, "此站点未接入站点目录")
    return h.site.home / "map-display" / map_id / version


def _basemap(h: Any, adm: MapAdmin, method: str, map_id: str, version: str) -> None:
    """底图(卫星截图、平面图):``POST`` 请求体就是 PNG / JPEG(≤ 8 MB),回 ``{"file"}``;``GET`` 取回。
    只是显示用,不进地图文件。"""
    adm.ref(map_id, version)
    d = _basemap_dir(h, map_id, version)
    if method == "GET":
        for ext, ctype in (("png", "image/png"), ("jpg", "image/jpeg")):
            if (d / f"basemap.{ext}").is_file():
                return h._send_bytes((d / f"basemap.{ext}").read_bytes(), ctype)
        raise HttpError(404, "此版本尚无底图")
    if method != "POST":
        raise HttpError(405, "只支持 GET/POST")
    ext = _IMG.get((h.headers.get("Content-Type") or "").split(";")[0].strip().lower())
    if ext is None:
        raise HttpError(400, "底图要是 PNG 或 JPEG")
    try:
        n = int(h.headers.get("Content-Length") or 0)
    except ValueError as exc:
        raise HttpError(400, "Content-Length 不是数") from exc
    if not 0 < n <= MAX_BASEMAP:
        raise HttpError(413, "底图最大 8 MB")
    data = h.rfile.read(n)
    magic = b"\x89PNG" if ext == "png" else b"\xff\xd8"
    if not data.startswith(magic):
        raise HttpError(400, "文件不是 PNG / JPEG")
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("basemap.*"):
        old.unlink()
    (d / f"basemap.{ext}").write_bytes(data)
    h._audit_target = f"{map_id}:{version}"
    return h._send_json(200, {"file": f"basemap.{ext}"})


def _rollback(h: Any, adm: MapAdmin, user: Any) -> None:
    d = h._body()
    rid = d.get("robot_id")
    if not isinstance(rid, str) or not SAFE_ID.match(rid) or rid not in set(adm.robot_ids()):
        raise AdminError(404, "机器人不存在")
    h._audit_target = rid
    target = adm.rollback_target(rid)
    if target is None:
        raise AdminError(409, f"{rid} 没有可以退回的上一版(机器人上未留、"
                              "命令账里也没有换过别的版本)",
                         reason="no_previous")
    ref = adm.ref(target["map_id"], target["version"])
    try:
        payload = h._map_activate_payload(rid, ref, d.get("without_home", False))
    except HttpError as exc:
        raise AdminError(exc.status, str(exc), **(exc.extra or {})) from exc
    (res,) = h.site.dispatch(lambda: adm.send_all([(rid, payload)], issued_by=str(user)))
    h._audit_detail = {"rollback_to": f"{ref.map_id}:{ref.version}", "local": target["local"],
                       "state": res["state"]}
    h._send_json(200, {"result": res, "target": target})


def _diff(h: Any, adm: MapAdmin, map_id: str, version: str, what: str) -> None:
    q = parse_qs(urlsplit(h.path).query)
    other = (q.get("with") or [""])[0]
    got = adm.diff(map_id, version, other)
    if what == "diff.png":
        return h._send_bytes(got.png, "image/png")
    h._send_json(200, got.stats | {"a": version, "b": other, "map_id": map_id})
