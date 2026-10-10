"""中英文词条(B1):站点能报的每一种告警都有英文、中文标题(design/i18n/alerts.json),多出来的也不留。"""

from __future__ import annotations

import json
from pathlib import Path

from d1max_site.alerts import LEVEL_OF as LEVELS

ROOT = Path(__file__).resolve().parents[1]
CAT = json.loads((ROOT / "design" / "i18n" / "alerts.json").read_text(encoding="utf-8"))


def test_每种告警都有英文中文标题_没有多余的():
    kinds = {k for k in CAT if not k.startswith("$")}
    assert kinds == set(LEVELS), (set(LEVELS) - kinds, kinds - set(LEVELS))
    for k in kinds:
        e = CAT[k]
        assert set(e) == {"en", "zh"}, k
        assert e["en"].strip() and e["zh"].strip(), k
        assert e["en"].isascii(), k
        assert "{" not in e["en"] + e["zh"], f"{k}:标题不带占位符,编号和时间界面上另外显示"
