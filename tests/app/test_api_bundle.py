"""任务包与排程的四条路由。

**时刻全靠 ``ctx.clock`` 注进来。** 这一整卷都是时间逻辑,一个 ``sleep`` 都
没有(§8.5 第 2 条):测试里要能把钟拨到 22:00,而不是等到 22:00。
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from d1max_patrol.app.server import AppServer
from d1max_patrol.engine.bundle import (
    SCHEDULE_NAME,
    apply_bundle,
    build_bundle,
    land,
)

from .conftest import get_json, make_ctx, request

吉隆坡 = ZoneInfo("Asia/Kuala_Lumpur")
时刻 = "2026-09-07T14:03:00+08:00"

排程一条 = """\
timezone: Asia/Kuala_Lumpur
entries:
  - id: night-1
    mission: night
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 45
    on_missed: skip
"""

一个任务 = {
    "mission": "night",
    "map_id": "floor1",
    "waypoints": [{
        "name": "P1",
        "pose": {"position": {"x": 1.2, "y": 3.4, "z": 0.0},
                 "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
        "actions": [{"type": "photo", "camera": "front"}],
    }],
    "policy": {},
}


def 毫秒(月: int, 日: int, 时: int, 分: int) -> int:
    """吉隆坡时间的那一刻,UTC 毫秒。"""
    return int(datetime(2026, 月, 日, 时, 分, tzinfo=吉隆坡).timestamp() * 1000)


def 打包(tmp_path, 记号: str, version: int, *, 排程: str = 排程一条,
        targets=()):
    """打一个包出来,返回包目录。"""
    src = tmp_path / f"src-{记号}"
    (src / "missions").mkdir(parents=True)
    (src / "missions" / "night.json").write_text(
        json.dumps(一个任务, ensure_ascii=False), encoding="utf-8")
    (src / SCHEDULE_NAME).write_text(排程, encoding="utf-8")
    return build_bundle(src, tmp_path / f"staged-{记号}",
                        bundle_id="site-kl", version=version, built_at=时刻,
                        targets=targets)


@pytest.fixture
def 起一台(bridge, tmp_path):
    """起一台真服务,钟是拨得动的。返回 ``(ctx, server)``。

    收尾照 ``conftest.ctx`` 那个 fixture 的做法:不收引擎的话,那条协程会活
    到桥停为止,而它中途还会往一个 pytest 正在删的临时目录里写。
    """
    起过 = []

    def 造(*, bundles_root, clock=None, time_reference=None):
        c = make_ctx(bridge, tmp_path, bundles_root=bundles_root,
                     clock=clock, time_reference=time_reference)
        s = AppServer(c, port=0)
        s.start()
        起过.append((c, s))
        return c, s

    yield 造
    for c, s in 起过:
        s.stop()
        with contextlib.suppress(Exception):
            bridge.call(c.engine.aclose, timeout_s=10.0)


@pytest.fixture
def 装好(tmp_path, 起一台):
    """一台装好一个包、包已经生效的狗。钟停在 21:00。"""
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1))
    apply_bundle(root, "site-kl-1")
    ctx, s = 起一台(bundles_root=root, clock=lambda: 毫秒(9, 7, 21, 0))
    return root, ctx, s


# ---- GET /api/bundle -----------------------------------------------------

def test_读得到当前是哪一版(装好):
    _root, _ctx, s = 装好
    body = get_json(s, "/api/bundle")
    assert body["state"]["current"] == "site-kl-1"
    assert body["state"]["previous"] == ""
    assert body["state"]["proven"] is False
    assert body["manifest"]["bundle_id"] == "site-kl"
    assert body["manifest"]["version"] == 1


def test_心跳要的三样都在(装好):
    """§3.2:心跳报 ``(bundle_id, version, content_hash)``。**三样都要。**

    只报版本号的话,一个在路上被改过内容的包,在服务器眼里跟好包长得一模
    一样 —— 而那正是 ``content_sha256`` 唯一要防的那件事。
    """
    _root, _ctx, s = 装好
    m = get_json(s, "/api/bundle")["manifest"]
    assert set(m) >= {"bundle_id", "version", "content_sha256"}


def test_一个包都没有的时候也答得出来(tmp_path, 起一台):
    """**不是 500,也不是 404。**

    一台还没下过包的新狗是正常状态,不是故障。值守屏要能看到「这台是空的」。
    """
    root = tmp_path / "bundles"
    root.mkdir()
    _ctx, s = 起一台(bundles_root=root)
    body = get_json(s, "/api/bundle")
    assert body["state"]["current"] == ""
    assert body["manifest"] is None


def test_包被改过的时候状态还是答得出来(装好):
    """内容对不上哈希 ⇒ ``manifest`` 是 ``None``;但 ``current`` 指着谁这件事
    仍然要答得出来 —— 那正是出事之后第一个要看的东西。
    """
    root, _ctx, s = 装好
    (root / "site-kl-1" / SCHEDULE_NAME).write_text("改过了\n",
                                                    encoding="utf-8")
    body = get_json(s, "/api/bundle")
    assert body["state"]["current"] == "site-kl-1"
    assert body["manifest"] is None


# ---- POST /api/bundle/apply ---------------------------------------------

def test_生效另一版(tmp_path, 装好):
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    code, body, _ = request(s, "/api/bundle/apply", method="POST",
                            payload={"slot": "site-kl-2"})
    assert code == 200
    st = json.loads(body)["state"]
    assert st["current"] == "site-kl-2"
    assert st["previous"] == "site-kl-1"


def test_生效一个不存在的槽是400(装好):
    _root, _ctx, s = 装好
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-9"})
    assert code == 400


def test_不给slot是400(装好):
    _root, _ctx, s = 装好
    code, _b, _h = request(s, "/api/bundle/apply", method="POST", payload={})
    assert code == 400


def test_槽名带路径分隔符也是400(装好):
    """``slot`` 会被拼进路径。**这条是路径穿越那道闸的接口侧。**"""
    _root, _ctx, s = 装好
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "../../etc"})
    assert code == 400


def test_生效一个被改过的包是400而且链不动(tmp_path, 装好):
    """**换链之前先校验。** 换完再校验的话,坏包已经在跑了。"""
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    (root / "site-kl-2" / SCHEDULE_NAME).write_text("改过了\n",
                                                    encoding="utf-8")
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2"})
    assert code == 400
    assert get_json(s, "/api/bundle")["state"]["current"] == "site-kl-1"


def test_发错机器的包换不上去而且链不动(tmp_path, 装好):
    """§3.1 的目标 SN(定夺 13)。``make_ctx`` 那台的 SN 是 ``D1M-TEST``。

    **SN 是服务端从 ``ctx.identity`` 自己填的,不看请求体。** 让调用方报
    「我是谁」,这道闸就等于不存在。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2, targets=["D1M-别的机器"]))
    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2"})
    assert code == 400
    assert "targets" in body.decode("utf-8")
    assert get_json(s, "/api/bundle")["state"]["current"] == "site-kl-1"


