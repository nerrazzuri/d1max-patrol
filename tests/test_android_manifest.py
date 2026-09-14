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
