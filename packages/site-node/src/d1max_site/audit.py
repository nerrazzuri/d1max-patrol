"""审计(W00c3 设计决定三 A):只追加的 ``audit`` 表。记登录成功/失败/锁定、注销、每一次改动类
API 调用(谁、什么动作、对象、结果、时刻、来源地址)。派单本身在 ``commands`` 表(带 ``issued_by``),
审计这一行的 ``detail`` 里带 ``command_id`` 指过去。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from d1max_site.db import SiteDB

#: ``detail`` 落库最多这么长。**落进去的必须是一份完整的 JSON**(W20 外审:原来序列化之后硬截,25 个
#: 防区名的访客模式一截,``list()`` 就读不回来了)。
MAX_DETAIL = 2000
_KEEP_ITEMS = 10
_KEEP_CHARS = 200


def _shrink(v: Any) -> Any:
    """长列表留前几项、写总数;长字符串截短。结构不变,读的人还看得懂。"""
    if isinstance(v, dict):
        return {str(k)[:_KEEP_CHARS]: _shrink(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        head = [_shrink(x) for x in v[:_KEEP_ITEMS]]
        return head + [f"…共 {len(v)} 项"] if len(v) > _KEEP_ITEMS else head
    if isinstance(v, str) and len(v) > _KEEP_CHARS:
        return v[:_KEEP_CHARS] + "…"
    return v


def encode_detail(detail: dict[str, Any] | None) -> str:
    """→ 不超过 ``MAX_DETAIL`` 的完整 JSON:原样放得下就原样;放不下先缩;还放不下只留键名。"""
    for d in (detail or {}, _shrink(detail or {})):
        raw = json.dumps(d, ensure_ascii=False)
        if len(raw) <= MAX_DETAIL:
            return raw
    keys = [str(k)[:64] for k in (detail or {})][:20]
    return json.dumps({"truncated": True, "keys": keys}, ensure_ascii=False)


def decode_detail(raw: str) -> dict[str, Any]:
    """读回来。以前硬截坏了的行(W20 外审之前写的)不许让整张审计读不出来:原文给一截看。"""
    try:
        d = json.loads(raw)
    except ValueError:
        return {"unreadable": True, "raw": raw[:_KEEP_CHARS]}
    return d if isinstance(d, dict) else {"value": d}


class AuditLog:
    def __init__(self, db: SiteDB, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def record(self, *, actor: str, action: str, target: str = "", status: int = 0,
               detail: dict[str, Any] | None = None, remote: str = "") -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO audit(at, actor, action, target, status, detail, remote) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (self._now(), actor[:128], action[:128], target[:256], int(status),
                       encode_detail(detail), remote[:64]))

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) | {"detail": decode_detail(r["detail"])} for r in rows]
