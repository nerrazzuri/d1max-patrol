"""W41 全局重定位接进代理:能力里报 ``relocalize.global``;``relocalize {global: true}`` 自动找
位置、请定位器按它重定位;定位器自信地锁错了(仿真定位器加偏移)→ 点云套图核出来、按对的那处
重定位;认不出就标不可信、等人。真代理 + 仿真狗 + 仿真定位器(真本机桥);墙那一层用射线仿真。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from test_globalloc import RES, _房间, _扫, 不对称
from test_relocalize import _ack, _事件
from test_runtime_localizer import 台子
from test_runtime_maps import _cmd

from d1max_agent.globalloc import GlobalLoc

TRUE = (3.0, 3.0, 0.4)


class 假视图:
    def __init__(self, pts):
        self.pts = pts

    def wall_scan(self, *, since=-math.inf):
        return None if self.pts is None else list(self.pts)


def _写图(d, occ):
    d.mkdir(parents=True, exist_ok=True)
    img = np.where(np.flipud(occ), 0, 254).astype(np.uint8)
    h, w = occ.shape
    (d / "floor.pgm").write_bytes(b"P5\n%d %d\n255\n" % (w, h) + img.tobytes())
    (d / "floor.yaml").write_text(f"resolution: {RES}\norigin: [0.0, 0.0, 0.0]\nnegate: 0\n"
                                  "occupied_thresh: 0.65\nfree_thresh: 0.196\n")


@pytest.fixture
async def 台(tmp_path):
    t = await 台子().起(tmp_path)
    t.dog.teleport(*TRUE)
    occ, _ = _房间(extra=不对称)
    _写图(tmp_path / "map", occ)
    t.view = 假视图(_扫(occ, *TRUE, noise=0.02))
    t.rt.globalloc = GlobalLoc(t.view)
    t.rt.globalloc.set_map(("m", "1"), tmp_path / "map")
    t.occ = occ
    yield t
    await t.收()


async def test_能力报能自动找位置_命令找到_请定位器按它重定位(台):
    t = 台
    await t.连()
    await t.rt._publish_caps()
    await t.broker.drain()
    assert t.ears.by["capabilities"][-1]["tasks"]["relocalize"]["global"] is True
    await t.rt._on_cmd(_cmd("relocalize", {"global": True}, "r1", t.c))
    await t.拍(3)
    a = _ack(t.ears)
    assert a["result"] == "accepted", a
    assert a["data"]["fit"] > 0.7 and len(a["data"]["candidates"]) >= 1
    r = t.loc.relocs[-1]
    assert math.hypot(r.x - TRUE[0], r.y - TRUE[1]) < 0.15 and r.sigma_xy == pytest.approx(0.3)
    ev = _事件(t.ears, "relocalized")[-1]
    assert ev["source"] == "global" and ev["fit"] > 0.7


async def test_命令_认不出_说原因_不请定位器(台):
    t = 台
    await t.连()
    t.view.pts = None
    await t.rt._on_cmd(_cmd("relocalize", {"global": True}, "r1", t.c))
    await t.拍(1)
    a = _ack(t.ears)
    assert a["result"] == "rejected" and "not_found" in a["reason"] and "点云" in a["reason"], a
    t.view.pts = [(1.0, 0.0)] * 3
    await t.rt._on_cmd(_cmd("relocalize", {"global": True}, "r2", t.c))
    await t.拍(1)
    assert "太少" in _ack(t.ears)["reason"]
    await t.rt._on_cmd(_cmd("relocalize", {"global": 1}, "r3", t.c))
    await t.拍(1)
    assert "global 只能是 true" in _ack(t.ears)["reason"]
    assert t.loc.relocs == []


async def test_定位器自信地锁错了_核出来_按对的那处重定位(台):
    t = 台
    await t.连()
    t.loc.offset = [-3.0, 2.0, 0.0]                      # 定位器报到 3.6 m 外,还说可信
    await t.拍(15)
    await t.rt._scan_check(-math.inf)
    await t.拍(2)
    wrong = _事件(t.ears, "loc_wrong_solution")
    assert wrong and wrong[-1]["fit"] < 0.5 and "reloc" in wrong[-1], wrong
    r = t.loc.relocs[-1]
    assert math.hypot(r.x - TRUE[0], r.y - TRUE[1]) < 0.15 and not r.human


async def test_定位器给的对_核了不动(台):
    t = 台
    await t.连()
    await t.rt._scan_check(-math.inf)
    await t.拍(2)
    assert not _事件(t.ears, "loc_wrong_solution") and t.loc.relocs == []
    assert t.rt.parts.nav.anchor.scan_disagree == ""


async def test_锁错了又认不出对的那处_标不可信_等人设位置(台):
    t = 台
    occ, free = _房间(w=10.0, h=10.0)                    # 对称的空房间
    _写图(t.tmp / "sym", occ)
    t.rt.globalloc.set_map(("m", "1"), t.tmp / "sym")
    t.dog.teleport(5.0, 5.0, 0.3)
    t.view.pts = _扫(occ, 5.0, 5.0, 0.3)
    await t.连()
    t.loc.offset = [-2.0, -1.5, 0.0]
    await t.拍(15)
    await t.rt._scan_check(-math.inf)
    await t.拍(2)
    anchor = t.rt.parts.nav.anchor
    assert "请重新设位置" in anchor.scan_disagree and t.loc.relocs == []
    assert anchor.why_not(True) == anchor.scan_disagree
    assert _事件(t.ears, "loc_wrong_solution")[-1]["stuck"] is True
    await t.rt._on_cmd(_cmd("relocalize", {"x": 5.0, "y": 5.0, "yaw": 0.3}, "r1", t.c))
    await t.拍(2)
    assert anchor.scan_disagree == "", "人给了位置:点云套图的结论作废"
