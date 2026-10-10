"""视觉规范的数值(B0 专业版,design/tokens.json):对比度够 WCAG AA,报警色专用,优先级三重编码。"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
COLORS = {**TOKENS["color"], "osdPlateWorst": TOKENS["overlay"]["osdPlateWorst"]}
ALARM = {"p1", "p1Text", "p1Frame", "p1Bg", "p1Soft", "p2", "p2Bg"}


def _lum(hex_: str) -> float:
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_颜色都是六位十六进制():
    for k, v in COLORS.items():
        assert re.fullmatch(r"#[0-9A-F]{6}", v), k


def test_正文颜色对够AA_非文字的够3比1():
    bad = [(a, b, round(contrast(COLORS[a], COLORS[b]), 2)) for a, b in TOKENS["contrast"]["body"]
           if contrast(COLORS[a], COLORS[b]) < 4.5]
    bad += [(a, b, round(contrast(COLORS[a], COLORS[b]), 2))
            for a, b in TOKENS["contrast"]["nonText"] if contrast(COLORS[a], COLORS[b]) < 3.0]
    assert not bad, bad


def test_最淡的灰不能拿来写要读的字():
    assert contrast(COLORS["textDisabled"], COLORS["bg"]) < 4.5, "只给禁用、装饰用"
    assert "textDisabled" not in {a for a, _ in TOKENS["contrast"]["body"]}


def test_最淡可读字不配比raised更亮的底():
    raised = _lum(COLORS["raised"])
    for a, b in TOKENS["contrast"]["body"]:
        if a == "textMuted":
            assert _lum(COLORS[b]) <= raised, b


def test_Stop_all靠外框达标():
    """P1 实底对页面底只有 2.99:Stop all 必须带 p1Frame 外框(它对 bg 够 3:1)。"""
    assert contrast(COLORS["p1"], COLORS["bg"]) < 3.0
    assert contrast(COLORS["p1Frame"], COLORS["bg"]) >= 3.0
    assert TOKENS["size"]["stopAllRing"] >= 2


def test_报警色专用_正常状态不着色():
    """ISA-101:报警色不挪作他用。正常状态、模式都是中性色;只有异常状态用报警色。"""
    abnormal = {"deterring", "estop", "fell", "offline"}
    for state, ref in TOKENS["robotState"].items():
        if state.startswith("$"):
            continue
        assert ref in COLORS, state
        assert (ref in ALARM) == (state in abnormal), state
    for mode, ref in TOKENS["modeState"].items():
        assert ref not in ALARM, mode


def test_优先级三重编码():
    pr = {k: v for k, v in TOKENS["priority"].items() if not k.startswith("$")}
    assert set(pr) == {"P1", "P2", "P3"}
    for k, v in pr.items():
        assert v["shape"] and v["color"] in COLORS, k
    assert len({v["shape"] for v in pr.values()}) == 3, "三级形状各不相同"
    assert pr["P1"]["flashUnacked"] and not pr["P2"]["flashUnacked"]
    assert not pr["P1"]["shelvable"] and pr["P2"]["shelvable"]
