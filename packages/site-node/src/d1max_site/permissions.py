"""角色与权限(W00c3 设计决定一 A:三个固定角色、一张固定权限表)。

- **admin** 管理员:一切。
- **guard** 保安:看、派单(goto/patrol/回待命点)、abort、确认与解决告警(W00c5a)。
- **engineer** 工程师(临时,W44 第二期):管理员开的,到点失效;一切,除了管账号。
- **owner** 经理:看、abort、确认告警(W20:留守时自己看到了能点「我知道了」)、切布防模式(W20);
  W53(用户 2026-10-30):**能派巡逻、能排日程**(``dispatch``、``schedule``),
  不能遥控、不能改路线和地图。
- **viewer** 只看(W53 / W52):只能看。App 上申请的账号批准时默认给它。

布防模式(W20,``modes``):切到布防谁都行(``arm``,保安也有);切到留守、临时撤防(撤防)是经理、管理员的决定
(``set_mode``,保安没有)。
"""

from __future__ import annotations

VIEW = "view"                      # 看状态、事件、排程、事件账
DISPATCH = "dispatch"              # 派 goto / patrol、回待命点
ABORT = "abort"                    # 叫停
MANAGE = "manage"                  # 任务包、待命点、拦截点、防区
MANAGE_ACCOUNTS = "manage_accounts"
VIEW_AUDIT = "view_audit"
HANDLE_ALERTS = "handle_alerts"    # 确认、解决告警(W00c5a):「我看见了在处理」是值班的人的事
TELEOP = "teleop"                  # 遥控(W00c5c,决策 7):持租约的人类实时操作;经理没有
REVIEW = "review"                  # 判读、复核照片(W00c5d):值班的人的事
EXPORT = "export"                  # 导出证据(W00c5d):整段数据带出站点,只给管理员
ARM = "arm"                        # 切到布防(W20):谁值班谁都能
SET_MODE = "set_mode"              # 切到留守、临时撤防(W20):撤防是经理、管理员的决定
SCHEDULE = "schedule"              # 改巡逻日程、布防时间(W53):经理能排日程;路线本身还是 manage

ALL = frozenset({VIEW, DISPATCH, ABORT, MANAGE, MANAGE_ACCOUNTS, VIEW_AUDIT, HANDLE_ALERTS,
                 TELEOP, REVIEW, EXPORT, ARM, SET_MODE, SCHEDULE})
ROLES: dict[str, frozenset[str]] = {
    "admin": ALL,
    # W44 第二期:工程师临时账号(管理员开,4 / 24 小时到点失效)= 管理员减去管账号(不能给自己续命)
    "engineer": ALL - {MANAGE_ACCOUNTS},
    "guard": frozenset({VIEW, DISPATCH, ABORT, HANDLE_ALERTS, TELEOP, REVIEW, ARM}),
    "owner": frozenset({VIEW, ABORT, HANDLE_ALERTS, ARM, SET_MODE, DISPATCH, SCHEDULE}),
    "viewer": frozenset({VIEW}),
}

#: 新申请的账号批准时默认给的角色(W52)。
DEFAULT_APPLY_ROLE = "viewer"


def allowed(role: str, perm: str) -> bool:
    return perm in ROLES.get(role, frozenset())
