#!/usr/bin/env python3
"""贴纸打印文件(W42 §3,生产线的电脑上跑):``factory burn`` 出的 ``<序列号>.txt``
→ 60 × 40 mm 的 SVG,左边二维码 30 mm,右边文字版绑定码和序列号。
要 ``pip install '.[factory]'``(segno)。**打完删掉**两个文件。

    python tools/sticker_label.py <序列号>.txt
"""

from __future__ import annotations

import os
import sys
from html import escape
from pathlib import Path

W_MM, H_MM, QR_MM = 60, 40, 30


def _read(txt: Path) -> dict[str, str]:
    out = {}
    for line in txt.read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition(" ")
        out[k] = v.strip()
    return out


def render(txt: Path) -> Path:
    import segno
    d = _read(txt)
    qr = segno.make(d["二维码"], error="m")
    n = qr.symbol_size(scale=1, border=0)[0]
    cell = QR_MM / n
    rects = "".join(
        f'<rect x="{5 + x * cell:.3f}" y="{5 + y * cell:.3f}" width="{cell:.3f}" '
        f'height="{cell:.3f}"/>'
        for y, row in enumerate(qr.matrix) for x, v in enumerate(row) if v)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W_MM}mm" height="{H_MM}mm" '
           f'viewBox="0 0 {W_MM} {H_MM}"><rect width="{W_MM}" height="{H_MM}" fill="#fff"/>'
           f'<g fill="#000">{rects}</g>'
           f'<g font-family="sans-serif" fill="#000">'
           f'<text x="38" y="12" font-size="3">机器人绑定码</text>'
           f'<text x="38" y="18" font-size="2.6" font-family="monospace">'
           f'{escape(d["绑定码"][:9])}</text>'
           f'<text x="38" y="22" font-size="2.6" font-family="monospace">'
           f'{escape(d["绑定码"][10:])}</text>'
           f'<text x="38" y="29" font-size="2.2">序列号 {escape(d["序列号"])}</text>'
           f'<text x="38" y="34" font-size="2">用 Nexus Warden App 扫码</text></g></svg>')
    out = txt.with_suffix(".svg")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(svg)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(render(Path(sys.argv[1])))