def test_请求体里塞sn也没用(tmp_path, 装好):
    """**这条是上一条的另一半。** 上一条证明闸拦得住,这条证明它绕不过去。"""
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2, targets=["D1M-别的机器"]))
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2", "sn": "D1M-别的机器"})
    assert code == 400


def test_名单里有本机就换得上(tmp_path, 装好):
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2, targets=["D1M-TEST"]))
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2"})
    assert code == 200
    assert get_json(s, "/api/bundle")["manifest"]["targets"] == ["D1M-TEST"]


# ---- POST /api/bundle/rollback ------------------------------------------

def test_没有previous退不了是409(装好):
    """**409,不是 400。**

    请求本身没毛病,是这台机器现在的状态不允许。这个区别决定了手机上弹的是
    「参数错了」还是「现在退不了」—— 后者才是真的。
    """
    _root, _ctx, s = 装好
    code, _b, _h = request(s, "/api/bundle/rollback", method="POST",
                           payload={"reason": "崩了"})
    assert code == 409


def test_退回上一版(tmp_path, 装好):
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    code, body, _ = request(s, "/api/bundle/rollback", method="POST",
                            payload={"reason": "首次执行就崩"})
    assert code == 200
    st = json.loads(body)["state"]
    assert st["current"] == "site-kl-1"
    assert st["previous"] == "site-kl-2"
    assert len(st["rollbacks"]) == 1
    assert st["rollbacks"][0]["from"] == "site-kl-2"
    assert st["rollbacks"][0]["to"] == "site-kl-1"
    assert st["rollbacks"][0]["reason"] == "首次执行就崩"


