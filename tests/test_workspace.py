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


def test_别名壳已删_全仓不再有旧名字():
    """W00c4 设计决定三 A:根包里那层引擎别名壳删了,引擎只有一个名字
    ``d1max_agent.engine``。旧名字再出现就是有人照着老代码抄。"""
    import pytest
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("d1max_patrol" + ".engine")
    assert not (ROOT / "src" / "d1max_patrol" / "engine").exists()
    旧 = "d1max_patrol" + ".engine"
    for base in ("src", "tests", "packages", "deploy"):
        for f in (ROOT / base).rglob("*"):
            if f.suffix in (".py", ".sh", ".service", ".toml") and f.is_file():
                assert 旧 not in f.read_text(encoding="utf-8", errors="replace"), f


def test_搬走的engine里没有旧包名():
    src = PKGS / "robot-agent" / "src" / "d1max_agent" / "engine"
    for py in src.glob("*.py"):
        代码 = "\n".join(行 for 行 in py.read_text(encoding="utf-8").splitlines()
                        if 行.lstrip().startswith(("from ", "import ")))
        assert "d1max_patrol" + ".engine" not in 代码, f"{py.name} 里还 import 旧包名"
    assert 'd1max-patrol' in _toml("robot-agent"), "过渡性依赖要声明"


def test_几何类型在契约包里_根包转手的是同一个类():
    """W00c2a:任务点位的 Pose 进契约(站点要解析任务)。根包、引擎、导航后端用的必须是
    同一个类 —— 两种 Pose 混用,isinstance 与相等比较都会悄悄出错。"""
    from d1max_contract import geometry
    from d1max_patrol.protocol import nav_types
    for name in ("Position", "Orientation", "Pose", "yaw_to_orientation", "orientation_to_yaw"):
        assert getattr(nav_types, name) is getattr(geometry, name), name


def test_任务与排程模块在契约包里_旧名字是同一个模块对象():
    import d1max_contract.mission as cm
    import d1max_contract.schedule as cs
    for old in ("d1max_agent.engine.mission", "d1max_agent.engine.mission"):
        assert importlib.import_module(old) is cm, old
    for old in ("d1max_agent.engine.schedule", "d1max_agent.engine.schedule"):
        assert importlib.import_module(old) is cs, old
    assert "d1max_patrol" not in (PKGS / "contract" / "src" / "d1max_contract" / "mission.py"
                                  ).read_text(encoding="utf-8")


def test_任务包格式与指纹在契约包里_狗上用的是同一份():
    from d1max_agent.engine import bundle, export, release
    from d1max_contract import bundle_format, digest
    for name in ("verify_bundle", "parse_manifest", "read_manifest", "verify_pure_data",
                 "bundle_sha256", "read_bundle_schedule", "BundleError", "BundleManifest"):
        assert getattr(bundle, name) is getattr(bundle_format, name), name
    assert export.sha256_file is digest.sha256_file
    assert release._tree_sha256 is digest.tree_sha256, "发布包与任务包只有一个指纹算法"


def test_契约包不反向依赖根包与代理():
    src = PKGS / "contract" / "src" / "d1max_contract"
    for py in src.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for bad in ("import d1max_patrol", "from d1max_patrol", "import d1max_agent",
                    "from d1max_agent", "import d1max_site", "from d1max_site"):
            assert bad not in text, f"{py.name}: {bad}"
