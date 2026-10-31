"""手机 App 新设计系统的令牌(design/mobile.json,浅色、深色两套)的规矩:两套键一样、对比度够
WCAG AA、红只给 P1 和急停类、橙只给 P2 和低电、字号不小于 12。

设计稿(FlutterFlow 导出)原来的几个颜色过不了这里,是在 JSON 里调了深浅,不是放松这里的线。
要成对检查的颜色写在这个文件里,不写在 JSON 里:改 JSON 的人改不掉规矩。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = json.loads((ROOT / "design" / "mobile.json").read_text(encoding="utf-8"))
THEMES = {name: TOKENS["color"][name] for name in ("light", "dark")}
TONES = ("success", "warning", "error", "info")

# 红、橙各自许用在哪些用途上(用户 2026-10-14 定的:红 = P1 和急停类,橙 = P2 和低电)
RED_ONLY = {"p1", "estop", "offline", "destructive"}
ORANGE_ONLY = {"p2", "lowBattery"}

# 当正文读的字:前景压背景 ≥ 4.5
BODY = [
    ("text", "bg"), ("text", "surface"), ("text", "surfaceVariant"),
    ("textSecondary", "bg"), ("textSecondary", "surface"), ("textSecondary", "surfaceVariant"),
    ("textMuted", "bg"), ("textMuted", "surface"),
    ("tertiary", "bg"), ("tertiary", "surface"),
    ("onPrimary", "primary"), ("onPrimary", "secondary"),
    # P1 卡片:软红底上写正文色的字(设计稿是白字压 5% 红底,看不见)
    ("text", "errorSoft"), ("text", "warningSoft"), ("text", "successSoft"), ("text", "infoSoft"),
    ("textSecondary", "errorSoft"),
]
for _t in TONES:
    # 状态色当文字:压卡片底、页面底、自己的软底(药丸、图标格 = 软底 + 同色字)
    BODY += [(_t, "surface"), (_t, "bg"), (_t, f"{_t}Soft")]
    # 实底上的字
    BODY.append((f"on{_t.capitalize()}", _t))

# 不是字(框、图标、色块):≥ 3
NON_TEXT = [("errorFrame", "bg"), ("errorFrame", "surface"), ("errorFrame", "errorSoft"),
            ("primary", "bg"), ("primary", "surface")]

# 分隔线、卡片的边:看得出来就行,≥ 1.5
BORDER = [("outline", "bg")]


def _rgb(hex_: str) -> list[int]:
    return [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]


def _lum(hex_: str) -> float:
    c = [x / 255 for x in _rgb(hex_)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _over(fg: str, bg: str, alpha: float) -> str:
    return "#" + "".join(f"{round(f * alpha + b * (1 - alpha)):02X}"
                         for f, b in zip(_rgb(fg), _rgb(bg), strict=True))


def _bad(pairs, floor: float) -> list:
    out = []
    for name, c in THEMES.items():
        for fg, bg in pairs:
            r = contrast(c[fg], c[bg])
            if r < floor:
                out.append((name, fg, bg, round(r, 2)))
    return out


def test_浅色深色两套的键一样():
    assert list(THEMES["light"]) == list(THEMES["dark"])


def test_颜色都是六位十六进制_压在视频上的那层带透明度():
    for name, c in THEMES.items():
        for k, v in c.items():
            want = r"#[0-9A-F]{8}" if k == "scrim" else r"#[0-9A-F]{6}"
            assert re.fullmatch(want, v), (name, k, v)


def test_正文颜色对够AA():
    assert not _bad(BODY, 4.5), _bad(BODY, 4.5)


def test_非文字的够3比1_边线够看得出来():
    assert not _bad(NON_TEXT, 3.0), _bad(NON_TEXT, 3.0)
    assert not _bad(BORDER, 1.5), _bad(BORDER, 1.5)


def test_软底是同色百分之十到十五压在卡片底上():
    alpha = TOKENS["softAlpha"]
    assert 0.10 <= alpha <= 0.15
    for name, c in THEMES.items():
        for t in TONES:
            soft = c[f"{t}Soft"]
            want, got = _rgb(_over(c[t], c["surface"], alpha)), _rgb(soft)
            assert all(abs(a - b) <= 1 for a, b in zip(want, got, strict=True)), (name, t, soft)


def test_压在视频上的字_最亮的画面上也读得出():
    for name, c in THEMES.items():
        scrim, alpha = c["scrim"][:7], int(c["scrim"][7:], 16) / 255
        worst = _over(scrim, "#FFFFFF", alpha)
        assert contrast(c["onScrim"], worst) >= 4.5, (name, worst)


def test_红只给P1和急停类_橙只给P2和低电():
    usage = {k: v for k, v in TOKENS["usage"].items() if not k.startswith("$")}
    assert RED_ONLY | ORANGE_ONLY <= set(usage), "这几种用途都要声明"
    for use, tone in usage.items():
        assert tone in (*TONES, "neutral"), use
        assert (tone == "error") == (use in RED_ONLY), use
        assert (tone == "warning") == (use in ORANGE_ONLY), use


def test_红和橙认得出是红和橙_不靠调色调混过去():
    for name, c in THEMES.items():
        r, g, b = _rgb(c["error"])
        assert r > 1.8 * g and r > 1.8 * b, (name, "error")
        r, g, b = _rgb(c["warning"])
        assert r > g > b and r > 1.4 * g, (name, "warning")
        r, g, b = _rgb(c["success"])
        assert g > r and g > b, (name, "success")


def test_字号最小12_字体只有那三种():
    assert set(TOKENS["font"]) == {"display", "sans", "mono", "fallback"}
    for name, t in TOKENS["type"].items():
        assert t["size"] >= 12, name
        assert t["family"] in ("display", "sans", "mono"), name
        assert t["weight"] in (400, 500, 600, 700), name
        assert 1.0 <= t["height"] <= 1.6, name
    # 设计稿的比例(字号 10 的抬到 12)
    sizes = {k: (v["family"], v["size"], v["weight"]) for k, v in TOKENS["type"].items()}
    assert sizes["headlineMedium"] == ("display", 28, 600)
    assert sizes["bodyMedium"] == ("sans", 14, 400)
    assert sizes["labelSmall"] == ("sans", 12, 500)   # W60:标签用常规字,等宽只给编号、数字


def test_间距和圆角是设计稿那几档():
    assert TOKENS["space"] == {"s4": 4, "s8": 8, "s16": 16, "s24": 24, "s32": 32}
    assert TOKENS["radius"] == {"r2": 2, "r4": 4, "r8": 8, "r12": 12, "rPill": 9999}
