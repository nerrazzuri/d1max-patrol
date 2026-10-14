"""决策 65(2026-10-14):遥控守「硬禁区」(水体、落差)。仿真狗带链路延迟 0.2 s、刹车减速度 0.5 m/s²
(跟守卫用的占位数一样),每拍量机身矩形离硬禁区多远 —— 不是只看停在哪。

- 朝硬禁区开:几种速度、几种定位 σ,机身一下都没压进去,而且停得不太远(能开到跟前);
- 往外开、平着开照发;在边上、在里面的狗能开出来;转身会把角甩进去的不许转;
- 普通禁行区不管(人开的可以进花坛);没有区域、没有硬禁区照常;
- 定位不可信:图上有硬禁区就全程限速,没有就照常;
- 狗尾为前的会话:人眼里的「往前」是机身往后,照样守。
"""

from __future__ import annotations

import math

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.events import EventBook
from d1max_agent.localization import LocEstimate
from d1max_agent.tasks.teleop import TeleopTask
from d1max_agent.zone_guard import (
    BODY_HL,
    UNTRUSTED_VX,
    TeleopZoneGuard,
    footprint,
    overlap_area,
    rect_poly_distance,
)
from d1max_contract.teleop import TeleopFrame
from d1max_contract.zones import Zone

#: 硬禁区:x ≥ 3 的一大片(「池子」)。
POOL = ((3.0, -10.0), (10.0, -10.0), (10.0, 10.0), (3.0, 10.0))


def _zone(poly=POOL, hard=True, zid="pool"):
    return Zone(zid, "nogo", tuple(poly), "池子", hard=hard)


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000

    def __call__(self) -> int:
        return self.ms


class 位置:
    """定位:仿真里程就是地图位姿;σ、可不可信可调。"""

    def __init__(self) -> None:
        self.sigma = 0.0
        self.trusted = True

    def __call__(self, odom):
        est = LocEstimate("m", "1", odom[0], odom[1], odom[2], self.sigma, 0.05, "test")
        return est, self.trusted, "" if self.trusted else "定位器 3 秒没来位姿"


class 台:
    def __init__(self, tmp_path, zones, *, x=0.0, y=0.0, yaw=0.0, head="head") -> None:
        self.c = 钟()
        self.r = SimRobot(now_ms=self.c, max_vx=1.0, max_wz=1.5, deadband_vx=0.05,
                          stop_latency_s=0.2, latency_s=0.2, max_decel=0.5)
        self.r.inject_head(head)
        self.r.teleport(x, y, yaw)
        self.zones = list(zones)
        self.loc = 位置()
        self.guard = TeleopZoneGuard(zones=lambda: self.zones, locate=self.loc, deadband_vx=0.05)
        self.t = TeleopTask(task_id="teleop-1", lease_epoch=1, operator="gina",
                            lease_ttl_ms=600_000, hal=self.r, now_ms=self.c,
                            video_live=lambda: True,
                            events=EventBook(tmp_path / "ev.jsonl", boot_id="b", now_ms=self.c),
                            zone_guard=self.guard)
        self.seq = 0
        self.worst = math.inf             # 机身离硬禁区最近的时候(米;0 = 压上了)
        self.max_v = 0.0

    async def start(self):
        await self.r.connect()
        await self.r.acquire_control()
        await self.t.start()

    async def 开(self, vx=0.0, wz=0.0, secs=1.0, dt=0.1):
        for _ in range(int(round(secs / dt))):
            self.seq += 1
            self.t.on_frame(TeleopFrame(lease_epoch=1, seq=self.seq, sent_at=self.c.ms - 30,
                                        ttl_ms=300, vx=vx, wz=wz), rx_ms=self.c.ms)
            await self.t.step(dt)
            for _ in range(5):
                self.r.tick(dt / 5)
                self.c.ms += int(dt * 200)
                self._量()

    def _量(self):
        o = self.r
        rect = footprint(o.x, o.y, o.yaw)
        for z in self.zones:
            if z.hard:
                self.worst = min(self.worst, rect_poly_distance(rect, z.polygon))
        self.max_v = max(self.max_v, abs(self.r.speed[0]))


