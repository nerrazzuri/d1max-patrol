"""重启后那一遍。它的输出不是给人看的,是给机器执行的 —— 留着,还是退回去。"""

from __future__ import annotations

import itertools
import logging

import pytest

from d1max_agent.engine.preflight import CheckResult
from d1max_agent.engine.selfcheck import (
    BRIDGE_WAIT_S,
    GRAB_WAIT_S,
    MAX_BRIDGE_TRIES,
    MAX_GRAB_TRIES,
    POST_CHECKS,
    POSTCHECK_TIMEOUT_S,
    SERVICE_UNIT,
    RestartPlan,
    Verdict,
    _check_bridges,
    grab_control,
    postcheck_verdict,
    restart_plan,
    run_postcheck,
)
from d1max_patrol.backends.base import (
    DeviceBackendError,
    NavConnectionError,
    NavTimeoutError,
)


def _四项(**over) -> list[CheckResult]:
    base = dict.fromkeys(POST_CHECKS, True)
    base.update(over)
    return [CheckResult(name, ok, "") for name, ok in base.items()]


def test_四项全过就留着(tmp_path):
    assert postcheck_verdict(_四项()) is Verdict.KEEP


@pytest.mark.parametrize("坏的", POST_CHECKS)
def test_任意一项不过就回滚(坏的):
    assert postcheck_verdict(_四项(**{坏的: False})) is Verdict.ROLLBACK


def test_少跑了一项就回滚(tmp_path):
    """跑不全等于不确定。**不确定就退回去** —— 跟起飞门槛那条同一个道理。"""
    assert postcheck_verdict(_四项()[:3]) is Verdict.ROLLBACK


def test_一项都没跑就回滚(tmp_path):
    assert postcheck_verdict([]) is Verdict.ROLLBACK


def test_多出来的项不影响判定(tmp_path):
    """将来加第五项的话,这个函数得先被改 —— 而不是悄悄把新项忽略掉。"""
    extra = [*_四项(), CheckResult("将来的第五项", False, "")]
    assert postcheck_verdict(extra) is Verdict.ROLLBACK


def test_同一项报了两遍且有一遍不过就回滚(tmp_path):
    dup = [*_四项(), CheckResult("control", False, "")]
    assert postcheck_verdict(dup) is Verdict.ROLLBACK


class _假设备:
    """一个开机时会被别人占着、过几次就放手的 SDK 会话。"""

    def __init__(self, 放手在第几次: int) -> None:
        self._n = 0
        self._free = 放手在第几次

    async def has_control(self) -> bool:
        self._n += 1
        return self._n >= self._free

    async def acquire_control(self) -> None:
        # 还没轮到放手的时候,抢是抢不动的 —— 抛异常。原来这里是无条件
        # ``return None``,等于"抢成功了但会话还不在手里",跟这个假设备
        # 自己的设定(被别人占着)自相矛盾。矛盾在旧实现下看不出来,因为
        # 旧实现根本不理会 acquire 的成功;改成抢到就当场确认之后,这份
        # 矛盾会让"被占着"的用例走到"抢到了"那一支上去。
        if self._n < self._free:
            raise RuntimeError("会话被别人握着")

    @property
    def 试了几次(self) -> int:
        return self._n


async def test_一次就拿到会话(tmp_path):
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    got = await grab_control(_假设备(1), tries=3, sleep=假睡)
    assert got.ok is True
    assert 睡过 == []          # 一次就成的话一秒都不许等


async def test_被占着就重试到拿回来(tmp_path):
    """没有上装也可能开机那一刻被短暂占着(§7.1 理由 2)。这条路必须存在。"""
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    device = _假设备(3)
    got = await grab_control(device, tries=3, sleep=假睡)
    assert got.ok is True
    assert device.试了几次 == 3
    assert len(睡过) == 2       # 等了两次,不是三次 —— 拿到之后不再等


