"""本机定位桥,定位器这一头(W09b):跟代理那一头(W09a 的 ``LocBridgeServer`` + ``BridgeLocalizer``)
走真的 Unix 套接字对上。MOLA 用假后端代替(收下就调核心)。"""

from __future__ import annotations

import asyncio
import math
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from d1max_localizer.client import BridgeClient
from d1max_localizer.core import Estimate, LocalizerCore
from d1max_localizer.frames import Frames, mat_to_quat

from d1max_agent.bridge_localizer import SETTLE_FIXES, BridgeLocalizer
from d1max_agent.locbridge import LocBridgeServer

FLAT = Frames(up=(0.0, 0.0, 1.0), sensor_up=(0.0, 0.0, 1.0), sensor_forward=(1.0, 0.0, 0.0),
              sensor_height=0.0)
M = ("estate-1", "7")


class 假MOLA:
    """收下就办:换先验立刻「载好」,重定位立刻「收下」。``refuse`` 给了就拒。"""

    def __init__(self, core):
        self.core = core
        self.priors, self.relocs = [], []
        self.refuse = ""
        self.missing = set()

    async def load_prior(self, map_ref, dir):
        self.priors.append((map_ref, dir))
        if dir in self.missing:
            return f"{dir} 里没有先验"
        self.core.prior_loading(map_ref)
        self.core.prior_loaded(map_ref, FLAT)
        self.core.backend_started()
        return ""

    async def relocalize(self, x, y, yaw, sigma, *, req=None, human=True):
        self.relocs.append((x, y, yaw, sigma))
        if not self.refuse:
            self.core.relocalized(req=req, x=x, y=y, yaw=yaw, sigma=sigma, human=human)
        return self.refuse


def _est(x, y, yaw=0.0, q=0.95):
    c, s = math.cos(yaw), math.sin(yaw)
    return Estimate(stamp=time.time(), p=(x, y, 0.0),
                    q=mat_to_quat(((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))), quality=q)


class 台子:
    async def 起(self):
        self.d = Path(tempfile.mkdtemp(prefix="lc", dir="/tmp"))
        self.agent = BridgeLocalizer(monotonic=time.monotonic)
        self.srv = LocBridgeServer(self.d / "loc.sock", self.agent)
        self.agent.link = self.srv
        self.agent.on_map(M, "/maps/estate-1/7")
        await self.srv.start()
        self.core = LocalizerCore(monotonic=time.monotonic)
        self.mola = 假MOLA(self.core)
        self.cli = BridgeClient(self.d / "loc.sock", self.core, self.mola, reconnect_s=0.1,
                                hb_every_s=0.2)
        self.task = asyncio.get_running_loop().create_task(self.cli.run())
        self.odom = (0.0, 0.0, 0.0)
        return self

    async def 帧(self, x, y, yaw=0.0, n=1):
        for _ in range(n):
            self.agent.update(self.odom, True)
            self.core.on_scan()
            self.core.on_estimate(_est(x, y, yaw))
            self.cli.flush()
            await asyncio.sleep(0.02)

    async def 收(self):
        self.cli.stop()
        await asyncio.wait_for(self.task, 2.0)
        await self.srv.close()
        shutil.rmtree(self.d, ignore_errors=True)


async def _等(cond, n=300):
    for _ in range(n):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到")


@pytest.fixture
async def 台():
    t = await 台子().起()
    yield t
    await t.收()


async def test_连上_代理请换先验_等人给初始位置(台):
    t = 台
    await _等(lambda: t.srv.connected and t.agent.prior_task is not None)
    await t.agent.prior_task
    assert t.mola.priors == [(M, "/maps/estate-1/7")]
    await _等(lambda: "等人给初始位置" in t.agent.why_not(True))


async def test_人给位置_定位器收下_出帧_代理稳下来就信(台):
    t = 台
    await _等(lambda: t.srv.connected and t.agent.prior_task is not None)
    await t.agent.prior_task
    await _等(lambda: "等人给初始位置" in t.agent.why_not(True))
    assert await t.agent.relocalize(M, (1.0, 2.0, 0.3)) == ""
    assert t.mola.relocs == [(1.0, 2.0, 0.3, 0.5)]
    await t.帧(1.0, 2.0, 0.3, n=SETTLE_FIXES + 2)
    await _等(lambda: t.agent.ok(True))
    e = t.agent.estimate(t.odom)
    assert (round(e.x, 3), round(e.y, 3)) == (1.0, 2.0)


async def test_重定位_图不对_先验没载好_MOLA_不收_都说原因(台):
    t = 台
    await _等(lambda: t.srv.connected and t.agent.prior_task is not None)
    await t.agent.prior_task
    await _等(lambda: t.core.can_relocalize())
    t.mola.refuse = "初值附近对不上"
    assert await t.agent.relocalize(M, (1.0, 2.0, 0.3)) == "localizer_refused: 初值附近对不上"
    from d1max_contract.locbridge import Relocalize
    got = await t.cli._reloc(Relocalize(req=9, map_id="estate-1", map_version="8", x=0.0, y=0.0,
                                        yaw=0.0, sigma_xy=0.5))
    assert got == {"ok": False, "reason": "定位器载的不是这张图"}


async def test_换先验_目录里没有_说原因(台):
    t = 台
    t.mola.missing.add("/maps/estate-1/8")
    await _等(lambda: t.srv.connected and t.agent.prior_task is not None)
    await t.agent.prior_task
    t.agent.on_map(("estate-1", "8"), "/maps/estate-1/8")
    await t.agent.prior_task
    assert "里没有先验" in t.agent.why_not(True)


async def test_代理断了_重连之后把状态再发一遍_心跳一直在(台):
    t = 台
    await _等(lambda: t.srv.connected and t.agent.prior_task is not None)
    await t.agent.prior_task
    await _等(lambda: "等人给初始位置" in t.agent.why_not(True))
    conn = t.srv._conn
    heard = conn.heard
    await asyncio.sleep(0.5)
    assert conn.heard > heard, "心跳在走"
    t.srv._drop(conn, "测试:断一下")
    await _等(lambda: t.srv.connected and t.srv._conn is not conn)
    await t.agent.prior_task
    await _等(lambda: "等人给初始位置" in t.agent.why_not(True))


async def test_没连上的时候照样能喂帧_不炸(tmp_path):
    core = LocalizerCore(monotonic=time.monotonic)
    cli = BridgeClient(tmp_path / "nobody.sock", core, 假MOLA(core), reconnect_s=0.05)
    core.prior_loaded(M, FLAT)
    core.backend_started()
    core.relocalized(req=1, x=0.0, y=0.0, yaw=0.0, sigma=0.5, human=True)
    core.on_scan()
    core.on_estimate(_est(0.0, 0.0))
    cli.flush()
    task = asyncio.get_running_loop().create_task(cli.run())
    await asyncio.sleep(0.2)
    assert "连不上" in cli.error
    cli.stop()
    await asyncio.wait_for(task, 1.0)
