"""这台机器是哪只狗。

**为什么需要这个模块。** 每只 D1 Max 的内网地址是同一套 —— Orin NX 永远是
``192.168.168.100``,RK3588 永远是 ``192.168.168.168``。地址是机身内部固定
的,不是我们配的,所以**靠地址分不出谁是谁**:手机上今天连的和昨天连的,在
IP 上长得一模一样。多机管理只能按机身身份走。

**麻烦在于厂商没给 SN 这个字段。** 翻遍 SDK 开发指南和导航 WebSocket API,
没有任何一个接口报序列号。所以这里做成一条候选链,从最可信的往下找,
**找不到就明说找不到,不编一个出来** —— 编出来的 SN 比没有 SN 更坏:它看着
像真的,而两台机器上编出同一个值的那天,两批归档就悄悄混成了一批。

MAC 无论如何都记。它是唯一一个一定读得到的机身标识,SN 全线扑空时至少还有
它兜底;而且手机连热点的时候看得见 BSSID,不用登录就能认出面前是哪只狗。

身份最终去两个地方:``GET /api/identity`` 给手机认狗,以及每次运行的
``manifest.json`` 的 ``fingerprint`` 里 —— 后者让历史报告自己带着"这是哪只
狗跑的",不用另建目录也不会认错。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

#: 人填的 SN 从这个环境变量读。命令行 ``--sn`` 也收。
SN_ENV = "D1MAX_SN"
#: 人给这只狗起的名。现场喊"三号"比喊 C40221 顺口。
NICKNAME_ENV = "D1MAX_NICKNAME"

#: SN 的候选文件,按可信度从高到低。前两个是设备树(Jetson 把模组序列号烧在
#: 这儿,刷机不会变);后两个是 DMI,x86 上才有,放着是为了在开发机上也能通。
SN_FILES: tuple[Path, ...] = (
    Path("/proc/device-tree/serial-number"),
    Path("/sys/firmware/devicetree/base/serial-number"),
    Path("/sys/devices/virtual/dmi/id/product_serial"),
    Path("/sys/devices/virtual/dmi/id/board_serial"),
)

#: 网卡都挂在这儿,每个目录下的 ``address`` 就是 MAC。
NET_ROOT = Path("/sys/class/net")

#: 主板厂出厂时塞的占位串。**这些必须当成"没有"**:好几台不同的机器上
#: 读出来是同一个值,拿它当 SN 等于把所有机器认成一台。
JUNK_SN = frozenset({
    "to be filled by o.e.m.",
    "to be filled by o.e.m",
    "system serial number",
    "default string",
    "not specified",
    "not applicable",
    "none",
    "null",
    "unknown",
    "0",
    "00000000",
    "0123456789",
    "123456789",
    "x.x.x",
})

#: 环回口不算机身标识,全零 MAC 也不算。
SKIP_IFACES = frozenset({"lo"})
NULL_MAC = "00:00:00:00:00:00"

#: SN 一个都没查到时用这个。**不是编一个,是明写"没查到"。**
UNKNOWN_SN = "unknown"

#: 「这台有没有装上装」记在哪。**不在版本目录里** —— 它是机器的属性,
#: 换一版软件不该把它换掉。环境变量是给测试和开发机用的。
PAYLOAD_ENV = "D1MAX_PAYLOAD_FILE"
PAYLOAD_FILE = Path("/etc/d1max/payload.json")

#: 改这个属性要原样打这句话。**不是防手滑,是让改的人停一秒**:
#: 这一改会静默地改掉升级的安全模型(§7.1 纪律 2) —— 从「只重启服务」
#: 变成「整机重启且不许自动升」,或者反过来。
CONFIRM_PHRASE = "我知道这会改掉升级方式"


@dataclass(frozen=True, slots=True)
class Payload:
    """这台有没有装上装。

    **``has`` 和 ``recorded`` 是两个字段,不能合成一个。** 「没装上装」是一个
    结论,「没人记过」是没有结论;合成一个的话,一台刚装完还没登记的机器
    看起来就跟一台确认过没有上装的机器一模一样,而升级流程正是照着它选路的。
    """

    has: bool = False
    recorded: bool = False
    at_ms: int = 0
    #: 谁记的。现场排查时要问的第一句话是"这是谁填的"。
    by: str = ""

    def to_wire(self) -> dict[str, object]:
        return {"has_payload": self.has, "payload_recorded": self.recorded,
                "payload_at_ms": self.at_ms, "payload_by": self.by}


def payload_path(override: Path | str | None = None) -> Path:
    """上装属性记在哪。显式给的赢,其次环境变量,最后默认路径。"""
    if override is not None:
        return Path(override)
    env = os.environ.get(PAYLOAD_ENV, "").strip()
    return Path(env) if env else PAYLOAD_FILE


def read_payload(path: Path | str | None = None) -> Payload:
    """读上装属性。**读不动就是"没记过",不是"没装"。**

    这个区别是本模块里最要紧的一条:把读失败当成"没装上装",等于让一台装了
    上装的机器走上"只重启我们的服务"那条路 —— 而重启我们的进程就是放掉 SDK
    会话,放掉之后不一定抢得回来(§7.1)。
    """
    try:
        raw = json.loads(payload_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Payload()
    if not isinstance(raw, dict) or "has_payload" not in raw:
        return Payload()
    try:
        at_ms = int(raw.get("at_ms", 0))
    except (TypeError, ValueError):
        at_ms = 0
    return Payload(has=bool(raw.get("has_payload")), recorded=True,
                   at_ms=at_ms, by=str(raw.get("by", "")))


def write_payload(*, has: bool, by: str, now_ms: int,
                  path: Path | str | None = None, confirm: str) -> Payload:
    """记下上装属性。**确认语对不上就一个字节都不写。**"""
    if confirm != CONFIRM_PHRASE:
        raise ValueError(f"要改这个属性得原样打一遍确认语: {CONFIRM_PHRASE}")
    target = payload_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = Payload(has=bool(has), recorded=True, at_ms=int(now_ms),
                      by=str(by or ""))
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"has_payload": payload.has, "at_ms": payload.at_ms,
                   "by": payload.by}, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return payload


@dataclass(frozen=True, slots=True)
class Identity:
    """一只狗的身份。"""

    #: 主键。多机管理按它走。
    sn: str
    #: 这个 sn 是从哪儿来的 —— 人填的、哪个文件、还是拿 MAC 兜的底。
    #: **这一条要露给人看**:兜底出来的 SN 和烧在模组上的 SN 可信度差得远。
    source: str
    #: 这台机器上所有网卡的 MAC,排过序。
    macs: tuple[str, ...]
    #: 人起的名字,没起就是空串。
    nickname: str
    #: 主机名。同一批机器上常常是一样的,所以它只是参考,不当主键。
    host: str
    #: 这台有没有装上装。**它决定升级怎么重启**(§7.1),而且它是会变的。
    payload: Payload = Payload()

    @property
    def provisional(self) -> bool:
        """这个 SN 是兜底来的吗?是的话别拿它当长期主键。"""
        return self.source in ("mac", "没查到")

    @property
    def hand_written(self) -> bool:
        """这个 SN 是装机时人填进去的吗(§6.2)?

        跟 ``provisional`` 不是一回事:``provisional`` 问的是"这个值稳不稳
        定",而设备树里读出来的模组序列号很稳定 —— 它只是**标识错了对象**。
        客户认的是机身上贴的那张标签,换一次主板,模组号就跟标签对不上了,
        而所有归档、所有备份盘、所有任务包的 ``targets`` 都是按 SN 归堆的。
        """
        return self.source == "配置"

    def to_wire(self) -> dict[str, object]:
        return {
            "sn": self.sn,
            "source": self.source,
            "provisional": self.provisional,
            "hand_written": self.hand_written,
            "macs": list(self.macs),
            "nickname": self.nickname,
            "host": self.host,
            **self.payload.to_wire(),
        }

    def fingerprint(self) -> dict[str, str]:
        """并进 ``manifest.json`` 的那几个键。

        只放 SN 和名字,不放 MAC 列表 —— 指纹是给人看报告用的,一串网卡地址
        在那儿只会挤走真正有用的信息(SDK 版本、协议版本、地图 ID)。
        """
        out = {"robot_sn": self.sn}
        if self.nickname:
            out["robot_name"] = self.nickname
        if self.provisional:
            # 报告上要看得出这个 SN 靠不靠得住,不然日后按它归堆会归错。
            out["robot_sn_source"] = self.source
        return out


def clean_sn(raw: str) -> str:
    """把读出来的值收拾干净,是垃圾就返回空串。

    设备树里的字符串是 NUL 结尾的,直接读会带一个 ``\x00``。
    """
    value = raw.replace("\x00", "").strip()
    if not value or value.lower() in JUNK_SN:
        return ""
    # 出厂占位串里 x 和 . 反复出现("x.x.x"、"0.0.0"),真序列号不会长这样。
    if not any(c.isalnum() for c in value):
        return ""
    return value


def read_sn(files: tuple[Path, ...] = SN_FILES) -> tuple[str, str]:
    """按顺序找 SN,返回 ``(sn, 来源)``。都没有就返回 ``("", "")``。"""
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        value = clean_sn(raw)
        if value:
            return value, str(path)
    return "", ""


def read_macs(net_root: Path = NET_ROOT) -> tuple[str, ...]:
    """这台机器上所有网卡的 MAC,去重排序。

    读不到就是空 —— 开发机(Windows)上没有 ``/sys``,那不是错误。
    """
    found: set[str] = set()
    try:
        entries = sorted(net_root.iterdir())
    except OSError:
        return ()
    for iface in entries:
        if iface.name in SKIP_IFACES:
            continue
        try:
            mac = iface.joinpath("address").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        mac = mac.lower()
        if mac and mac != NULL_MAC:
            found.add(mac)
    return tuple(sorted(found))


def resolve(sn: str | None = None, nickname: str | None = None, *,
            files: tuple[Path, ...] = SN_FILES,
            net_root: Path = NET_ROOT,
            payload_file: Path | str | None = None,
            host: str | None = None) -> Identity:
    """定下这台机器的身份。

    顺序是有讲究的:**人填的永远赢**。厂商没给 SN 这个字段,现场贴的标签才是
    唯一权威;自动查出来的那些(设备树、DMI)标识的是**模组**不是**整机** ——
    换过一次主板就变了,而客户那边认的是机身编号。
    """
    source = ""
    value = clean_sn(sn) if sn else ""
    if value:
        source = "配置"
    else:
        value, source = read_sn(files)

    macs = read_macs(net_root)
    if not value:
        # 兜底:拿主网卡的 MAC 当 SN。它是稳定的(重启不变、刷机不变),
        # 只是标识的是网卡不是整机 —— 所以 source 里写清楚,让人一眼看得出
        # 这个 SN 是凑出来的,该去补一个真的。
        if macs:
            value, source = "mac:" + macs[0].replace(":", ""), "mac"
        else:
            value, source = UNKNOWN_SN, "没查到"

    return Identity(sn=value, source=source, macs=macs,
                    nickname=(nickname or "").strip(),
                    host=host if host is not None else socket.gethostname(),
                    payload=read_payload(payload_file))