async def test_试满了还拿不到就算这一项没过(tmp_path):
    async def 假睡(_s: float) -> None:
        return None

    got = await grab_control(_假设备(99), tries=3, sleep=假睡)
    assert got.ok is False
    assert got.name == "control"
    assert "上装" in got.detail


async def test_抢会话炸了也只算这一项没过(tmp_path):
    class 会炸的:
        async def has_control(self) -> bool:
            raise RuntimeError("旁路进程没起来")

        async def acquire_control(self) -> None:
            raise RuntimeError("旁路进程没起来")

    async def 假睡(_s: float) -> None:
        return None

    got = await grab_control(会炸的(), tries=2, sleep=假睡)
    assert got.ok is False
    assert "旁路进程没起来" in got.detail


class _抢到才放手的设备:
    """厂商进程握着会话,抢满几次之后才松手的设备。

    跟 ``_假设备`` 的区别是**会话什么时候到手由 acquire 决定,不由第几次查
    决定**:``has_control()`` 只如实回答"现在在不在我们手里",要等某一次
    ``acquire_control()`` 真的成功了才会翻成 True。升级重启后厂商进程还没
    退干净就是长这样。
    """

    def __init__(self, 第几次抢得到: int) -> None:
        self.查过几次 = 0
        self.抢过几次 = 0
        self._第几次 = 第几次抢得到
        self._在手里 = False

    async def has_control(self) -> bool:
        self.查过几次 += 1
        return self._在手里

    async def acquire_control(self) -> None:
        self.抢过几次 += 1
        if self.抢过几次 < self._第几次:
            raise RuntimeError("厂商进程还握着")
        self._在手里 = True


async def test_最后一圈抢到会话要当场认账_不能判成没拿到():
    """必修 3。**最后一圈没有下一圈** —— 成功与否不许推给下一圈去确认。

    原来的写法是:``acquire_control()`` 成功走 ``else`` 分支,只把 ``last``
    写成"会话被别人握着",真正的确认要等下一圈开头那次 ``has_control()``。
    第 ``tries`` 圈跑完循环就结束了,那一圈抢到的会话没人认。

    后果不是"少报一条成功":``run_postcheck`` 把这一项塞进四项,
    ``postcheck_verdict`` 要求四项全过才 KEEP,于是判 ROLLBACK ——
    **一次已经成功的升级被自动退回去**,现场看到的是"升级失败了",
    人还会照着回滚流程再走一遍。
    """
    睡过: list[float] = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    device = _抢到才放手的设备(3)
    got = await grab_control(device, tries=3, sleep=假睡)
    assert got.ok is True, f"抢到了却报没拿到: {got.detail}"
    assert device.抢过几次 == 3          # 重试次数的语义没被改掉
    assert len(睡过) == 2                # 成功那一圈之后一秒都不许再等


async def test_一次就抢到也要当场认账():
    """``tries=1`` 是同一个病最直白的样子:一次成功的 acquire 也必然报失败,
    因为根本没有"下一圈"可以确认。
    """
    async def 假睡(_s: float) -> None:
        raise AssertionError("一次就抢到了,不该等")

    device = _抢到才放手的设备(1)
    got = await grab_control(device, tries=1, sleep=假睡)
    assert got.ok is True, f"抢到了却报没拿到: {got.detail}"
    assert device.抢过几次 == 1


async def test_抢完确认还是不在手里_那就接着重试():
    """抢没抢到和会话到没到手是两件事。``acquire_control()`` 不抛异常不等于
    会话就在手里(厂商的实现可以是"排队申请"),所以确认这一步不许省 ——
    确认下来还不在手里的,照常走完剩下的重试,最后如实报没拿到。
    """
    class 抢得动但从不到手:
        async def has_control(self) -> bool:
            return False

        async def acquire_control(self) -> None:
            return None

    async def 假睡(_s: float) -> None:
        return None

    got = await grab_control(抢得动但从不到手(), tries=2, sleep=假睡)
    assert got.ok is False
    assert got.name == "control"


