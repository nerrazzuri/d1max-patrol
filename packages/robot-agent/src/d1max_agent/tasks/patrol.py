"""``patrol``:整趟巡检(W00c2a)。站点把完整的任务定义放进命令里(设计决定二 A),代理用
契约的 ``parse_mission`` 读出来,在 MissionEngine 上跑。

契约上看到的:``task_progress`` 不发距离(多个航点,距离没有意义),改为每个航点一条
``patrol_waypoint``(``index``、``name``、``ok``、``note``);终态同 goto —— 全部航点到了
``done``,有被跳过的 ``failed``(理由列出没到的航点),我们发的 abort ``aborted/preempted``,
引擎自己收尾(急停、电量、丢定位超时……)``failed``。
"""

from __future__ import annotations

from collections.abc import Callable

from d1max_agent.assembly import EngineParts
from d1max_agent.events import EventBook
from d1max_agent.tasks.engine_goto import EngineMissionTask
from d1max_contract.mission import Mission


class PatrolTask(EngineMissionTask):
    def __init__(self, *, task_id: str, mission: Mission, parts: EngineParts,
                 events: EventBook, now_ms: Callable[[], int], priority: int = 0) -> None:
        super().__init__(task_id=task_id, kind="patrol", max_speed_mps=None, parts=parts,
                         events=events, now_ms=now_ms, priority=priority)
        self.mission = mission

    def _mission(self) -> Mission:
        return self.mission

    def _report_waypoints(self, results) -> None:
        for i in range(self._reported_results, len(results)):
            r = results[i]
            self._events.emit("patrol_waypoint", {"task_id": self.task_id, "index": i,
                                                  "name": r.name, "ok": r.ok, "note": r.note})
        self._reported_results = len(results)

    def _failed_reason(self, bad) -> str:
        names = ", ".join(r.name for r in bad)
        first = bad[0].note or "waypoint failed"
        return f"{len(bad)} 个航点没到({names}):{first}"
