#!/usr/bin/env python3
"""从 docs/庄园场景待真机测试.md 生成「还没做的真机项」一张清单(一项一行)。

    python3 tools/field_pending.py docs/庄园场景待真机测试.md docs/真机待验清单.md

演示站点做成了的编号写在下面 ``DEMO_DONE``、做了一半的写在 ``DEMO_HALF``(§3af、§3ag);
以后又验了别的,加进去再生成。
"""

import re
import sys

src, out = sys.argv[1], sys.argv[2]
L = open(src, encoding="utf-8").read().split("\n")
s4 = [i for i, ln in enumerate(L) if ln.startswith("## 4.")][0]
paren = " ".join(re.findall(r"[（(]([^）)]*)[）)]", "\n".join(L[s4:])))
done = set(re.findall(r"(?<![\w.])(\d+[a-z]{0,3}\.\d+)(?!\d)", paren))
# 演示站点上做成了的(§3af、§3ag):用户 2026-10-28「演示站点做完了成功了就当做完了」
DEMO_DONE = set("""
3ae.1 3ae.2 3ae.5 3aa.3 3aa.4 3aa.5 3aa.9 3aa.10 3aa.11 3ab.1 3ab.2 3ab.3 3ab.4 3ab.5 3ab.6
3ac.3 3ac.5 3ad.3 3ad.4 3ad.6 3c.20 3p.3 3p.5 3t.4 3t.6 3t.7 3t.8 3t.9 3t.10 3t.11 3t.12 3t.13
3t.15 3t.16 3t.17 3t.18 3t.19 3v.1 3v.2 3v.3 3v.4 3v.5 3v.6 3v.8 3w.1 3w.2 3w.8 3w.9 3w.10
3x.8 3x.12 3x.13 3x.14 3x.15 3x.16 3x.17 3x.18 3x.19
""".split())
# 演示站点上做了一半的:剩下那一半要真机(写在状态里)
DEMO_HALF = {
    "3aa.1": "热像、机器人 WiFi 上行要真机",
    "3aa.2": "热像要实物",
    "3ac.1": "机器人照旧绕开要动狗",
    "3ac.4": "朝向对不对实物要真摄像头",
    "3ad.1": "发布到机器人、从擦掉的地方走要真机",
    "3ad.2": "机器人过楼梯放慢要动狗",
    "3ae.6": "真换版本要真机",
    "3t.5": "跟装机有关的几条(站点用户、服务)要真装机",
}


def clean(t):
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)
    t = re.sub(r"<br>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


secs = []
cur = None
hdr = None
for ln in L[:s4]:
    m = re.match(r"^## (\S+)\s*(.*)", ln)
    if m:
        cur = {"id": m.group(1), "title": clean(m.group(2)), "rows": []}
        secs.append(cur)
        hdr = None
        continue
    if cur is None or cur["id"] in ("0.", "3af."):
        continue
    if re.match(r"^\|\s*(#|项)\s*\|", ln):
        hdr = [c.strip() for c in ln.strip().strip("|").split("|")]
        continue
    n = re.match(r"^(\d+)\. (.*)", ln)
    if n and hdr is None and cur["id"] == "3v.":
        txt = clean(n.group(2))
        rid = f"3v.{n.group(1)}"
        st = ("真机做过（§4 留档）" if rid in done else "演示站点做完" if rid in DEMO_DONE
              else f"演示站点做了一半（{DEMO_HALF[rid]}）" if rid in DEMO_HALF else "没做")
        cur["rows"].append((rid, txt[:90], int(n.group(1)) >= 9, st))
        continue
    m = re.match(r"^\|\s*(\d+[a-z]{0,3}\.\d+)\s*(🐕)?\s*\|", ln)
    if not m or hdr is None:
        continue
    cells = [c.strip() for c in ln.strip().strip("|").split("|")]
    rid, dog = m.group(1), bool(m.group(2))
    # 「做什么 / 步骤」那一列
    col = next(
        (i for i, h in enumerate(hdr) if h in ("做什么", "步骤", "要做的", "内容", "项目")),
        2 if len(cells) > 3 else 1,
    )
    what = cells[col] if col < len(cells) else cells[1]
    if what.startswith("~~"):
        st = "退役"
    elif rid in done:
        st = "真机做过（§4 留档）"
    elif rid in DEMO_HALF:
        st = f"演示站点做了一半（{DEMO_HALF[rid]}）"
    elif rid in DEMO_DONE:
        st = "演示站点做完"
    else:
        st = "没做"
    dog = dog or bool(re.search(r"动狗|🐕|（动狗|要动机器", ln))
    cur["rows"].append((rid, clean(what)[:90], dog, st))
o = [
    "# 真机验证 · 还没做的（自动生成）",
    "",
    f"从 `docs/庄园场景待真机测试.md` 生成（{sum(len(s['rows']) for s in secs)} 项）："
    "划掉的算「退役」；§4 留档里括号点名的编号算「真机做过」；"
    "§3af、§3ag 演示站点上做成的算「演示站点做完」"
    "（用户：演示站点做完了成功了就当做完了）、做了一半的写明剩下哪一半；"
    "其余都是「没做」。"
    "🐕 = 要动机器人（按动作批准规矩先问用户）。"
    "「做过」只说明留档点过名，有的只做了一半，细节看 §4。",
    "",
]
tot = {}
for s in secs:
    rows = [r for r in s["rows"] if r[3] != "退役"]
    if not rows:
        continue
    pend = [r for r in rows if r[3] not in ("真机做过（§4 留档）", "演示站点做完")]
    if not pend:
        continue
    o.append(f"## {s['id']} {s['title']}（还剩 {len(pend)} / {len(rows)}）")
    o.append("")
    o.append("| 编号 | 做什么 | 动狗 | 状态 |")
    o.append("|---|---|---|---|")
    for rid, what, dog, st in pend:
        o.append(f"| {rid} | {what.replace('|', '/')} | {'🐕' if dog else ''} | {st} |")
        tot[st.split("（")[0].split("，")[0]] = tot.get(st.split("（")[0].split("，")[0], 0) + 1
    o.append("")
allrows = [r for s in secs for r in s["rows"]]
summ = {
    k: sum(1 for r in allrows if r[3].startswith(k))
    for k in ("退役", "真机做过", "演示站点做完", "演示站点做了一半", "没做")
}
o.insert(
    4,
    "**合计**："
    + "、".join(f"{k} {v}" for k, v in summ.items())
    + "。「动狗」一栏只在原表标了的地方有(很多老章节没标,按章节标题判断)。"
)
o.insert(5, "")
open(out, "w", encoding="utf-8").write("\n".join(o) + "\n")
print(summ, "dog-pending", sum(1 for r in allrows if r[3] == "没做" and r[2]))
for s in secs:
    rows = [r for r in s["rows"] if r[3] not in ("退役", "真机做过（§4 留档）", "演示站点做完")]
    if rows:
        print(s["id"], len(rows), sum(1 for r in rows if r[3] == "没做"))