async def test_SN变了就回滚(tmp_path):
    """升级前后 SN 必须是同一只狗。不是的话,这台机器上跑的不是我们以为的东西。"""

    class 桩nav:
        async def nav_status(self):
            return object()

        async def loc_status(self):
            return object()

        async def get_map_grid(self, map_id: str):
            return {}

        async def list_maps(self):
            return ["m1"]

    async def 假睡(_s: float) -> None:
        return None

    results = await run_postcheck(桩nav(), _假设备(1), want_sn="D1M-0007",
                                  got_sn="D1M-0008", sleep=假睡)
    assert postcheck_verdict(results) is Verdict.ROLLBACK
    assert next(c for c in results if c.name == "identity").ok is False


# --------------------------------------------------------------- 重启方式


def test_没上装就只重启服务():
    plan = restart_plan(has_payload=False)
    assert plan.kind == "service"
    assert plan.argv == ("systemctl", "restart", SERVICE_UNIT)


def test_有上装就整机重启():
    plan = restart_plan(has_payload=True)
    assert plan.kind == "machine"
    assert plan.argv == ("systemctl", "reboot")


def test_重启单元名可以换():
    plan = restart_plan(has_payload=False, unit="别的单元")
    assert plan.argv == ("systemctl", "restart", "别的单元")


def test_重启方案能上线():
    wire = RestartPlan("service", ("systemctl", "restart", SERVICE_UNIT),
                       "理由").to_wire()
    assert wire == {"kind": "service",
                     "argv": ["systemctl", "restart", SERVICE_UNIT],
                     "why": "理由"}


# ------------------------------------------------------- fix2 B4: 组合跑全

@pytest.mark.parametrize("组合", list(itertools.product([True, False], repeat=4)),
                         ids=lambda c: "".join("T" if x else "F" for x in c))
def test_四项的十六种组合(组合):
    """§8.5 写的是「把组合跑全」,那就**字面穷举**:2^4 = 16 种。

    上面那几条(全过、单坏四条、少跑一项、一项没跑、多出来的项、同一项报两遍)
    一条都没删 —— 它们测的是「项数」那半条判据,这一条测的是「都过」那半条。
    两半合起来才是 ``postcheck_verdict`` 的全部。

    这个函数必须保持**纯**(不读盘、不看时钟、不发网络),所以这里连
    ``tmp_path`` 都不要:一条需要临时目录才跑得起来的判据测试,本身就说明
    判据不纯了。
    """
    checks = [CheckResult(name, ok, "") for name, ok in zip(POST_CHECKS, 组合, strict=True)]
    want = Verdict.KEEP if all(组合) else Verdict.ROLLBACK
    assert postcheck_verdict(checks) is want


# --------------------------------------------- fix2 B2: 三个桥的有界重试

class _慢一拍的导航桥:
    """前几次探测炸掉,之后正常应答 —— 冷启动时导航桥慢一拍的样子。

    ``main()`` 里三个 ``connect()`` 各只有 10 秒超时而且失败被吞掉,所以
    ``_boot_postcheck`` 跑到这儿时桥没起来是真会发生的。
    """

    def __init__(self, 从第几次开始通: int) -> None:
        self._n = 0
        self._通 = 从第几次开始通

    async def loc_status(self):
        self._n += 1
        if self._n < self._通:
            raise RuntimeError("导航桥还没起来")
        return object()

    async def list_maps(self):
        return ["m1"]

    @property
    def 探了几次(self) -> int:
        return self._n


async def test_桥第一次不应答第二次通了就算过():
    """**好版本被误判回滚的最短路径**,堵的就是这条。

    第一层(``_boot_postcheck``)零宽限、第一次不过就直接回滚,而第二层
    ``MAX_BOOT_ATTEMPTS`` 那两次机会在这条路上一次也用不上(服务没崩,
    没人会再启动一次)。宽限只能加在这一层。
    """
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    nav = _慢一拍的导航桥(2)
    got = await _check_bridges(nav, _假设备(1), sleep=假睡)
    assert got.ok is True
    assert got.name == "bridges"
    assert nav.探了几次 == 2
    assert len(睡过) == 1        # 等了一次,不是三次 —— 通了就立刻返回


