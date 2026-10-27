"""界面用词(W44,用户 2026-10-27):给人看的文字写「四足机器人 / 机器人」、英文写 robot,不写「狗」「dog」。

扫的是**字符串和界面文字**,不扫注释、文档字符串、标识符(那些是写给开发者看的)。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAD_ZH = "狗"
BAD_EN = re.compile(r"\bdogs?\b", re.IGNORECASE)


def code_outside_comments(src: str) -> list[tuple[int, str]]:
    """JS/TS/Dart:去掉 ``//``、``/* */`` 注释后,每行剩下的(行号, 文字)。字符串里的 ``//`` 不当注释。"""
    out: list[list[str]] = [[]]
    i, n = 0, len(src)
    quote: str | None = None            # 在字符串里:引号(' " ` 或三引号)
    while i < n:
        c = src[i]
        if quote is not None:
            if c == "\\":
                out[-1].append(src[i:i + 2])
                i += 2
                continue
            if src.startswith(quote, i):
                out[-1].append(quote)
                i += len(quote)
                quote = None
                continue
            if c == "\n":
                out.append([])
            else:
                out[-1].append(c)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            seg = src[i:n if j < 0 else j + 2]
            for _ in range(seg.count("\n")):
                out.append([])
            i = n if j < 0 else j + 2
            continue
        for q in ("'''", '"""', "'", '"', "`"):
            if src.startswith(q, i):
                quote = q
                out[-1].append(q)
                i += len(q)
                break
        else:
            if c == "\n":
                out.append([])
            else:
                out[-1].append(c)
            i += 1
    return [(k + 1, "".join(line)) for k, line in enumerate(out)]


def _hits(paths: list[Path]) -> list[str]:
    bad = []
    for p in paths:
        for no, text in code_outside_comments(p.read_text(encoding="utf-8")):
            if BAD_ZH in text:
                bad.append(f"{p.relative_to(ROOT)}:{no}: {text.strip()[:100]}")
    return bad


def test_扫描器不把字符串里的双斜杠当注释():
    got = code_outside_comments('a = "http://x/狗"; // 狗\n/* 狗\n狗 */ b = \'狗\'')
    assert [(no, BAD_ZH in t) for no, t in got] == [(1, True), (2, False), (3, True)]


def test_网页界面文字不写狗():
    src = ROOT / "web/console/src"
    files = [p for p in sorted(src.glob("*.ts*")) if ".test." not in p.name]
    bad = _hits(files)
    cat = json.loads((ROOT / "design/i18n/alerts.json").read_text(encoding="utf-8"))

    def walk(x, where):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{where}.{k}")
        elif isinstance(x, list):
            for k, v in enumerate(x):
                walk(v, f"{where}[{k}]")
        elif isinstance(x, str) and (BAD_ZH in x or BAD_EN.search(x)):
            bad.append(f"design/i18n/alerts.json{where}: {x[:100]}")
    walk(cat, "")
    i18n = (src / "i18n.ts").read_text(encoding="utf-8")
    for no, line in enumerate(i18n.splitlines(), 1):
        m = re.match(r'\s*\w+: "(.*)",?\s*$', line)
        if m and BAD_EN.search(m.group(1)):
            bad.append(f"web/console/src/i18n.ts:{no}: {m.group(1)[:100]}")
    assert not bad, "界面上还写着狗 / dog:\n" + "\n".join(bad)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
        # 单独一行的字符串(变量说明那种)也是写给开发者的
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ids.add(id(node.value))
    return ids


def test_站点给人看的文字不写狗():
    bad = []
    srcs = [ROOT / "packages/site-node/src", ROOT / "packages/robot-agent/src", ROOT / "packages/contract/src"]
    for p in sorted(f for d in srcs for f in d.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in skip and BAD_ZH in node.value:
                bad.append(f"{p.relative_to(ROOT)}:{node.lineno}: {node.value[:100]}")
    assert not bad, "站点给人看的文字还写着狗:\n" + "\n".join(bad)


def test_手机界面文字不写狗():
    files = sorted((ROOT / "mobile/lib").rglob("*.dart"))
    bad = _hits(files)
    assert not bad, "手机界面还写着狗:\n" + "\n".join(bad)


def test_英文界面文字不写dog():
    """``tr(英, 中)`` 的英文那一半(网页各页、手机)。"""
    rx = re.compile(r"""\btr\(\s*(?:lang\.value\s*,\s*)?(['"])((?:\\.|(?!\1).)*)\1""")
    files = [p for p in sorted((ROOT / "web/console/src").glob("*.tsx")) if ".test." not in p.name]
    files += sorted((ROOT / "mobile/lib").rglob("*.dart"))
    bad = []
    for p in files:
        for no, text in code_outside_comments(p.read_text(encoding="utf-8")):
            for m in rx.finditer(text):
                if BAD_EN.search(m.group(2)):
                    bad.append(f"{p.relative_to(ROOT)}:{no}: {m.group(2)[:100]}")
    assert not bad, "英文界面还写着 dog:\n" + "\n".join(bad)
