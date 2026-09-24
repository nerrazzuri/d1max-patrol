"""上线形状的契约夹具(挂账 11)。

**同一份形状,两头都在读。** 狗这头的 ``to_wire()`` 改一个字段名,Dart 那头
要等到真机上点开那一屏才会发现 —— 那时候狗在客户现场。

做法:**Python 这头从真的路由处理器里取输出**,写进 ``mobile/test/fixtures/``;
Dart 那头解析**同一批签进仓库的文件**。狗这头改了形状而没重生成,这条测试
先红;重生成了而 Dart 没跟上,Dart 那条测试红。两种漏法都堵住了。

形状变了要重生成:

    D1MAX_UPDATE_FIXTURES=1 python -m pytest tests/app/test_wire_fixtures.py \\
        --basetemp=D:/pytest-tmp/fx

**重生成之后一定要跑一遍 Dart 测试**
(``cd mobile && flutter test --concurrency=1``):Python 这头重生成就不红了,
Dart 那头的解析可能刚被这次改动打断。

``--concurrency=1`` **不是可选的**。这台机器上裸 ``flutter test`` 会时不时
无声地吞掉字母序最后那个测试文件 —— 正好就是 ``wire_fixtures_test.dart``,
也就是这段话让人去跑的那一个 —— 而屏幕上照样打 ``All tests passed!``、退出
码照样是 0。是并发加载的竞态,所以**不是每次都复现**:2026-09-10 这天裸跑
就是完整的 251 条。这恰恰是它危险的地方 —— 一件时灵时不灵、失灵时还给你
一个绿的东西,没法靠"我上次跑过没事"排除。少了这个参数,这条提示就是在教
人做一件看起来做过了、实际没做的事。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from d1max_agent.engine.machine import RunState
from d1max_patrol.app.server import AppServer
from d1max_patrol.app.video import CAMERAS, CameraFeed
from d1max_patrol.backends.base import BatteryEvent, DevicePoseEvent
from d1max_patrol.protocol.nav_types import Pose
from tests.app.conftest import get_json, make_ctx, status

# ``有pin的服务``/``墙钟`` 这两个名字在这个模块的普通代码里都不会被直接
# 引用到 —— 它们只出现在测试函数的参数列表里,那是 pytest 按名字做依赖
# 注入,不是这里的哪一行代码在读这个名字。ruff 看不出这层,会把它们当成
# "导入了没用上"(F401),再把测试函数参数里同名的那个当成"重复定义"
# (F811)。两边都是这个跨模块夹具写法本身带来的,不是真的死代码,所以
# 都挂 noqa,不改写法绕开它。
from tests.app.test_api_control import auth, 墙钟, 打, 有pin的服务, 解锁  # noqa: F401
from tests.app.test_video import fake_ffmpeg_freezes  # noqa: F401
from tests.app.test_watch import 摆一趟, 摆包, 摆镜像盘

夹具目录 = Path(__file__).resolve().parents[2] / "mobile" / "test" / "fixtures"

#: 自己搭服务的那几份夹具统一用的那一刻(UTC 毫秒)。
#:
#: **跟 ``易变的键`` 里那个 ``at_ms`` 是同一个数**,也跟
#: ``tests/app/test_watch.py`` 的 ``NOW_MS`` 是同一个数 —— 生成出来的文件里
#: 凡是时刻就都对得上,读夹具的人不会以为两个不同的数是两个不同的时刻。
#:
#: 用它的那几条测试**把钟整个注进去**(``make_ctx(clock=...)``),不是在断言
#: 里容忍一点误差:告警的 ``first_ms``/``last_ms``/``acked_ms`` 这几个键不在
#: ``易变的键`` 那张表里,靠 ``定住()`` 换不掉,只能让它们从源头就是定值。
T0 = 1_757_000_000_000

#: 每跑一次(或者每台机器)都不一样的键 -> 换上去的定值。
#:
#: **换的是值,不是类型。** 换成 null 或者一个字符串占位就等于把类型信息也
#: 删了,而 Dart 那头正是靠这份文件知道 ``at_ms`` 是个整数。
#:
#: brief 原来那份表里的 ``free_mb``/``used_mb``/``total_mb`` 在仓库里根本不
#: 存在(grep 过 ``/api/storage`` 的真实响应,那三个键叫
#: ``used_bytes``/``total_bytes``/``used_ratio``)。**这三个键不进这张表**——
#: ``forecast.detail`` 里那句"盘 86%,..."是拿真实 ``used_ratio`` 现拼的自由
#: 文本,键值换成定值治不了拼进字符串里的百分比;真正的办法是在
#: ``test_夹具_盘况`` 里把 ``_disk()`` 整个换成固定读数,让这条路径从盘用量
#: 到文案全程确定,见那条测试自己的 docstring。
#:
#: ``generated_at``(``/api/storage`` 的 ``forecast`` 段,``datetime.now()``
#: 格式化出来的时间戳)是原表没有、这里补上的一个键。
易变的键: dict[str, Any] = {
    "at_ms": 1_757_000_000_000,
    "started_ms": 1_757_000_000_000,
    "expires_ms": 1_757_000_030_000,
    "grace_ends_ms": 1_757_000_015_000,
    "expires_in_ms": 30_000,
    "grace_in_ms": 15_000,
    "since_frame_s": 0.12,
    "elapsed_s": 12.5,
    "ref": "ab12cd34",
    "seq": 1,
    "generated_at": "20250904T000000Z",
}


def 定住(x: Any) -> Any:
    """``易变的键`` 只换**真的每次都变的那个值**,``None`` 不换。

    键名对上了但原值是 ``None`` 时不替换 —— 直接拿定值去覆盖会把 ``null``
    换成一个数,那正好砸了这个机制自己的招牌:"换的是值,不是类型"。
    ``video_health`` 里没起泵的相机,``since_frame_s`` 本来就是
    ``null``(从没收到过帧,没有"多久之前"可言),留着 ``null`` 才是
    对这台相机此刻状态的如实描述。
    """
    if isinstance(x, dict):
        return {k: (易变的键[k] if k in 易变的键 and v is not None else 定住(v))
                for k, v in x.items()}
    if isinstance(x, list):
        return [定住(v) for v in x]
    return x


def 对(name: str, payload: Any) -> None:
    """把 ``payload`` 跟签进仓库的那份比。形状变了就报清楚怎么重生成。"""
    夹具目录.mkdir(parents=True, exist_ok=True)
    路 = 夹具目录 / f"{name}.json"
    正 = json.dumps(定住(payload), ensure_ascii=False,
                    indent=2, sort_keys=True) + "\n"
    if os.environ.get("D1MAX_UPDATE_FIXTURES") == "1":
        路.write_text(正, encoding="utf-8")
        return
    assert 路.exists(), (
        f"{路} 还没生成 —— D1MAX_UPDATE_FIXTURES=1 跑一遍这个文件")
    旧 = 路.read_text(encoding="utf-8")
    assert 旧 == 正, (
        f"{name} 的上线形状变了。\n"
        f"改对了就重生成:D1MAX_UPDATE_FIXTURES=1 python -m pytest "
        f"tests/app/test_wire_fixtures.py\n"
        f"**重生成之后一定要跑一遍 "
        f"cd mobile && flutter test --concurrency=1** —— Python 这头重生成"
        f"就不红了,Dart 那头的解析可能刚被打断。``--concurrency=1`` 不能省:"
        f"裸跑会时不时无声吞掉 wire_fixtures_test.dart 自己,还打 "
        f"All tests passed。")


def _任务定义(map_id: str) -> dict[str, Any]:
    """一个点、一张照片,够把引擎推进 ``RUNNING`` 就行 —— 这条测试不关心
    任务本身,只关心 ``RunSnapshot`` 挂起时的形状。
    """
    return {
        "mission": "wire_fixture",
        "map_id": map_id,
        "waypoints": [
            {"name": "P1", "pose": {
                "position": {"x": 1.0, "y": 0.0, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
             "check": "配电柜门是否关闭",
             "actions": [{"type": "photo", "camera": "front"}]},
        ],
    }


# ------------------------------------------------------------------ 十份夹具


def test_夹具_租约(有pin的服务):  # noqa: F811
    """**走 ``GET /api/control``,不是 ``/api/state`` 里的 ``control`` 段。**

    那两个不是同一个形状:``/api/state`` 那一份是所有人共用的广播快照,
    按定义没有 ``mine``,也没有 ``remote_sessions``/``max_sessions``/``notice``
    (理由见 ``_control_wire`` 自己的 docstring)。而手机的控制权面板读的正是
    那四个键 —— 拿广播那一份当夹具,面板的测试就会对着一份缺了一半的报文写,
    缺的那一半永远没人钉。
    """
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    对("lease_state", get_json(有pin的服务, "/api/control", headers=auth(tok)))


def test_夹具_运行快照(server, ctx, bridge):
    """**取的是挂起中的那一份** —— ``suspended_at`` 非空的形状只有这时候有,
    而那正是 Dart 那头最容易解析错的一块。

    仓库里没有现成的"直接给我一份挂起状态"的 app 层夹具,只能照
    ``tests/app/test_teleop.py`` 的 ``跑着的engine``/``挂起的engine`` 那套
    真把引擎推起来:先通过 HTTP 起一趟任务(参照
    ``tests/app/test_end_to_end.py`` 怎么走 ``PUT /api/missions/<mid>`` +
    ``POST /api/missions/<mid>/run``),引擎在 ``ctx`` 自己的桥上跑,不能在
    这条线程上直接 ``await`` —— 用 ``bridge.call`` 把协程送过去,并且带截止
    时间轮询(§8.5 第 2 条不许 ``sleep(固定余量)``)。

    **``pose`` 必须非空。** ``SuspendPoint.pose`` 抄的是 ``last_pose`` ——
    引擎收到过至少一条 ``DevicePoseEvent`` 才有;不喂这一条,挂起点位里的
    ``pose`` 永远是 ``None``,Dart 那头就测不出 ``pose`` 有哪些键。所以先
    ``ctx.device.emit(DevicePoseEvent(...))``、等引擎真收到了,再挂起。
    """
    mid = "wire_fixture"
    assert status(server, f"/api/missions/{mid}", method="PUT",
                  payload=_任务定义("map_test")) == 200
    assert status(server, f"/api/missions/{mid}/run", method="POST") == 200

    eng = ctx.engine
    bridge.call(lambda: eng.wait_state(RunState.RUNNING), timeout_s=10.0)

    async def _喂位姿再挂起() -> None:
        ctx.device.emit(DevicePoseEvent(Pose.from_xy_yaw(3.0, 4.0, 0.0)))
        截止 = asyncio.get_running_loop().time() + 2.0
        while eng._live is None or eng._live.last_pose is None:
            if asyncio.get_running_loop().time() > 截止:
                raise AssertionError("位姿没被引擎收到")
            await asyncio.sleep(0)
        await eng.suspend("契约夹具:模拟人接管遥控")
        await eng.wait_state(RunState.SUSPENDED)

    bridge.call(_喂位姿再挂起, timeout_s=10.0)

    对("run_snapshot", get_json(server, "/api/state")["run"])


def test_夹具_视频健康(server, ctx):
    """**必须先挂真的 ``CameraFeed``。** ``ctx.video`` 默认是空字典
    (``AppContext.video`` 的工厂),空的时候 ``_video_health`` 走的是
    ``app/server.py`` 自己兜底那句硬编码 ``{"online": False, ...}``,
    根本碰不到 ``video.py`` 的 ``health()`` —— Step 5 拆的正是
    ``video.py`` 那一份,不挂真 feed 的话那次拆解在这条夹具上什么反应
    都不会有。

    挂上之后**不用起泵**(不接 ``/api/video/<name>``,不用假 ffmpeg):
    没起泵时 ``CameraFeed.health()`` 本来就是确定的一份
    (``online=False``、``since_frame_s=None``、``viewers=0``、
    ``detail=""``),不用真拉流也能让这条夹具走真的 ``video.py`` 代码路径。
    """
    ctx.video = {name: CameraFeed(f"rtsp://x:8554/{name}") for name in CAMERAS}
    对("video_health", get_json(server, "/api/video/health"))


def test_夹具_视频健康_有一路真的在出画面(server, ctx, fake_ffmpeg_freezes):  # noqa: F811
    """**``video_health`` 那一份从来没把 ``online`` 驱动成 ``true``。**

    两台相机都是 ``online:false``、``since_frame_s`` 都是 ``null``,于是
    Dart 那头有两件事学不到:``VideoHealth.anyLive`` 为真长什么样(Task 11
    的遥控屏拿它决定摇杆灰不灰),以及 ``since_frame_s`` 是个什么类型 ——
    ``null`` 不带类型信息。这一份补上:``front`` 泵过一帧,``back`` 照旧
    没起泵。

    用的是"吐一帧就冻住"的假件加注进去的钟(跟 ``test_video`` 里那条
    ``画面冻住`` 同一套):真的走一遍 ``video.py`` 的 ``health()``,又不用
    sleep(§8.5 第 2 条)。钟往前拨 0.12 秒是为了让 ``since_frame_s`` 落成
    一个**真的数**;它本来就在 ``易变的键`` 里,签进文件的值是那张表里的
    定值,不随机器快慢变。
    """
    now = [1000.0]
    front = CameraFeed("rtsp://x:8554/front", ffmpeg=fake_ffmpeg_freezes,
                       clock=lambda: now[0], stale_s=2.0, start_timeout_s=5.0)
    帧流 = front.stream()
    try:
        next(帧流)
        now[0] += 0.12
        ctx.video = {name: CameraFeed(f"rtsp://x:8554/{name}")
                     for name in CAMERAS}
        ctx.video["front"] = front
        份 = get_json(server, "/api/video/health")
        assert 份["cameras"]["front"]["online"] is True
        assert isinstance(份["cameras"]["front"]["since_frame_s"], float)
        对("video_health_live", 份)
    finally:
        帧流.close()
        front.close()


def test_夹具_控制权留痕(有pin的服务):  # noqa: F811
    """**取的是切过远程之后的那一份**:``mode_switched`` 那条留痕的形状要在
    Dart 那头解析得出来,否则值守的人翻记录时看到一条空白。
    """
    tok = 解锁(有pin的服务, "张三")
    打(有pin的服务, "/api/control/acquire", tok)
    打(有pin的服务, "/api/teleop/mode", tok, {"mode": "remote", "confirmed": True})
    对("control_audit", get_json(有pin的服务, "/api/control/audit",
                                headers=auth(tok)))


def test_夹具_盘况(server, monkeypatch):
    """盘用量来自这台机器真实的 ``shutil.disk_usage()``(``app/server.py``
    的 ``_disk()``)——换一台机器、甚至同一台机器隔一阵子跑,读数都不一样,
    连带着 ``forecast.detail`` 里现拼的那句"盘 NN%,..."也会跟着变。

    **这条不能只靠 ``定住()`` 换键值。** 百分比是拼进一句自由文本里的字符串,
    换 ``used_ratio`` 这个键的值治不了已经拼进 ``detail`` 里的那个数。所以
    直接把 ``_disk()`` 整个换成固定读数,让这条路径从盘用量到文案全程确定——
    这样生成出来的夹具在任何一台机器上都是同一份,不用等下一次红了才发现。
    """
    import d1max_patrol.app.server as _server_mod
    monkeypatch.setattr(_server_mod, "_disk",
                        lambda _path: (420_000_000_000, 1_000_000_000_000))
    对("storage", get_json(server, "/api/storage"))


def test_夹具_身份(server):
    对("identity", get_json(server, "/api/identity"))


def test_夹具_证明向量():
    """质询-应答的跨语言向量(``auth.proof_for``)。

    **两头各自实现同一个算法、各自绿、合起来对不上**,是这一类代码最典型的
    翻车法。它翻车的表现是「手机上输对 PIN 也进不去」,而两边的测试都是绿的。

    PIN 是编的,不是任何一台真机上的:这份文件要签进手机仓库。
    """
    from d1max_patrol.app.auth import PROOF_ALG, proof_for
    样本 = [("000000", "00" * 16), ("123456", "a1b2c3d4" * 4),
            ("864209", "ff" * 16)]
    对("proof_vectors", {
        "alg": PROOF_ALG,
        "vectors": [{"pin": p, "nonce": n, "proof": proof_for(p, n)}
                    for p, n in 样本],
    })


# ------------------------------------------------------ 值守屏那两份(裁决二十)


async def _推(emitter, event) -> None:
    """在循环线程里推一个事件出去。``EventEmitter`` 的订阅者列表不带锁。"""
    emitter.emit(event)


class _盘架:
    """一个**能事后插盘**的探针替身。

    ``ctx.removable`` 是个只读属性 —— 它转手给的是引擎手上那一个,赋不了值。
    而摆一块镜像盘要先知道这台狗的序列号(``init_target`` 要往盘上写),那得
    先有 ``ctx``。所以顺序只能是:先把这个空架子交给 ``make_ctx``,拿到 ``ctx``
    之后再把盘插上来。跟 ``tests/conftest.py`` 的 ``SomeDisks`` 是同一个东西,
    只是那个的盘在构造时就定死了。
    """

    def __init__(self) -> None:
        self.disks: tuple[Any, ...] = ()

    async def scan(self) -> tuple[Any, ...]:
        return self.disks


def _告警路径(key: str, 动作: str) -> str:
    """把一个告警键拼成 URL。**整段转义** —— 键形如 ``robot/kind#seq``,里头
    的 ``/`` 会把路由参数切断,``#`` 在客户端那一步就被当片段丢掉了。跟
    ``tests/app/test_alert_routes.py`` 里那个 ``路径`` 同一个道理。
    """
    return f"/api/alerts/{quote(key, safe='')}/{动作}"


@contextlib.contextmanager
def _起一台(bridge, ctx):
    """起真服务、跑完收干净。这两份夹具都要自己搭 ctx(钟、盘、时间参照全得
    注进去),用不上 conftest 那个 ``server`` 夹具。
    """
    s = AppServer(ctx, port=0)
    s.start()
    try:
        yield s
    finally:
        s.stop()
        with contextlib.suppress(Exception):
            bridge.call(ctx.engine.aclose, timeout_s=10.0)


def test_夹具_告警名单(bridge, tmp_path, monkeypatch):
    """``GET /api/alerts/all`` 那一份 —— 手机那头 ``alertsFromWire`` 收的就是
    这个信封(``{"alerts": [...]}``)。

    **取的是 ``/all`` 不是 ``/api/alerts``。** 两条路由的元素形状是同一个
    ``Alert.to_wire()``,但只有 ``/all`` 里同时出得来三种局面:没人管的、有人
    确认过的(``acked_ms`` 非空)、已经解决的(``resolved_ms`` 非空)。拿只
    有未解决告警的那一份当夹具,``acked_ms``/``resolved_by`` 这几个键在文件里
    永远是 ``null``/空串,Dart 那头就学不到它们非空时长什么样 —— 而
    ``model/alert.dart`` 的 ``ackedMs`` 上那段注释("塌成 0 之后屏上写着
    1970-01-01 已确认")说的正是解析这两个键出错的后果。

    **四条都是真的走 ``AlertBook`` / 那两条路由记出来的**,不是手拼的字典:
    ``count``、``escalated``、``channel``、``key`` 里那个 ``#seq``,每一个都是
    簿子自己算的。手拼一份的话,``channel`` 会被写成人以为的那个值,而不是
    ``escalated`` 真正算出来的那个(裁决十一要的就是这两个不许分叉)。

    **``_disk`` 必须换掉。** 挂账 76:这台开发机的盘水位过了 ``disk_80`` 的
    线,只要起真服务、租约看门狗跑到第一拍,簿子里就会多一条这台机器上才有的
    P2 —— 那一条会原样签进夹具,而别人机器上重生成时它又不见了。换成一个
    0.42 的固定读数,这一份就只剩下面这四条自己记的。

    **钟整个注成定值。** ``first_ms``/``last_ms``/``acked_ms``/``resolved_ms``
    都不在 ``易变的键`` 那张表里(它们的键名跟别处的 ``at_ms`` 不一样),
    ``定住()`` 换不掉;唯一的办法是让它们从源头就是定值。顺带这也把升级链钉
    死了:``due_escalations`` 从 ``first_ms`` 算起,钟不动,后台那条 ``_tick``
    再跑多少遍算出来的档位都一样。
    """
    import d1max_patrol.app.server as _server_mod
    monkeypatch.setattr(_server_mod, "_disk",
                        lambda _path: (420_000_000_000, 1_000_000_000_000))
    ctx = make_ctx(bridge, tmp_path, clock=lambda: T0)
    狗 = ctx.identity.sn
    with _起一台(bridge, ctx) as s:
        簿 = ctx.alerts
        # 一、六分钟前摔的,到现在没人吭声 —— 升到顶了(escalated=2,
        # channel=sound)。这一条是"没人看见"那个事实在报文里的样子。
        簿.raise_alert(kind="fallen", robot=狗, title="狗趴下了",
                       detail="姿态报翻倒,得有人去现场把它扶起来",
                       now_ms=T0 - 6 * 60_000)
        # 升级是真跑出来的,不是手填的。跑完之后它就到顶了,后台那条 _tick
        # 再调多少次也不会再动 —— 这一份夹具因此不看运气。
        簿.due_escalations(now_ms=T0)
        急停 = 簿.raise_alert(kind="estop_pressed", robot=狗, title="急停被按下",
                             detail="现场有人按了急停,松开之前谁也开不走",
                             now_ms=T0)
        # 三、同一个 kind 报两次 —— 聚合窗口内合成一条,count=2,而且
        # first_ms 跟 last_ms 是两个不同的数(手拼的夹具最容易把它们写成
        # 同一个,于是 Dart 那头把两个键读串了也没人发现)。
        落差 = 簿.raise_alert(kind="bundle_lag", robot=狗,
                             title="任务包落好了但没生效",
                             detail="盘上有 site-kl-4,current 还指着旧的",
                             now_ms=T0 - 10 * 60_000)
        簿.raise_alert(kind="bundle_lag", robot=狗, title="任务包落好了但没生效",
                       detail="盘上有 site-kl-4,current 还指着旧的",
                       now_ms=T0 - 2 * 60_000)
        簿.raise_alert(kind="run_done", robot=狗, title="这一趟跑完了",
                       detail="12 个点全过了,照片都归了档", now_ms=T0)
        # 确认和解决走的是真路由,不是簿子上的方法:那两条路由自己还管着
        # "确认必须记名"和"解决不许冒充确认",形状是它们定的。
        assert status(s, _告警路径(急停.key, "ack"), method="POST",
                      payload={"who": "老王"}) == 200
        assert status(s, _告警路径(落差.key, "resolve"), method="POST",
                      payload={"who": "小李"}) == 200
        份 = get_json(s, "/api/alerts/all")
        assert len(份["alerts"]) == 4, f"多出来的那条是哪儿来的: {份}"
        按种 = {a["kind"]: a for a in 份["alerts"]}
        assert 按种["fallen"]["escalated"] == 2
        assert 按种["fallen"]["channel"] == "sound"
        assert 按种["estop_pressed"]["acked_ms"] == T0
        assert 按种["bundle_lag"]["count"] == 2
        assert 按种["bundle_lag"]["resolved_ms"] == T0
        assert 按种["run_done"]["acked_ms"] is None
        对("alerts", 份)


def test_夹具_值守汇总(bridge, tmp_path, monkeypatch):
    """``GET /api/watch/summary`` 那一份(``app/watch.py`` 的六项)。

    **每一项都摆非缺省值。** 全走默认的那一份里,``clock_skew_s`` 是
    ``null``、``battery_pct`` 是 ``null``、``bundle_lag`` 是空列表、``mirror``
    是 ``null`` —— 那样一份夹具能证明的只有"这几个键存在",证明不了 Dart 那头
    把它们**解析对了**:``mirror`` 那个嵌套对象、``bundle_lag`` 那个字符串
    列表、``detail`` 那个字符串字典,一个都没被真的走过。所以这一份把能摆出
    非空的都摆上:盘 0.83、电量 36.0、钟偏 12.5 秒、盘上一个没生效的槽、一块
    落后一趟的镜像盘。

    **两处机器相关的读数换成定值,理由跟 ``test_夹具_盘况`` 那条一样。**

    * ``_disk`` —— 不换的话夹具记的是生成它那台机器上 D 盘的水位。
    * ``engine/backup._free_bytes`` —— ``plan_sync`` 拿镜像盘的剩余空间判
      ``full``,而这里的"镜像盘"是临时目录,量到的是跑测试这台机器的真盘。
      不换的话,一台快满的机器上生成出来的 ``mirror.full`` 是 ``true``,别人
      机器上是 ``false``,而这条差别跟被测的那份形状毫无关系。

    **``alerts_open`` 那一格是要轮询到位的,不是定长 sleep 等一等的。**
    下面那个 ``time.sleep(0.05)`` 是**轮询的间隔**,不是"等 0.05 秒就该好了"
    —— 退出条件是真实状态(电量到了、两条告警都在了),外加 20 秒硬上限兜底。
    盘水位摆成 0.83 就
    过了 ``disk_80`` 的报警线,盘上那个没生效的槽也会招来一条 ``bundle_lag``
    —— 这两条**是这份局面的正确后果,不是污染**:同一份事实喂给值守屏和喂给
    告警簿,本来就该同时出现在两处。但它们由租约看门狗那条协程隔半秒才量一
    次,所以要轮询到"这两条都在了"再取这一屏,取早了签进去的是 ``0``,而
    ``0`` 会在别人机器上稳定地红。电量同理:那是个推过来的事件。
    """
    import d1max_agent.engine.backup as _backup_mod
    import d1max_patrol.app.server as _server_mod
    monkeypatch.setattr(_server_mod, "_disk",
                        lambda _path: (830_000_000_000, 1_000_000_000_000))
    monkeypatch.setattr(_backup_mod, "_free_bytes",
                        lambda _mount: 500_000_000_000)
    包 = 摆包(tmp_path / "bundles", ["site-kl-3", "site-kl-4"],
             current="site-kl-3")
    盘架 = _盘架()
    ctx = make_ctx(bridge, tmp_path, removable=盘架, clock=lambda: T0,
                   # 本机钟比参照快 12.5 秒。**这个数够不着报警线**
                   # (``CLOCK_SKEW_ALARM_S`` 是 60 秒),所以它只让屏上那一格
                   # 有个真数,不会再多招一条 clock_skew 告警把下面那个
                   # alerts_open 的等法搅浑。
                   time_reference=lambda: (T0 - 12_500, "ntp"),
                   bundles_root=包)
    # 一趟没同步过去的归档 —— 镜像盘那一格的 behind 才会是 1 而不是 0。
    摆一趟(ctx.runs_root, "巡逻", "20250904T000000Z")
    盘架.disks = (摆镜像盘(ctx, tmp_path, "mirror",
                          last_sync_ms=T0 - 3_600_000),)
    with _起一台(bridge, ctx) as s:
        ctx.bridge.spawn(lambda: _推(ctx.device, BatteryEvent(36.0)))
        截止 = time.monotonic() + 20.0
        while True:
            份 = get_json(s, "/api/watch/summary")
            if 份["battery_pct"] == 36.0 and 份["alerts_open"] == 2:
                break
            assert time.monotonic() < 截止, f"这一屏没安定下来: {份}"
            time.sleep(0.05)
        assert 份["disk_used_ratio"] == 0.83, "值域是 0-1,不是 0-100"
        assert "disk_pct" not in 份, "旧键长回来了"
        assert 份["clock_skew_s"] == 12.5
        assert 份["bundle_lag"] == ["site-kl-4"]
        assert 份["upload_backlog"] is None, "这一卷恒为 None,不是 0"
        assert 份["battery_as_of_ms"] == T0
        assert 份["mirror"]["behind"] == 1
        assert 份["mirror"]["full"] is False
        assert 份["mirror"]["last_sync_ms"] == T0 - 3_600_000
        # 4153 = 4096 + 57:前者是 ``摆一趟()`` 默认的 ``size``(那张 a.jpg),
        # 后者是它那份 ``manifest.json`` 的 UTF-8 字节数(``{"mission":
        # {"name": "巡逻"}, "summary": {"photos": 1}}``,"巡逻"两个字各三字节)。
        # **钉这一条不是怕跨平台漂**:a.jpg 走的是 ``write_bytes``,而
        # ``json.dumps`` 不带 ``indent``,产出里一个换行符都没有,
        # ``newline=None`` 也就没有字符可翻译 —— 这个数跟平台无关。
        # 钉它是因为 ``摆一趟()`` 是**别的测试也在用的共享助手**:谁改了它的
        # 默认大小、或者往 manifest 里多塞一个键,这份夹具就跟着变,而那时候
        # 红的是下面 ``对()`` 的整份比对(读起来是"上线形状变了"),看的人会
        # 去查协议改了什么 —— 协议其实一个字没动。这一条把误诊挡在源头。
        assert 份["mirror"]["behind_bytes"] == 4153, "摆一趟() 摆的那两个文件变了"
        对("watch_summary", 份)
