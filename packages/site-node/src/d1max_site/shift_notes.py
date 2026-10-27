"""交班留言(W44,设计 §4.5):下班的保安给下一班留一句话,报警记录页显示最近一条(谁、几点)。

作者是登录账号(接口不信请求体);一条 1–500 字(去掉首尾空白)。只追加,不改不删 ——
交接说过什么要查得到。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MAX_CHARS = 500


class ShiftNotes:
    def __init__(self, db: Any, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def add(self, author: str, text: Any) -> dict[str, Any]:
        if not isinstance(text, str):
            raise ValueError("text 要是字符串")
        body = text.strip()
        if not body:
            raise ValueError("留言是空的")
        if len(body) > MAX_CHARS:
            raise ValueError(f"留言最多 {MAX_CHARS} 字,这条 {len(body)} 字")
        now = self._now()
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO shift_notes(at_ms, author, text) VALUES (?, ?, ?)",
                            (now, author, body))
            nid = int(cur.lastrowid)
        return {"id": nid, "at_ms": now, "author": author, "text": body}

    def latest(self) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT id, at_ms, author, text FROM shift_notes ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "at_ms": r["at_ms"], "author": r["author"], "text": r["text"]}
