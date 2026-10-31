"""W57:客户看到的只有一个版本号(``d1max_contract.product.PRODUCT_VERSION``)。各处写的版本要跟它一样,
改版本时这里列出还没跟着改的文件。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from d1max_contract.product import PRODUCT_NAME, PRODUCT_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _places() -> dict[str, str]:
    out = {}
    for p in [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]:
        m = re.search(r'^version = "([^"]+)"', p.read_text(encoding="utf-8"), re.M)
        out[str(p.relative_to(ROOT))] = m.group(1) if m else ""
    for rel in ("src/d1max_patrol/__init__.py", "src/d1max_sim/__init__.py"):
        m = re.search(r'^__version__ = "([^"]+)"', (ROOT / rel).read_text(encoding="utf-8"), re.M)
        out[rel] = m.group(1) if m else ""
    pub = re.search(r"^version: (\S+)", (ROOT / "mobile/pubspec.yaml").read_text(encoding="utf-8"),
                    re.M).group(1)
    out["mobile/pubspec.yaml"] = pub.split("+")[0]
    app = re.search(r"^const String appVersion = '([^']+)';",
                    (ROOT / "mobile/lib/net/site_client.dart").read_text(encoding="utf-8"), re.M)
    out["mobile/lib/net/site_client.dart(appVersion)"] = app.group(1) if app else ""
    web = ROOT / "web/console"
    out["web/console/package.json"] = json.loads((web / "package.json").read_text())["version"]
    lock = json.loads((web / "package-lock.json").read_text())
    out["web/console/package-lock.json"] = lock["version"]
    return out


def test_各处的版本都跟产品版本一样():
    wrong = {k: v for k, v in _places().items() if v != PRODUCT_VERSION}
    assert not wrong, f"产品版本是 {PRODUCT_VERSION},这些还没跟着改:{wrong}"


def test_版本写法_主次修_候选版加rc():
    assert re.fullmatch(r"\d+\.\d+\.\d+(-rc\.\d+)?", PRODUCT_VERSION)
    assert PRODUCT_NAME == "Nexus Warden"


def test_站点显示的就是产品版本():
    from d1max_site.health import site_version
    assert site_version() == PRODUCT_VERSION


def test_候选版的App安装包也能签_站点也收():
    from d1max_site.app_dist import build_of
    assert build_of(f"{PRODUCT_VERSION}+3") == 3
