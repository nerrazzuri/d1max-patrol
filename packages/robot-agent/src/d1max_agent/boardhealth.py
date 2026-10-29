"""上装主控板的温度、风扇(W46,上装 V2;决策 74 定的三级)。

硬件组说:盒内空气最坏约 56 °C,离主控板 60 °C 上限只剩 4 °C;3 只风扇停了盒内会明显过热。这里:

- **每 ``period_s``(缺省 10 s)读一次**:
  - SoC 各温区:``/sys/class/thermal/thermal_zone*/{type,temp}``;
  - NVMe:hwmon 里名字叫 ``nvme`` 的那个(内核从盘的 SMART 读出来的综合温度);
  - 风扇:测速线(FG)接 GPIO,数下降沿(``d1max_contract.gpio.EdgeCounter``),每转 ``pulses`` 个脉冲。
- **写一行 CSV**(``<log_dir>/board-<UTC 日期>.csv``):样机热测试 C2 直接拿这份。
- **分三级**(决策 74),升级当场升,降级要比门槛低 ``hyst_c``(缺省 3 °C)才降,免得在线上来回跳:
  - 警戒 ``warn``:SoC ≥ 80 °C、NVMe ≥ 65 °C,或者停了一只风扇 → 报警给业主;
  - 降负载 ``shed``:SoC ≥ 85 °C、NVMe ≥ 70 °C,或者停了两只 → 停热像录像、暂停上传、不接新的建图;
  - 危急 ``critical``:SoC ≥ 95 °C、NVMe ≥ 75 °C → 结束这一趟、回家,出 P1。
  降负载、回家由运行时按 :attr:`BoardMonitor.level` 做,这里只管量和判。
- 风扇**连着两拍**低于 ``min_rpm`` 才算停转(刚上电、读数抖)。
  测速线拿不到(引脚被占、没接)只报那只风扇故障,**不算停转、不降负载**:不知道不等于停了。
- 级别变了报一次 ``board_thermal`` 事件,站点出告警。
  设备健康:``board`` 一条、每只风扇 ``fan:<名>`` 一条。

配置 ``/etc/d1max/board.json``(代理 ``--board``;没有这个文件就不起,老狗、厂商 Orin 上都没有)::

    {"fans": [{"name": "exhaust_l", "chip": "gpiochip3", "line": 5},
              {"name": "exhaust_r", "chip": "gpiochip3", "line": 6},
              {"name": "stir", "chip": "gpiochip4", "line": 1, "pulses": 2, "min_rpm": 300}],
     "log_dir": "/var/lib/d1max/board", "limits": {"warn_soc": 80}}
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LEVELS = ("ok", "warn", "shed", "critical")
PERIOD_S = 10.0
#: 连着几拍太慢才算停转。
STOP_SAMPLES = 2
_NAME = re.compile(r"[a-z][a-z0-9_]{0,15}")
THERMAL_ROOT = Path("/sys/class/thermal")
HWMON_ROOT = Path("/sys/class/hwmon")


class BoardConfigError(ValueError):
    """``board.json`` 写得不对。"""


@dataclass(frozen=True)
class Limits:
    warn_soc: float = 80.0
    shed_soc: float = 85.0
    crit_soc: float = 95.0
    warn_nvme: float = 65.0
    shed_nvme: float = 70.0
    crit_nvme: float = 75.0
    warn_fans: int = 1
    shed_fans: int = 2
    #: 降级要比门槛低这么多。
    hyst_c: float = 3.0

    def check(self) -> Limits:
        if not (self.warn_soc < self.shed_soc < self.crit_soc
                and self.warn_nvme < self.shed_nvme < self.crit_nvme
                and 0 < self.warn_fans <= self.shed_fans and 0 <= self.hyst_c <= 10):
            raise BoardConfigError("limits 要 警戒 < 降负载 < 危急,风扇只数 ≥ 1,回差 0–10 °C")
        return self

    def lowered(self) -> Limits:
        h = self.hyst_c
        return replace(self, warn_soc=self.warn_soc - h, shed_soc=self.shed_soc - h,
                       crit_soc=self.crit_soc - h, warn_nvme=self.warn_nvme - h,
                       shed_nvme=self.shed_nvme - h, crit_nvme=self.crit_nvme - h)


@dataclass(frozen=True)
class FanCfg:
    name: str
    chip: str
    line: int
    #: 每转几个脉冲(常见 2)。
    pulses: int = 2
    #: 低于这个转速算停转。
    min_rpm: int = 300


@dataclass(frozen=True)
class BoardConfig:
    fans: tuple[FanCfg, ...] = ()
    log_dir: str = "/var/lib/d1max/board"
    limits: Limits = field(default_factory=Limits)
    period_s: float = PERIOD_S

    @staticmethod
    def parse_fans(raw: Any) -> tuple[FanCfg, ...]:
        from d1max_contract.gpio import GpioError, chip_path
        if not isinstance(raw, list):
            raise BoardConfigError("fans 要是列表")
        out: list[FanCfg] = []
        for f in raw:
            if not isinstance(f, dict) or set(f) - {"name", "chip", "line", "pulses", "min_rpm"}:
                raise BoardConfigError(f"风扇写法不对:{f!r}")
            name, line = f.get("name"), f.get("line")
            pulses, min_rpm = f.get("pulses", 2), f.get("min_rpm", 300)
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise BoardConfigError(f"风扇名小写字母开头、最多 16 个字符:{name!r}")
            try:
                chip_path(f.get("chip", ""))
            except GpioError as exc:
                raise BoardConfigError(f"风扇 {name}:{exc}") from exc
            for what, v, lo, hi in (("line", line, 0, 1023), ("pulses", pulses, 1, 8),
                                    ("min_rpm", min_rpm, 0, 20000)):
                if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                    raise BoardConfigError(f"风扇 {name} 的 {what} 要是 {lo}–{hi} 的整数")
            if any(o.name == name for o in out):
                raise BoardConfigError(f"风扇名重了:{name}")
            out.append(FanCfg(name, f["chip"], line, pulses, min_rpm))
        return tuple(out)

    @classmethod
    def load(cls, path: str | Path) -> BoardConfig:
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BoardConfigError(f"板子配置读不了: {exc}") from exc
        if not isinstance(d, dict) or set(d) - {"fans", "log_dir", "limits", "period_s"}:
            raise BoardConfigError("板子配置只认 fans、log_dir、limits、period_s")
        lim = d.get("limits") or {}
        names = {f.name for f in fields(Limits)}
        if not isinstance(lim, dict) or set(lim) - names \
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           for v in lim.values()):
            raise BoardConfigError(f"limits 只认 {sorted(names)},值要是数字")
        try:
            limits = Limits(**lim).check()
            period = float(d.get("period_s", PERIOD_S))
        except (TypeError, ValueError) as exc:
            raise BoardConfigError(f"板子配置的数字不对: {exc}") from exc
        if not 1 <= period <= 300:
            raise BoardConfigError("period_s 要在 1–300 秒")
        return cls(fans=cls.parse_fans(d.get("fans") or []),
                   log_dir=str(d.get("log_dir", cls.log_dir)), limits=limits, period_s=period)


# ------------------------------------------------------------ 读数


def _milli(p: Path) -> float | None:
    try:
        return int(p.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_soc(root: Path = THERMAL_ROOT) -> dict[str, float]:
    """各温区 → 摄氏度(名字去掉 ``-thermal``)。读不了的温区跳过。"""
    out: dict[str, float] = {}
    try:
        zones = sorted(root.glob("thermal_zone*"))
    except OSError:
        return out
    for z in zones:
        try:
            name = (z / "type").read_text().strip().removesuffix("-thermal")
        except OSError:
            continue
        t = _milli(z / "temp")
        if t is not None and name:
            out[name] = t
    return out


def read_nvme(root: Path = HWMON_ROOT) -> float | None:
    """NVMe 盘的温度(hwmon 名字叫 ``nvme`` 的第一个的 ``temp1_input``);没有就 None。"""
    try:
        mons = sorted(root.glob("hwmon*"))
    except OSError:
        return None
    for h in mons:
        try:
            if (h / "name").read_text().strip() != "nvme":
                continue
        except OSError:
            continue
        t = _milli(h / "temp1_input")
        if t is not None:
            return t
    return None


# ------------------------------------------------------------ 判级


def _raw(soc: float | None, nvme: float | None, stopped: int, lim: Limits) -> int:
    def at(v: float | None, w: float, s: float, c: float) -> int:
        if v is None:
            return 0
        return 3 if v >= c else 2 if v >= s else 1 if v >= w else 0
    fans = 2 if stopped >= lim.shed_fans else 1 if stopped >= lim.warn_fans else 0
    return max(at(soc, lim.warn_soc, lim.shed_soc, lim.crit_soc),
               at(nvme, lim.warn_nvme, lim.shed_nvme, lim.crit_nvme), fans)


def classify(soc: float | None, nvme: float | None, stopped: int, prev: str,
             lim: Limits) -> tuple[str, str]:
    """→ (级别, 原因)。升级当场升;降级按门槛低 ``hyst_c`` 的那一套算。"""
    raw = _raw(soc, nvme, stopped, lim)
    p = LEVELS.index(prev) if prev in LEVELS else 0
    lvl = raw if raw >= p else min(p, _raw(soc, nvme, stopped, lim.lowered()))
    parts = []
    if soc is not None:
        parts.append(f"SoC {soc:.0f} °C")
    if nvme is not None:
        parts.append(f"NVMe {nvme:.0f} °C")
    if stopped:
        parts.append(f"{stopped} 只风扇停了")
    return LEVELS[lvl], ",".join(parts) or "读不到温度"


# ------------------------------------------------------------ 监视


@dataclass
class _Fan:
    cfg: FanCfg
    rpm: int | None = None
    slow: int = 0
    error: str = ""


class BoardMonitor:
    def __init__(self, cfg: BoardConfig, *, emit: Callable[[str, dict[str, Any]], Any],
                 mono: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 read_soc: Callable[[], dict[str, float]] = read_soc,
                 read_nvme: Callable[[], float | None] = read_nvme,
                 counter: Callable[[str, list[int]], Any] | None = None) -> None:
        self.cfg = cfg
        self.emit = emit
        self._mono, self._wall = mono, wall
        self._read_soc, self._read_nvme = read_soc, read_nvme
        if counter is None:
            from d1max_contract.gpio import EdgeCounter

            def counter(chip: str, lines: list[int]) -> Any:
                return EdgeCounter(chip, lines)
        self._counter_of = counter
        self._fans = [_Fan(f) for f in cfg.fans]
        self._counters: dict[str, Any] = {}
        self._last = 0.0
        self._next = 0.0
        self.level = "ok"
        self.reason = ""
        self.soc: dict[str, float] = {}
        self.nvme: float | None = None
        self.sampled = False

    def start(self) -> None:
        by_chip: dict[str, list[int]] = {}
        for f in self.cfg.fans:
            by_chip.setdefault(f.chip, []).append(f.line)
        for chip, lines in by_chip.items():
            try:
                self._counters[chip] = self._counter_of(chip, lines)
            except OSError as exc:
                log.error("风扇测速线拿不到(%s):%s", chip, exc)
                for fan in self._fans:
                    if fan.cfg.chip == chip:
                        fan.error = f"测速线拿不到:{exc}"
        self._last = self._mono()

    def close(self) -> None:
        for c in self._counters.values():
            try:
                c.close()
            except Exception:
                log.exception("关测速线出错")
        self._counters.clear()

    @property
    def rpm(self) -> dict[str, int | None]:
        return {f.cfg.name: f.rpm for f in self._fans}

    def stopped(self) -> list[str]:
        return [f.cfg.name for f in self._fans if not f.error and f.slow >= STOP_SAMPLES]

    async def step(self) -> None:
        """运行时每拍调:到点了在线程里读一遍(读 sysfs、写日志,不卡事件循环)。"""
        if self._mono() < self._next:
            return
        self._next = self._mono() + self.cfg.period_s
        try:
            await asyncio.to_thread(self.sample)
        except Exception:
            log.exception("读板子温度、风扇出错")

    def sample(self) -> None:
        now = self._mono()
        dt = max(now - self._last, 1e-3)
        self._last = now
        counts: dict[tuple[str, int], int] = {}
        for chip, c in self._counters.items():
            for line, n in c.take().items():
                counts[(chip, line)] = n
            if getattr(c, "error", ""):
                for fan in self._fans:
                    if fan.cfg.chip == chip:
                        fan.error = c.error
        for fan in self._fans:
            if fan.error:
                continue
            n = counts.get((fan.cfg.chip, fan.cfg.line), 0)
            fan.rpm = int(round(n / fan.cfg.pulses / dt * 60))
            fan.slow = fan.slow + 1 if fan.rpm < fan.cfg.min_rpm else 0
        self.soc = self._read_soc()
        self.nvme = self._read_nvme()
        soc_max = max(self.soc.values()) if self.soc else None
        stopped = self.stopped()
        lvl, why = classify(soc_max, self.nvme, len(stopped), self.level, self.cfg.limits)
        self.reason = why
        self.sampled = True
        self._log(soc_max, lvl)
        if lvl != self.level:
            log.warning("上装主控板 %s → %s:%s", self.level, lvl, why)
            self.level = lvl
            self.emit("board_thermal", {"level": lvl, "reason": why, "soc_c": soc_max,
                                        "nvme_c": self.nvme, "fans_stopped": stopped})

    def _log(self, soc_max: float | None, lvl: str) -> None:
        stamp = datetime.fromtimestamp(self._wall(), tz=timezone.utc)
        d = Path(self.cfg.log_dir)
        p = d / f"board-{stamp:%Y%m%d}.csv"
        head = ["utc", "level", "soc_c", "nvme_c", *(f"fan_{f.cfg.name}_rpm" for f in self._fans),
                "zones"]

        def num(v: float | None) -> str:
            return "" if v is None else f"{v:.1f}"
        row = [stamp.isoformat(timespec="seconds"), lvl, num(soc_max), num(self.nvme),
               *("" if f.rpm is None else str(f.rpm) for f in self._fans),
               ";".join(f"{k}={v:.1f}" for k, v in sorted(self.soc.items()))]
        try:
            d.mkdir(parents=True, exist_ok=True)
            new = not p.exists()
            with p.open("a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(head)
                w.writerow(row)
        except OSError as exc:
            log.warning("板子温度日志写不进去:%s", exc)

    def health(self) -> list[dict[str, Any]]:
        from d1max_contract.devices import device
        out = []
        if not self.sampled:
            out.append(device("board", "offline", "还没读过温度"))
        elif not self.soc and self.nvme is None:
            out.append(device("board", "offline", "读不到温度(温区、NVMe 都没有)"))
        else:
            state = {"ok": "online", "warn": "degraded"}.get(self.level, "error")
            name = {"ok": "", "warn": "警戒:", "shed": "过热,已降负载:",
                    "critical": "严重过热:"}[self.level]
            out.append(device("board", state, f"{name}{self.reason}"))
        stopped = set(self.stopped())
        for f in self._fans:
            fid = f"fan:{f.cfg.name}"
            if f.error:
                out.append(device(fid, "error", f.error))
            elif f.cfg.name in stopped:
                out.append(device(fid, "error", f"停转了({f.rpm} rpm)"))
            else:
                out.append(device(fid, "online", "还没测" if f.rpm is None else f"{f.rpm} rpm"))
        return out
