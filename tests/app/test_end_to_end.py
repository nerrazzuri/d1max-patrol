"""整条链路对着仿真器跑一遍。

**一个真机零件都不需要。** 导航是 ``SimNavServer``,运控是 ``SimAgentServer``,
建图桥是 ``SimMapServer``,相机是一段往标准输出吐 JPEG 的脚本。除此之外,从
HTTP 路由到引擎到归档到报告,跑的全是真东西。

这是整个设计"可测性"承诺的兑现处。前面十八个任务各自都测过自己那一层,但
"每一层都对"和"接起来能跑"是两件事 —— 层与层之间的假设(谁先载图、谁负责
停车、照片存在哪、报告从哪读)只有在这里才会露面。

这几条一挂,基本只有一种解释:某个环节偷偷依赖了真机,而它在自己那层的测试
里被假件盖住了。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest

from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.app.mapping import MappingConfig, MappingOrchestrator
from d1max_patrol.app.procs import ProcManager, ProcSpec
from d1max_patrol.app.server import AppContext, AppServer, shutdown
from d1max_patrol.app.teleop import Teleop
from d1max_patrol.app.video import RtspStill
from d1max_patrol.backends.map_bridge import MapBridgeClient
from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.engine.homing import HomePoint, save_home
from d1max_patrol.engine.machine import MissionEngine
from d1max_patrol.protocol.agent_frames import MotionStatus
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus, Pose
from d1max_sim.agent_server import SimAgentServer
from d1max_sim.map_server import SimMapServer
from d1max_sim.nav_server import SimNavServer
from tests.app import conftest as C
from tests.app.conftest import _fake

REPO = Path(__file__).resolve().parents[2]

#: 假相机的全部内容:一个最小但合法的 JPEG,写完就退。
#: 报告要把它 base64 内嵌进去,所以它必须真的以 SOI 开头、EOI 结尾。
_ONE_FRAME = r'''
import sys
sys.stdout.buffer.write(b"\xff\xd8CAM\xff\xd9")
sys.stdout.buffer.flush()
'''

#: 假相机吐出来的那几个字节。断言归档里躺的就是它时用。
_JPEG = b"\xff\xd8CAM\xff\xd9"

#: 一个不停往文件里追字节的子进程,用来验证"app 走了它也得走"。
#: **只追不覆盖**:文件长度因此只增不减,读两次比一下就能判断它还写不写。
_HEARTBEAT = r'''
import sys, time
while True:
    with open(sys.argv[1], "a") as f:
        f.write("x")
    time.sleep(0.02)
'''


# ------------------------------------------------------------------ 小工具


def _q(path: str) -> str:
    """URL 里的中文得先转义 —— urllib 的请求行是纯 ASCII 的。"""
    return quote(path, safe="/.%")


def _json(server: AppServer, path: str):
    return C.get_json(server, _q(path), timeout=30.0)


def _text(server: AppServer, path: str) -> str:
    code, body, _ = C.request(server, _q(path), timeout=60.0)
    assert code == 200, f"{path} 给的是 {code}"
    return body.decode("utf-8")


def _put(server: AppServer, path: str, payload) -> int:
    return C.request(server, _q(path), method="PUT", payload=payload)[0]


def _post(server: AppServer, path: str, timeout: float = 120.0) -> int:
    return C.request(server, _q(path), method="POST", payload={},
                     timeout=timeout)[0]


def _wait(fn, timeout_s: float = 30.0) -> bool:
    """轮询等一个条件。等到就 True,超时就 False —— 断言留给调用方写。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return False


def _run_state(server: AppServer) -> str:
    return _json(server, "/api/state")["run"]["state"]


