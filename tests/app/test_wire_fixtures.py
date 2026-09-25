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
import json
import os
from pathlib import Path
from typing import Any

from d1max_agent.engine.machine import RunState
from d1max_patrol.app.video import CAMERAS, CameraFeed
from d1max_patrol.backends.base import DevicePoseEvent
from d1max_patrol.protocol.nav_types import Pose
from tests.app.conftest import get_json, status

# ``有pin的服务``/``墙钟`` 这两个名字在这个模块的普通代码里都不会被直接
# 引用到 —— 它们只出现在测试函数的参数列表里,那是 pytest 按名字做依赖
# 注入,不是这里的哪一行代码在读这个名字。ruff 看不出这层,会把它们当成
# "导入了没用上"(F401),再把测试函数参数里同名的那个当成"重复定义"
# (F811)。两边都是这个跨模块夹具写法本身带来的,不是真的死代码,所以
# 都挂 noqa,不改写法绕开它。
from tests.app.test_api_control import auth, 墙钟, 打, 有pin的服务, 解锁  # noqa: F401
from tests.app.test_video import fake_ffmpeg_freezes  # noqa: F401

夹具目录 = Path(__file__).resolve().parents[2] / "mobile" / "test" / "fixtures"

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
