"""页面上的方向键:一拍一拍地走,松手就停。

**为什么是"脉冲"而不是"持续速度"。** 这台机器的运动接口是
``walk(seconds, forward, lateral, yaw)`` —— 开环直驱,给一个时长和一组
控制量,到点自己停(见 ``DeviceBackend.walk``)。所以"按住前进"这件事在
底下就是一串短脉冲,页面每按一下发一拍,松手就没有下一拍了。

**为什么控制量要顶到死区之上。** 控制量是**百分比**不是 m/s,而且死区不
小(清单 #37/#38):``0.11`` 几乎不动,``0.3~0.5`` 才真的走。想让狗慢一点
的正确做法是**把一拍缩短**,不是把控制量压低 —— 压低的结果是原地不动,
而且不报错,页面上看起来像是掉线了。

**守死人开关。** 页面每隔一小会儿要送一次心跳,超过
``HEARTBEAT_TIMEOUT_S`` 没送到就停车。检查放在一条后台任务里周期性地做,
理由和 ``LocalNavBackend._watch_loc`` 一样:**不能只在有人来问的时候才
发现**。浏览器崩了、WiFi 断了、人走开了 —— 这些情况下恰恰没有人来问,
而狗还在走。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from d1max_patrol.backends.base import DeviceBackend
from d1max_patrol.engine.machine import MissionEngine

log = logging.getLogger(__name__)

#: 心跳断了多久就停车。0.6s ≈ 半米内(死区控制量下的粗略估计)。
HEARTBEAT_TIMEOUT_S = 0.6

#: 一拍默认多长。
DEFAULT_PULSE_S = 0.4

#: 一拍最长多久。再长就等于把守死人开关架空了 —— 心跳断了也还要走这么久。
MAX_PULSE_S = 2.0


@dataclass(frozen=True, slots=True)
class PulseProfile:
    """一档节奏。**两档的差别只在时长上,不在控制量上。**

    控制量是百分比不是速度,死区在 0.3 附近(清单 #37/#38)。想让狗慢一点,
    唯一正确的做法是把一拍缩短 —— 把控制量压低的结果是原地不动,而且不报错
    (见模块开头)。所以这里没有「转向系数」这种字段,只有两个时长。

    **为什么转向和平移各有一个时长。** 扫图的时候人要的是转向一点一点来,
    平移倒不必碎成一样。合成一个数就没法分别调,而这两件事在现场是分开
    调的。
    """

    #: 上线时用的名字。``PROFILES`` 的键跟它必须一致。
    name: str
    #: 平移一拍多长。
    fwd_seconds: float
    #: 转向一拍多长。
    yaw_seconds: float

    def seconds_for(self, fwd: float, lat: float, yaw: float) -> float:
        """这一拍该走多久。

        既有平移又有转向时**取长的那个**。取短的话,人推着「往前走并且拐个
        弯」,前进会被截成转向那么短的一小步,走出来像「原地拐了一下」。
        """
        moving = fwd != 0.0 or lat != 0.0
        turning = yaw != 0.0
        if moving and turning:
            return max(self.fwd_seconds, self.yaw_seconds)
        return self.yaw_seconds if turning else self.fwd_seconds


#: 自主操控(漫游):走路用的那一档,一拍 0.4 秒,转向给满(§7.7)。
ROAM = PulseProfile("roam", DEFAULT_PULSE_S, DEFAULT_PULSE_S)

#: 扫图:建图的时候用,一拍更碎,转向尤其碎 —— 建图要的是慢慢挪、慢慢转,
#: 让激光有时间把同一片地方看够(§7.7)。**碎是靠缩时长实现的,不是靠压
#: 控制量**,理由见 ``PulseProfile``。
SCAN = PulseProfile("scan", 0.25, 0.12)

#: 上线时认的两个词。
PROFILES: Mapping[str, PulseProfile] = MappingProxyType(
    {ROAM.name: ROAM, SCAN.name: SCAN})

#: 死区之上的最小控制量。见清单 #37/#38。
MIN_FWD = 0.30
MIN_YAW = 0.30
#: 侧移(蟹步)的死区。**今天的值是从 MIN_FWD 抄来的,还没在真机上量过** ——
#: 清单 #37/#38 量的是前进那一轴。横着走是另一种步态,没有任何理由认为它跟
#: 前进共用一个死区。独立成一个常量不是为了现在有区别,是为了标定那天改它
#: 不会顺手把前进也改了。真机清单 #67。
MIN_LAT = 0.30

#: 守死人的检查周期。取超时的三分之一:够快地发现,又不至于空转。
WATCH_PERIOD_S = HEARTBEAT_TIMEOUT_S / 3

#: 遥控的两档(§7.8)。**跟 Task 2 的节奏(``PROFILES``)是两件事,别混。**
#: 节奏管一拍多长,是手感;档管人在不在现场,是留痕。两者正交 —— 远程档
#: 也能用扫图节奏。放在同一个文件里,但不共用任何状态。
MODES: tuple[str, ...] = ("onsite", "remote")

#: 切到远程时要人确认的那句话。**放在狗这头,不放在手机那头**:
#: 措辞改一次要跟着到每一个客户端上,而狗只有一个。
REMOTE_CONFIRM = ("远程遥控:你看不见狗周围的实际情况,只能看见画面里的那一块。"
                  "确认切过去 —— 这次确认会记在案。")

#: 四个轴全零 —— "停"就是这个。
_STOP = (0.0, 0.0, 0.0)


class EmergencyStopFailed(RuntimeError):
    """急停这一下**没有全部做到**。消息里逐条写着哪一步没成。

    以前急停把停车的错误 ``suppress`` 掉、从来不发软急停,页面照样回
    「已急停」。人按了急停之后看到的那句话,必须是真的。
    """


class TeleopBusy(RuntimeError):
    """现在不能遥控:任务在跑,或者急停按着,或者没有画面(§5.9)。

    引擎让开腿(``SUSPENDED``)时**不算**任务在跑 —— 那正是人要亲自开的时候
    (§5.10)。租约那一半不在这儿,``app/control.py`` 的 ``CONTROLLED`` 已经
    把整条路由钉在租约后面了。
    """


def _clamp_above_deadband(value: float, floor: float) -> float:
    """零还是零,非零顶到死区之上,并且不超过满量程。

    零必须原样传下去:那是"停",不是"慢慢走"。要是这里也给它顶到 0.3,
    松手之后狗会继续走 —— 这是这个函数里唯一真正危险的一行。
    """
    if value == 0.0:
        return 0.0
    return math.copysign(min(max(abs(value), floor), 1.0), value)


def snap_to_axis(fwd: float, lat: float) -> tuple[float, float]:
    """左摇杆吸附到单轴:留下大的那一轴,另一轴清零(§7.7)。

    **这是正确性,不是手感。** ``_clamp_above_deadband`` 是逐轴把非零值顶到
    死区之上的,而那个动作只有在输入是单轴时才有意义:两个轴一起顶,
    ``(0.5, 0.2)`` 会变成 ``(0.5, 0.3)`` —— 人指的是「基本朝前」,狗走的是
    「四十度斜着」。

    机器狗自带的摇杆本来就是这个行为(推四十五度它也只挑一个轴),所以吸附
    还顺手让页面和实体摇杆的手感对上了。

    **平局判给前进。** 正推四十五度是「想往前、手抖了」的概率远大于反过来;
    而且前进是唯一一个真机量过死区的轴(清单 #37/#38),侧移那个数是借来的
    (见 ``MIN_LAT``)。平局倒向量过的那一头。
    """
    if abs(lat) > abs(fwd):
        return 0.0, lat
    return fwd, 0.0


class Teleop:
    """一拍一拍的手动遥控,带守死人开关。

    和 ``MissionEngine`` 是互斥的:任务在跑的时候不许遥控,遥控在动的时候
    不许起任务。后一半靠 ``engine.add_busy_check`` 反过来告诉引擎 —— 引擎
    不认识这个类(全局约束:业务层不许知道外壳)。
    """

    def __init__(self, device: DeviceBackend, engine: MissionEngine,
                 *, video_gate: Callable[[], str],
                 clock: Callable[[], float] = time.monotonic,
                 watch_period_s: float = WATCH_PERIOD_S,
                 heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S) -> None:
        self._device = device
        self._engine = engine
        self._clock = clock
        self._period = watch_period_s
        #: 守死人开关的超时。**内部一律读这个字段,不读模块常量** ——
        #: ``HEARTBEAT_TIMEOUT_S`` 是生产缺省(值不变、``__all__`` 里也还在),
        #: 注进来是为了让"挂起 -> 接管 -> 手松开 -> 继续"那条端到端能在测试
        #: 里走完:那条链非跨过这个超时不可,而被测代码里不许有真等待
        #: (§8.5 第 2 条)。留一处读模块常量,注进来的值就是个摆设,而摆设
        #: 不会有任何测试红。
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._last_beat = 0.0
        self._active = False
        self._watch: asyncio.Task[None] | None = None
        #: 「现在能看得见吗」。返回拦住的理由,空串表示可以走(§5.9)。
        #:
        #: **必填,没有默认值。** 给个默认就意味着有一天会有人建一个没有闸的
        #: ``Teleop`` 而不自知,而那一天没有任何测试会红 —— 这是安全约束,
        #: 不是体验约束。
        #:
        #: 做成回调而不是让遥控认识 ``CameraFeed``:跟 ``add_busy_check`` 同
        #: 一个理由,这一层不该长出对相机实现的依赖。
        self._video_gate = video_gate
        #: (档名, 是谁切的)。**档是「这个人现在在哪儿」,不是狗的属性** ——
        #: 甲切了远程然后掉线,乙接过控制权时必须从现场档起步,否则乙会在一个
        #: 自己从没确认过的档上开狗,而事后的留痕上写着甲的名字。
        self._mode: tuple[str, str] = ("onsite", "")
        engine.add_busy_check(self._busy_reason)

    # ------------------------------------------------------------------ 对外

    @property
    def heartbeat_timeout_s(self) -> float:
        """这一台**实际生效**的守死人超时(秒)。

        **将来任何一处"告诉手机该多久发一次心跳"的地方必须读这个属性,不许
        读模块常量 ``HEARTBEAT_TIMEOUT_S``。** 核查过了:今天全仓一处这样的
        宣告点都没有 —— ``/api/teleop`` 和 ``/api/teleop/mode`` 的响应里没有
        这个数,``/api/control`` 里带的 ``heartbeat_ms`` 是**租约**那条线
        (§6.4 明写两条线不是一回事),手机那头 ``teleop_page.dart`` 的
        ``_beatPeriod`` 是它自己拍的一个 200 毫秒。所以这里**不凭空造一个
        API 字段**,只把"该读哪儿"这件事钉住。

        为什么钉:``_watchdog`` 已经改读实例字段。哪天某个部署为了省电注一个
        ``heartbeat_timeout_s=1.5`` 进来,而新加的宣告点仍去读模块常量的
        0.6 —— 手机按 0.6 的节奏发、狗按 1.5 判,或者反过来,两者分家之后
        **不会有任何一条测试红**:模块常量还在、值也没变,实例字段也确实被
        ``_watchdog`` 用着,两边各自都自洽。
        """
        return self._heartbeat_timeout_s

    @property
    def active(self) -> bool:
        """这次遥控会话还开着吗。

        靠 ``heartbeat()`` 续命,靠守死人开关(``HEARTBEAT_TIMEOUT_S``)到期
        自动清掉;``stop()``/``aclose()`` 会主动清。**不是**"这一拍此刻正
        在走"——一拍很快就发完了,``pulse()`` 发完之后不会把它清回去,清它
        的只有上面这几条路。
        """
        return self._active

    def mode(self, holder_ref: str) -> str:
        """这个人现在在哪一档。**不是他切的那一档就算现场档。**"""
        name, who = self._mode
        return name if who == holder_ref else "onsite"

    def set_mode(self, name: str, holder_ref: str) -> None:
        """切档(§7.8)。**§5.9 不受这里影响** —— 远程档一样要过视频那道闸,
        闸在 ``pulse()`` 里,这个方法只管记账。
        """
        if name not in MODES:
            raise ValueError(f"只有 {'、'.join(MODES)} 两档,给的是 {name!r}")
        self._mode = (name, holder_ref)

    async def pulse(self, fwd: float, lat: float, yaw: float,
                    seconds: float | None = None, *,
                    profile: PulseProfile = ROAM) -> None:
        """走一拍。

        控制量取 [-1, 1],会被顶到死区之上;想走慢就换一档(``profile``)。
        三个轴全零等于 :meth:`stop`。

        ``seconds`` 显式给了就听人的,不给就按这一档的节奏算 —— 网页版 app
        现在传的就是显式秒数,它一行都不用改。
        """
        _check_range(fwd, "fwd")
        _check_range(lat, "lat")
        _check_range(yaw, "yaw")

        fwd, lat = snap_to_axis(fwd, lat)
        if seconds is None:
            seconds = profile.seconds_for(fwd, lat, yaw)
        if not 0.0 < seconds <= MAX_PULSE_S:
            raise ValueError(f"一拍得在 (0, {MAX_PULSE_S}] 秒之间,给的是 {seconds}")

        if (fwd, lat, yaw) == _STOP:
            await self.stop()
            return

        # **闸放在停车早返的后面。** 三轴全零走的是 stop() —— 停车永远不许被
        # 闸挡住:一个「因为看不见所以不许停」的实现,会在视频掉线的那一刻把狗
        # 锁在最后一个动作上,而掉线恰恰是最需要它停下来的时候。
        blind = self._video_gate()
        if blind:
            # §5.9 明写**不设「确认后继续」的绕过口子**:要挪狗,就必须能看得见。
            # 网差到没有画面,那就走过去挪。
            raise TeleopBusy(f"看不见就不许动:{blind}")

        if self._engine.running and not self._engine.yielding:
            raise TeleopBusy("任务在跑,先停任务再遥控 —— 两边一起动是在抢腿")
        if await self._device.emergency():
            raise TeleopBusy("急停按着,松开急停再遥控")

        self._last_beat = self._clock()
        self._active = True
        self._ensure_watch()
        await self._device.walk(
            seconds,
            _clamp_above_deadband(fwd, MIN_FWD),
            _clamp_above_deadband(lat, MIN_LAT),
            _clamp_above_deadband(yaw, MIN_YAW),
        )

    def heartbeat(self) -> None:
        """页面还在,人还在看着。守死人开关靠这个续命。"""
        self._last_beat = self._clock()

    async def stop(self) -> None:
        """停车。停不动也要把 ``active`` 落下来 —— 它是"该不该继续看着"。

        走的是 ``device.halt()``,**不是** ``walk(0, 0, 0, 0)``:后者在真后端上
        会因为时长不为正被拒掉(见 ``DeviceBackend.halt``)。
        """
        self._active = False
        self._cancel_watch()
        await self._device.halt()

    async def emergency_stop(self, reason: str = "按了急停") -> None:
        """页面上那个红按钮:软急停,停遥控,**并且**打断正在跑的任务。

        三件事都要做,而且**一件失败不许挡住下一件**:急停的语义是"现在什么
        都别动了",遥控和任务是两条各自独立的动腿路径,只停一条等于没停。

        顺序:软急停最先 —— 它在旁路进程里插队、作废所有 walk,是最快让腿
        停下来的那一下;然后停车(软急停没发出去时靠它);最后打断任务。

        **任何一步失败都要抛** ``EmergencyStopFailed``,不许吞:页面上「已急停」
        那句话只在这里不抛的时候才说。
        """
        self._active = False
        self._cancel_watch()
        failures: list[str] = []
        try:
            await self._device.emergency_stop(True)
        except Exception as exc:  # noqa: BLE001 - 每一步都得试,错误汇总后再抛
            failures.append(f"软急停没发出去:{exc}")
        try:
            await self._device.halt()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"停车没发出去:{exc}")
        if self._engine.running:
            try:
                await self._engine.abort(reason)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"任务没打断:{exc}")
        if failures:
            log.error("急停没有全部做到:%s", ";".join(failures))
            raise EmergencyStopFailed(";".join(failures))

    async def release_emergency_stop(self) -> None:
        """解除软急停。**要控制权**(闸在 ``CONTROLLED`` 里),急停本身不要。

        按下不设门槛、解除设门槛:按错了多按一次没有代价,解错了狗会动。
        解除之后遥控和任务**不会自己恢复** —— 要人重新推杆、重新起任务。
        """
        await self._device.emergency_stop(False)

    async def aclose(self) -> None:
        """收尾。只停后台任务,不给狗发指令 —— 关服务不该让它动一下。"""
        self._active = False
        self._cancel_watch()
        if self._watch is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        self._watch = None

    # ------------------------------------------------------------------ 内部

    def _busy_reason(self) -> str:
        return "遥控在动,先松手再起任务" if self._active else ""

    def _ensure_watch(self) -> None:
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watchdog())

    def _cancel_watch(self) -> None:
        if self._watch is not None and not self._watch.done():
            self._watch.cancel()

    async def _watchdog(self) -> None:
        """周期地看心跳还在不在。

        只在这里停车,不在 ``pulse`` 里顺手判 —— 心跳断了的时候恰恰没有人
        在调 ``pulse``。
        """
        while True:
            await asyncio.sleep(self._period)
            if not self._active:
                continue
            blind = self._video_gate()
            if blind:
                # 掉线时没有人在调 pulse:人的手还压在摇杆上,页面还在发心跳。
                # 真正危险的是「画面刚黑掉、人还没反应过来」那半秒,而这条协程
                # 是唯一一直在看的东西。
                self._active = False
                await self._halt_from_watchdog("画面掉了")
                return
            if self._clock() - self._last_beat > self._heartbeat_timeout_s:
                self._active = False
                await self._halt_from_watchdog("心跳断了")
                return

    async def _halt_from_watchdog(self, why: str) -> None:
        """守死人停车。这里没有调用方可以抛给,所以失败只能记下来 —— 但**必须
        记**:以前这里 ``suppress`` 了 ``walk(0,...)`` 的拒绝,两条守死人停车在
        真狗上一直是空的,日志里一个字都没有。
        """
        try:
            await self._device.halt()
        except Exception:  # 后台协程,抛出去没人接 —— 只能记日志
            log.exception("守死人开关要停车(%s),停车命令没发出去", why)


def _check_range(value: float, name: str) -> None:
    if not -1.0 <= value <= 1.0:
        raise ValueError(f"{name} 得在 [-1, 1] 之间,给的是 {value}")


__all__ = [
    "DEFAULT_PULSE_S",
    "HEARTBEAT_TIMEOUT_S",
    "MAX_PULSE_S",
    "MIN_FWD",
    "MIN_LAT",
    "MIN_YAW",
    "MODES",
    "PROFILES",
    "REMOTE_CONFIRM",
    "ROAM",
    "SCAN",
    "WATCH_PERIOD_S",
    "EmergencyStopFailed",
    "PulseProfile",
    "Teleop",
    "TeleopBusy",
    "snap_to_axis",
]
