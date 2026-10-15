"""换图时规划栅格只装当前这一次换图的(外审 W39-I1,2026-10-15)。

原来每次换图起一个线程读栅格、线程里直接改导航后端的 ``_base``;旧的线程晚回来,
会把**已经不是现在这张**
图的栅格装上(能力里报的图号是新的、实际规划用的是旧的)。R5 定位器不收新图退回原图时,同一趟换图里就会
碰到:新图的线程还没完、退回先完成,新图的线程回来又盖掉。

现在读、算放在线程里(不碰后端),装在事件循环里,而且只装**换图代次还是自己**的结果;旧的成功、旧的失败
都不改当前的规划器。验的是后端实际装着的栅格(原点),不只是能力里的图号。
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from test_runtime_zones import 地面

from d1max_agent.bridges import planned_nav as pn


def _图(tmp_path, version, origin):
    d = tmp_path / version
    d.mkdir()
    files = 地面()
    files["floor.yaml"] = files["floor.yaml"].replace(
        b"[0.0, 0.0, 0.0]", f"[{origin}.0, 0.0, 0.0]".encode())
    for name, data in files.items():
        (d / name).write_bytes(data)


@pytest.fixture
def 台(tmp_path, monkeypatch):
    """真 ``AgentRuntime._switch_map``/``_load_grid``、真 ``PlannedNavBackend``;
    ``hold`` 里的版本读栅格时卡住,
    ``fail`` 里的版本读栅格时出错,直到放行。"""
    from d1max_adapter_sim.robot import SimRobot
    from d1max_agent.runtime import AgentRuntime
    for version, origin in (("3", 30), ("4", 0), ("5", 50)):
        _图(tmp_path, version, origin)
    nav = pn.PlannedNavBackend(SimRobot(now_ms=lambda: 0), now_ms=lambda: 0, map_id="m")
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.parts = SimpleNamespace(nav=nav, switch_map=lambda *a, **k: None)
    rt.processor = SimpleNamespace()
    rt.maps = SimpleNamespace(dir_of=lambda ref: tmp_path / ref.version, home_of=lambda ref: None)
    rt.homes = SimpleNamespace(get=lambda *a: None)
    rt.zonebook = SimpleNamespace(get=lambda *a: None)
    rt._rtk_check = SimpleNamespace(on_map=lambda *a: None)
    rt._load_geo = lambda *a: None
    rt._supported = lambda: set()
    rt._now = lambda: 0
    rt.transport = SimpleNamespace(connected=False)
    gates: dict[str, threading.Event] = {}
    started: dict[str, threading.Event] = {}
    fail: set[str] = set()
    real = pn.cmod.load_floor

    def slow(path):
        v = path.name
        if v in gates:
            started[v].set()
            if not gates[v].wait(5):
                raise RuntimeError("测试放行超时")
        if v in fail:
            raise OSError(f"读不了 {v}")
        return real(path)
    monkeypatch.setattr(pn.cmod, "load_floor", slow)

    def hold(v):
        gates[v], started[v] = threading.Event(), threading.Event()

    yield SimpleNamespace(rt=rt, nav=nav, hold=hold, gates=gates, started=started, fail=fail)
    for g in gates.values():
        g.set()
    nav.close_planner()


def _换(rt, version):
    rt._switch_map(SimpleNamespace(map_id="m", version=version))
    return rt._grid_job


async def _等开始(t, v):
    for _ in range(200):
        if t.started[v].is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{v} 没开始读")


def _原点(nav):
    assert nav._base is not None, nav.plan_problem
    return nav._base[2][0]


async def test_旧图晚成功_不盖掉当前的(台):
    台.hold("3")
    old = _换(台.rt, "3")
    await _等开始(台, "3")
    await _换(台.rt, "4")
    assert 台.rt.loaded_map == ("m", "4") and _原点(台.nav) == 0.0
    台.gates["3"].set()
    await old
    assert 台.rt.loaded_map == ("m", "4")
    assert _原点(台.nav) == 0.0, "晚到的 v3 栅格(原点 30)不能装上"
    assert 台.nav.plan_ok


async def test_旧图晚失败_不清掉当前的(台):
    台.hold("3")
    台.fail.add("3")
    old = _换(台.rt, "3")
    await _等开始(台, "3")
    await _换(台.rt, "4")
    台.gates["3"].set()
    await old
    assert _原点(台.nav) == 0.0 and 台.nav.plan_ok, "旧图读失败不能把当前的规划器清掉"
    assert 台.nav.plan_problem is None


async def test_退回原图_新图的线程后回来也不盖(台):
    """R5:换到 v4,定位器不收,退回 v3;v4 的栅格线程比退回晚完成。"""
    await _换(台.rt, "3")
    台.hold("4")
    new = _换(台.rt, "4")
    await _等开始(台, "4")
    await _换(台.rt, "3")                                     # 退回
    assert _原点(台.nav) == 30.0
    台.gates["4"].set()
    await new
    assert 台.rt.loaded_map == ("m", "3")
    assert _原点(台.nav) == 30.0, "退回以后,新图(v4)的栅格晚到不能装上"


async def test_连换三次_只装最后一次的(台):
    台.hold("3")
    台.hold("4")
    j3 = _换(台.rt, "3")
    await _等开始(台, "3")
    j4 = _换(台.rt, "4")
    await _等开始(台, "4")
    await _换(台.rt, "5")
    assert _原点(台.nav) == 50.0
    台.gates["4"].set()
    await j4
    台.gates["3"].set()
    await j3
    assert 台.rt.loaded_map == ("m", "5") and _原点(台.nav) == 50.0


async def test_当前这次读失败_照旧记原因不能规划(台):
    await _换(台.rt, "3")
    台.fail.add("4")
    await _换(台.rt, "4")
    assert not 台.nav.plan_ok
    assert "读不了 4" in (台.nav.plan_problem or "")


async def test_换图期间_还没装好时不能规划(台):
    台.hold("4")
    await _换(台.rt, "3")
    job = _换(台.rt, "4")
    await _等开始(台, "4")
    assert not 台.nav.plan_ok, "新图的栅格没装好之前不能拿旧图的规划"
    台.gates["4"].set()
    await job
    assert _原点(台.nav) == 0.0
