"""审计(W00c3 设计决定三 A):只追加的 ``audit`` 表。记登录成功/失败/锁定、注销、每一次改动类
API 调用(谁、什么动作、对象、结果、时刻、来源地址)。派单本身在 ``commands`` 表(带 ``issued_by``),
审计这一行的 ``detail`` 里带 ``command_id`` 指过去。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from d1max_site.db import SiteDB


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
                       json.dumps(detail or {}, ensure_ascii=False)[:2000], remote[:64]))

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]
