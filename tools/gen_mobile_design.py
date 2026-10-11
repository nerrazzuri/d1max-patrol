#!/usr/bin/env python3
"""把设计的单一来源(``design/tokens.json``、``design/i18n/alerts.json``)生成手机 App 用的
Dart 常量(App V2)。

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
    return {OUT / "tokens.g.dart": tokens_dart(tokens),
            OUT / "alert_titles.g.dart": alerts_dart(catalog)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for path, text in generate().items():
        path.write_text(text, encoding="utf-8")
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
