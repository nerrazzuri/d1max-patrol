"""角色与权限(W00c3 设计决定一 A:三个固定角色、一张固定权限表)。

- **admin** 管理员:一切。
- **guard** 保安:看、派单(goto/patrol/回待命点)、abort。
- **owner** 业主:看、abort —— 业主看到狗在院子里乱走要能叫停,但不能派它去哪儿。
"""

from __future__ import annotations

VIEW = "view"                      # 看状态、事件、排程、事件账
DISPATCH = "dispatch"              # 派 goto / patrol、回待命点
ABORT = "abort"                    # 叫停
MANAGE = "manage"                  # 任务包、待命点、拦截点、防区
MANAGE_ACCOUNTS = "manage_accounts"
VIEW_AUDIT = "view_audit"

ALL = frozenset({VIEW, DISPATCH, ABORT, MANAGE, MANAGE_ACCOUNTS, VIEW_AUDIT})
ROLES: dict[str, frozenset[str]] = {
    "admin": ALL,
    "guard": frozenset({VIEW, DISPATCH, ABORT}),
    "owner": frozenset({VIEW, ABORT}),
}


def allowed(role: str, perm: str) -> bool:
    return perm in ROLES.get(role, frozenset())
