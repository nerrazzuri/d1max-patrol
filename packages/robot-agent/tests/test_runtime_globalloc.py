"""W41 全局重定位接进代理:能力里报 ``relocalize.global``;``relocalize {global: true}`` 自动找
位置、请定位器按它重定位;定位器自信地锁错了(仿真定位器加偏移)→ 点云套图核出来、按对的那处
重定位;认不出就标不可信、等人。真代理 + 仿真狗 + 仿真定位器(真本机桥);墙那一层用射线仿真。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from test_globalloc import RES, _房间, _扫, 不对称
from test_relocalize import _ack, _goto, _事件, _遥测
from test_runtime_localizer import 台子
from test_runtime_maps import _cmd

from d1max_agent.globalloc import GlobalLoc

TRUE = (3.0, 3.0, 0.4)


class 假视图:
    """只给墙那一层;外审 R3 / 复查 N1、N5 要的「攒点云时狗在哪」「最新里程」跟着仿真狗走
    (= 攒完没动)。"""

    def __init__(self, pts, dog=None):
        self.pts = pts
        self.dog = dog

    def wall_scan(self, *, since=-math.inf):
        return None if self.pts is None else list(self.pts)

    def _here(self):
        d = self.dog
        return (d.x, d.y, d.yaw) if d is not None else None

    @property
    def wall_pose(self):
        return self._here()

    @property
    def odom(self):
        return [(0.0, self._here())] if self.dog is not None else []


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
    t.view = 假视图(_扫(occ, *TRUE, noise=0.02), t.dog)
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
    t.rt._conflict_next = math.inf      # 测的是稳下来后的点云核对(工单 10 的两处核对另有测试)
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
    t.rt._conflict_next = math.inf      # 测的是全图也认不出那条路(工单 10 的两处核对另有测试)
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


async def test_工单10_人给错了4米_定位器自己回对了_点云核出来信定位器_不用等人(台):
    """2026-10-28 3z.18:设错 3 m(超过核对的线)后代理一直「人给的位置跟定位器稳住的地方差
    3.0 m:请重新设位置」,
    定位器那边 σ 已回到 0.1。拿点云在两处各套一次图,哪处贴合度高信哪处。"""
    t = 台
    t.loc.reloc_basin_m = 10.0                       # 仿真:给错了定位器也能自己回到真实位置
    await t.连()
    await t.rt._on_cmd(_cmd("relocalize", {"x": TRUE[0] + 4.0, "y": TRUE[1], "yaw": TRUE[2]},
                            "r1", t.c))
    anchor = t.rt.parts.nav.anchor
    said: list[str] = []
    real_off = anchor._off_anchor

    def off(new):
        why = real_off(new)
        said.append(why)
        return why
    anchor._off_anchor = off           # 记下判没判过「对不上」(核对可能同一拍就解开,事后看不到)
    for _ in range(60):
        await t.拍(1)
        if anchor.ok(True):
            break
    assert any("请重新设位置" in w for w in said), "先是判了对不上(原来的样子)"
    assert anchor.ok(True), anchor.why_not(True)
    await t.拍(2)
    ev = _事件(t.ears, "loc_conflict_resolved")
    assert ev and ev[-1]["trust"] == "localizer" and ev[-1]["fit_localizer"] > ev[-1]["fit_given"]
    assert t.loc.relocs[-1].human, "没替人另请重定位:信的是定位器现在的位置"


# ------------------------------------------------------------ W68 开机自己找位置(用户 11-02)


def _乱点(n=400, seed=3):
    """跟图上哪儿都对不上的点云(机器人不在这张图上)。"""
    rs = np.random.default_rng(seed)
    a, d = rs.uniform(0, 2 * math.pi, n), rs.uniform(1.0, 6.0, n)
    return list(zip((d * np.cos(a)).tolist(), (d * np.sin(a)).tolist(), strict=True))


async def _没初值地连上(t):
    t.loc.need_init = True
    assert await t.loc.connect(), t.loc.error
    await t.拍(3)


async def test_W68_开机没有初值_自己找位置_请定位器按它重定位_之后可信(台):
    t = 台
    await _没初值地连上(t)
    assert "等人给初始位置" in _遥测(t.ears).loc["reason"]
    await t.拍(60)
    assert len(t.loc.relocs) == 1, t.loc.relocs
    r = t.loc.relocs[-1]
    assert math.hypot(r.x - TRUE[0], r.y - TRUE[1]) < 0.15 and not r.human
    ev = _事件(t.ears, "relocalized")[-1]
    assert ev["source"] == "auto_global" and ev["fit"] > 0.7
    await t.拍(20)
    assert _遥测(t.ears).loc["reason"] == ""


async def test_W68_开机自己找_不在地图上_报位置偏差过大_导航不许起飞_三次后不再找(台):
    t = 台
    t.view.pts = _乱点()
    await _没初值地连上(t)
    await t.拍(60)
    assert t.loc.relocs == []
    nf = _事件(t.ears, "loc_not_found")
    assert nf and nf[-1]["kind"] == "off_map", nf
    reason = _遥测(t.ears).loc["reason"]
    assert "位置偏差过大" in reason and "可能不在地图上" in reason, reason
    await t.rt._on_cmd(_goto(4.0, 3.0, "g1", t.c))
    await t.拍(40)
    failed = _事件(t.ears, "task_failed")
    assert failed and "定位" in failed[-1]["reason"], t.ears.by.get("event")
    assert math.hypot(t.dog.x - TRUE[0], t.dog.y - TRUE[1]) < 0.01, "没动"
    await t.拍(10 * 200)                                  # 200 秒:最多再找两次
    assert len(_事件(t.ears, "loc_not_found")) == 3
    assert "请到场设位置" in _遥测(t.ears).loc["reason"]
    assert t.loc.relocs == []


async def test_W68_开机自己找_几处差不多像_说无法确定_不请定位器(台):
    t = 台
    occ, _ = _房间(w=10.0, h=10.0)                        # 对称的空房间
    _写图(t.tmp / "sym", occ)
    t.rt.globalloc.set_map(("m", "1"), t.tmp / "sym")
    t.dog.teleport(5.0, 5.0, 0.3)
    t.view.pts = _扫(occ, 5.0, 5.0, 0.3)
    await _没初值地连上(t)
    await t.拍(60)
    assert t.loc.relocs == []
    nf = _事件(t.ears, "loc_not_found")
    assert nf and nf[-1]["kind"] == "ambiguous", nf
    assert "无法确定" in _遥测(t.ears).loc["reason"]


async def test_W68_找位置时还没有点云_不算一次_有了再找(台):
    t = 台
    pts, t.view.pts = t.view.pts, None
    await _没初值地连上(t)
    await t.拍(60)
    assert t.loc.relocs == [] and not _事件(t.ears, "loc_not_found")
    t.view.pts = pts
    await t.拍(80)
    assert len(t.loc.relocs) == 1


async def test_W68_有初值但一直对不上_30秒后自己找(台):
    t = 台
    await t.连()
    await t.拍(120)                                       # 稳下来后那次点云核对先核完
    t.loc.paused = 10 ** 6                                # 定位器不出位姿了(按旧初值对不上)
    await t.loc.lose("初值附近不匹配")
    t.dog.teleport(8.0, 2.0, 0.0)                         # 被搬走了
    t.view.pts = _扫(t.occ, 8.0, 2.0, 0.0, noise=0.02)
    await t.拍(250)
    # 30 秒以内不抢(代理自己按最后可信的位置请的那几次照旧,都在旧位置附近)
    assert all(math.hypot(r.x - 8.0, r.y - 2.0) > 2.0 for r in t.loc.relocs), t.loc.relocs
    await t.拍(80)
    r = t.loc.relocs[-1]
    assert math.hypot(r.x - 8.0, r.y - 2.0) < 0.15


async def test_W68_定位一直可信_不自动找(台):
    t = 台
    await t.连()
    await t.拍(500)
    assert t.loc.relocs == [] and not _事件(t.ears, "loc_not_found")


async def test_W68_手动设位置照样能用_自动找的结论作废(台):
    t = 台
    t.view.pts = _乱点()
    await _没初值地连上(t)
    await t.拍(60)
    assert "可能不在地图上" in _遥测(t.ears).loc["reason"]
    t.view.pts = _扫(t.occ, *TRUE, noise=0.02)
    await t.rt._on_cmd(_cmd("relocalize", {"x": TRUE[0], "y": TRUE[1], "yaw": TRUE[2]},
                            "r1", t.c))
    await t.拍(20)
    assert t.loc.relocs[-1].human
    assert _遥测(t.ears).loc["reason"] == ""