@pytest.mark.parametrize("sigma", [0.0, 0.2, 0.4])
@pytest.mark.parametrize("vx", [0.5, 0.3, 0.1])
async def test_朝硬禁区开_几种速度几种σ_机身一下都没进去_能开到跟前(tmp_path, vx, sigma):
    s = 台(tmp_path, [_zone()])
    s.loc.sigma = sigma
    await s.start()
    await s.开(vx=vx, secs=25.0)
    assert s.worst > 0.0, f"机身压进了硬禁区(离 {s.worst:.3f} m)"
    assert s.worst >= sigma, f"σ {sigma}:离池边只剩 {s.worst:.3f} m(定位偏 σ 就进去了)"
    nose = s.r.x + BODY_HL
    assert 3.0 - nose <= sigma + 0.5, f"停得太远:机头离池边 {3.0 - nose:.2f} m"
    assert s.guard.last.state in ("blocked", "inside"), s.guard.last
    note = s.t.zone_note()
    assert note["state"] in ("blocked", "inside") and note["zone"] == "pool"
    assert note["label"] == "池子"


def test_快了刹不住就先减速_减了还不行再只许转():
    """机头离池边 0.3 m、σ 0:0.5 m/s 再走一拍、再刹停要 0.4 m,不行;一半 0.25 m/s 只要 0.14 m,行 ——
    先慢下来(limited),不是一下截成零。"""
    loc = 位置()
    g = TeleopZoneGuard(zones=lambda: [_zone()], locate=loc, deadband_vx=0.05)
    x = 3.0 - BODY_HL - 0.3
    v = g.check(0.5, 0.0, (x, 0.0, 0.0))
    assert v.state == "limited" and v.vx == 0.25 and v.zone.id == "pool", v
    v = g.check(0.5, 0.3, (3.0 - BODY_HL - 0.04, 0.0, 0.0))
    assert v.state == "blocked" and v.vx == 0.0, v
    assert g.check(-0.5, 0.0, (x, 0.0, 0.0)).state == "ok", "往后退不管"
    # 斜对着池子、右前角离池边 3 cm:往前(再慢也)更近,往左转是把机头转开 —— 截掉往前、留着转
    yaw = 0.6
    corner = 0.465 * math.cos(yaw) + 0.24 * math.sin(yaw)
    v = g.check(0.5, 0.75, (3.0 - 0.03 - corner, 0.0, yaw))
    assert v.state == "blocked" and v.vx == 0.0 and v.wz == 0.75, v


async def test_离远了照常_不减速(tmp_path):
    s = 台(tmp_path, [_zone()], x=-10.0)
    await s.start()
    await s.开(vx=0.5, secs=3.0)
    assert s.max_v >= 0.5 - 1e-6 and s.guard.last.state == "ok"


async def test_往外开_平着开照发_转身甩进去的不许转(tmp_path):
    # 机头离池边 0.12 m(σ 0.0 + 余量 0.05 以外):往前不行,往后行,侧着平行开行
    s = 台(tmp_path, [_zone()], x=3.0 - BODY_HL - 0.12)
    await s.start()
    x0 = s.r.x
    await s.开(vx=0.5, secs=2.0)
    assert s.r.x - x0 < 0.12 and s.worst > 0.0
    await s.开(vx=-0.4, secs=2.0)
    assert s.r.x < x0 - 0.3, "往外开照发"
    # 机身侧着贴在池边(侧边离 0.1 m):顺着池边往前开行;原地转会把角甩进去,不许
    s2 = 台(tmp_path, [_zone()], x=3.0 - 0.24 - 0.10, yaw=math.pi / 2)
    await s2.start()
    y0 = s2.r.y
    await s2.开(vx=0.4, secs=2.0)
    assert s2.r.y - y0 > 0.5, "顺着池边开照发"
    yaw1 = s2.r.yaw
    await s2.开(wz=0.7, secs=2.0)
    assert abs(math.remainder(s2.r.yaw - yaw1, 2 * math.pi)) < 0.2, "原地转会把机身角甩进池子"
    assert s2.worst > 0.0 and s2.guard.last.state == "blocked"


