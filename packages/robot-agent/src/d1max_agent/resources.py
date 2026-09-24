"""资源账本(总设计 §4.1/4.2):谁占着什么。表本身在契约包(``resources_for``);
这里只记「当前谁持有」。抢占的顺序(先停旧、等停止确认、再给新)在 commands.py。"""

from __future__ import annotations

from d1max_contract.resources import RESOURCES, resources_for


class ResourceLedger:
    def __init__(self) -> None:
        self._holder: dict[str, str] = {}

    def holder(self, resource: str) -> str | None:
        if resource not in RESOURCES:
            raise KeyError(resource)
        return self._holder.get(resource)

    def conflicts(self, kind: str) -> set[str]:
        """要跑 kind 这种任务,得先等哪些任务让出资源。不认识的 kind 抛 KeyError。"""
        return {self._holder[r] for r in resources_for(kind) if r in self._holder}

    def acquire(self, task_id: str, kind: str) -> None:
        for r in resources_for(kind):
            held = self._holder.get(r)
            if held is not None and held != task_id:
                raise RuntimeError(f"{r} 被 {held} 占着,{task_id} 拿不到")
            self._holder[r] = task_id

    def release(self, task_id: str) -> None:
        for r in [r for r, t in self._holder.items() if t == task_id]:
            del self._holder[r]
