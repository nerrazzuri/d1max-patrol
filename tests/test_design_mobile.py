"""手机 App 的设计常量(App V2)是从 design/ 生成的:仓库里的跟重新生成的一样(改了 JSON 忘了跑生成脚本的会红)。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_mobile_design", ROOT / "tools" / "gen_mobile_design.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_仓库里的Dart常量跟重新生成的一样():
    for path, text in _gen().generate().items():
        assert path.read_text(encoding="utf-8") == text, f"{path.name} 过期了:跑 python3 tools/gen_mobile_design.py"


def test_颜色写错了当场报_不生成半截():
    import pytest

    with pytest.raises(ValueError):
        _gen().tokens_dart({"version": 2, "color": {"bg": "rgb(0,0,0)"}, "size": {}, "radius": {}, "type": {},
                            "motion": {"fast": 1, "p1FlashMs": 1, "p1FlashMinOpacity": 0.4}})


def test_告警标题里的引号和美元符转义():
    out = _gen().alerts_dart({"$doc": "x", "k": {"en": "it's $5", "zh": "五"}})
    assert r"'it\'s \$5'" in out and "$doc" not in out
