#!/usr/bin/env python3
"""数一数《真机验证》(docs/真机验证.md)每一节的结论:做完的、做了一半的、没做的、退役的。

    python3 tools/field_pending.py                 # 每一节的合计
    python3 tools/field_pending.py --list          # 再把没做、做了一半的一条条列出来
    python3 tools/field_pending.py --all           # 每一条都列(带结论)

结论写在每一条后面(表格最后一格、「—— 结论:…」、「〔结论:…〕」),做完了直接改那一格:
✅ 做完(真机 / 演示站点,写着结果)/ ❌ 没过 / ◐ 做了一半(写着剩下什么)/ ⬜ 没做 / — 不用做 / 退役。
用户 2026-10-28:演示站点做完了成功了就算做完。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "真机验证.md"
KINDS = ("✅", "❌", "◐", "⬜", "—", "退役")
#: 表头里「做什么」那一列可能叫的名字
WHAT = ("做什么", "步骤", "要做的", "内容", "项目", "假设", "怎么做")


def _clean(t: str) -> str:
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)
    t = re.sub(r"~~([^~]*)~~", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def items(path: Path = DOC) -> list[tuple[str, str, str, str]]:
    """→ [(章节, 编号, 做什么, 结论)]。"""
    out = []
    sec = ""
    col = 1
    for line in path.read_text(encoding="utf-8").split("\n"):
        m = re.match(r"^## (.+)", line)
        if m:
            sec = m.group(1).strip()
            continue
        if not sec:
            continue                                   # 开头的合计表
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells[-1] == "结论":          # 表头:「做什么」是哪一列
                col = next((i for i, h in enumerate(cells)
                            if h in WHAT), 1)
                continue
            if len(cells) >= 3 and cells[-1].startswith(KINDS):
                rid = cells[0].replace("🐕", "").strip()
                what = cells[col] if col < len(cells) - 1 else cells[1]
                out.append((sec, rid, _clean(what), cells[-1]))
            continue
        m = (re.search(r"〔结论：(.+?)〕\s*$", line)
             or re.search(r"—— \*\*结论：(.+?)\*\*\s*$", line))
        if m and not m.group(1).startswith("— 记数栏"):      # §6 末尾的记数栏不是测试项
            body = _clean(re.sub(r"〔结论：.+?〕|—— \*\*结论：.+?\*\*", "", line))
            body = re.sub(r"^(- \[[ xX]\] |\d+\. )", "", body)
            n = re.match(r"^(\d+[a-z]{0,3}\.\d+[a-z]?|\d+\.\d+ \.\d+)\s*", body)
            rid = n.group(1) if n else ""
            out.append((sec, rid, body[len(n.group(0)):] if n else body, m.group(1)))
    return out


def kind(st: str) -> str:
    for k in KINDS:
        if st.startswith(k):
            return {"✅": "做完", "❌": "没过", "◐": "做了一半", "⬜": "没做", "—": "不用做",
                    "退役": "退役"}[k]
    return "?"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="列出没做、做了一半的")
    ap.add_argument("--all", action="store_true", help="每一条都列")
    ap.add_argument("--doc", type=Path, default=DOC)
    a = ap.parse_args(argv)
    rows = items(a.doc)
    secs: dict[str, list] = {}
    for r in rows:
        secs.setdefault(r[0], []).append(r)
    tot: dict[str, int] = {}
    for sec, rs in secs.items():
        c: dict[str, int] = {}
        for r in rs:
            c[kind(r[3])] = c.get(kind(r[3]), 0) + 1
            tot[kind(r[3])] = tot.get(kind(r[3]), 0) + 1
        print(f"{sec[:40]:<40} " + "、".join(f"{k} {v}" for k, v in c.items()))
        if a.list or a.all:
            for _, rid, what, st in rs:
                if a.all or kind(st) in ("没做", "做了一半", "没过"):
                    print(f"    {rid:<8} {what[:70]} —— {st}")
    print("合计:" + "、".join(f"{k} {v}" for k, v in tot.items()) + f"(共 {len(rows)} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