async def test_压在池边上_只许往外_能开出来(tmp_path):
    # 机头已经伸进池子 0.2 m(定位漂了、被人推的):往前不许,往后退能开出来
    s = 台(tmp_path, [_zone()], x=3.0 - BODY_HL + 0.2)
    await s.start()
    a0 = overlap_area(footprint(s.r.x, s.r.y, s.r.yaw), POOL)
    assert a0 > 0
    x0 = s.r.x
    await s.开(vx=0.4, secs=1.0)
    assert s.r.x <= x0 + 1e-6, "往里不许"
    assert s.guard.last.state == "blocked"
    await s.开(vx=-0.3, secs=3.0)
    assert s.r.x + BODY_HL < 3.0, "退出来了"


async def test_斜着压在池边上_越转压得越多的不许_转开的行(tmp_path):
    """机身斜 46°、右前角伸进池子 10 cm:往右转(朝池子摆正)整条前沿都会压进去,不许;
    往左转角往外摆,行。
    狗身中心转的时候不动 —— 只看中心判不出来,按压进去的面积。"""
    s = 台(tmp_path, [_zone()], x=2.6, yaw=0.8)
    await s.start()
    a0 = overlap_area(footprint(s.r.x, s.r.y, s.r.yaw), POOL)
    assert a0 > 0
    await s.开(wz=-0.6, secs=1.0)
    assert abs(s.r.yaw - 0.8) < 1e-6, "往池子那边转:不许"
    await s.开(wz=0.6, secs=1.0)
    assert s.r.yaw > 0.9 and overlap_area(footprint(s.r.x, s.r.y, s.r.yaw), POOL) < a0


async def test_整个在硬禁区里面_往最近的边开得出来_往里开不行(tmp_path):
    big = ((-1.0, -5.0), (20.0, -5.0), (20.0, 5.0), (-1.0, 5.0))    # 狗在里面,左边 1 m 是边
    s = 台(tmp_path, [_zone(big)], x=0.5, yaw=0.0)
    await s.start()
    await s.开(vx=0.4, secs=1.0)
    assert abs(s.r.x - 0.5) < 1e-6, "往里开不行"
    await s.开(vx=-0.4, secs=6.0)
    assert s.r.x + BODY_HL < -1.0, f"往最近的边退、整个出来了({s.r.x:.2f})"


async def test_普通禁行区不管_没有区域照常(tmp_path):
    s = 台(tmp_path, [_zone(hard=False)])
    await s.start()
    await s.开(vx=0.5, secs=10.0)
    assert s.r.x > 3.5, "花坛那类人开的可以进"
    assert s.guard.last.state == "ok" and s.t.zone_note() == {"state": "ok"}
    s2 = 台(tmp_path, [])
    await s2.start()
    await s2.开(vx=0.5, secs=3.0)
    assert s2.max_v >= 0.5 - 1e-6


async def test_定位不可信_有硬禁区全程限速_没有照常(tmp_path):
    s = 台(tmp_path, [_zone()], x=-20.0)
    s.loc.trusted = False
    await s.start()
    await s.开(vx=0.5, wz=0.0, secs=3.0)
    assert 0.1 < s.max_v <= UNTRUSTED_VX + 1e-6, s.max_v
    note = s.t.zone_note()
    assert note["state"] == "untrusted" and note["max_vx"] == UNTRUSTED_VX
    assert "不可信" in note["reason"]
    s2 = 台(tmp_path, [_zone(hard=False)], x=-20.0)
    s2.loc.trusted = False
    await s2.start()
    await s2.开(vx=0.5, secs=3.0)
    assert s2.max_v >= 0.5 - 1e-6, "图上没有硬禁区:不可信也照常"


async def test_狗尾为前的会话_人眼往前是机身往后_照样守(tmp_path):
    behind = ((-10.0, -10.0), (-3.0, -10.0), (-3.0, 10.0), (-10.0, 10.0))
    s = 台(tmp_path, [_zone(behind)], head="tail")
    await s.start()
    await s.开(vx=0.4, secs=20.0)                          # 人推「往前」= 机身往后,冲着池子
    assert s.r.x < -1.0, "确实往狗尾那头走了"
    assert s.worst > 0.0 and s.r.x - BODY_HL > -3.0


async def test_守卫出错_当零速(tmp_path):
    s = 台(tmp_path, [_zone()], x=-20.0)
    await s.start()

    def 坏(odom):
        raise RuntimeError("定位炸了")
    s.guard._locate = 坏
    await s.开(vx=0.5, secs=1.0)
    assert s.max_v == 0.0
