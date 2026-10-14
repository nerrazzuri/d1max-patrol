"""包里不是 .py 的文件都要在 pyproject 的 package-data 里声明:装成 wheel(狗上的装法)时不声明就不带,
用到它的地方只会悄悄退回(建图回环的管线配置就差点这样)。"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]


def test_包里的数据文件都声明了_装成_wheel_才带得上():
    # 狗上是 Python 3.10,没有 tomllib:按 pyproject 里那一节的写法直接读
    text = (PKG / "pyproject.toml").read_text()
    sec = re.search(r"^\[tool\.setuptools\.package-data\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    line = re.search(r"^d1max_localizer\s*=\s*\[(.*?)\]", sec.group(1), re.M) if sec else None
    globs = re.findall(r'"([^"]+)"', line.group(1)) if line else []
    root = PKG / "src" / "d1max_localizer"
    data = [p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts]
    assert data, "包里应该有数据文件(建图回环的管线配置)"
    missing = [f for f in data if not any(fnmatch.fnmatch(f, g) for g in globs)]
    assert not missing, f"没在 package-data 里声明:{missing}"
