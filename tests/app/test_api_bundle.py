"""任务包与排程的四条路由。

**时刻全靠 ``ctx.clock`` 注进来。** 这一整卷都是时间逻辑,一个 ``sleep`` 都
没有(§8.5 第 2 条):测试里要能把钟拨到 22:00,而不是等到 22:00。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest

import d1max_patrol.app.server as S
from d1max_patrol.app.server import MAX_ROLLBACK_REASON_LEN, AppServer
from d1max_patrol.engine.bundle import (
    LANDED,
    SCHEDULE_NAME,
    apply_bundle,
    build_bundle,
    land,
)
from d1max_patrol.engine.homing import HomePoint, save_home
from d1max_patrol.protocol.nav_types import Pose
from tests.app.conftest import get_json, make_ctx, request

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


def test_current链悬空的时候状态还是答得出来(装好):
    """评审 F1(修订轮 1)。``current`` 指着的槽目录被删掉(prune / 人手删 /
    ``bundles_root`` 被搬过)—— ``active_bundle()`` 对这种情况抛
    ``BundleError``(Task 8 收尾定的),但那不该让 ``manifest`` 是 ``None``
    这件事以外的任何东西也跟着炸:``current`` 指着谁,链名本身还在,
    ``read_bundle_state`` 不用管目录在不在就能答出来。
    """
    root, _ctx, s = 装好
    shutil.rmtree(root / "site-kl-1")
    got = get_json(s, "/api/bundle")
    assert got["state"]["current"] == "site-kl-1"
    assert got["manifest"] is None


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


def test_reason太长了是400(tmp_path, 装好):
    """评审 F3(修订轮 1)。``reason`` 原样写进狗盘的 ``landed.json`` 并逐次
    累加,没有上限的话客户端能把任意大的文本堆上去 —— 跟 ``engine/bundle.py``
    的 ``MAX_SN_LEN`` 挡的是同一类事。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    太长了 = "崩" * (MAX_ROLLBACK_REASON_LEN + 1)
    code, _b, _h = request(s, "/api/bundle/rollback", method="POST",
                           payload={"reason": 太长了})
    assert code == 400


def test_reason不是字符串是400(tmp_path, 装好):
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    code, _b, _h = request(s, "/api/bundle/rollback", method="POST",
                           payload={"reason": 12345})
    assert code == 400


def test_退过的那一版从这条路也上不去(tmp_path, 装好):
    """§3.2 的自动回退不许变成一个安静的死循环。

    **409,不是 400。** 评审定夺(修订轮 1):请求本身没毛病 —— 槽名合规、
    SN 也对 —— 是这台机器现在的状态不允许,跟 ``_bundle_rollback`` 那边
    「没有 previous 就是 409」同一个判据,不该在相邻路由上给出两个答案。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2"})
    assert code == 409


def test_连点两次回退第二次是409而且狗没被送回崩掉的那一版(tmp_path, 装好):
    """评审 F1(全卷终评)。手机双击、客户端超时重试、值守员不确定第一次成没成
    —— 这条序列在现场是**寻常操作**,不是攻击。

    黑名单闸本来只装在 ``/api/bundle/apply`` 那一扇门上,而「装回一个已知会崩
    的版本」有两扇门:换过去,和**退**过去。第二次回退的 ``previous`` 正是
    第一次刚拉黑的那一版。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    code, body, _h = request(s, "/api/bundle/rollback", method="POST",
                             payload={"reason": "首次执行就崩"})
    assert code == 200
    assert json.loads(body)["state"]["current"] == "site-kl-1"

    code, _b, _h = request(s, "/api/bundle/rollback", method="POST",
                           payload={"reason": "再点一次"})
    assert code == 409                      # 不是 200

    st = get_json(s, "/api/bundle")["state"]
    assert st["current"] == "site-kl-1"      # 没被送回刚崩掉的那一版
    assert "site-kl-2" in st["denied"]       # 仍然在黑名单里
    assert len(st["rollbacks"]) == 1         # 没多记一条


# ---- 强推那扇门(评审复评 finding 5) ------------------------------------

