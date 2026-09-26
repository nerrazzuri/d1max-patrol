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


def test_主界面只许横屏():
    """用户 2026-09-26「手机app需要改成横屏模式」。清单里锁住,启动画面(Flutter 起来之前)就是横的;
    ``sensorLandscape``:两个横向都行,手机怎么拿都能转过来。Flutter 那边 ``lockLandscape`` 也锁。"""
    root = ET.parse(MAIN_MANIFEST).getroot()
    acts = root.findall("application/activity")
    main = [a for a in acts if a.get(f"{ANDROID}name") == ".MainActivity"]
    assert main, "找不到 MainActivity"
    assert main[0].get(f"{ANDROID}screenOrientation") == "sensorLandscape"
