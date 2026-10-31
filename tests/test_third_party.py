"""W58:第三方软件声明(tools/third_party.py)。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("third_party", ROOT / "tools" / "third_party.py")
tp = importlib.util.module_from_spec(spec)
sys.modules["third_party"] = tp
spec.loader.exec_module(tp)

BUILT = ROOT / "packages/site-node/src/d1max_site/web/assets/third-party-notices.txt"


def test_运行时依赖没有GPL和AGPL():
    for d in tp.python_closure():
        assert not tp.COPYLEFT.search(tp.python_license(d)), d.metadata["Name"]
    for name, _v, lic, _t in tp.npm_packages():
        assert not tp.COPYLEFT.search(lic), name


def test_提交的声明列全了每个运行时依赖_构建产物里的是同一份():
    text = tp.OUT.read_text(encoding="utf-8")
    names = [d.metadata["Name"] for d in tp.python_closure()]
    assert {"PyYAML", "paho-mqtt", "numpy", "websockets", "tzdata"} <= set(names)
    for n in names:
        assert f"  - {n} " in text, f"{n} 不在声明里:重跑 tools/third_party.py"
    for name, _v, _l, _t in tp.npm_packages():
        assert f"  - {name} " in text, f"{name} 不在声明里:重跑 tools/third_party.py"
    assert BUILT.read_text(encoding="utf-8") == text, "构建产物里的声明旧了:npm run build"


def test_声明里写明不随包的程序和模型许可():
    text = tp.OUT.read_text(encoding="utf-8")
    assert "MOLA" in text and "GPL-3.0" in text and "AGPL-3.0" in text
