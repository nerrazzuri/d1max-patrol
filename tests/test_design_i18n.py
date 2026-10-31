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


#: W59 文案标准(简洁、正式;失败写「失败 / 未 / 无法」):告警标题里不用的口语说法
COLLOQUIAL = ("没成", "没过", "没能", "没到", "没人", "没加密", "没传", "存不下", "删不掉",
              "封不上", "解不开", "读不懂", "连不上", "派不了", "太老", "要人", "等你", "有人申请",
              "有人派", "快满", "快到期", "跑完", "掉线", "盘紧", "看门机器人")


def test_W59_告警中文标题不用口语():
    bad = []
    for kind, v in CAT.items():
        if kind.startswith("$"):
            continue
        zh = v["zh"]
        hit = [w for w in COLLOQUIAL if w in zh]
        if hit or zh.endswith("了") or "了，" in zh or "了（" in zh:
            bad.append(f"{kind}: {zh} {hit}")
    assert not bad, "\n".join(bad)
