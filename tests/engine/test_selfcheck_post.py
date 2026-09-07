"""重启后那一遍。它的输出不是给人看的,是给机器执行的 —— 留着,还是退回去。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.preflight import CheckResult
from d1max_patrol.engine.selfcheck import (
    POST_CHECKS,
    SERVICE_UNIT,
    RestartPlan,
    Verdict,
    grab_control,
    postcheck_verdict,
    restart_plan,
    run_postcheck,
)


def _四项(**over) -> list[CheckResult]:
    base = dict.fromkeys(POST_CHECKS, True)
    base.update(over)
    return [CheckResult(name, ok, "") for name, ok in base.items()]


def test_四项全过就留着(tmp_path):
    assert postcheck_verdict(_四项()) is Verdict.KEEP


@pytest.mark.parametrize("坏的", POST_CHECKS)
def test_任意一项不过就回滚(坏的):
    assert postcheck_verdict(_四项(**{坏的: False})) is Verdict.ROLLBACK


def test_少跑了一项就回滚(tmp_path):
    """跑不全等于不确定。**不确定就退回去** —— 跟起飞门槛那条同一个道理。"""
    assert postcheck_verdict(_四项()[:3]) is Verdict.ROLLBACK


def test_一项都没跑就回滚(tmp_path):
    assert postcheck_verdict([]) is Verdict.ROLLBACK


def test_多出来的项不影响判定(tmp_path):
    """将来加第五项的话,这个函数得先被改 —— 而不是悄悄把新项忽略掉。"""
    extra = [*_四项(), CheckResult("将来的第五项", False, "")]
    assert postcheck_verdict(extra) is Verdict.ROLLBACK


def test_同一项报了两遍且有一遍不过就回滚(tmp_path):
    dup = [*_四项(), CheckResult("control", False, "")]
    assert postcheck_verdict(dup) is Verdict.ROLLBACK


class _假设备:
    """一个开机时会被别人占着、过几次就放手的 SDK 会话。"""

    def __init__(self, 放手在第几次: int) -> None:
        self._n = 0
        self._free = 放手在第几次

    async def has_control(self) -> bool:
        self._n += 1
        return self._n >= self._free

    async def acquire_control(self) -> None:
        return None

    @property
    def 试了几次(self) -> int:
        return self._n


async def test_一次就拿到会话(tmp_path):
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    got = await grab_control(_假设备(1), tries=3, sleep=假睡)
    assert got.ok is True
    assert 睡过 == []          # 一次就成的话一秒都不许等


async def test_被占着就重试到拿回来(tmp_path):
    """没有上装也可能开机那一刻被短暂占着(§7.1 理由 2)。这条路必须存在。"""
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    device = _假设备(3)
    got = await grab_control(device, tries=3, sleep=假睡)
    assert got.ok is True
    assert device.试了几次 == 3
    assert len(睡过) == 2       # 等了两次,不是三次 —— 拿到之后不再等


async def test_试满了还拿不到就算这一项没过(tmp_path):
    async def 假睡(_s: float) -> None:
        return None

    got = await grab_control(_假设备(99), tries=3, sleep=假睡)
    assert got.ok is False
    assert got.name == "control"
    assert "上装" in got.detail


async def test_抢会话炸了也只算这一项没过(tmp_path):
    class 会炸的:
        async def has_control(self) -> bool:
            raise RuntimeError("旁路进程没起来")

        async def acquire_control(self) -> None:
            raise RuntimeError("旁路进程没起来")

    async def 假睡(_s: float) -> None:
        return None

    got = await grab_control(会炸的(), tries=2, sleep=假睡)
    assert got.ok is False
    assert "旁路进程没起来" in got.detail


async def test_SN变了就回滚(tmp_path):
    """升级前后 SN 必须是同一只狗。不是的话,这台机器上跑的不是我们以为的东西。"""

    class 桩nav:
        async def nav_status(self):
            return object()

        async def loc_status(self):
            return object()

        async def get_map_grid(self, map_id: str):
            return {}

        async def list_maps(self):
            return ["m1"]

    async def 假睡(_s: float) -> None:
        return None

    results = await run_postcheck(桩nav(), _假设备(1), want_sn="D1M-0007",
                                  got_sn="D1M-0008", sleep=假睡)
    assert postcheck_verdict(results) is Verdict.ROLLBACK
    assert next(c for c in results if c.name == "identity").ok is False


# --------------------------------------------------------------- 重启方式


def test_没上装就只重启服务():
    plan = restart_plan(has_payload=False)
    assert plan.kind == "service"
    assert plan.argv == ("systemctl", "restart", SERVICE_UNIT)


def test_有上装就整机重启():
    plan = restart_plan(has_payload=True)
    assert plan.kind == "machine"
    assert plan.argv == ("systemctl", "reboot")


def test_重启单元名可以换():
    plan = restart_plan(has_payload=False, unit="别的单元")
    assert plan.argv == ("systemctl", "restart", "别的单元")


def test_重启方案能上线():
    wire = RestartPlan("service", ("systemctl", "restart", SERVICE_UNIT),
                       "理由").to_wire()
    assert wire == {"kind": "service",
                     "argv": ["systemctl", "restart", SERVICE_UNIT],
                     "why": "理由"}
