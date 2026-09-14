"""装出去的包里得有网页面板。

``deploy/install.sh`` 是 ``pip install`` 我们的包, 不是从源码目录直接跑。
``app/server.py`` 从**包里**的 ``static/`` 读页面 —— 源码树里有、wheel 里没有,
在开发机上一切正常, 装到狗上 ``/`` 就是 404。所以这里真的构建一次 wheel,
看里面有什么, 而不是去读 ``pyproject.toml`` 猜。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "src" / "d1max_patrol" / "app" / "static"


def _build_wheel(tmp_path: Path) -> list[str]:
    # 在副本里构建: 就地构建会往源码树里留 build/ 和 *.egg-info。
    src = tmp_path / "src-copy"
    src.mkdir()
    shutil.copy2(REPO / "pyproject.toml", src / "pyproject.toml")
    shutil.copytree(REPO / "src", src / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    out = tmp_path / "wheels"
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(src), "--no-deps",
         "--no-build-isolation", "-q", "-w", str(out)],
        check=True, timeout=300)
    (wheel,) = out.glob("d1max_patrol-*.whl")
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def test_wheel里有网页面板的每一个静态文件(tmp_path):
    names = set(_build_wheel(tmp_path))
    static_files = sorted(p.relative_to(STATIC).as_posix()
                          for p in STATIC.rglob("*") if p.is_file())
    # 这三样缺一样, 页面就打不开 —— 单独点名, 免得 static 目录被清空时
    # 下面那句"逐个核对"变成对空集合的恒真断言。
    for must in ("index.html", "app.js", "app.css"):
        assert must in static_files, must
    missing = [f for f in static_files
               if f"d1max_patrol/app/static/{f}" not in names]
    assert not missing, f"这些静态文件没进 wheel, 装到狗上会 404:{missing}"
