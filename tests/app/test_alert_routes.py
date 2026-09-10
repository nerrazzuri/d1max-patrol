"""告警的四条路由(§5.3 记名确认)。

**这些用例起的是真服务、打真 HTTP**(跟 ``tests/app`` 里其余的一样)。这一层
真正会出问题的地方全在 HTTP 那一侧: 告警键形如 ``robot/kind#seq``,里头**同时
带斜杠和井号** —— 斜杠在路由匹配那一步会把参数切断,井号在客户端那一步就被
当成片段丢掉了。直接调处理函数一个都测不出来。

**挂账 76:** 这台开发机的盘水位过了 ``disk_80`` 的线,只要起真服务,那条 P2 就
合法常驻。所以这里一条 ``alerts.open() == ()`` 式的断言都没有,全部按自己造的
那几个 ``key`` 去取行。
"""

from __future__ import annotations

import contextlib
import json
import time
from urllib.parse import quote

import pytest

from d1max_patrol.app.server import AppServer
from d1max_patrol.engine.alerts import ESCALATE_AFTER_MS
from tests.app.conftest import get_json, make_ctx, request

T0 = 1_757_000_000_000

#: ``make_ctx`` 里那份假身份的序列号。告警键的第一段就是它。
ROBOT = "D1M-TEST"


class 假墙钟:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def 前进(self, ms: int) -> None:
        self.t += ms


@pytest.fixture
def 钟():
    return 假墙钟()


@pytest.fixture
def 服务(bridge, tmp_path, 钟):
    ctx = make_ctx(bridge, tmp_path, clock=钟)
    s = AppServer(ctx, port=0)
    s.start()
    yield s
    s.stop()
    with contextlib.suppress(Exception):
        bridge.call(ctx.engine.aclose, timeout_s=10.0)


def 报一条(服务, 钟, kind: str, *, title: str = "出事了", robot: str = ROBOT):
    """往簿子里记一条。**直接走 ``AlertBook``** —— 事实源怎么产生告警是任务
    6/7 的事,这一组测的是路由。"""
    return 服务.alerts.raise_alert(kind=kind, robot=robot, title=title,
                                   now_ms=钟.t)


def 路径(key: str, 动作: str = "") -> str:
    """把一个告警键拼成 URL。**整段转义** —— 键里的 ``/`` 和 ``#`` 都不是
    URL 的结构字符,原样拼上去一个会切参数、一个会被当片段丢掉。"""
    tail = f"/{动作}" if 动作 else ""
    return f"/api/alerts/{quote(key, safe='')}{tail}"


def 打(服务, path: str, payload=None):
    code, body, _ = request(服务, path, method="POST", payload=payload or {})
    return code, json.loads(body)


def 取(服务, key: str, 哪条: str = "/api/alerts"):
    行 = [a for a in get_json(服务, 哪条)["alerts"] if a["key"] == key]
    assert len(行) == 1, (key, 哪条, 行)
    return 行[0]


def 键们(服务, 哪条: str = "/api/alerts") -> set[str]:
    return {a["key"] for a in get_json(服务, 哪条)["alerts"]}


# --------------------------------------------------------------- 记名确认


def test_确认要记名_空的话400(服务, 钟):
    """空姓名的确认等于没人负责,升级链就没有停下来的依据(§5.3)。"""
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    码, 体 = 打(服务, 路径(a.key, "ack"), {"who": ""})
    assert 码 == 400, 体
    assert "记名" in 体["error"], 体
    assert 取(服务, a.key)["acked_ms"] is None


def test_确认要记名_全是空格也不算(服务, 钟):
    """"  " 跟 "" 是同一件事:屏幕上认不出是谁确认的。"""
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    码, 体 = 打(服务, 路径(a.key, "ack"), {"who": "   "})
    assert 码 == 400, 体
    assert "记名" in 体["error"], 体


def test_确认记下的是谁(服务, 钟):
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    码, 体 = 打(服务, 路径(a.key, "ack"), {"who": "老王"})
    assert 码 == 200, 体
    行 = 取(服务, a.key)
    assert 行["acked_by"] == "老王"
    assert 行["acked_ms"] == 钟.t
    # 确认不是解决 —— 事情还在,还得留在未解决那张表上。
    assert 行["resolved_ms"] is None


