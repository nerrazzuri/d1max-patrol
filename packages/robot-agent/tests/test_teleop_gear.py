"""遥控按速度分档 + 自动换 SDK 档位(POC 第 2 项改版,用户 11-05)。手机每帧带选中的速度 ``vmax``;
超过低速档最快就要中速档 —— **只在摇杆回中时换**,没回中丢帧 ``gear_wait``,换档期间丢
``gear_switching``;
中速档每秒续一次(旁路 3 s 没续回低速);结束回低速。机器人不能换档就按低速档最快夹。"""

from __future__ import annotations

import pytest
from test_teleop_task import _走, 钟

from nexwar_adapter_sim.robot import SimRobot
from nexwar_agent.events import EventBook
from nexwar_agent.status import compose_capabilities
from nexwar_agent.tasks.teleop import TeleopTask
from nexwar_contract.errors import ContractError
from nexwar_contract.teleop import TeleopFrame

_n = {"seq": 0}


def _帧(c, vx=0.0, vmax=None, wz=0.0, vy=0.0):
    _n["seq"] += 1
    return TeleopFrame(lease_epoch=2, seq=_n["seq"], sent_at=c.ms - 30, ttl_ms=300, vx=vx,
                       wz=wz, vy=vy, vmax=vmax)


async def _台(tmp_path, *, mid=2.0, lease=2.0):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, deadband_vx=0.05, stop_latency_s=0.2,
                 max_vx_mid=mid, lateral=True)
    await r.connect()
    await r.acquire_control()
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b", now_ms=c)
    t = TeleopTask(task_id="teleop-2", lease_epoch=2, operator="gina", lease_ttl_ms=60_000, hal=r,
                   now_ms=c, video_live=lambda: True, events=book, max_vx_mps=lease)
    await t.start()
    return c, r, t


async def test_选1点6_松杆时换中速_换好跑到1点6_横移关掉(tmp_path):
    c, r, t = await _台(tmp_path)
    await _走(c, r, t, 5, frame=lambda: _帧(c, vmax=1.6))          # 回中:换档
    assert r.teleop_gear() == 2
    await _走(c, r, t, 20, frame=lambda: _帧(c, vx=1.6, vmax=1.6, vy=0.3))
    o = await r.odometry()
    assert 1.5 <= o.vx <= 1.6 + 1e-9 and r._cmd_vy == 0.0, (o, r._cmd_vy)


async def test_推着杆换档_丢帧等松杆_松了才换(tmp_path):
    c, r, t = await _台(tmp_path)
    await _走(c, r, t, 10, frame=lambda: _帧(c, vx=0.6, vmax=0.6))
    assert 0.5 < (await r.odometry()).vx <= 0.6 + 1e-9
    await _走(c, r, t, 10, frame=lambda: _帧(c, vx=1.6, vmax=1.6))
    assert r.teleop_gear() == 1 and t.drops_note()["last"] == "gear_wait"
    assert abs((await r.odometry()).vx) < 0.05, "没松杆:停下等,不按旧档接着跑"
    await _走(c, r, t, 5, frame=lambda: _帧(c, vmax=1.6))
    assert r.teleop_gear() == 2


async def test_换档期间推杆_丢帧gear_switching(tmp_path):
    c, r, t = await _台(tmp_path)
    await _走(c, r, t, 1, frame=lambda: _帧(c, vmax=1.6))           # 刚要了中速,还没报上来
    await _走(c, r, t, 1, frame=lambda: _帧(c, vx=1.6, vmax=1.6))
    assert t.drops_note()["last"] == "gear_switching"


async def test_中速档每秒续一次_结束回低速(tmp_path):
    c, r, t = await _台(tmp_path)
    await _走(c, r, t, 50, frame=lambda: _帧(c, vmax=2.0))
    assert r.teleop_gear() == 2 and r.gear_requests.count(2) >= 4, r.gear_requests
    t.release()
    await _走(c, r, t, 20)
    assert t.done and r.gear_requests[-1] == 1 and r.teleop_gear() == 1


async def test_选回1点0_松杆时回低速(tmp_path):
    c, r, t = await _台(tmp_path)
    await _走(c, r, t, 5, frame=lambda: _帧(c, vmax=1.6))
    await _走(c, r, t, 5, frame=lambda: _帧(c, vmax=1.0))
    assert r.teleop_gear() == 1


async def test_机器人不能换档_按低速档最快夹_不丢帧(tmp_path):
    c, r, t = await _台(tmp_path, mid=0.0)
    await _走(c, r, t, 15, frame=lambda: _帧(c, vx=1.6, vmax=1.6))
    assert r.gear_requests == [] and t.drops_note() is None
    assert 0.9 <= (await r.odometry()).vx <= 1.0 + 1e-9


async def test_帧里的vmax也夹(tmp_path):
    c, r, t = await _台(tmp_path)
    await _走(c, r, t, 15, frame=lambda: _帧(c, vx=1.0, vmax=0.6))
    assert (await r.odometry()).vx <= 0.6 + 1e-9


def test_帧的vmax_不带照旧_不对的拒():
    f = TeleopFrame(lease_epoch=1, seq=1, sent_at=0, ttl_ms=300, vx=0.0, wz=0.0, vmax=1.3)
    assert TeleopFrame.from_wire(f.to_wire()).vmax == 1.3
    g = TeleopFrame(lease_epoch=1, seq=1, sent_at=0, ttl_ms=300, vx=0.0, wz=0.0)
    assert "vmax" not in g.to_wire() and TeleopFrame.from_wire(g.to_wire()).vmax is None
    for bad in (0, -1, 2.5, True, "1"):
        with pytest.raises(ContractError):
            TeleopFrame.from_wire({**g.to_wire(), "vmax": bad})


def test_能力里报遥控最快():
    r = SimRobot(now_ms=lambda: 0, max_vx=1.0, max_vx_mid=2.0)
    caps = compose_capabilities(robot_id="r", adapter_id="sim", hal_caps=r.hal_capabilities(),
                                loaded_map=None, nav_path="builtin", autonomy="full")
    assert caps.tasks["teleop"]["max_vx_teleop"] == 2.0
    r1 = SimRobot(now_ms=lambda: 0, max_vx=1.0)
    caps1 = compose_capabilities(robot_id="r", adapter_id="sim", hal_caps=r1.hal_capabilities(),
                                 loaded_map=None, nav_path="builtin", autonomy="full")
    assert "max_vx_teleop" not in caps1.tasks["teleop"]