def test_退回的时刻是服务端盖的不是请求里说的(tmp_path, 装好):
    """**请求里带的 ``at`` 一律不收。**

    手机的钟可以是任何值,而这条记录是事后追责用的:一条能被客户端随便写的
    时间戳,追责的时候等于没有。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    _c, body, _h = request(s, "/api/bundle/rollback", method="POST",
                           payload={"reason": "崩了",
                                    "at": "1999-01-01T00:00:00+08:00"})
    at = json.loads(body)["state"]["rollbacks"][0]["at"]
    assert at.startswith("2026-09-07")
    assert "1999" not in at


def test_退过的那一版从这条路也上不去(tmp_path, 装好):
    """§3.2 的自动回退不许变成一个安静的死循环。"""
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2"})
    assert code == 400


# ---- GET /api/schedule ---------------------------------------------------

def test_排程读得出来(装好):
    _root, _ctx, s = 装好
    body = get_json(s, "/api/schedule")
    assert body["timezone"] == "Asia/Kuala_Lumpur"
    assert [e["id"] for e in body["entries"]] == ["night-1"]
    assert body["entries"][0]["mission"] == "night"


def test_每条排程带着此刻的决定和下一轮(装好):
    """21:00 问:还没到点,下一轮是今晚 22:00。"""
    _root, _ctx, s = 装好
    e = get_json(s, "/api/schedule")["entries"][0]
    assert e["decision"]["kind"] == "not_yet"
    assert e["next_run"].startswith("2026-09-07T22:00")


def test_到点了就说到点了(tmp_path, 起一台):
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1))
    apply_bundle(root, "site-kl-1")
    _ctx, s = 起一台(bundles_root=root, clock=lambda: 毫秒(9, 7, 22, 10))
    e = get_json(s, "/api/schedule")["entries"][0]
    assert e["decision"]["kind"] == "due"
    assert e["decision"]["late_min"] == 10


def test_用的是包里那个时区不是系统的(装好):
    """§3.3 第 1 条。

    ``ctx.clock`` 给的是一个 UTC 毫秒数;转成本地时刻用的必须是
    ``schedule.yaml`` 里写的那个时区,而不是这台机器的系统时区。
    """
    _root, _ctx, s = 装好
    body = get_json(s, "/api/schedule")
    assert body["now"].startswith("2026-09-07T21:00")
    assert body["now"].endswith("+08:00")


def test_没有参照的时候漂移是不知道不是零(装好):
    """§3.3 第 4 条。断网时没有 NTP。

    **报 0 等于说「钟是准的」**,而那正是断网久了之后最不可能成立的一句话。
    """
    _root, _ctx, s = 装好
    assert get_json(s, "/api/schedule")["clock"] == {
        "clock_skew_s": None, "alarm": False, "source": "unknown"}


def test_有参照就报得出漂移而且会告警(tmp_path, 起一台):
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1))
    apply_bundle(root, "site-kl-1")
    _ctx, s = 起一台(bundles_root=root,
                    clock=lambda: 毫秒(9, 7, 21, 0) + 90_000,
                    time_reference=lambda: (毫秒(9, 7, 21, 0), "ntp"))
    assert get_json(s, "/api/schedule")["clock"] == {
        "clock_skew_s": 90.0, "alarm": True, "source": "ntp"}


def test_没有包的时候排程是空的不是500(tmp_path, 起一台):
    root = tmp_path / "bundles"
    root.mkdir()
    _ctx, s = 起一台(bundles_root=root)
    body = get_json(s, "/api/schedule")
    assert body["entries"] == []
    assert body["timezone"] == ""
    # 钟这一段跟有没有包无关 —— 一台空狗的钟照样可能是歪的。
    assert body["clock"]["source"] == "unknown"


def test_包里排程坏了要说清楚是哪儿坏了(tmp_path, 起一台):
    """**409 而不是 500。**

    现场的人看到 500 只能猜;看到「schedule 缺少 timezone」就能自己修 ——
    而这台狗此刻在一个没有网的地方。
    """
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1, 排程="entries: []\n"))
    apply_bundle(root, "site-kl-1")
    _ctx, s = 起一台(bundles_root=root)
    code, body, _ = request(s, "/api/schedule")
    assert code == 409
    assert "timezone" in body.decode("utf-8")


def test_这条路由不会真起跑任何任务(装好):
    """**这条路由是「看」,不是「跑」。**

    ``decide()`` 说 due 了,它也只是把这句话答出来 —— 真起跑是第 8 卷排程器
    的事。问三遍,归档目录里不该多出任何东西。
    """
    _root, ctx, s = 装好
    ctx.runs_root.mkdir(parents=True, exist_ok=True)
    之前 = sorted(p.name for p in ctx.runs_root.iterdir())
    for _ in range(3):
        get_json(s, "/api/schedule")
    assert sorted(p.name for p in ctx.runs_root.iterdir()) == 之前
