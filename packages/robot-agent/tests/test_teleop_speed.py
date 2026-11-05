"""POC 第 2 项:遥控速度档。站点在授予租约时带 ``max_vx_mps``(= 快档),代理按 min(机身能力, 它) 限速,
转向、横移同比例;没带(老站点)照旧是能力的一半。能力里报机身上限,站点据此算档。"""

from __future__ import annotations

import pytest
from test_teleop_task import _帧, _走, 钟

from nexwar_adapter_sim.robot import SimRobot
from nexwar_agent.events import EventBook
from nexwar_agent.status import compose_capabilities
from nexwar_agent.tasks.teleop import TeleopTask
from nexwar_contract.errors import ContractError
from nexwar_contract.teleop import parse_teleop_speed, teleop_grant_payload


async def _台(tmp_path, max_vx_mps):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.5, max_wz=1.5, deadband_vx=0.05, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b", now_ms=c)
    t = TeleopTask(task_id="teleop-2", lease_epoch=2, operator="gina", lease_ttl_ms=5000, hal=r,
                   now_ms=c, video_live=lambda: True, events=book, max_vx_mps=max_vx_mps)
    await t.start()
    return c, r, t


async def test_租约带了快档_按它限速_转向同比例(tmp_path):
    c, r, t = await _台(tmp_path, 1.0)
    await _走(c, r, t, 10, frame=lambda: _帧(c, vx=3.0, wz=-9.0))
    o = await r.odometry()
    assert 0.95 <= o.vx <= 1.0 + 1e-9, o.vx          # 快档 1.0,不是机身 1.5 的一半 0.75
    assert abs(o.wz) <= 1.0 + 1e-9                    # 1.5 × 1.0/1.5


async def test_快档比机身还快_按机身(tmp_path):
    c, r, t = await _台(tmp_path, 3.0)
    await _走(c, r, t, 10, frame=lambda: _帧(c, vx=9.0))
    assert (await r.odometry()).vx <= 1.5 + 1e-9


async def test_老站点没带_照旧一半(tmp_path):
    c, r, t = await _台(tmp_path, None)
    await _走(c, r, t, 10, frame=lambda: _帧(c, vx=3.0))
    assert (await r.odometry()).vx <= 0.75 + 1e-9


def test_载荷_带不带都认_不对的拒():
    p = teleop_grant_payload(lease_epoch=1, operator="g", lease_ttl_ms=2000, max_vx_mps=1.0)
    assert parse_teleop_speed(p) == 1.0
    assert parse_teleop_speed(teleop_grant_payload(lease_epoch=1, operator="g",
                                                   lease_ttl_ms=2000)) is None
    for bad in (0, -1, 3.5, "1", True, float("nan")):
        with pytest.raises(ContractError):
            parse_teleop_speed({"max_vx_mps": bad})


def test_能力里报机身上限_站点据此算档():
    r = SimRobot(now_ms=lambda: 0, max_vx=1.5, max_wz=1.2)
    caps = compose_capabilities(robot_id="r", adapter_id="sim", hal_caps=r.hal_capabilities(),
                                loaded_map=None, nav_path="builtin", autonomy="full")
    assert caps.tasks["teleop"]["max_vx"] == 0.75, "老站点照旧看这个"
    assert caps.tasks["teleop"]["max_vx_hal"] == 1.5
    assert caps.tasks["teleop"]["max_wz_hal"] == 1.2
