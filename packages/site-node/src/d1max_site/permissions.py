"""角色与权限(W00c3 设计决定一 A:三个固定角色、一张固定权限表)。

- **admin** 管理员:一切。
- **guard** 保安:看、派单(goto/patrol/回待命点)、abort、确认与解决告警(W00c5a)。
- **owner** 业主:看、abort —— 业主看到狗在院子里乱走要能叫停,但不能派它去哪儿;告警只看。
"""

from __future__ import annotations

VIEW = "view"                      # 看状态、事件、排程、事件账
DISPATCH = "dispatch"              # 派 goto / patrol、回待命点
ABORT = "abort"                    # 叫停
MANAGE = "manage"                  # 任务包、待命点、拦截点、防区
MANAGE_ACCOUNTS = "manage_accounts"
VIEW_AUDIT = "view_audit"
HANDLE_ALERTS = "handle_alerts"    # 确认、解决告警(W00c5a):「我看见了在处理」是值班的人的事
TELEOP = "teleop"                  # 遥控(W00c5c,决策 7):持租约的人类实时操作;业主没有
REVIEW = "review"                  # 判读、复核照片(W00c5d):值班的人的事
EXPORT = "export"                  # 导出证据(W00c5d):整段数据带出站点,只给管理员

ALL = frozenset({VIEW, DISPATCH, ABORT, MANAGE, MANAGE_ACCOUNTS, VIEW_AUDIT, HANDLE_ALERTS,
                 TELEOP, REVIEW, EXPORT})
ROLES: dict[str, frozenset[str]] = {
    "admin": ALL,
    "guard": frozenset({VIEW, DISPATCH, ABORT, HANDLE_ALERTS, TELEOP, REVIEW}),
    "owner": frozenset({VIEW, ABORT}),
}


def allowed(role: str, perm: str) -> bool:
    return perm in ROLES.get(role, frozenset())
