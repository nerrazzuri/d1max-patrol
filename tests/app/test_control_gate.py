"""租约闸门:要控制权的那几条,没租约就进不去。"""

from __future__ import annotations

import json
import re

import pytest

from d1max_patrol.app.auth import AUTH_PATH
from d1max_patrol.app.control import CONTROLLED, needs_lease
from d1max_patrol.app.server import AppServer, _compile
from tests.app.conftest import make_ctx, request

PIN = "428913"
T0 = 1_757_000_000_000
走一拍 = {"fwd": 0.0, "lat": 0.0, "yaw": 0.0}

#: ``CONTROLLED`` 那几条正则展开之后能匹配到的全部具体路径。
#: 逐条钉住"没租约就 409", 少一条闸就是少一道闸, 而漏掉不会有任何报错。
#: **名字里不带数字**:带数字的名字每加一条路由就要跟着改, 改漏了也没人
#: 红 —— 数字本身就是下一次遗漏的种子。底下
#: ``test_这张表跟CONTROLLED两边对得上`` 补的就是这张表跟正主的一致性。
要控制权的每一条 = (
    "/api/teleop",
    "/api/teleop/heartbeat",
    "/api/teleop/mode",
    "/api/mapping/record/start",
    "/api/mapping/record/stop",
    # 重建会 ``forget_home`` 掉这张图的原点(``app/mapping.py``), 删完这只狗
    # 用这张图起不了飞 —— 判据是 §3.5 规则 4 的"下一趟还能不能出发"。
    "/api/mapping/rebuild",
    "/api/missions/m1/run",
    "/api/run/pause",
    "/api/run/resume",
    "/api/run/abort",
    "/api/run/suspend",
    "/api/maps/load",
    "/api/pose/initial",
    "/api/pose/reset",
    # 告警的记名确认/解决(§5.3)。**这两条写的是解码之后的路径** ——
    # ``needs_lease`` 拿到的就是 ``_dispatch`` 里 ``unquote`` 过的那一份,
    # 而告警键(``robot/kind#seq``)里的斜杠到这一步已经是真斜杠了。这里
    # 特意挑了一个不带 ``#`` 的键: ``request()`` 走的是真 urllib, ``#``
    # 会在客户端就被当成片段切掉, 这张表上的路径必须是原样发得出去的。
    "/api/alerts/D1M-TEST/stuck/ack",
    "/api/alerts/D1M-TEST/stuck/resolve",
)


class 假墙钟:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def 墙钟():
    return 假墙钟()


@pytest.fixture
def 有pin的服务(bridge, tmp_path, 墙钟):
    ctx = make_ctx(bridge, tmp_path, clock=墙钟)
    s = AppServer(ctx, port=0, pin=PIN)
    s.start()
    yield s
    s.stop()


