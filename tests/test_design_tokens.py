"""视觉规范的颜色(B0,design/tokens.json):界面上真的会叠在一起的颜色对,对比度够 WCAG AA。"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))


def _lum(hex_: str) -> float:
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_颜色都是六位十六进制():
    for k, v in TOKENS["color"].items():
        assert re.fullmatch(r"#[0-9A-F]{6}", v), k


def test_正文颜色对够AA_非文字的够3比1():
    col = TOKENS["color"]
    bad = [(a, b, round(contrast(col[a], col[b]), 2)) for a, b in TOKENS["contrast"]["body"]
           if contrast(col[a], col[b]) < 4.5]
    bad += [(a, b, round(contrast(col[a], col[b]), 2)) for a, b in TOKENS["contrast"]["nonText"]
            if contrast(col[a], col[b]) < 3.0]
    assert not bad, bad


def test_最淡的灰不能拿来写要读的字():
    col = TOKENS["color"]
    assert contrast(col["textDisabled"], col["bg"]) < 4.5, "只给禁用、装饰用"
    used = {a for a, _ in TOKENS["contrast"]["body"]}
    assert "textDisabled" not in used


def test_狗的状态都指向有的颜色():
    for state, ref in TOKENS["robotState"].items():
        if not state.startswith("$"):
            assert ref in TOKENS["color"], state