async def test_桥试满了还不应答才算这一项没过():
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    nav = _慢一拍的导航桥(99)
    got = await _check_bridges(nav, _假设备(1), sleep=假睡)
    assert got.ok is False
    assert got.name == "bridges"
    assert nav.探了几次 == MAX_BRIDGE_TRIES
    assert len(睡过) == MAX_BRIDGE_TRIES - 1     # 最后一次不过之后不再等
    assert "导航桥还没起来" in got.detail          # 坏在哪儿要说得出来


async def test_桥一次就通的话一秒都不等():
    """绝大多数启动走的是这一条。加宽限不该给它们添一次等待。"""
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    got = await _check_bridges(_慢一拍的导航桥(1), _假设备(1), sleep=假睡)
    assert got.ok is True
    assert 睡过 == []


async def test_重启后那一遍会把sleep传给桥这一项():
    """``run_postcheck`` 漏传 ``sleep=`` 的话,套件里就会有真等待(§8.5)。"""
    睡过 = []

    async def 假睡(s: float) -> None:
        睡过.append(s)

    results = await run_postcheck(_慢一拍的导航桥(2), _假设备(1),
                                  want_sn="D1M-0007", got_sn="D1M-0007",
                                  sleep=假睡)
    assert postcheck_verdict(results) is Verdict.KEEP
    assert 睡过 == [2.0]          # 桥那一项等的那一次,走的是注入进来的假 sleep


# ------------------------------------- fix2 B3: recorded 不能在边界上丢掉

def test_没记过上装就整机重启():
    """``Payload.has`` 在「没记过」时是 ``False``,跟「记过,确认没上装」

    长得一模一样。只收 ``has_payload`` 一位的话,一台**真装了上装但还没
    登记**的机器会被只重启服务 = 放掉 SDK 会话,而放掉之后要跟上装抢开机
    窗口才拿得回来(§7.1)。整机重启从不会永久丢会话 —— 不知道的时候
    站在安全那一侧。
    """
    plan = restart_plan(has_payload=False, recorded=False)
    assert plan.kind == "machine"
    assert plan.argv == ("systemctl", "reboot")
    assert "没记过" in plan.why


def test_没记过压过有没有上装那一位():
    """两位都给到最坏的组合上,结论也得是整机重启。"""
    assert restart_plan(has_payload=True, recorded=False).kind == "machine"
    assert restart_plan(has_payload=False, recorded=False).kind == "machine"
    # 记过了才轮到 has_payload 说话。
    assert restart_plan(has_payload=False, recorded=True).kind == "service"
    assert restart_plan(has_payload=True, recorded=True).kind == "machine"


def test_默认当成记过了():
    """``recorded`` 默认 ``True``:调用方不传时行为跟改动之前一模一样。

    默认值选 ``True`` 而不是 ``False``,是因为 ``False`` 会让每一个没跟上
    这次改动的调用点都变成整机重启 —— 那是把一个「可能丢会话」的错换成
    一个「一定重启整台机器」的错。三个调用点都显式传了值(见
    ``app/server.py``),默认值只是签名上的礼貌。
    """
    assert restart_plan(has_payload=False).kind == "service"


# ------------------------------------------------------------- W01c:日志不许刷 traceback

def _selfcheck日志(caplog, 级别=logging.WARNING):
    return [r for r in caplog.records
            if r.name == "d1max_agent.engine.selfcheck" and r.levelno >= 级别]


