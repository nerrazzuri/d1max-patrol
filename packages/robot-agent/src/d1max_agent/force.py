"""异常受力检测(W26,决策 52):翻倒、被抱起来、被撞(推、踢)。

狗上每拍读一次 :meth:`RobotHAL.imu`(:class:`~d1max_contract.hal.ImuSample`),这里判:

- **翻倒**:横滚或俯仰超过 ``flip_tilt_rad`` 持续 ``flip_hold_s``。代理:撤掉会让狗动的任务、停车、
  **软急停**(不让它自己乱蹬),报站点 P1,等人去扶、人工解除急停(决策 52)。扶正了(倾角回到
  ``upright_rad`` 以内稳 ``upright_hold_s``)状态回 ``ok``,急停不自动解。
- **被抱起来**:四条腿承重(``load``)掉到**站立基线**的 ``lift_load_ratio`` 以下持续 ``lift_hold_s``。
  基线 = 最近 ``baseline_s`` 秒里站着、没歪时承重的中位数;没有基线(刚起来、厂家不给力矩)就不判。
  代理:撤任务、停车(悬空还迈腿会伤人),站点报 P1、布防时响警笛(决策 52)。放回地上(承重回到
  基线的一半以上稳 ``land_hold_s``)回 ``ok``。
- **被撞**:``shock_g``(两次读之间加速度偏离 1 g 最大的那一下)超过 ``bump_g``。报站点 P2、任务照跑
  (门槛没标定前误报会多,决策 52);``bump_cooldown_s`` 内只报一次。翻倒、抱起来时不报撞。

**落盘**(W26 外审 3):翻倒、抱起来的状态,抱起前的承重基线,还欠着没确认做成的安全动作
(:attr:`ForceWatch.owed`),都写进代理存储目录的 ``force.json``。代理重启了照旧锁着、照旧补做,
要数据确认安全(扶正稳住、放回地上承重回来)才解锁;锁着的时候不学基线(悬空的低承重学不进去)。

**门槛全是占位**:真狗上标定(``庄园场景待真机测试.md`` §3r),标好写进 ``/etc/d1max/force.json``
(:meth:`ForceConfig.load`,只写要改的项)。厂家 SDK 给哪几样就判哪几样,:attr:`ForceWatch.checks`
报站点(读不到承重就不判抱起来)。
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
from collections import deque
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from d1max_contract.hal import ImuSample

log = logging.getLogger(__name__)

OK, FLIPPED, LIFTED = "ok", "flipped", "lifted"


@dataclass(frozen=True)
class ForceConfig:
    flip_tilt_rad: float = math.radians(60)
    flip_hold_s: float = 0.5
    upright_rad: float = math.radians(20)
    upright_hold_s: float = 3.0
    lift_load_ratio: float = 0.25
    lift_hold_s: float = 0.5
    land_hold_s: float = 2.0
    baseline_s: float = 10.0
    #: 基线至少要攒这么久的样本才算数。
    baseline_min_s: float = 2.0
    bump_g: float = 1.5
    bump_cooldown_s: float = 30.0

    @classmethod
    def load(cls, path: Path | str | None) -> ForceConfig:
        """``path`` 不在就用缺省;在就按里面写的项盖(认不出的项、不是数的值报错:写错了不能
        悄悄不生效)。"""
        if path is None or not Path(path).is_file():
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{path}:要是一个对象")
        known = {f.name for f in fields(cls)}
        bad = sorted(set(raw) - known)
        if bad:
            raise ValueError(f"{path}:认不出 {', '.join(bad)}")
        for k, v in raw.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                    or v <= 0:
                raise ValueError(f"{path}:{k} 要是正数")
        return replace(cls(), **{k: float(v) for k, v in raw.items()})


@dataclass(frozen=True)
class Finding:
    """这一拍判出来的事:``flipped``、``upright``、``lifted``、``landed``、``bump``。"""
    kind: str
    data: dict[str, Any]


class ForceWatch:
    def __init__(self, cfg: ForceConfig | None = None, *, path: Path | str | None = None) -> None:
        self.cfg = cfg or ForceConfig()
        self.state = OK
        self.since_ms: int | None = None
        #: 欠着、还没确认做成的安全动作(``abort``、``stop``、``estop``):代理每拍补,做成一样划一样。
        self.owed: set[str] = set()
        self._path = Path(path) if path is not None else None
        #: 这只狗判得了哪几样(读到过有效的姿态才判翻倒、撞;有承重基线才判抱起来)。
        self.checks = {"flip": False, "lift": False, "bump": False}
        self._base: deque[tuple[int, float]] = deque()
        self._cand: tuple[str, int] | None = None        # (候选状态, 从什么时候起)
        self._bump_ms: int | None = None
        #: 门槛文件读不懂(照缺省判):能力里带上,站点报 P2。
        self.config_error = ""
        self._load()

    # ------------------------------------------------------------ 落盘

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            st = d.get("state")
            if st not in (OK, FLIPPED, LIFTED):
                raise ValueError(f"state={st!r}")
            self.state = st
            self.since_ms = d.get("since_ms")
            base = d.get("lift_base")
            self._lift_base = float(base) if base is not None else None
            self.owed = {a for a in d.get("owed", []) if a in ("abort", "stop", "estop")}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # 读不懂:当翻倒了锁着(宁可要人去看一眼),不当没事
            log.error("受力状态 %s 读不懂(%s):当翻倒锁着,要扶正稳住才解", self._path, exc)
            self.state, self.owed = FLIPPED, {"abort", "stop"}
        if self.state != OK:
            log.warning("代理起来时受力状态是 %s(上次没恢复):照旧锁着", self.state)

    #: 有改动还没写成盘(W26 复查 1):代理每拍看,写成了才放下。
    dirty = False

    def save(self) -> None:
        """状态、基线、欠着的动作写盘(原子替换)。写成了放下 :attr:`dirty`;写不进去照抛、
        :attr:`dirty` 留着(调用方记日志,下一拍再写)。"""
        if self._path is None:
            self.dirty = False
            return
        d = {"state": self.state, "since_ms": self.since_ms, "lift_base": self._lift_base,
             "owed": sorted(self.owed)}
        tmp = self._path.with_name(self._path.name + ".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, self._path)
        self.dirty = False

    def caps(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "checks": dict(self.checks)}
        if self.config_error:
            out["config_error"] = self.config_error
        if self.owed:
            out["owed"] = sorted(self.owed)             # 安全处置还没做完(还锁着)
        if self.since_ms is not None and self.state != OK:
            out["since_ms"] = self.since_ms
        return out

    def _baseline(self, now_ms: int) -> float | None:
        b = self._base
        while b and now_ms - b[0][0] > self.cfg.baseline_s * 1000:
            b.popleft()
        if not b or now_ms - b[0][0] < self.cfg.baseline_min_s * 1000:
            return None
        return statistics.median(v for _, v in b)

    def _hold(self, want: str | None, now_ms: int, hold_s: float) -> bool:
        """``want`` 连续 ``hold_s`` 秒才算。"""
        if want is None:
            self._cand = None
            return False
        if self._cand is None or self._cand[0] != want:
            self._cand = (want, now_ms)
        return now_ms - self._cand[1] >= hold_s * 1000

    def feed(self, s: ImuSample | None, now_ms: int, *, standing: bool) -> list[Finding]:
        """喂一拍。``standing``:运控说狗站着(站立、能收速度):只有站着时的承重进基线、才判抱起来。
        回这一拍判出来的事(状态变了、撞了一下)。没数据、不新鲜:什么都不判,状态不变。"""
        if s is None or not s.valid:
            self._cand = None
            return []
        c = self.cfg
        self.checks["flip"] = self.checks["bump"] = True
        tilt = max(abs(s.roll), abs(s.pitch))
        info = {"roll_deg": round(math.degrees(s.roll), 1),
                "pitch_deg": round(math.degrees(s.pitch), 1)}
        base = self._baseline(now_ms)
        self.checks["lift"] = base is not None or self.state == LIFTED
        out: list[Finding] = []
        if self.state == OK:
            if tilt >= c.flip_tilt_rad:
                if self._hold("flip", now_ms, c.flip_hold_s):
                    out.append(self._enter(FLIPPED, now_ms, info))
            elif s.load is not None and base is not None and standing \
                    and s.load < base * c.lift_load_ratio:
                if self._hold("lift", now_ms, c.lift_hold_s):
                    out.append(self._enter(LIFTED, now_ms, {**info, "load": round(s.load, 2),
                                                            "baseline": round(base, 2)}))
            else:
                self._cand = None
                if s.load is not None and standing and tilt < c.upright_rad:
                    self._base.append((now_ms, s.load))
        elif self.state == FLIPPED:
            if self._hold("upright" if tilt < c.upright_rad else None, now_ms, c.upright_hold_s):
                out.append(self._leave("upright", now_ms, info))
        elif self.state == LIFTED:
            if tilt >= c.flip_tilt_rad:
                if self._hold("flip", now_ms, c.flip_hold_s):  # 抱着翻过来了:按翻倒(急停)
                    out.append(self._enter(FLIPPED, now_ms, info))
            else:
                back = s.load is not None and self._lift_base is not None \
                    and s.load >= self._lift_base * 0.5
                if self._hold("land" if back else None, now_ms, c.land_hold_s):
                    out.append(self._leave("landed", now_ms, info))
        if self.state == OK and s.shock_g >= c.bump_g and not out and (
                self._bump_ms is None or now_ms - self._bump_ms >= c.bump_cooldown_s * 1000):
            self._bump_ms = now_ms
            out.append(Finding("bump", {**info, "shock_g": round(s.shock_g, 2)}))
        return out

    _lift_base: float | None = None

    def _enter(self, state: str, now_ms: int, info: dict[str, Any]) -> Finding:
        if state == LIFTED:
            self._lift_base = info.get("baseline")
        self.state, self.since_ms, self._cand = state, now_ms, None
        self.owed |= {"abort", "stop"} | ({"estop"} if state == FLIPPED else set())
        self.dirty = True
        return Finding(state, info)

    def _leave(self, kind: str, now_ms: int, info: dict[str, Any]) -> Finding:
        # 姿态恢复 ≠ 安全处置做完了(W26 复查 2):欠着的动作**不清**,代理接着补,补完之前照旧锁着
        self.state, self.since_ms, self._cand = OK, now_ms, None
        self._lift_base = None
        self.dirty = True
        self._base.clear()                              # 换了个姿势、位置:基线重攒
        self.checks["lift"] = False
        return Finding(kind, info)
