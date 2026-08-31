"""把厂商文档里的 JSON 样例抠成 fixture。

这些样例是无真机阶段能拿到的、最接近真机的输入。抠出来入库,
解析器每次改动都要重新对着它们过一遍。

厂商手写文档里的 JSON 偶尔带尾逗号或 `//` 行注释,这两种是常见的手写
JSON 语法错,不是内容缺失。严格解析失败时会先尝试机械修复(见 repair()),
修复用了哪些规则会记进 index.json 的 "repaired" 字段——修复本身是关于
这份文档的发现,不能悄悄发生。除此之外的失败(片段、省略号、真的漏了
括号)原样跳过,不做任何猜测性修补。

用法:
    python scripts/extract_doc_samples.py refs/nav-api/自主导航_WEBSOCKET_API.md \\
        tests/protocol/fixtures
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FENCE = re.compile(r"^```json\s*$")
END = re.compile(r"^```\s*$")

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_line_comments(text: str) -> str:
    """去掉 // 到行尾。字符串字面量里的 // 不动(比如 ws:// 这种)。"""
    out: list[str] = []
    in_string = escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and text[index + 1 : index + 2] == "/":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def repair(text: str) -> tuple[str, list[str]]:
    """厂商手写 JSON 的两种常见语法错。返回 (修好的文本, 用了哪些修复)。"""
    applied: list[str] = []
    stripped = _strip_line_comments(text)
    if stripped != text:
        applied.append("行注释")
    fixed = _TRAILING_COMMA.sub(r"\1", stripped)
    if fixed != stripped:
        applied.append("尾逗号")
    return fixed, applied


def expected_req_func(payload: dict) -> str | None:
    """从报文里直接扒出期望的 req_func,供测试与解析器结果比对。

    **刻意不调用 `d1max_patrol.protocol` 的解析函数**:这里要的是一份独立的
    第二实现。若改成调用解析器,测试就成了"解析器跟自己比",恒真,这条断言
    的全部价值就没了。以后不要"顺手去重"把它合并掉。
    """
    head_type = (payload.get("head") or {}).get("type")
    data = payload.get("data") or {}
    if head_type == "app_req":
        req_func = data.get("req_func")
        if isinstance(req_func, str):  # §7.14 exit_charging 那种裸字符串
            return req_func
        if isinstance(req_func, dict) and len(req_func) == 1:
            return next(iter(req_func))
        return None
    if head_type == "app_resp":
        result = data.get("req_result") or {}
        # 两种外壳键名都要认(厂商在不同章节写了两种拼法)。这里的重复是
        # 有意的:解析器那边少认一种时,这一份还认,测试才会红。
        for shell in ("AppReponseObjectData", "AppResponse"):
            nested = result.get(shell)
            if isinstance(nested, dict):
                result = nested
                break
        req_func = result.get("req_func")
        return req_func if isinstance(req_func, str) else None
    return None


def extract(doc: str) -> list[tuple[int, str]]:
    """返回 [(起始行号, 代码块文本)]。"""
    out: list[tuple[int, str]] = []
    lines = doc.splitlines()
    index = 0
    while index < len(lines):
        if FENCE.match(lines[index]):
            start = index + 1
            body: list[str] = []
            index += 1
            while index < len(lines) and not END.match(lines[index]):
                body.append(lines[index])
                index += 1
            out.append((start + 1, "\n".join(body)))
        index += 1
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    doc_path, out_dir = Path(argv[1]), Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    blocks = extract(doc_path.read_text(encoding="utf-8"))
    index: list[dict[str, object]] = []
    kept = skipped = 0
    for number, (line_no, body) in enumerate(blocks, 1):
        repaired: list[str] = []
        try:
            payload = json.loads(body)
        except ValueError:
            fixed, repaired = repair(body)
            try:
                payload = json.loads(fixed)
            except ValueError as exc:
                # 文档里有些块是片段或带省略号,不是完整报文;
                # 也可能是修复规则治不了的真笔误(比如漏了括号)
                print(f"跳过第 {line_no} 行的样例: {exc}", file=sys.stderr)
                skipped += 1
                continue
        name = f"{number:03d}.json"
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        index.append(
            {
                "file": name,
                "doc_line": line_no,
                "type": payload.get("head", {}).get("type"),
                "expected_req_func": expected_req_func(payload),
                "repaired": repaired,
            }
        )
        kept += 1

    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"抠出 {kept} 段,跳过 {skipped} 段 -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
