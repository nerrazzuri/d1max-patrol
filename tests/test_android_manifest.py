"""release 包必须有网络权限。

Flutter 模板把 ``INTERNET`` 只放在 ``src/debug`` 和 ``src/profile`` 的清单里,
``src/main`` 里没有。调试包合并了 debug 清单, 连得上狗; release 包只合并
main, 装到手机上连不上 —— 而且开发时永远发现不了, 因为开发跑的都是调试包。
所以这里只看 **main** 那一份。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAIN_MANIFEST = REPO / "mobile" / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
ANDROID = "{http://schemas.android.com/apk/res/android}"


def _permissions(path: Path) -> set[str]:
    root = ET.parse(path).getroot()
    # 只认 <manifest> 的直接子节点: 写进 <application> 里的 uses-permission
    # 不生效, 不能算数。
    return {el.get(f"{ANDROID}name", "") for el in root.findall("uses-permission")}


def test_release包的清单里有网络权限():
    assert "android.permission.INTERNET" in _permissions(MAIN_MANIFEST)


def test_主界面跟着手机的旋转设置_不锁横屏():
    """App V2(决策 44):普通页面竖屏横屏都行,遥控页自己锁横屏(Flutter 那边进出时锁放)。清单里是
    ``fullUser``:四个方向都行,但听用户的自动旋转开关 —— 关了自动旋转就是竖屏,不会自己转。
    (2026-09-26 到 App V2 之前是 ``sensorLandscape``,整个 app 只许横屏。)"""
    root = ET.parse(MAIN_MANIFEST).getroot()
    acts = root.findall("application/activity")
    main = [a for a in acts if a.get(f"{ANDROID}name") == ".MainActivity"]
    assert main, "找不到 MainActivity"
    assert main[0].get(f"{ANDROID}screenOrientation") == "fullUser"


def test_应用名走资源_英文系统英文_中文系统中文():
    """App V2:英文为主、中文可选。名字不写死在清单里。"""
    root = ET.parse(MAIN_MANIFEST).getroot()
    assert root.find("application").get(f"{ANDROID}label") == "@string/app_name"
    res = MAIN_MANIFEST.parent / "res"
    names = {d: ET.parse(res / d / "strings.xml").getroot().find("string[@name='app_name']").text
             for d in ("values", "values-zh")}
    assert names == {"values": "D1 Max Patrol", "values-zh": "D1 Max 巡检"}


def test_W17_后台值守的前台服务与权限():
    """后台值守(W17,决策 30):前台服务(Android 14 要声明类型)、弹通知(13+ 要权限)、CPU 别睡死。
    少一条:要么一开就崩(没声明类型、没前台服务权限),要么一声不响(没通知权限)。"""
    perms = _permissions(MAIN_MANIFEST)
    for p in ("FOREGROUND_SERVICE", "FOREGROUND_SERVICE_SPECIAL_USE", "POST_NOTIFICATIONS",
              "WAKE_LOCK"):
        assert f"android.permission.{p}" in perms, p
    root = ET.parse(MAIN_MANIFEST).getroot()
    svc = [s for s in root.findall("application/service")
           if s.get(f"{ANDROID}name") == ".WatchService"]
    assert svc, "找不到 WatchService"
    assert svc[0].get(f"{ANDROID}foregroundServiceType") == "specialUse"
    assert svc[0].get(f"{ANDROID}exported") == "false", "别的 app 不许启停值守"
    props = {p.get(f"{ANDROID}name") for p in svc[0].findall("property")}
    assert "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" in props
