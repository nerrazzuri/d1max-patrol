"""站点的优先级表(W00c2b 设计决定一 A)。**优先级由站点按任务来源定**,代理只比数字:
新任务严格更高才抢占正在跑的,相等或更低回 ``busy``。请求体里的 ``priority`` 一律不认 ——
登录的人填 1000 就能打断一切(包括事件派遣)是不行的。"""

from __future__ import annotations

#: 回待命点:最低,谁来都能抢。
STANDBY_RETURN = -10
#: 排程巡检:10 + 条目的 priority(夹在 0..39),即 10–49。
SCHEDULE_BASE = 10
SCHEDULE_SPAN = 39
#: 人在站点 API 上手动派的(goto、patrol)。
MANUAL = 60
#: 外部事件派遣(W00c2c)。
EVENT = 80
#: 回待命点任务的 task_id 前缀:排程执行器据此把「正在回待命点」看成空闲。
STANDBY_PREFIX = "standby-"
#: 事件派遣任务的 task_id 前缀(W00c2c)。
INCIDENT_PREFIX = "incident-"


def schedule_priority(entry_priority: int) -> int:
    return SCHEDULE_BASE + max(0, min(SCHEDULE_SPAN, int(entry_priority)))
