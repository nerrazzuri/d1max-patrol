"""W00 决定 1:三个新包按总设计 §5 摆在 packages/ 下,各有自己的 pyproject。"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKGS = ROOT / "packages"


def _toml(name: str) -> str:
    return (PKGS / name / "pyproject.toml").read_text(encoding="utf-8")


def test_三个包都能import():
    assert importlib.import_module("d1max_contract").SCHEMA == "1.0"
    importlib.import_module("d1max_adapter_sim")
    importlib.import_module("d1max_agent")


def test_包名与依赖方向():
    c, s, a = _toml("contract"), _toml("adapter-sim"), _toml("robot-agent")
    assert 'name = "d1max-contract"' in c
    assert 'name = "d1max-adapter-sim"' in s
    assert 'name = "d1max-robot-agent"' in a
    assert "d1max-contract" in s and "d1max-contract" in a
    assert "d1max-patrol" not in c, "契约包不许依赖根包 —— 它是唯一被两侧同时依赖的东西"
    assert "paho" not in a and "paho" not in s
    assert re.search(r"(?ms)\[project\.optional-dependencies\].*mqtt\s*=\s*\[.*paho-mqtt", c), \
        "paho 只作为契约包的 mqtt 可选依赖"


def test_根pyproject把三处测试目录都收进来():
    root = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for p in ("packages/contract/tests", "packages/adapter-sim/tests",
              "packages/robot-agent/tests"):
        assert f'"{p}"' in root, p


def test_每个包有src布局和tests目录():
    for name, mod in (("contract", "d1max_contract"), ("adapter-sim", "d1max_adapter_sim"),
                      ("robot-agent", "d1max_agent")):
        assert (PKGS / name / "src" / mod / "__init__.py").is_file(), name
        assert (PKGS / name / "tests").is_dir(), name


def test_包内tests目录不是包而且文件名不与根tests撞():
    """根 tests/ 是一个叫 ``tests`` 的包;packages/*/tests 若也放 ``__init__.py``,pytest 的
    prepend 导入模式下两个 ``tests`` 包会互相覆盖(collection 时 ModuleNotFoundError)。
    所以包内 tests 不带 ``__init__.py``,代价是测试文件基名必须全仓唯一。"""
    根 = {p.name for p in (ROOT / "tests").rglob("test_*.py")}
    for pkg in PKGS.iterdir():
        t = pkg / "tests"
        assert not (t / "__init__.py").exists(), f"{t} 不许是包"
        for f in t.glob("test_*.py"):
            assert f.name not in 根, f"{f} 与根 tests 里同名文件撞了"
