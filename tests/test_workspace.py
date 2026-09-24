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
    importlib.import_module("d1max_site")


def test_包名与依赖方向():
    c, s, a = _toml("contract"), _toml("adapter-sim"), _toml("robot-agent")
    assert 'name = "d1max-contract"' in c
    assert 'name = "d1max-adapter-sim"' in s
    assert 'name = "d1max-robot-agent"' in a
    assert "d1max-contract" in s and "d1max-contract" in a
    assert "d1max-patrol" not in c, "契约包不许依赖根包 —— 它是唯一被两侧同时依赖的东西"
    assert "paho" not in a and "paho" not in s
    site = _toml("site-node")
    deps = re.search(r"(?m)^dependencies\s*=\s*\[(.*)\]", site).group(1)
    assert 'name = "d1max-site-node"' in site and deps.strip() == '"d1max-contract[mqtt]"', \
        "站点只依赖契约包:不依赖根包(老 HTTP 面要在 W00c4 退役),也不依赖代理与适配器"
    assert re.search(r"(?ms)\[project\.optional-dependencies\].*mqtt\s*=\s*\[.*paho-mqtt", c), \
        "paho 只作为契约包的 mqtt 可选依赖"


def test_根pyproject把三处测试目录都收进来():
    root = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for p in ("packages/contract/tests", "packages/adapter-sim/tests",
              "packages/robot-agent/tests", "packages/site-node/tests"):
        assert f'"{p}"' in root, p


def test_每个包有src布局和tests目录():
    for name, mod in (("contract", "d1max_contract"), ("adapter-sim", "d1max_adapter_sim"),
                      ("robot-agent", "d1max_agent"), ("site-node", "d1max_site")):
        assert (PKGS / name / "src" / mod / "__init__.py").is_file(), name
        assert (PKGS / name / "tests").is_dir(), name


def test_包内tests目录不是包而且文件名不与根tests撞():
    """根 tests/ 是一个叫 ``tests`` 的包;packages/*/tests 若也放 ``__init__.py``,pytest 的
    prepend 导入模式下两个 ``tests`` 包会互相覆盖(collection 时 ModuleNotFoundError)。
    所以包内 tests 不带 ``__init__.py``,代价是测试文件基名必须全仓唯一。"""
    根 = {p.name for p in (ROOT / "tests").rglob("test_*.py")}
    见过: dict[str, Path] = {}
    for pkg in PKGS.iterdir():
        t = pkg / "tests"
        assert not (t / "__init__.py").exists(), f"{t} 不许是包"
        for f in t.glob("test_*.py"):
            assert f.name not in 根, f"{f} 与根 tests 里同名文件撞了"
            assert f.name not in 见过, f"{f} 与 {见过[f.name]} 同名(包与包之间也会撞)"
            见过[f.name] = f


_ENGINE_MODULES = (
    "alerts", "archive", "backup", "baselines", "bundle", "datadir", "export", "form",
    "homing", "http_sink", "lease", "machine", "mission", "preflight", "privileged",
    "release", "removable", "retention", "safety", "schedule", "selfcheck", "storage",
    "uploader", "upload_queue",
)


def test_engine别名壳_两个名字是同一个模块对象():
    """W00b 决定 1:engine/ 真身在 robot-agent,根包留壳;monkeypatch、私有名、isinstance 都
    只有在「同一个对象」时才成立,所以断言 is。"""
    for m in _ENGINE_MODULES:
        a = importlib.import_module(f"d1max_patrol.engine.{m}")
        b = importlib.import_module(f"d1max_agent.engine.{m}")
        assert a is b, m
    import d1max_patrol.engine as shell
    assert sorted(p.name for p in Path(shell.__file__).parent.glob("*.py")) == ["__init__.py"], \
        "壳里只许有 __init__.py,引擎代码不许再长回根包"


def test_别名壳的名单等于真身目录_新模块不会漏挂():
    """壳里的 ``_MODULES`` 是手写的;真身目录里新添一个模块而名单没跟上,
    ``from d1max_patrol.engine import 新模块`` 就会拿到 ImportError(或者更糟:拿到另一份对象)。"""
    import d1max_patrol.engine as shell
    real = PKGS / "robot-agent" / "src" / "d1max_agent" / "engine"
    stems = sorted(p.stem for p in real.glob("*.py") if p.stem != "__init__")
    assert sorted(shell._MODULES) == stems


def test_搬走的engine里没有旧包名():
    src = PKGS / "robot-agent" / "src" / "d1max_agent" / "engine"
    for py in src.glob("*.py"):
        代码 = "\n".join(行 for 行 in py.read_text(encoding="utf-8").splitlines()
                        if 行.lstrip().startswith(("from ", "import ")))
        assert "d1max_patrol.engine" not in 代码, f"{py.name} 里还 import 旧包名"
    assert 'd1max-patrol' in _toml("robot-agent"), "过渡性依赖要声明"
