#!/usr/bin/env python3
"""把设计的单一来源(``design/tokens.json``、``design/mobile.json``、``design/i18n/alerts.json``)
生成手机 App 用的 Dart 常量(App V2)。2026-10-14 重做的那一套:``mobile.json`` →
``nw_tokens.g.dart``。

App 不在运行时读 JSON:颜色、字号是编译期常量,告警标题按种类查表。改了 ``design/`` 之后跑一遍::

    python3 tools/gen_mobile_design.py

``tests/test_design_mobile.py`` 会重新生成一遍跟仓库里的比,对不上就红(忘了跑的会被抓住)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "mobile" / "lib" / "design"

HEADER = "// 生成的文件,别手改:改 design/ 下的 JSON,再跑 python3 tools/gen_mobile_design.py。\n"


def _hex(c: str) -> str:
    m = re.fullmatch(r"#([0-9A-Fa-f]{6})", c)
    if m is None:
        raise ValueError(f"颜色要写成 #RRGGBB:{c!r}")
    return f"Color(0xFF{m.group(1).upper()})"


def _dart_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("$", "\\$") + "'"


def tokens_dart(tokens: dict) -> str:
    lines = [HEADER, "import 'dart:ui' show Color;\n",
             f"const int tokensVersion = {int(tokens['version'])};\n",
             "/// 颜色(``design/tokens.json`` 的 color)。", "abstract final class D1Color {"]
    for name, value in tokens["color"].items():
        lines.append(f"  static const Color {name} = {_hex(value)};")
    lines += ["}\n", "/// 尺寸(size),逻辑像素。", "abstract final class D1Size {"]
    for name, value in tokens["size"].items():
        lines.append(f"  static const double {name} = {float(value)};")
    lines += ["}\n", "/// 圆角(radius)。", "abstract final class D1Radius {"]
    for name, value in tokens["radius"].items():
        lines.append(f"  static const double {name} = {float(value)};")
    lines += ["}\n", "/// 字号、行高、字重(type);family 是 mono 的用等宽字体。",
              "abstract final class D1Type {"]
    for name, t in tokens["type"].items():
        mono = "true" if t.get("family") == "mono" else "false"
        lines.append("  static const ({double size, double line, int weight, bool mono}) "
                     f"{name} = "
                     f"(size: {float(t['size'])}, line: {float(t['line'])}, "
                     f"weight: {int(t['weight'])}, mono: {mono});")
    lines += ["}\n", "/// 动效(motion),毫秒。", "abstract final class D1Motion {",
              f"  static const int fast = {int(tokens['motion']['fast'])};",
              f"  static const int p1FlashMs = {int(tokens['motion']['p1FlashMs'])};",
              "  static const double p1FlashMinOpacity = "
              f"{float(tokens['motion']['p1FlashMinOpacity'])};",
              "}"]
    return "\n".join(lines) + "\n"


def _hex_alpha(c: str) -> str:
    """``#RRGGBBAA``(压在视频上的那层带透明度)→ Dart 的 ``0xAARRGGBB``。"""
    m = re.fullmatch(r"#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})", c)
    if m is None:
        raise ValueError(f"带透明度的颜色要写成 #RRGGBBAA:{c!r}")
    return f"Color(0x{m.group(2).upper()}{m.group(1).upper()})"


def nw_tokens_dart(mobile: dict) -> str:
    """新设计系统(``design/mobile.json``):浅色、深色两套颜色,间距,圆角,字体,字。"""
    light, dark = mobile["color"]["light"], mobile["color"]["dark"]
    if list(light) != list(dark):
        raise ValueError("浅色、深色两套颜色的键要一样、顺序也一样")
    lines = [HEADER, "import 'package:flutter/painting.dart' show Color, FontWeight, TextStyle;\n",
             f"const int nwTokensVersion = {int(mobile['version'])};\n"]
    for cls, doc, colors in (("NwLight", "浅色", light), ("NwDark", "深色", dark)):
        lines += [f"/// {doc}那一套颜色(``design/mobile.json`` 的 color)。",
                  f"abstract final class {cls} {{"]
        for name, value in colors.items():
            dart = _hex_alpha(value) if name == "scrim" else _hex(value)
            lines.append(f"  static const Color {name} = {dart};")
        lines.append("}\n")
    lines += ["/// 间距(space),逻辑像素。", "abstract final class NwSpace {"]
    for name, value in mobile["space"].items():
        lines.append(f"  static const double {name} = {float(value)};")
    lines += ["}\n", "/// 圆角(radius)。", "abstract final class NwRadius {"]
    for name, value in mobile["radius"].items():
        lines.append(f"  static const double {name} = {float(value)};")
    font = mobile["font"]
    fallback = ", ".join(_dart_str(f) for f in font["fallback"])
    lines += ["}\n", "/// 字体(font):标题、正文、等宽;中文落到系统字体。",
              "abstract final class NwFont {"]
    for name in ("display", "sans", "mono"):
        lines.append(f"  static const String {name} = {_dart_str(font[name])};")
    lines += [f"  static const List<String> fallback = <String>[{fallback}];", "}\n",
              "/// 字(type):不带颜色。", "abstract final class NwType {"]
    for name, t in mobile["type"].items():
        if t["family"] not in ("display", "sans", "mono"):
            raise ValueError(f"字体只有 display / sans / mono:{name}")
        if float(t["size"]) < 12:
            raise ValueError(f"字号最小 12:{name}")
        lines += [f"  static const TextStyle {name} = TextStyle(",
                  f"      fontFamily: NwFont.{t['family']},",
                  "      fontFamilyFallback: NwFont.fallback,",
                  f"      fontSize: {float(t['size'])},",
                  f"      height: {float(t['height'])},",
                  f"      fontWeight: FontWeight.w{int(t['weight'])},",
                  "      letterSpacing: 0.0);"]
    lines += ["}\n", "/// 哪种状态用哪种色调(usage);红只给 P1 和急停类,橙只给 P2 和低电。",
              "const Map<String, String> nwUsage = <String, String>{"]
    for use, tone in mobile["usage"].items():
        if use.startswith("$"):
            continue
        lines.append(f"  {_dart_str(use)}: {_dart_str(tone)},")
    lines.append("};")
    return "\n".join(lines) + "\n"


def alerts_dart(catalog: dict) -> str:
    lines = [HEADER,
             "/// 告警标题按种类(kind):``(英文, 中文)``。来源 ``design/i18n/alerts.json``。",
             "const Map<String, (String, String)> alertTitles = <String, (String, String)>{"]
    for kind, entry in catalog.items():
        if kind.startswith("$"):
            continue
        lines.append(f"  {_dart_str(kind)}: ({_dart_str(entry['en'])}, {_dart_str(entry['zh'])}),")
    lines.append("};")
    return "\n".join(lines) + "\n"


def generate() -> dict[Path, str]:
    tokens = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "design" / "i18n" / "alerts.json").read_text(encoding="utf-8"))
    mobile = json.loads((ROOT / "design" / "mobile.json").read_text(encoding="utf-8"))
    return {OUT / "tokens.g.dart": tokens_dart(tokens),
            OUT / "nw_tokens.g.dart": nw_tokens_dart(mobile),
            OUT / "alert_titles.g.dart": alerts_dart(catalog)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for path, text in generate().items():
        path.write_text(text, encoding="utf-8")
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