def test_确认了升级就停(服务, 钟):
    """§5.3:升级只看有没有人确认。

    **对照那一条是这个用例的全部分量。** ``escalated`` 是个天然容易恒零的
    字段,光断"确认过的那条是 0"跟把实现整个删掉写死 0 是分不出来的。所以
    这里自己驱一次升级判定,并且**同一次判定**里放一条没人确认的:它升到了
    顶档,上面那个 0 才叫"确认把升级停住了"。

    (任务 12 之前,这条判定在全仓没有任何一处生产调用点 —— 也就是说线上
    从来没升过级。驱动者现在挂在闸门那一拍上,见
    ``test_升级真的有人在驱动``。这里仍然自己驱一次:这条测的是"确认停住
    升级"这条规矩本身,不该跟着那条协程的节拍走。)
    """
    卡住 = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    没人管 = 报一条(服务, 钟, "fallen", title="狗倒了")
    assert 打(服务, 路径(卡住.key, "ack"), {"who": "老王"})[0] == 200

    钟.前进(ESCALATE_AFTER_MS[1] + 1)
    服务.alerts.due_escalations(now_ms=钟.t)

    assert 取(服务, 卡住.key)["escalated"] == 0
    assert 取(服务, 卡住.key)["channel"] == "screen"
    对照 = 取(服务, 没人管.key)
    assert 对照["escalated"] == len(ESCALATE_AFTER_MS), 对照
    assert 对照["channel"] == "sound", 对照


def test_解决不停升级(服务, 钟):
    """解决了但没人看见,照样升级(§5.3)。跟上面那条正好是一对。"""
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    assert 打(服务, 路径(a.key, "resolve"))[0] == 200
    钟.前进(ESCALATE_AFTER_MS[1] + 1)
    服务.alerts.due_escalations(now_ms=钟.t)
    行 = 取(服务, a.key, "/api/alerts/all")
    assert 行["escalated"] == len(ESCALATE_AFTER_MS), 行
    assert 行["channel"] == "sound", 行


def test_确认和解决是两条路由两个字段(服务, 钟):
    a = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    assert 打(服务, 路径(a.key, "resolve"))[0] == 200
    行 = 取(服务, a.key, "/api/alerts/all")
    assert 行["resolved_ms"] is not None
    assert 行["acked_ms"] is None          # 解决了,但没有人看见过
    assert 行["acked_by"] == ""


# ------------------------------------------------------------------ 两张表


def test_P1排在最上面(服务, 钟):
    """人扫一眼屏幕,要在第一行看到该起身的那件事。"""
    报一条(服务, 钟, "run_done", title="这趟跑完了")     # P3
    报一条(服务, 钟, "finding", title="配电柜有痕迹")     # P2
    报一条(服务, 钟, "stuck", title="狗卡在楼梯口")       # P1
    levels = [a["level"] for a in get_json(服务, "/api/alerts")["alerts"]]
    assert levels == sorted(levels), levels   # P1 < P2 < P3,字典序正好对
    # 光断有序不够:一张空表也是有序的。第一行必须真是那条 P1。
    assert levels[0] == "P1", levels


def test_同级里新的在上面(服务, 钟):
    """同一档里按 ``last_ms`` 倒序 —— 刚出的事在上面。"""
    早 = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    钟.前进(60_000)
    晚 = 报一条(服务, 钟, "fallen", title="狗倒了")
    表 = [a["key"] for a in get_json(服务, "/api/alerts")["alerts"]]
    assert 表.index(晚.key) < 表.index(早.key), 表


def test_未解决那张表里没有已解决的(服务, 钟):
    留着 = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    消掉 = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    assert 打(服务, 路径(消掉.key, "resolve"))[0] == 200
    开着 = 键们(服务)
    assert 留着.key in 开着
    assert 消掉.key not in 开着


def test_交接班那张表连已解决的一起给(服务, 钟):
    """``/api/alerts/all`` 是给交接班看的 —— 这一班处理掉的事也得留在上面。"""
    消掉 = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    assert 打(服务, 路径(消掉.key, "resolve"))[0] == 200
    assert 消掉.key in 键们(服务, "/api/alerts/all")


def test_线上字典带得出该不该出声(服务, 钟):
    """裁决十一:``channel`` 跟 ``escalated`` 都要上线。

    手机拿到 ``escalated`` 这个整数还得自己重算一遍 ``Level -> Channel``,
    同一份判据落在两处,早晚分叉 —— 而分叉的那天正好是该响的那次没响。
    """
    a = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    行 = 取(服务, a.key)
    assert 行["channel"] == "screen", 行
    assert 行["escalated"] == 0, 行


# ------------------------------------------------------- 键里的斜杠和井号