@pytest.mark.parametrize("不算数", ["true", "false", "1", 1, 0, "yes", None,
                                    [], {}])
def test_force不是字面量布尔就是400(tmp_path, 装好, 不算数):
    """**这扇门后面是「装一个已知会崩的版本」。**

    跟同一个文件里 ``reason`` 那几条同一条道理:一个 ``if body.get("force")``
    式的真值判断会把字符串 ``"false"``、``"0"`` 都当成真 —— 而现场发这个请求
    的人写的可能正是 ``"false"``。这道门宁可多拒一次,也不能靠猜。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2", "force": 不算数})
    assert code == 400
    assert "force" in body.decode("utf-8")
    assert get_json(s, "/api/bundle")["state"]["current"] == "site-kl-1"


def test_明确的force能把退过的那一版装回去(tmp_path, 装好):
    """``docs/任务包格式.md`` 对客户写着「除非人明确强推」—— 而路由上原来
    根本没有这扇门(评审复评 finding 5):文档承诺的那条路不存在,现场唯一
    能走的是 SSH 上去手工改链,那比强推危险得多。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})

    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2", "force": True})
    assert code == 200
    assert json.loads(body)["state"]["current"] == "site-kl-2"


def test_强推要留痕而且时刻是服务端盖的(tmp_path, 装好):
    """强推是「人明确地把一个已知会崩的版本装了回去」。**它必须留下痕迹**
    ——事后追这台狗为什么又崩了,这一笔是唯一的线索。

    时刻跟 ``rollbacks[].at`` 同一个来源:``ctx.clock``,服务端盖。请求体里
    带的一律不收 —— 一条能被客户端随便写的时间戳,追责的时候等于没有。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2", "force": True,
                     "at": "1999-01-01T00:00:00+08:00"})

    st = get_json(s, "/api/bundle")["state"]
    assert [f["slot"] for f in st["forced"]] == ["site-kl-2"]
    at = st["forced"][0]["at"]
    assert at.startswith("2026-09-07")
    assert "1999" not in at


def test_强推不是洗白下一次不带force照样是409(tmp_path, 装好):
    """**``denied`` 不许被强推清掉。**

    「这一次我知道我在干什么」跟「这一版从此可以随便装」是两句话。清掉的话,
    下一轮自动下发会安安静静地把这个崩过的版本再装一次 —— 那正是黑名单这道
    闸本来要拦的死循环,只不过多绕了一次人手。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2", "force": True})
    # 换回 v1,好让下一次 apply v2 走的是同一条判据而不是"已经指着它了"。
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-1"})

    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2"})
    assert code == 409
    assert "site-kl-2" in get_json(s, "/api/bundle")["state"]["denied"]


def test_force不跳SN那道闸(tmp_path, 装好):
    """``force`` 是给「退过的那一版」开的口子,**不是给「这份包不是给这台狗
    的」开的**。两件事混进一个开关里,现场只会剩下一个「加 force 就好了」的
    口诀 —— 然后一份别的站点的排程就在这台狗上跑起来了。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2, targets=["D1M-别的机器"]))
    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2", "force": True})
    assert code == 400
    assert "targets" in body.decode("utf-8")


def test_没被拉黑的时候传force什么也不记(tmp_path, 装好):
    """对一个本来就没被拉黑的槽传 ``force``,什么也没顶开。记下来只会把这份
    历史冲成噪音 —— 而它是给人看「谁在什么时候硬来过」的。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2", "force": True})
    assert code == 200
    assert get_json(s, "/api/bundle")["state"]["forced"] == []


def test_响应说得出这一次到底顶开了黑名单没有(tmp_path, 装好):
    """**评审复评第 3 轮 N4。** 留痕只在真顶开那一次才记(取舍不变),于是
    「传了 force、200 OK、``forced`` 里什么也没多」这件事在调用方看来无从
    分辨 —— 那正是挂账的 F8「静默降级」换了个位置。这个布尔把它变回看得见的。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})

    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2", "force": True})
    assert code == 200
    答 = json.loads(body)
    assert 答["overrode_denied"] is True                    # 真顶开了
    assert [f["slot"] for f in 答["state"]["forced"]] == ["site-kl-2"]


def test_没顶开任何东西的强推响应里说得清楚(tmp_path, 装好):
    """同 N4 的另一半:``force`` 传了,但那一版本来就没被拉黑 ——
    ``forced`` 里一个字不多,响应里这个布尔是 ``false``。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2", "force": True})
    assert code == 200
    答 = json.loads(body)
    assert 答["overrode_denied"] is False
    assert 答["state"]["forced"] == []


