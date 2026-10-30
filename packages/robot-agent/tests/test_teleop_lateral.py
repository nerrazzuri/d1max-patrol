"""遥控横移(2026-10-30,手机左杆左右):HAL 报能横移才走、限速是能力的一半、前后和横移只走大的那一个、
狗尾为前时左右也反;有硬禁区时横移另过守卫(位置不可信不横移、往侧面扫过去会更靠近硬禁区就停)。"""

from __future__ import annotations

import math

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.events import EventBook
from d1max_agent.localization import LocEstimate
from d1max_agent.tasks.teleop import TeleopTask
from d1max_agent.zone_guard import TeleopZoneGuard, footprint, overlap_area
from d1max_contract.teleop import TeleopFrame
from d1max_contract.zones import Zone

#: 硬禁区:x ≥ 3 的一大片(「池子」)。
POOL = ((3.0, -10.0), (10.0, -10.0), (10.0, 10.0), (3.0, 10.0))


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000

    def __call__(self) -> int:
        return self.ms


class 位置:
    def __init__(self) -> None:
        self.trusted = True

    def __call__(self, odom):
        est = LocEstimate("m", "1", odom[0], odom[1], odom[2], 0.05, 0.05, "test")
        return est, self.trusted, "" if self.trusted else "定位器 3 秒没来位姿"


class 台:
    def __init__(self, tmp_path, *, lateral=True, zones=(), yaw=0.0, head="head") -> None:
        self.c = 钟()
        self.r = SimRobot(now_ms=self.c, max_vx=1.0, max_wz=1.5, deadband_vx=0.05,
                          stop_latency_s=0.2, lateral=lateral, max_vy=0.6)
        self.r.inject_head(head)
        self.r.teleport(0.0, 0.0, yaw)
        self.zones = list(zones)
        self.loc = 位置()
        guard = (TeleopZoneGuard(zones=lambda: self.zones, locate=self.loc, deadband_vx=0.05)
                 if zones else None)
        self.t = TeleopTask(task_id="teleop-1", lease_epoch=1, operator="gina",
                            lease_ttl_ms=600_000, hal=self.r, now_ms=self.c,
                            video_live=lambda: True,
                            events=EventBook(tmp_path / "ev.jsonl", boot_id="b", now_ms=self.c),
                            zone_guard=guard)
        self.seq = 0
        self.overlap = 0.0

    async def start(self):
        await self.r.connect()
        await self.r.acquire_control()
        await self.t.start()
        return self

    async def 开(self, vx=0.0, wz=0.0, vy=0.0, secs=1.0, dt=0.1):
        for _ in range(int(round(secs / dt))):
            self.seq += 1
            self.t.on_frame(TeleopFrame(lease_epoch=1, seq=self.seq, sent_at=self.c.ms - 30,
                                        ttl_ms=300, vx=vx, wz=wz, vy=vy), rx_ms=self.c.ms)
            await self.t.step(dt)
            self.r.tick(dt)
            self.c.ms += int(dt * 1000)
            for z in self.zones:
                self.overlap = max(self.overlap, overlap_area(
                    footprint(self.r.x, self.r.y, self.r.yaw), z.polygon))


async def test_能横移的狗_左杆往左推_往机身左边走_限速一半(tmp_path):
    s = await 台(tmp_path).start()
    await s.开(vy=5.0, secs=1.0)
    assert s.r.y > 0.2, "机身左边是 +y(朝向 0)"
    assert abs(s.r.x) < 1e-6, "横移不往前走"
    assert abs(s.r.yaw) < 1e-9
    assert s.r.y <= 0.3 + 1e-6, "限速 = 能力 0.6 的一半"


async def test_不能横移的狗_横移帧当零(tmp_path):
    s = await 台(tmp_path, lateral=False).start()
    await s.开(vy=0.3, secs=1.0)
    assert (s.r.x, s.r.y) == (0.0, 0.0)


async def test_前后和横移同时给_只走大的那一个(tmp_path):
    s = await 台(tmp_path).start()
    await s.开(vx=0.4, vy=0.1, secs=1.0)
    assert s.r.x > 0.2 and abs(s.r.y) < 1e-6
    s.r.teleport(0.0, 0.0, 0.0)
    await s.开(vx=0.1, vy=-0.25, secs=1.0)
    assert s.r.y < -0.15 and abs(s.r.x) < 1e-6


async def test_狗尾为前_左右也反(tmp_path):
    s = await 台(tmp_path, head="tail").start()
    await s.开(vy=0.3, secs=1.0)
    assert s.r.y < -0.2, "人眼里的左边是机身右边"


async def test_横着往硬禁区挪_机身压不进去(tmp_path):
    # 朝向 +y(π/2):机身右边朝 +x,也就是朝池子
    s = await 台(tmp_path, zones=[Zone("pool", "nogo", POOL, "池子", hard=True)],
                yaw=math.pi / 2).start()
    await s.开(vy=-0.3, secs=20.0)
    assert s.overlap == 0.0, "机身一下都没压进去"
    assert s.r.x > 2.0, "能挪到跟前"
    # 往外(往左)照走
    x0 = s.r.x
    await s.开(vy=0.3, secs=1.0)
    assert s.r.x < x0 - 0.1


async def test_位置不可信_有硬禁区就不横移(tmp_path):
    s = await 台(tmp_path, zones=[Zone("pool", "nogo", POOL, "池子", hard=True)]).start()
    s.loc.trusted = False
    await s.开(vy=0.3, secs=1.0)
    assert s.r.y == 0.0


async def test_普通禁行区不管横移(tmp_path):
    s = await 台(tmp_path, zones=[Zone("bed", "nogo", POOL, "高楼楼梯", hard=False)],
                yaw=math.pi / 2).start()
    await s.开(vy=-0.3, secs=20.0)
    assert s.r.x > 3.0, "人开的可以进高楼楼梯"


def test_遥控能力_能横移才报max_vy_是一半():
    from d1max_agent.status import compose_capabilities

    def caps(lateral):
        r = SimRobot(now_ms=钟(), lateral=lateral, max_vy=0.6)
        return compose_capabilities(robot_id="A", hal_caps=r.hal_capabilities(),
                                    adapter_id="sim", loaded_map=None, nav_path="planned",
                                    autonomy="autonomous").tasks["teleop"]
    assert math.isclose(caps(True)["max_vy"], 0.3)
    assert "max_vy" not in caps(False)