def test_键里的斜杠和井号原样走完一趟HTTP(服务, 钟):
    """告警键是 ``robot/kind#seq``,两个字符都是 URL 的结构字符。

    这条钉的是**整条链**: 客户端转义 -> ``_dispatch`` 那一步 ``unquote``
    -> 跨斜杠的那个占位符把整段捞回来 -> ``AlertBook`` 那本字典查得到。
    中间任何一环退回"占位符不跨斜杠",这里就是 404。
    """
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    assert "/" in a.key and "#" in a.key, a.key
    码, 体 = 打(服务, 路径(a.key, "ack"), {"who": "老王"})
    assert 码 == 200, (a.key, 码, 体)
    assert 体["alert"]["key"] == a.key
    assert 取(服务, a.key)["acked_by"] == "老王"


def test_键不转义就到不了(服务, 钟):
    """反过来钉住上面那条为什么必须转义。

    ``#`` 在客户端就被当成片段切掉了(服务端一个字节都收不到),剩下的
    ``/api/alerts/D1M-TEST/stuck`` 谁也匹配不上。**这不是服务端能兜的事** ——
    手机端拼 URL 时必须整段转义,这一条就是那句话的证据。
    """
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    码, _体 = 打(服务, f"/api/alerts/{a.key}/ack", {"who": "老王"})
    assert 码 == 404, 码
    assert 取(服务, a.key)["acked_ms"] is None


def test_点一条已经不在的告警是404不是500(服务, 钟):
    """前端点了一条已经不在的告警是正常的误操作,不是我们写错了代码。"""
    没这条 = f"{ROBOT}/stuck#99"
    码, 体 = 打(服务, 路径(没这条, "ack"), {"who": "老王"})
    assert 码 == 404, (码, 体)
    码, 体 = 打(服务, 路径(没这条, "resolve"))
    assert 码 == 404, (码, 体)


def test_确认体不是对象就是400(服务, 钟):
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    code, body, _ = request(服务, 路径(a.key, "ack"), method="POST",
                            raw='"老王"'.encode())
    assert code == 400, body


def test_解决不要体(服务, 钟):
    """``resolve`` 的记名是**可选的** —— 空体就该过(手机端至今发的就是空体,
    见 ``mobile/lib/net/patrol_client.dart`` 的 ``resolveAlert``)。"""
    a = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    code, _body, _h = request(服务, 路径(a.key, "resolve"), method="POST")
    assert code == 200


# ------------------------------------------------------- 升级到底有没有人驱


def 等到(条件, *, 最多等: float = 3.0) -> bool:
    """轮到条件成立为止,**有超时上界**。

    等的是闸门那条真协程(跑在桥那根线程上),没有可注入的钟能拨快它 ——
    §8.5 第 2 条管的是被测代码里的时间,不管这种轮询。跟
    ``test_lease_expiry.py`` 里那个是同一份东西,同一个理由。
    """
    截止 = time.monotonic() + 最多等
    while True:
        if 条件():
            return True
        if time.monotonic() >= 截止:
            return False
        time.sleep(0.01)


def test_升级真的有人在驱动(服务, 钟):
    """**这条钉的是那一行驱动本身,不是 ``due_escalations`` 的算法。**

    ``engine/alerts.py`` 那一侧早就测全了,可任务 12 之前**全仓没有一处生产
    代码调它** —— 于是线上一条 P1 挂着没人管,``escalated`` 永远是 0、
    ``channel`` 永远是 ``screen``,该出的声一次也没出过,而所有 engine 层的
    测试全绿。

    所以这一条必须走**真服务的那一拍**:起服务、拨钟、等闸门自己醒。把
    ``server.py`` 里那一行驱动删掉,engine 那一组一条都不会红,这一条会。
    """
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    assert 取(服务, a.key)["escalated"] == 0, "还没拨钟就升上去了"

    钟.前进(ESCALATE_AFTER_MS[1] + 1)
    assert 等到(lambda: 取(服务, a.key)["escalated"] == len(ESCALATE_AFTER_MS)), \
        f"P1 挂着没人确认,闸门醒了这么多拍也没人把它升上去:{取(服务, a.key)}"
    assert 取(服务, a.key)["channel"] == "sound", 取(服务, a.key)