def test_寻常的apply这个布尔永远是false(tmp_path, 装好):
    """不带 ``force`` 的 apply 走不到那扇门,所以它永远什么也没顶开。"""
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2"})
    assert code == 200
    assert json.loads(body)["overrode_denied"] is False


def test_landed里forced不是列表强推也不是500(tmp_path, 装好):
    """**评审复评第 3 轮 N6 的端到端症状。**

    ``记.setdefault("forced", []).append(...)`` 遇到非 list 抛
    ``AttributeError``,而这条路由只接 ``BundleError`` —— 于是一份被人手改过
    的 ``landed.json`` 让强推冒成一个 500。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    记["forced"] = "上一版写坏了"
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                                      encoding="utf-8")

    code, body, _h = request(s, "/api/bundle/apply", method="POST",
                             payload={"slot": "site-kl-2", "force": True})
    assert code == 200
    答 = json.loads(body)
    assert 答["overrode_denied"] is True
    assert [f["slot"] for f in 答["state"]["forced"]] == ["site-kl-2"]


def test_force是false跟不传一样(tmp_path, 装好):
    """字面量 ``false`` 是合法的,而且必须**什么都不改变** —— 不然客户端
    「显式写清楚」这个好习惯反而会踩雷。
    """
    root, _ctx, s = 装好
    land(root, 打包(tmp_path, "b", 2))
    request(s, "/api/bundle/apply", method="POST",
            payload={"slot": "site-kl-2"})
    request(s, "/api/bundle/rollback", method="POST", payload={"reason": "崩了"})
    code, _b, _h = request(s, "/api/bundle/apply", method="POST",
                           payload={"slot": "site-kl-2", "force": False})
    assert code == 409


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


def test_current链悬空的时候是409不是500(装好):
    """评审 F1(修订轮 1)。``active_bundle()`` 对悬空链抛的是 ``BundleError``
    (Task 8 收尾定的);这条路由「取不到包就没有排程可算」,得跟排程本身坏掉
    一样翻成 409 —— **不能是未捕获异常冒出来的 500**,``detail`` 里也不许有
    traceback。
    """
    root, _ctx, s = 装好
    shutil.rmtree(root / "site-kl-1")
    code, body, _h = request(s, "/api/schedule")
    assert code == 409
    text = body.decode("utf-8")
    assert "Traceback" not in text
    assert "site-kl-1" in text


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


def _post(server, path: str, payload=None):
    code, body, _ = request(server, path, method="POST", payload=payload or {})
    return code, body


# ---- 排程执行器(W06) ---------------------------------------------------
#
# 以前 ``/api/schedule`` 那条路由的 docstring 写着「到点自动出发这件事,整个仓库里
# 没有人做」。下面这几条钉的就是那件事:到点起跑、起过就不再起、重启后靠归档
# 回查、正在跑/有人握着控制权就不起。


夜巡原点 = HomePoint(map_id="floor1", pose=Pose.from_xy_yaw(0.0, 0.0),
                 marked_at_ms=1_757_000_000_000)


def _装好带原点(tmp_path, 起一台, *, clock, period: float = 0.3):
    """包生效、任务的那张图标了原点(起飞门槛要)、执行器拨快。"""
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1))
    apply_bundle(root, "site-kl-1")
    ctx, s = 起一台(bundles_root=root, clock=clock)
    save_home(ctx.mapping.maps_dir, 夜巡原点)      # 在第一拍(period)之前
    return ctx, s


def _等到(条件, 秒: float = 5.0) -> None:
    deadline = time.monotonic() + 秒
    while time.monotonic() < deadline:
        if 条件():
            return
        time.sleep(0.02)
    raise AssertionError("等超时")


def test_到点了执行器自己把任务起跑(tmp_path, 起一台, monkeypatch):
    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    ctx, s = _装好带原点(tmp_path, 起一台, clock=lambda: 毫秒(9, 7, 22, 10))
    _等到(lambda: ctx.engine.running)
    assert ctx.engine.snapshot.mission == "night"
    body = get_json(s, "/api/schedule")
    assert body["executor"]["last_started"]["night-1"] > 0
    assert body["entries"][0]["decision"]["kind"] == "not_yet", "起过这一轮了,别再判 due"


def test_起过一次就不再起_哪怕这趟跑完了(tmp_path, 起一台, monkeypatch):
    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    ctx, s = _装好带原点(tmp_path, 起一台, clock=lambda: 毫秒(9, 7, 22, 10))
    _等到(lambda: ctx.engine.running)
    ctx.bridge.call(lambda: ctx.engine.abort("测试:结束这趟"), timeout_s=10.0)
    ctx.bridge.call(lambda: ctx.engine.wait_done(timeout_s=10.0), timeout_s=15.0)
    time.sleep(1.0)                                       # 再过几拍
    assert not ctx.engine.running, "同一轮起了第二次"


def test_重启后靠归档回查_已经起过的那一轮不重复起(tmp_path, 起一台, monkeypatch):
    """服务重启内存清零,``last_started`` 得从 runs 目录回查:22:05 已经跑过一趟,
    22:10 起来的执行器不能再起。"""
    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    run = tmp_path / "runs" / "night" / "20260907T140500Z"     # 22:05 吉隆坡 = 14:05Z
    run.mkdir(parents=True)
    (run / "manifest.json").write_text("{}", encoding="utf-8")
    ctx, s = _装好带原点(tmp_path, 起一台, clock=lambda: 毫秒(9, 7, 22, 10))
    time.sleep(1.0)
    assert not ctx.engine.running
    assert get_json(s, "/api/schedule")["entries"][0]["decision"]["kind"] == "not_yet"


def test_还没到点就不起(tmp_path, 起一台, monkeypatch):
    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    ctx, _s = _装好带原点(tmp_path, 起一台, clock=lambda: 毫秒(9, 7, 21, 0))
    time.sleep(1.0)
    assert not ctx.engine.running


def test_有人握着控制权就不自动起跑(tmp_path, bridge, monkeypatch):
    """人正拿着手机开狗(或刚要开),排程不能从他手里把腿抢走;等他还了再起。

    执行器看的是 ``ControlDesk.sweep()`` 之后的租约簿 —— 没有活会话的租约会被
    它当场释放,所以这里得用一台**设了 PIN** 的服务、换真 token、真取控制权。
    """
    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1))
    apply_bundle(root, "site-kl-1")
    now = 毫秒(9, 7, 22, 10)                         # 已经到点;钟不走,租约不会过期
    ctx = make_ctx(bridge, tmp_path, bundles_root=root, clock=lambda: now)
    s = AppServer(ctx, port=0, pin="246810")
    s.start()
    try:
        save_home(ctx.mapping.maps_dir, 夜巡原点)
        code, body, _ = request(s, "/api/auth", method="POST",
                                payload={"pin": "246810", "operator": "张三"})
        assert code == 200, body
        tok = {"Authorization": f"Bearer {json.loads(body)['token']}"}
        code, body, _ = request(s, "/api/control/acquire", method="POST",
                                payload={}, headers=tok)
        assert code == 200, body
        time.sleep(1.0)
        assert not ctx.engine.running, "有人握着控制权,排程不该起"
        code, body, _ = request(s, "/api/schedule", headers=tok)
        assert "控制权" in json.loads(body)["executor"]["last_skip"]
        code, _, _ = request(s, "/api/control/release", method="POST", payload={},
                             headers=tok)
        assert code == 200
        _等到(lambda: ctx.engine.running)
    finally:
        s.stop()
        with contextlib.suppress(Exception):
            bridge.call(ctx.engine.aclose, timeout_s=10.0)


def test_到点了但引擎在跑_记成没轮到而不是静默丢掉(tmp_path, 起一台, monkeypatch):
    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    now = [毫秒(9, 7, 21, 0)]
    ctx, s = _装好带原点(tmp_path, 起一台, clock=lambda: now[0])
    # 人手起一趟别的任务(假导航到不了,就一直跑着)
    request(s, "/api/missions/" + quote("巡检一号"), method="PUT",
            payload={"mission": "巡检一号", "map_id": "map_test",
                     "waypoints": [{"name": "P1", "pose": {"position": {"x": 1, "y": 0, "z": 0},
                                    "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}},
                                    "actions": []}],
                     "policy": {}})
    assert _post(s, "/api/missions/" + quote("巡检一号") + "/run")[0] == 200
    _等到(lambda: ctx.engine.running)
    now[0] = 毫秒(9, 7, 22, 10)
    _等到(lambda: "night-1" in get_json(s, "/api/schedule")["executor"]["last_displaced"])
    assert ctx.engine.snapshot.mission == "巡检一号", "排程不许打断正在跑的"


def test_钟不可信就不按钟出发(tmp_path, 起一台, monkeypatch):
    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    now = 毫秒(9, 7, 22, 10)
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1))
    apply_bundle(root, "site-kl-1")
    ctx, s = 起一台(bundles_root=root, clock=lambda: now,
                   time_reference=lambda: (now - 3 * 3600 * 1000, "手机"))   # 本地钟快了 3 小时
    save_home(ctx.mapping.maps_dir, 夜巡原点)
    time.sleep(1.0)
    assert not ctx.engine.running
    assert "钟" in get_json(s, "/api/schedule")["executor"]["last_skip"]


def test_起飞检查期间有人拿了控制权_也不起(tmp_path, bridge, monkeypatch):
    """检查后使用竞态(外部审核):第一次控制权检查过了,起飞检查里有好几个 await,
    这期间操作员拿到控制权,起跑前不再查的话就从他手里把腿抢走了。这里把起飞
    检查卡在半路,中途用真会话取控制权,再放行。"""
    import threading

    monkeypatch.setattr(S, "_SCHEDULE_PERIOD_S", 0.3)
    root = tmp_path / "bundles"
    land(root, 打包(tmp_path, "a", 1))
    apply_bundle(root, "site-kl-1")
    now = 毫秒(9, 7, 22, 10)
    ctx = make_ctx(bridge, tmp_path, bundles_root=root, clock=lambda: now)
    s = AppServer(ctx, port=0, pin="246810")
    进了检查 = threading.Event()
    放行 = threading.Event()
    原来的 = S._preflight_with_scan

    async def 卡在半路(ctx_, mission, home):
        进了检查.set()
        while not 放行.is_set():
            await asyncio.sleep(0.01)
        return await 原来的(ctx_, mission, home)

    monkeypatch.setattr(S, "_preflight_with_scan", 卡在半路)
    s.start()
    try:
        save_home(ctx.mapping.maps_dir, 夜巡原点)
        assert 进了检查.wait(5.0), "执行器没进起飞检查"
        code, body, _ = request(s, "/api/auth", method="POST",
                                payload={"pin": "246810", "operator": "李四"})
        tok = {"Authorization": f"Bearer {json.loads(body)['token']}"}
        code, body, _ = request(s, "/api/control/acquire", method="POST",
                                payload={}, headers=tok)
        assert code == 200, body
        放行.set()
        time.sleep(1.0)
        assert not ctx.engine.running, "起飞检查期间拿到控制权,排程还是起了"
        code, body, _ = request(s, "/api/schedule", headers=tok)
        assert "控制权" in json.loads(body)["executor"]["last_skip"]
        code, _, _ = request(s, "/api/control/release", method="POST", payload={},
                             headers=tok)
        assert code == 200
        _等到(lambda: ctx.engine.running)
    finally:
        s.stop()
        with contextlib.suppress(Exception):
            bridge.call(ctx.engine.aclose, timeout_s=10.0)