def 解锁(server, operator: str = "张三") -> str:
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload={"pin": PIN, "operator": operator})
    assert code == 200, body
    return json.loads(body)["token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def 打(server, path, token, payload=None):
    code, body, _ = request(server, path, method="POST",
                            payload=payload or {}, headers=auth(token))
    return code, json.loads(body)


def 取控制权(server, token):
    assert 打(server, "/api/control/acquire", token)[0] == 200


def test_没租约就动不了(有pin的服务):
    tok = 解锁(有pin的服务)
    code, body = 打(有pin的服务, "/api/teleop", tok, 走一拍)
    assert code == 409
    assert "先取控制权" in body["error"]
    assert "/api/control/acquire" in body["detail"]


def test_取了租约就动得了(有pin的服务):
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 200


def test_急停任何时候都按得下去(有pin的服务):
    """§3.5 规则 2。这一条错了会死人。"""
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    assert 打(有pin的服务, "/api/estop", 乙)[0] == 200


def test_一个租约都没有时急停照样按得下去(有pin的服务):
    """§3.5 规则 2 的另一半:没人持有租约时也不许把急停挡在闸外。

    ``CONTROLLED`` 里如果哪天混进了 ``/api/estop``, 上面那条(有人持有)
    会红, 这一条(无人持有)也会红 —— 两种局面各钉一条, 免得只补上其中
    一半。
    """
    tok = 解锁(有pin的服务)
    assert 打(有pin的服务, "/api/estop", tok)[0] == 200


def test_看不需要控制权(有pin的服务):
    """§3.5 规则 1。"""
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    code, _body, _h = request(有pin的服务, "/api/state", headers=auth(乙))
    assert code == 200


def test_没租约也读得到状态控制权和留痕(有pin的服务):
    """§3.5 规则 1:``CONTROLLED`` 里一条 GET 都没有,值守屏永远看得见。

    值守的人多半根本不打算开狗 —— 要他先抢一次方向盘才看得到"现在谁拿着"
    是荒唐的, 而且那正是出事时最需要看的一屏。
    """
    tok = 解锁(有pin的服务)
    for 路径 in ("/api/state", "/api/control", "/api/control/audit"):
        code, _body, _h = request(有pin的服务, 路径, headers=auth(tok))
        assert code == 200, 路径


@pytest.mark.parametrize("路径", 要控制权的每一条)
def test_每条路径没租约一律进不去(有pin的服务, 路径):
    """``CONTROLLED`` 展开后的每一条都得真的挂上闸。

    ``/api/missions/m1/run`` 指的是一个不存在的任务 —— 闸在路由匹配之前,
    所以拿到的必须是 409 而不是 404。这正是"按路径判、不按处理函数判"要的
    效果。
    """
    tok = 解锁(有pin的服务)
    code, body = 打(有pin的服务, 路径, tok, 走一拍)
    assert code == 409, (路径, body)
    # 光断 409 不够: ``/api/run/pause`` 这几条在"没在跑"时业务本身就回 409,
    # 闸拆掉它们照样绿。认文案才认得出这一下是谁挡的。
    assert body["error"] == "先取控制权", (路径, body)


@pytest.mark.parametrize("路径", 要控制权的每一条)
def test_每条路径取了租约就不再是409(有pin的服务, 路径):
    """取了控制权之后, 这道闸就不该再说话了。

    不断 200:``/api/missions/m1/run`` 该 404、``/api/run/pause`` 没在跑该
    409-但那是引擎说的。这里只断"不是这道闸说的话" —— 靠错误文案区分,
    免得把一条闸的测试写成一堆业务前置条件的测试。

    **只认文案,不认状态码。** 原来写的是
    ``not (code == 409 and "控制权" in error)``,那把状态码和文案绑在了一
    起:哪天有人把闸的拒绝码从 409 改成 403 或 423,``code == 409`` 当场为
    假,整条断言**静默转绿** —— 闸照样在错误地拦着,而没有一条测试会红。

    不认状态码之后还剩一个口子:一个体是空的 500 也没有"控制权"三个字,
    照样过。所以底下补一句 —— 取了控制权之后打这些路径,**结果可以是 404、
    可以是业务的 409,就是不许是服务端自己崩了**。
    """
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    code, body = 打(有pin的服务, 路径, tok, 走一拍)
    assert code != 500, (路径, code, body)
    assert "控制权" not in body.get("error", ""), (路径, body)


def test_这张表跟CONTROLLED两边对得上():
    """手抄的镜像必须跟正主对得上 —— **两个方向都要**。

    只查一个方向都不够:只查"元组里每条都要租约",往 ``CONTROLLED`` 加一条
    新路由照样静默漏掉(这一卷刚发生过);只查"每个模式都被命中",元组里混进
    一条不该要租约的也照样绿。
    """
    from d1max_patrol.app.control import CONTROLLED, needs_lease
    for 路径 in 要控制权的每一条:
        assert needs_lease("POST", 路径), 路径
    for 方法, 模式 in CONTROLLED:
        assert any(方法 == "POST" and 模式.match(p) for p in 要控制权的每一条), \
            模式.pattern


def test_别人拿着时动不了而且说得出是谁(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    code, body = 打(有pin的服务, "/api/teleop", 乙, 走一拍)
    assert code == 409
    assert "张三" in body["error"]
    assert "takeover" in body["detail"]


def test_租约过期之后就动不了了(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 200
    墙钟.t += 30_000
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 409


def test_心跳续得住(有pin的服务, 墙钟):
    tok = 解锁(有pin的服务)
    取控制权(有pin的服务, tok)
    for _ in range(5):
        墙钟.t += 10_000
        assert 打(有pin的服务, "/api/control/heartbeat", tok)[0] == 200
    assert 打(有pin的服务, "/api/teleop", tok, 走一拍)[0] == 200


def test_遥控心跳也要控制权(有pin的服务):
    """守死人那一层的心跳和租约心跳是两条不同的线(§6.4),但发遥控心跳的人
    必须是拿着控制权的那个 —— 否则一个没有控制权的会话可以替持有者把死人
    开关按住,而持有者自己早就走开了。
    """
    tok = 解锁(有pin的服务)
    assert 打(有pin的服务, "/api/teleop/heartbeat", tok)[0] == 409
    取控制权(有pin的服务, tok)
    assert 打(有pin的服务, "/api/teleop/heartbeat", tok)[0] == 200


def test_接管之后原来的人动不了了(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    打(有pin的服务, "/api/control/takeover", 乙,
       {"force": True, "reason": "现场有人要摔了"})
    assert 打(有pin的服务, "/api/teleop", 甲, 走一拍)[0] == 409
    assert 打(有pin的服务, "/api/teleop", 乙, 走一拍)[0] == 200


def test_没设pin的部署上这一层不生效(server):
    code, _body, _h = request(server, "/api/teleop", method="POST",
                              payload=走一拍)
    assert code == 200


def test_退出之后别人立刻拿得到(有pin的服务):
    甲 = 解锁(有pin的服务, "张三")
    乙 = 解锁(有pin的服务, "李四")
    取控制权(有pin的服务, 甲)
    request(有pin的服务, "/api/auth/logout", method="POST", headers=auth(甲))
    取控制权(有pin的服务, 乙)
    assert 打(有pin的服务, "/api/teleop", 乙, 走一拍)[0] == 200


# ------------------------------------------------- 路由表跟 CONTROLLED 对得上


#: **不要**控制权的那几条 POST(挂账 68)。写的是 ``route()`` 收到的**原始
#: 模式串**,跟 ``server._register_routes`` 里那一行一字不差 —— 加新路由的人
#: 把那一行原样抄过来就行,不必先想清楚占位符会被展开成什么。
#:
#: **这张表是反着写的,这是它的全部分量。** 正着写("这几条要控制权")的表
#: 漏掉一条不会有任何人红;反着写之后,加一条会改狗的 POST 而两处都不登记
#: 时,底下那条测试立刻红 —— 而这正是第 7 卷加 ``suspend`` 时缺的那道逼迫。
#:
#: 每一条为什么不要,判据见 ``app/control.py`` 里 ``CONTROLLED`` 上方那段
#: 注释,这里只记一句话的出处,不重抄论证。
不要控制权的POST = {
    # 登录/登出:还没有会话,谈不上租约。
    AUTH_PATH,
    "/api/auth/logout",
    # 控制权本身。要控制权才能取控制权是个死结。
    "/api/control/acquire",
    "/api/control/heartbeat",
    "/api/control/release",
    "/api/control/takeover",
    "/api/control/takeover/approve",
    # §3.5 规则 2:急停永远不要控制权。这一条错了会死人。
    "/api/estop",
    # 归档上的判读和复核,改的是纸面不是狗。
    "/api/runs/<run_id>/judge",
    "/api/runs/<run_id>/review/<name>",
    # 清盘、导出、备份:都不改变这只狗正在做什么。
    "/api/storage/sweep",
    "/api/exports",
    "/api/exports/<name>/confirm",
    "/api/backup/init",
    "/api/backup/sync",
    "/api/backup/eject",
    # 升级尤其不能要:一次恢复性的回滚不该被一个已经掉线的会话挡住。
    "/api/release/install",
    "/api/release/activate",
    "/api/release/rollback",
    # 任务包对在跑的那一趟是惰性的(下一趟才生效)。
    "/api/bundle/apply",
    "/api/bundle/rollback",
}

#: 允许 ``<name*>`` (跨斜杠) 的那几条。**只许开在不落地的参数上** ——
#: 判据原文在 ``server._compile`` 的文档串里:告警键唯一的去处是
#: ``AlertBook`` 那本内存字典的一次 ``get``,不拼路径、不拼命令、不进子进程。
#: 凡是会被拼进文件路径的段(``run_id``、``name``、``map_id``……)一律继续用
#: 不带星号的写法 —— 那些地方"不跨斜杠"就是那道穿越防线本身。
带星号的白名单 = {
    "/api/alerts/<key*>/ack",
    "/api/alerts/<key*>/resolve",
}

#: 不跨斜杠的占位符换成什么。一段普通的、不带斜杠的东西就够了。
#:
#: **它不带斜杠这件事跟 :data:`_跨段` 带斜杠一样是承重的**,所以底下那条
#: ``test_跨斜杠的告警键真的绕不过闸门`` 把两边都钉住:这里哪天被人改成一段
#: 带斜杠的东西,``CONTROLLED`` 里的 ``[^/]+`` 当场匹配不上,一堆本来受控的
#: 路由会被判成漏网(该绿判成红),而红的地方跟真正的原因隔着三层。
_一段 = "x"

#: **跨斜杠的占位符必须换成一个真带斜杠的值**,这是这一段唯一容易写错的地方。
#: ``<key*>`` 收的是告警键(``robot/kind#seq``,见 ``engine/alerts.py`` 的
#: ``_key``),斜杠是键本身的一部分;而 ``CONTROLLED`` 里那两行正是靠 ``.+``
#: 跨斜杠才匹配得上。换成 ``"x"`` 的话,哪天有人把那两行收紧成 ``[^/]+``,
#: 这条测试照样绿 —— 而真机上带斜杠的键会从闸门底下整个漏过去(该红判成绿)。
#: 反过来,给不带星号的占位符也塞一个带斜杠的值,``[^/]+`` 当场匹配不上,
#: 一堆本来受控的路由会被判成漏网(该绿判成红)。**换的东西必须跟占位符自己
#: 的跨度一致**,这就是这两个常量分开的理由。
_跨段 = "D1M-TEST/stuck#1"

#: 占位符长什么样。**字符类必须跟正主 ``server._compile`` 里那一行
#: (``re.split(r"(<[a-z_]+\*?>)", pattern)``)一模一样。**
#:
#: 宽了不行:这边认得、正主不认得的写法(比如把字符类放宽到收数字)会被这边
#: 换成一段具体路径,而正主把它当字面量 ``re.escape`` 掉 —— 问 ``needs_lease``
#: 的那条路径根本不是这条路由能匹配的路径。
#: 窄了更不行:这边认不出来的占位符会**原样留在路径里**,而 ``CONTROLLED``
#: 里的 ``[^/]+`` 会把 ``<name2>`` 这种东西照样匹配上 —— 那条路由被判成
#: "受管",其实一个字都没验证过。窄的这一半由 :func:`具体路径` 里那句
#: "换完不许还剩尖括号" 当场接住。
_占位符 = re.compile(r"<([a-z_]+\*?)>")


def 枚举注册过的路由(srv: AppServer) -> tuple[tuple[str, str], ...]:
    """从真的路由表里读 ``(方法, 原始模式)``。

    **不许手抄一份。** 手抄的那份就是下一次遗漏的种子 —— 加路由的人只会改
    ``server._register_routes``,不会想起来还有一份影子表要跟着改。

    读的是 ``_Route.raw``(``route()`` 收到的原始模式串),不是
    ``pattern.pattern``(编译后的正则源码):后者是换了个马甲的源码文本扫描,
    脆,而且要靠反推才知道哪一段原来是占位符。

    **顺手断一句 ``_compile(raw) == pattern``,这一句是承重的。** 底下三条闸门
    测试全部拿 ``raw`` 去问 ``needs_lease``,而真正拦请求的 ``_dispatch`` 拿的
    是 ``pattern``。今天两者同源(``route()`` 是唯一构造点,同一次调用里一个
    编译出另一个),可这是**约定不是机制**:哪天有人给 ``_Route`` 加第二个构造
    点、或者在 ``route()`` 里对 ``raw`` 做一次归一化(去尾斜杠、小写化……),
    两者就错开了 —— 闸门从此查的是一个跟真实匹配无关的字符串,而这三条测试
    照样全绿。
    """
    出 = []
    for r in srv._routes:
        assert _compile(r.raw).pattern == r.pattern.pattern, (
            f"{r.method} {r.raw!r} 的 raw 跟 pattern 不同源了:"
            f"{_compile(r.raw).pattern!r} != {r.pattern.pattern!r}。"
            "受控路由表那几条测试全靠 raw 说话,错开之后它们查的是一个跟真实"
            "匹配无关的字符串。")
        出.append((r.method, r.raw))
    return tuple(出)


def 具体路径(模式: str) -> str:
    """把带占位符的模式换成一条能拿去问 ``needs_lease`` 的具体路径。

    换法见 :data:`_一段` / :data:`_跨段` 上面那两段注释。
    """
    路径 = _占位符.sub(
        lambda m: _跨段 if m.group(1).endswith("*") else _一段, 模式)
    # 换完不许还剩尖括号 —— 剩下的那个是 :data:`_占位符` 认不出来的写法,
    # 而 ``CONTROLLED`` 里的 ``[^/]+`` 会把它照样匹配上(见 :data:`_占位符`)。
    assert "<" not in 路径 and ">" not in 路径, (模式, 路径)
    # **换出来的东西必须真的是这条路由匹配得上的路径。** 上面那句只接住"改窄"
    # (占位符原样留在路径里);这一句把"改宽"也一起钉死:这边认得、正主
    # ``server._compile`` 不认得的写法会被换成一段具体路径,而正主把它当字面量
    # ``re.escape`` 掉 —— 于是拿去问 ``needs_lease`` 的那条路径根本不是这条路由
    # 能匹配的路径,而红出来的地方(``漏网`` 那张表)跟真正的原因隔着三层。
    # 同理,``_一段`` / ``_跨段`` 的跨度跟占位符对不上时也在这儿当场红。
    # **这两条原来都只是注释纪律**,这一句把它们换成机制。
    assert _compile(模式).match(路径), (
        f"{模式!r} 换出来的 {路径!r} 根本不是这条路由匹配得上的路径 —— "
        "要么 _占位符 的字符类跟 server._compile 里那一行分家了,要么 "
        "_一段 / _跨段 的跨度跟占位符对不上。")
    return 路径


@pytest.fixture
def 没起的服务(ctx):
    """只建不起。路由表在 ``__init__`` 里就填好了,这几条表测试用不着端口。"""
    return AppServer(ctx, port=0)


def test_每条会改狗的POST都在CONTROLLED里(没起的服务):
    """挂账 68:原来只有"镜像表 <-> CONTROLLED"两个方向,漏的是第三个 ——
    **路由表 <-> CONTROLLED**。

    往 ``server.py`` 加一条会改狗的 POST 而两处都忘了登记时,镜像表和
    ``CONTROLLED`` 依然彼此自洽、依然全绿,而那条新路由不受控制权闸门管:
    没拿租约的人直接调得动。白名单反着写,加路由的人**必须**做出选择,
    忘了就红 —— 这正是第 7 卷加 ``suspend`` 时缺的那道逼迫。
    """
    漏网 = [模式 for 方法, 模式 in 枚举注册过的路由(没起的服务)
            if 方法 == "POST" and 模式 not in 不要控制权的POST
            and not needs_lease("POST", 具体路径(模式))]
    assert not 漏网, (
        f"{漏网} 不受控制权闸门管。要么把它加进 app/control.py 的 "
        "CONTROLLED,要么把它加进这个文件的 不要控制权的POST 并写清楚理由。")


def test_豁免的那几条真的不受闸门管():
    """豁免白名单是**单向**的,这一条补的是反方向。

    上面那条测试的列表推导里,``模式 not in 不要控制权的POST`` 排在
    ``needs_lease(...)`` 前面 —— 豁免表里的模式在 ``needs_lease`` 被问到之前
    就被过滤掉了。也就是说任务 12 那三条闸门测试对「**一条本该豁免的路由被
    闸门管住了**」完全瞎。``test_这张表跟CONTROLLED两边对得上`` 也接不住:
    它的反方向只问「``CONTROLLED`` 里每条正则至少能匹配上手抄表的某一条」,
    正则被放宽之后原来那条依然匹配得上,照样绿。

    **失败场景**:有人为了收紧遥控把 ``CONTROLLED`` 里 ``/api/teleop`` 那条
    放宽一格(比如写成 ``^/api/(estop|teleop)$``),``/api/estop`` 从此落进
    ``CONTROLLED`` —— 没拿到租约的人(租约在别人手上,或者刚掉线还没续上)
    按急停就是 409,**急停不生效**。§3.5 规则 2 就写在豁免表的注释里:
    这一条错了会死人。

    不用起服务:问的是 ``needs_lease`` 这个纯函数,而豁免表里的模式还在不在
    路由表上由 ``test_豁免名单里没有已经不存在的路由`` 单管一头。
    """
    误管 = sorted(模式 for 模式 in 不要控制权的POST
                  if needs_lease("POST", 具体路径(模式)))
    assert not 误管, (
        f"{误管} 写在豁免表里,却被 app/control.py 的 CONTROLLED 圈进去了。"
        "要么是 CONTROLLED 里某条正则放宽过了头,要么是这条路由不该豁免 —— "
        "先看 §3.5 规则 2:急停任何时候都得按得下去。")


def test_豁免名单里没有已经不存在的路由(没起的服务):
    """白名单会烂。

    路由被删掉或改名之后,那条豁免会一直留在表上;哪天有人用同一个路径加回
    一条**会改狗**的 POST,上面那条测试当场闭嘴 —— 一道闸就这么静悄悄地少
    了。所以豁免名单里的每一条都必须真的还是一条注册过的 POST。
    """
    所有POST = {模式 for 方法, 模式 in 枚举注册过的路由(没起的服务)
                if 方法 == "POST"}
    assert 不要控制权的POST <= 所有POST, 不要控制权的POST - 所有POST


def test_带星号的路由只许开在白名单里(没起的服务):
    """``<name*>`` 跨斜杠,那是 ``_compile`` 唯一的例外,也是唯一一处能让一段
    带斜杠的东西整个进到处理函数里的口子。

    ``_compile`` 的文档串写着这条纪律:**只许开在不落地的参数上**。今天没有
    任何测试逼着遵守它 —— 谁哪天给 ``/api/runs/<run_id*>/...`` 加个星号,
    "占位符不跨斜杠"那道穿越防线就没了,而一条都不会红。

    **两个方向都比。** 只查"新的星号要在白名单里",白名单里留着一条早就删掉
    的路由就没人发现;只查"白名单里的都还在",加一条新的星号路由照样静悄悄。

    这里读的是运行时的 ``_routes``,不是去扫 ``server.py`` 的源文件:源码扫描
    会被字符串拼接、被注释、被换行绕过,而且它证明的是"源文件里长这样",
    不是"服务器真的注册了这条"。
    """
    带星 = {模式 for _方法, 模式 in 枚举注册过的路由(没起的服务) if "*>" in 模式}
    assert 带星 == 带星号的白名单, (
        "带 <name*> 的路由变了。加星号之前先回答一个问题:这个参数会不会被"
        "拼进文件路径?会的话就别加星号 —— 那里的「不跨斜杠」就是防线"
        "本身(见 server._compile)。")


def test_跨斜杠的告警键真的绕不过闸门(没起的服务):
    """上面那条 ``具体路径`` 换出来的东西必须真的是带斜杠的。

    这一条钉的是**替换值本身**:``_跨段`` 哪天被人改成一段不带斜杠的字符串,
    ``test_每条会改狗的POST都在CONTROLLED里`` 会静默降级成一个恒真的断言 ——
    ``CONTROLLED`` 里那两行收紧成 ``[^/]+`` 它也照样绿,而真机上的告警键
    (``robot/kind#seq``)会从闸门底下整个漏过去。
    """
    assert "/" in _跨段, _跨段
    # 反过来的那一半:不跨斜杠的替换值**必须不带斜杠**(见 :data:`_一段`)。
    assert "/" not in _一段, _一段
    assert needs_lease("POST", 具体路径("/api/alerts/<key*>/ack"))
    assert needs_lease("POST", 具体路径("/api/alerts/<key*>/resolve"))
    # 反过来:CONTROLLED 里一条 GET 都没有(§3.5 规则 1)。
    assert not any(方法 != "POST" for 方法, _模式 in CONTROLLED)
