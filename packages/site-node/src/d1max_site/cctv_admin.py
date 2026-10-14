"""网页上登记 / 改 / 删固定摄像头(运营可视化;原来只有命令行 ``camera-add``、``camera-rm``)。

校验在命令行那一套之上(名字只许字母数字 ``._-``、要防区)再加地址形状:ONVIF 地址要是 http(s)、
**不许把账号口令写进地址里**(账号、口令有自己的栏;地址会出现在列表里)、RTSP 地址要是 rtsp(s)。

**口令不出库**:列表不回口令(只回「有没有」)、地址里的账号口令抹掉;改的时候没给 ``password``
就保留原来的
(库里是加密的,原样留着,不解开再封);审计、日志只记名字、防区、「口令改了没」。

**防区检查**(不挡登记,只警告):这台摄像头报的入侵算哪个防区 —— 防区没映射到拦截点,入侵来了没有狗可派
(记「没映射」、照样报告警);防区没在任何一张图上画多边形,名单(时段 / 人员授权)判不了「人在不在
授权的那块地上」。
"""

from __future__ import annotations

import re
from typing import Any

from d1max_site.ca import SAFE_ID
from d1max_site.cctv import Camera, add_camera, remove_camera, scrub, update_camera_keep_password
from d1max_site.db import SiteDB

_CTRL = re.compile(r"[\x00-\x1f\x7f\s]")
_ZONE_BAD = re.compile(r"[\x00-\x1f\x7f]")


class CameraConflict(ValueError):
    """名字已经有了(登记时)或者没有这台(改的时候)。"""


def _text(d: dict[str, Any], key: str, *, maxlen: int, required: bool = False) -> str:
    v = d.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ValueError(f"{key} 要是文字")
    v = v.strip()
    if required and not v:
        raise ValueError(f"要 {key}")
    if len(v) > maxlen:
        raise ValueError(f"{key} 太长了(最多 {maxlen} 个字符)")
    return v


def _onvif_url(raw: str) -> str:
    if not raw:
        raise ValueError("要 onvif_url:摄像头或 NVR 的地址(http://ip[:端口])")
    if _CTRL.search(raw):
        raise ValueError("onvif_url 里不能有空格、换行")
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        if scheme.lower() not in ("http", "https"):
            raise ValueError("onvif_url 要是 http 或 https 地址")
    else:
        rest = raw
    authority = rest.split("/", 1)[0]
    if "@" in authority:
        raise ValueError("地址里不要带账号口令:账号、口令在各自的栏里填")
    if not authority or authority.startswith(":"):
        raise ValueError("onvif_url 里没有主机地址")
    return raw


def _rtsp_url(raw: str) -> str:
    if not raw:
        return ""
    if _CTRL.search(raw):
        raise ValueError("rtsp_url 里不能有空格、换行")
    if not re.match(r"^rtsps?://[^/@\s]*@?[^/\s]+", raw, re.I):
        raise ValueError("rtsp_url 要是 rtsp:// 或 rtsps:// 地址")
    return raw


def check_camera(d: Any) -> dict[str, Any]:
    """校验网页交来的一台摄像头;返回清好的字段(不含 password,它另外处理)。不对抛 ValueError。"""
    if not isinstance(d, dict):
        raise ValueError("要一个对象")
    name = _text(d, "name", maxlen=64, required=True)
    if not SAFE_ID.match(name):
        raise ValueError(f"摄像头名字只许字母、数字、. _ -:{name!r}")
    zone = _text(d, "zone", maxlen=128, required=True)
    if _ZONE_BAD.search(zone):
        raise ValueError("防区名里不能有控制字符")
    motion = d.get("motion", False)
    if not isinstance(motion, bool):
        raise ValueError("motion 要是 true / false")
    return {"name": name, "zone": zone, "motion": motion,
            "onvif_url": _onvif_url(_text(d, "onvif_url", maxlen=300)),
            "username": _text(d, "username", maxlen=64),
            "rtsp_url": _rtsp_url(_text(d, "rtsp_url", maxlen=500))}


def zone_warnings(db: SiteDB, zone: str) -> list[dict[str, str]]:
    out = []
    if not db.query("SELECT 1 FROM zones WHERE zone=?", (zone,)):
        out.append({"code": "zone_unmapped",
                    "text": f"防区「{zone}」还没映射到拦截点:这台摄像头报入侵时没有狗可派"})
    if not db.query("SELECT 1 FROM zone_areas WHERE zone=? LIMIT 1", (zone,)):
        out.append({"code": "zone_no_polygon",
                    "text": f"防区「{zone}」没在任何一张图上画多边形:名单(时段 / 人员授权)判不了"})
    return out


def _strip_userinfo(url: str) -> str:
    return scrub(url) if "@" in url else url


def _row_view(db: SiteDB, r: Any) -> dict[str, Any]:
    warns = zone_warnings(db, r["zone"])
    return {"name": r["name"], "zone": r["zone"], "onvif_url": _strip_userinfo(r["onvif_url"]),
            "username": r["username"], "has_password": bool(r["password"]),
            "rtsp_set": bool(r["rtsp_url"]), "rtsp_url": _strip_userinfo(r["rtsp_url"]),
            "motion": bool(r["motion"]), "added_ms": r["added_ms"],
            "zone_mapped": not any(w["code"] == "zone_unmapped" for w in warns),
            "zone_polygon": not any(w["code"] == "zone_no_polygon" for w in warns),
            "warnings": warns}


def list_cameras(db: SiteDB) -> list[dict[str, Any]]:
    return [_row_view(db, r) for r in db.query("SELECT * FROM cameras ORDER BY name")]


def save(db: SiteDB, d: Any, *, now_ms: int) -> dict[str, Any]:
    """登记(``edit`` 假)或改(``edit`` 真)。返回 ``{camera, warnings, password_changed}``。"""
    f = check_camera(d)
    edit = d.get("edit", False)
    if not isinstance(edit, bool):
        raise ValueError("edit 要是 true / false")
    pw = d.get("password")
    if pw is not None and (not isinstance(pw, str) or len(pw) > 128):
        raise ValueError("password 要是文字(最多 128 个字符)")
    old = db.query("SELECT * FROM cameras WHERE name=?", (f["name"],))
    if edit and not old:
        raise CameraConflict(f"没有摄像头 {f['name']},不能改(要新登记就别带 edit)")
    if not edit and old:
        raise CameraConflict(f"摄像头 {f['name']} 已经有了:要改就带 edit")
    if edit and pw is None:
        # 保留原口令(库里是加密的,原样留着):只改别的列
        update_camera_keep_password(db, Camera(
            name=f["name"], onvif_url=f["onvif_url"], username=f["username"], password="",
            zone=f["zone"], rtsp_url=f["rtsp_url"], motion=f["motion"]))
        changed = False
    else:
        add_camera(db, Camera(name=f["name"], onvif_url=f["onvif_url"], username=f["username"],
                              password=pw or "", zone=f["zone"], rtsp_url=f["rtsp_url"],
                              motion=f["motion"]), now_ms=now_ms)
        changed = True
    row = db.query("SELECT * FROM cameras WHERE name=?", (f["name"],))[0]
    view = _row_view(db, row)
    return {"camera": view, "warnings": view["warnings"], "password_changed": changed}


def remove(db: SiteDB, name: str) -> bool:
    return remove_camera(db, name)