def _mission(map_id: str) -> dict:
    """两个点,每个点拍一张前广角。

    点位名用 ``P1`` / ``P2``:照片文件名是从它拼出来的,而报告按文件名分组
    —— 一个名字串起了归档、判读和报告三处。
    """
    def at(x: float, y: float) -> dict:
        return {"position": {"x": x, "y": y, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}

    return {
        "mission": "端到端",
        "map_id": map_id,
        "waypoints": [
            {"name": "P1", "pose": at(1.0, 0.0), "check": "配电柜门是否关闭",
             "actions": [{"type": "photo", "camera": "front"}]},
            {"name": "P2", "pose": at(1.0, 1.0), "check": "水泵是否漏液",
             "actions": [{"type": "photo", "camera": "front"}]},
        ],
    }


async def _until(getter, wanted, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")


# ------------------------------------------------------------------ 整套台子


@dataclass
class SimStack:
    """一整套仿真上下文。测试主要碰 ``server``,其余几项是给断言用的把手。"""

    server: AppServer
    ctx: AppContext
    sims: LoopBridge
    agent: SimAgentServer
    map_id: str
    runs_root: Path
    closed: bool = False

    def shutdown(self) -> None:
        """走 app 真正的收尾路径(``server.shutdown``)。跑第二次是空操作。"""
        if self.closed:
            return
        self.closed = True
        shutdown(self.server, self.ctx)


@pytest.fixture
def sim_stack(tmp_path: Path, ffdir: Path):
    """把 app 和仿真器接起来。

    **仿真器跑在自己那条事件循环上,不跟 app 共用。** 现场它们是三个独立进程,
    这里也得是两条循环 —— 不然"关掉 app"这件事会顺手把仿真器一起关掉,而
    ``test_关掉app不碰patrol_agent`` 要断言的恰恰是它没被关掉。
    """
    sims = LoopBridge()
    sims.start()
    bridge = LoopBridge()
    bridge.start()

    nav_sim = SimNavServer(tick_hz=100.0)
    agent = SimAgentServer(port=0)
    map_sim = SimMapServer(port=0, hz=20.0)

    async def _start_sims() -> None:
        await nav_sim.start()
        await agent.start()
        # 直接摆成站着的:站起在仿真里要等,而这几条测试关心的不是站起,
        # 是接起来能不能跑。站起本身归 test_sidecar_device 管。
        agent.motion = MotionStatus.GENERAL
        await map_sim.start()
        # 露出一部分格子,页面上才画得出"正在长的那张图"。
        map_sim.reveal(0.6)

    sims.call(_start_sims, timeout_s=30.0)

    nav = VendorNavBackend(NavConfig(
        url=nav_sim.url, request_timeout_s=5.0, status_poll_interval_s=0.05,
        reconnect_min_s=0.05, reconnect_max_s=0.2))
    device = SidecarDeviceBackend("127.0.0.1", agent.port, ack_timeout_s=5.0)
    maps = MapBridgeClient("127.0.0.1", map_sim.port)
    media = {"front": RtspStill("rtsp://sim:8554/front",
                                ffmpeg=_fake(ffdir, _ONE_FRAME, "cam"),
                                timeout_s=20.0)}
    runs_root = tmp_path / "runs"
    parts: dict[str, object] = {}

    async def _wire() -> str:
        await nav.connect()
        # 厂商路线的图是设备自己建的 —— 照着现场的顺序来一遍:建、存、载入、
        # 等定位收敛。起飞检查五项里有两项卡在这几步上。
        await nav.start_mapping()
        await _until(nav.mapping_status, MappingStatus.MAPPING_RUNNING)
        await nav.stop_mapping()
        await _until(nav.mapping_status, MappingStatus.MAPPING_SAVE_END)
        map_id = (await nav.list_maps())[0]
        await nav.load_map(map_id)
        await _until(nav.loc_status, LocStatus.CONTINUOUS_LOC)

        await device.connect()
        await device.acquire_control()
        await maps.connect()

        # 起飞门槛把原点当成前置条件(preflight §home)。这几条测的是全流程
        # 接不接得通,不是原点本身,图 ID 是仿真器现建出来的,等它出来了
        # 再落一份原点,免得每条测试都要自己摆一次。
        home = HomePoint(map_id=map_id, pose=Pose.from_xy_yaw(0.0, 0.0),
                         marked_at_ms=1_757_000_000_000)
        save_home(tmp_path / "maps", home)
        engine = MissionEngine(nav, device, media, runs_root, home=home)
        parts["engine"] = engine
        parts["teleop"] = Teleop(device, engine)
        return map_id

    map_id = bridge.call(_wire, timeout_s=60.0)
    engine = parts["engine"]
    procs = ProcManager(tmp_path / "logs")
    ctx = AppContext(
        bridge=bridge, engine=engine, nav=nav, device=device, maps=maps,
        procs=procs, teleop=parts["teleop"],
        mapping=MappingOrchestrator(procs, MappingConfig(
            bags_dir=tmp_path / "bags", maps_dir=tmp_path / "maps",
            params_template=tmp_path / "mapper_3d.yaml",
            work_dir=tmp_path / "work")),
        missions_dir=tmp_path / "missions", runs_root=runs_root)
    server = AppServer(ctx, port=0)
    server.start()

    stack = SimStack(server=server, ctx=ctx, sims=sims, agent=agent,
                     map_id=map_id, runs_root=runs_root)
    try:
        yield stack
    finally:
        if not stack.closed:
            # 引擎那条协程还可能在往归档目录里写,而 pytest 马上要删这个目录。
            with contextlib.suppress(Exception):
                bridge.call(engine.aclose, timeout_s=20.0)
        stack.shutdown()

        async def _stop_sims() -> None:
            await map_sim.stop()
            await agent.stop()
            await nav_sim.stop()

        with contextlib.suppress(Exception):
            sims.call(_stop_sims, timeout_s=30.0)
        sims.stop()


def _run_once(stack: SimStack) -> str:
    """存任务 → 起飞 → 等跑完。返回这趟的 run id。"""
    server = stack.server
    assert _put(server, "/api/missions/端到端", _mission(stack.map_id)) == 200
    code = _post(server, "/api/missions/端到端/run")
    assert code == 200, f"起飞被拒了({code})"
    assert _wait(lambda: _run_state(server) == "DONE", timeout_s=120.0), \
        f"没跑到 DONE,停在 {_run_state(server)}"
    runs = _json(server, "/api/runs")["runs"]
    assert len(runs) == 1
    return runs[0]["id"]


# ------------------------------------------------------------------ 全流程


def test_全流程对着仿真器跑一遍(sim_stack, monkeypatch):
    """建图 → 标点 → 编任务 → 跑 → 判读 → 报告,一个真机零件都不需要。

    这是整个设计"可测性"承诺的兑现。它一挂,说明某个环节偷偷依赖了真机。

    判读那一步**故意不给密钥**:没网也要走得完这条路,这本来就是工程版的常
    态。有密钥时会真去调 VLM,那属于 ``tests/inspect``,不该在这里发生。
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    server = sim_stack.server

    # 1. 建图桥推的那张图 —— 盘上一张存好的都没有,页面照样画得出底图
    assert _wait(lambda: sim_stack.ctx.maps.latest is not None), "地图桥一帧都没推"
    grid = _json(server, f"/api/maps/{sim_stack.map_id}/grid")
    assert grid["width"] > 0 and grid["height"] > 0 and grid["rle"]
    assert grid["source"] == "live", "还没存盘的图必须标成 live,不然人会当成成品"

    # 2~3. 编任务、起飞、跑完。起飞检查五项当场查,过不了就是 409
    run_id = _run_once(sim_stack)

    # 4. 归档:照片就是相机吐出来的那几个字节,一个都没被动过。
    #    压过一遍、少写几个字节,在报告里看都还是"一张图",要等到有人放大去
    #    看设备铭牌时才发现不对。
    photos = sorted((sim_stack.runs_root / "端到端").glob("*/photos/*.jpg"))
    assert len(photos) == 2, "两个点位各一张,少了说明取图那一环断了"
    assert all(p.read_bytes() == _JPEG for p in photos)
    assert [p.name.split("__")[0] for p in photos] == ["P1", "P2"]

    run_dir = photos[0].parent.parent
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mission"]["map_id"] == sim_stack.map_id, "报告要追溯得到用的哪张图"
    assert manifest["summary"]["state"] == "DONE"
    assert manifest["summary"]["succeeded"] == 2

    # 5. 判读。没密钥时每张照片标成 pending,这条路照样走得完
    assert _post(server, f"/api/runs/{run_id}/judge") == 200
    detail = _json(server, f"/api/runs/{run_id}")
    assert len(detail["photos"]) == 2
    assert {f["verdict"] for f in detail["findings"]} == {"pending"}

    # 6. 报告。双击就能看、能拷走 —— 所以照片必须是内嵌的,不是外链
    html = _text(server, f"/api/runs/{run_id}/report.html")
    assert "P1" in html and "P2" in html
    assert html.count("data:image/jpeg;base64,") == 2
    assert "/api/runs/" not in html, "带外链的报告拷到别的机器上就是一堆裂图"
    assert _text(server, f"/api/runs/{run_id}/report.md")


def test_跑到一半重启app_events还原得出停在哪(sim_stack):
    """app 崩了重开,页面第一帧就该看见"这趟还在跑到第几个点"。

    状态的**唯一出处**是引擎,不是 HTTP 这一层攒下来的东西。这条测的就是这
    件事:换一个 ``AppServer``、换一条新的 SSE,第一帧仍然对得上。攒在 HTTP
    层的实现能过前面所有测试,只会在现场重开一次页面时露馅。
    """
    server = sim_stack.server
    assert _put(server, "/api/missions/端到端", _mission(sim_stack.map_id)) == 200
    assert _post(server, "/api/missions/端到端/run") == 200
    assert _wait(lambda: _run_state(server) in ("RUNNING", "RETURNING", "DONE")), \
        f"任务没跑起来,停在 {_run_state(server)}"

    # 只关 HTTP 外壳 —— 引擎、链路、那趟正在跑的任务都还在,这正是"app 挂了
    # 但机器狗还在外面走"的样子。
    server.stop()
    again = AppServer(sim_stack.ctx, port=0)
    again.start()
    sim_stack.server = again        # 收尾时关的是这个

    with C.sse(again, timeout=20.0) as frames:
        first = next(frames)
    assert first["kind"] == "state"
    assert first["run"]["mission"] == "端到端"
    assert first["run"]["state"] in ("RUNNING", "RETURNING", "DONE")


def test_关掉app它起的子进程都不留(sim_stack, tmp_path):
    """建图那几个 ROS 进程是 app 起的,app 走了它们不能留。

    留下的后果不是"多一个进程":它们占着 ROS 话题和端口,下一次起 app 会撞
    上,而报出来的错跟"上次没关干净"看不出任何关系。
    """
    beat = tmp_path / "beat.txt"
    script = tmp_path / "hb.py"
    script.write_text(_HEARTBEAT, encoding="utf-8")
    spec = ProcSpec(name="假建图",
                    argv=(sys.executable, "-u", str(script), str(beat)))
    ctx = sim_stack.ctx
    ctx.bridge.call(lambda: ctx.procs.start(spec), timeout_s=30.0)
    assert ctx.procs.running() == ["假建图"]
    assert _wait(beat.is_file, timeout_s=10.0), "子进程没起来"

    sim_stack.shutdown()

    assert ctx.procs.running() == []
    # 光看账本不算数 —— 真去看它还写不写。心跳是 20ms 一次、只追不覆盖,
    # 静置半秒长度还没变就是真死了。
    was = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == was, "进程还活着,只是从账本上销了号"


def test_关掉app不碰patrol_agent(sim_stack):
    """控制权拿不回来(清单 #46/#47)—— app 的生死不许影响旁路进程。

    旁路进程不是 app 起的:它在 app 之前就跑着。app 顺手把它带走的话,控制权
    就落回上装手里,而那条路是单向的 —— 唯一的恢复手段是重启运控主机。也就
    是说,每关一次 app 都得重启一次机器狗。
    """
    sim_stack.shutdown()

    # app 那一侧的连接确实断了
    assert sim_stack.ctx.device.connected is False

    # 但旁路进程还在原地,还收新连接、还答得上话 —— 重开一次 app 就能接着用
    again = SidecarDeviceBackend("127.0.0.1", sim_stack.agent.port,
                                 ack_timeout_s=5.0)

    async def _reconnect() -> bool:
        await again.connect()
        try:
            # 一次真的往返:连上不等于活着,能答话才是
            await again.acquire_control()
            return await again.has_control()
        finally:
            await again.close()

    assert sim_stack.sims.call(_reconnect, timeout_s=20.0) is True


# --------------------------------------------------------------- 交付物本身


def test_app的入口装得出来():
    """``d1max-app`` 是现场唯一要敲的命令。装不出来,前面十九个任务都白做。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'd1max-app = "d1max_patrol.app.server:main"' in text


def test_老的看图脚本已经撤了():
    """``scripts/live_viewer.py`` 的活儿全被 app 接过来了。

    留着两套看图的东西,现场就会有人打开那个旧的,然后报告"图不刷新"。
    """
    assert not (REPO / "scripts" / "live_viewer.py").exists()


def test_管线文档里写了怎么用app跑():
    """现场照着文档一步步敲,文档里没有 app 就等于 app 不存在。"""
    doc = (REPO / "docs" / "建图定位与巡检管线.md").read_text(encoding="utf-8")
    assert "d1max-app" in doc