def test_升级不跟着水位分频走(服务, 钟):
    """升级挂在**每一拍**上,不在 ``if 水位:`` 里头。

    ``_WATER_EVERY`` 那个 60 拍(30 秒)的分频是为盘水位那几件 I/O 活儿设的
    (一次 ``disk_usage`` + 一趟目录遍历 + 一次读 ``landed.json``),而升级
    判定是一本内存字典的一次扫描,没有那个开销。挂进 ``if 水位:`` 里,等于把
    P1 出声的时机焊在盘水位的轮询节拍上 —— ``_LEASE_WATCH_PERIOD_S`` 上面
    那段注释早就写过这句:哪天要把屏幕调慢省电,不该顺手把闸门也调慢。

    直接驱一拍 ``水位=False``:这是"不量水位的那种拍"在代码里的样子,断言
    不用等、不看运气。
    """
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    钟.前进(ESCALATE_AFTER_MS[0] + 1)
    服务.ctx.bridge.call(lambda: 服务.hub._lease_once(水位=False))
    行 = 取(服务, a.key)
    assert 行["escalated"] == 1, 行
    assert 行["channel"] == "push", 行


# --------------------------------------------------------------- 解决也记名


def test_解决也记下是谁(服务, 钟):
    """§5.3 只说了确认要记名,可交接班那张表上"这条是谁消掉的"同样是要查的。

    不记名的解决,事后只看得到"某个时刻它没了" —— 而告警是被人消掉的,不是
    自己好的。
    """
    a = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    码, 体 = 打(服务, 路径(a.key, "resolve"), {"who": "老王"})
    assert 码 == 200, 体
    行 = 取(服务, a.key, "/api/alerts/all")
    assert 行["resolved_by"] == "老王", 行
    assert 行["resolved_ms"] is not None, 行


def test_解决记名不冒充确认(服务, 钟):
    """**这一条是"解决记名"这次改动里唯一危险的地方。**

    顺手把 ``acked_*`` 一起填上,看起来很贴心:反正都有名字了。但"没人看见"
    正是 §5.3 要暴露出来的那个事实 —— 升级只看有没有人确认。在这儿悄悄补一
    个确认,交接班那张表上就再也看不出这一班到底有没有人在盯屏幕,而升级链
    会被一次"我顺手点了解决"整个关掉。

    所以:**从头到尾没人确认过的那条,解决完 ``acked_by`` 必须还是空的。**
    """
    a = 报一条(服务, 钟, "stuck", title="狗卡在楼梯口")
    assert 取(服务, a.key)["acked_by"] == "", "起手就有人确认了,下面断不出东西"

    码, 体 = 打(服务, 路径(a.key, "resolve"), {"who": "老王"})
    assert 码 == 200, 体
    行 = 取(服务, a.key, "/api/alerts/all")
    assert 行["resolved_by"] == "老王", 行
    assert 行["acked_by"] == "", 行
    assert 行["acked_ms"] is None, 行

    # 而且升级照样往下走 —— 解决不等于有人看见(跟 ``test_解决不停升级`` 一对)。
    钟.前进(ESCALATE_AFTER_MS[1] + 1)
    服务.alerts.due_escalations(now_ms=钟.t)
    assert 取(服务, a.key, "/api/alerts/all")["escalated"] == len(ESCALATE_AFTER_MS)


def test_解决不记名时名字是空的(服务, 钟):
    """空体照样过(手机端至今就是空体),记下来的名字是空串,不是 ``null``、
    也不是一句编出来的"系统"。"""
    a = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    码, 体 = 打(服务, 路径(a.key, "resolve"))
    assert 码 == 200, 体
    assert 取(服务, a.key, "/api/alerts/all")["resolved_by"] == ""


def test_解决的名字不是一串字就400不是500(服务, 钟):
    """``{"who": 123}`` 是前端会犯的错,不是我们写错了代码。

    **不许静默降级成"没记名"。** 那样手机上一次拼错类型的解决会安安静静地
    成功,而交接班那张表上少一个名字,谁也不知道少在哪儿(docs/测试为什么会
    说谎.md 的第五种)。也不许 500 —— 那是我们的锅,人看到的是一句读不懂的
    话,而错在他自己那边。
    """
    a = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    码, 体 = 打(服务, 路径(a.key, "resolve"), {"who": 123})
    assert 码 == 400, (码, 体)
    assert 体["error"], 体
    assert "who" in 体.get("detail", "") or "名字" in 体["error"], 体
    # 没记成的解决**不许生效**:半成功比失败更难查。
    assert 取(服务, a.key)["key"] == a.key, "都 400 了,这条还是被解决掉了"


def test_解决体不是对象就是400(服务, 钟):
    """跟 ``ack`` 那条同一个规矩:体给的不是对象就当场说清楚。"""
    a = 报一条(服务, 钟, "finding", title="配电柜有痕迹")
    code, body, _ = request(服务, 路径(a.key, "resolve"), method="POST",
                            raw='"老王"'.encode())
    assert code == 400, body
