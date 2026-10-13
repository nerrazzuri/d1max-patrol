"""手机 App 的设计常量是从 design/ 生成的:仓库里的跟重新生成的一样
(改了 JSON 忘了跑生成脚本的会红)。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_mobile_design",
                                                  ROOT / "tools" / "gen_mobile_design.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_仓库里的Dart常量跟重新生成的一样():
    for path, text in _gen().generate().items():
        assert path.read_text(encoding="utf-8") == text, \
            f"{path.name} 过期了:跑 python3 tools/gen_mobile_design.py"


def test_新设计系统的令牌也在生成的里面_跟检入的一字节不差():
    out = {path.name: (path, text) for path, text in _gen().generate().items()}
    path, text = out["nw_tokens.g.dart"]
    assert path.read_bytes() == text.encode("utf-8"), "跑 python3 tools/gen_mobile_design.py"
    assert "abstract final class NwLight" in text and "abstract final class NwDark" in text


def test_新令牌写错了当场报_不生成半截():
    import json

    import pytest

    good = json.loads((ROOT / "design" / "mobile.json").read_text(encoding="utf-8"))
    gen = _gen()
    assert "static const Color scrim = Color(0xB3000000);" in gen.nw_tokens_dart(good)

    def broken(change):
        bad = json.loads(json.dumps(good))
        change(bad)
        return bad

    for change in (lambda d: d["color"]["dark"].pop("info"),              # 两套键不一样
                   lambda d: d["color"]["light"].update(bg="#FFF"),       # 不是六位
                   lambda d: d["color"]["light"].update(scrim="#000000"),  # 遮罩没带透明度
                   lambda d: d["type"]["labelSmall"].update(size=10),     # 小于 12
                   lambda d: d["type"]["bodySmall"].update(family="serif")):
        with pytest.raises(ValueError):
            gen.nw_tokens_dart(broken(change))


def test_网页值班台那份令牌_手机只生成动效_缺了当场报():
    """2026-10-14 重做之后手机不再用 ``tokens.json`` 的颜色、尺寸、圆角、字(颜色写错的检查在
    新令牌那条里);只剩告警页闪法的三项。"""
    import json

    import pytest

    gen = _gen()
    tokens = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
    out = gen.tokens_dart(tokens)
    motion = tokens["motion"]
    assert f"static const int fast = {int(motion['fast'])};" in out
    assert f"static const int p1FlashMs = {int(motion['p1FlashMs'])};" in out
    assert f"static const double p1FlashMinOpacity = {float(motion['p1FlashMinOpacity'])};" in out
    for gone in ("D1Color", "D1Size", "D1Radius", "D1Type", "Color("):
        assert gone not in out, gone
    for key in ("fast", "p1FlashMs", "p1FlashMinOpacity"):
        bad = json.loads(json.dumps(tokens))
        del bad["motion"][key]
        with pytest.raises(KeyError):
            gen.tokens_dart(bad)


def test_告警标题里的引号和美元符转义():
    out = _gen().alerts_dart({"$doc": "x", "k": {"en": "it's $5", "zh": "五"}})
    assert r"'it\'s \$5'" in out and "$doc" not in out