async def test_旁路进程没起来时抢会话只记一句人话_不带traceback(caplog):
    """真机(2026-09-22)点一下 /api/selfcheck,journal 里是三段 traceback,
    说的却只是「还没连上旁路进程」—— 后端自己会说人话的错,不该带栈。"""
    caplog.set_level(logging.DEBUG, logger="d1max_agent.engine.selfcheck")

    class 没连上的:
        async def has_control(self) -> bool:
            return False

        async def acquire_control(self) -> None:
            raise DeviceBackendError("还没连上旁路进程")

    async def 假睡(_s: float) -> None:
        return None

    got = await grab_control(没连上的(), tries=3, sleep=假睡)
    assert got.ok is False
    assert "还没连上旁路进程" in got.detail
    警告 = _selfcheck日志(caplog)
    assert len(警告) == 1, [r.getMessage() for r in 警告]
    assert "还没连上旁路进程" in 警告[0].getMessage()
    assert 警告[0].exc_info is None, "后端说的人话不配 traceback"


async def test_抢会话遇到意料之外的异常仍然留traceback(caplog):
    """人话只给认识的错;不认识的错正是 bug 的线索,栈要留着。"""
    caplog.set_level(logging.DEBUG, logger="d1max_agent.engine.selfcheck")

    class 会炸的:
        async def has_control(self) -> bool:
            raise KeyError("state")

        async def acquire_control(self) -> None:
            raise KeyError("state")

    async def 假睡(_s: float) -> None:
        return None

    await grab_control(会炸的(), tries=2, sleep=假睡)
    带栈 = [r for r in _selfcheck日志(caplog) if r.exc_info]
    assert 带栈, "意料之外的异常没有留下 traceback"


async def test_桥不应答时只记人话_不带traceback(caplog):
    caplog.set_level(logging.DEBUG, logger="d1max_agent.engine.selfcheck")

    class 断了的导航:
        async def loc_status(self):
            raise NavConnectionError("尚未连接导航设备")

        async def list_maps(self):
            raise NavTimeoutError("等了 5s 没回")

    class 没连上的设备:
        async def has_control(self) -> bool:
            raise DeviceBackendError("还没连上旁路进程")

    async def 假睡(_s: float) -> None:
        return None

    got = await _check_bridges(断了的导航(), 没连上的设备(), tries=2, sleep=假睡)
    assert got.ok is False
    assert not [r for r in _selfcheck日志(caplog, logging.DEBUG) if r.exc_info], \
        "后端自己抛的连接/超时错不配 traceback"
    警告 = _selfcheck日志(caplog)
    assert len(警告) == 1, [r.getMessage() for r in 警告]
    for 词 in ("位姿", "地图", "相机/设备"):
        assert 词 in 警告[0].getMessage()


async def test_桥探测遇到意料之外的异常仍然留traceback(caplog):
    caplog.set_level(logging.DEBUG, logger="d1max_agent.engine.selfcheck")

    class 炸的导航:
        async def loc_status(self):
            raise ZeroDivisionError("bug")

        async def list_maps(self):
            return []

    class 好设备:
        async def has_control(self) -> bool:
            return True

    async def 假睡(_s: float) -> None:
        return None

    await _check_bridges(炸的导航(), 好设备(), tries=1, sleep=假睡)
    assert [r for r in _selfcheck日志(caplog) if r.exc_info]


def test_自检接口的超时预算盖得住最坏情况():
    """真机上 ``/api/selfcheck`` 走 ``_call`` 的默认 10 s 就 504 了 —— 可是抢会话
    三次之间就要等 4 s,桥探测三轮又是 4 s,再加每次探测本身的请求超时。
    预算要按常量推出来,改了重试次数或间隔这里就得跟着红。"""
    from d1max_patrol.backends.sidecar_device import DEFAULT_ACK_TIMEOUT_S
    from d1max_patrol.config.models import NavConfig

    nav_t = NavConfig().request_timeout_s
    抢 = MAX_GRAB_TRIES * DEFAULT_ACK_TIMEOUT_S + (MAX_GRAB_TRIES - 1) * GRAB_WAIT_S
    桥 = MAX_BRIDGE_TRIES * (2 * nav_t) + (MAX_BRIDGE_TRIES - 1) * BRIDGE_WAIT_S
    assert POSTCHECK_TIMEOUT_S >= 抢 + 桥, (POSTCHECK_TIMEOUT_S, 抢 + 桥)
    assert POSTCHECK_TIMEOUT_S > 10.0
