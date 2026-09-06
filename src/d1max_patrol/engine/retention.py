"""归档的保留:什么该留、什么可以删。**这一段只读盘,不删任何东西。**

删除计划和真删在同一个模块的下半部分(Task 5 追加),分开写是因为两件事
失败的方式完全不同:这里的坑是"读错了"(时间戳拆不开、manifest 坏了、
目录名带撞名后缀),那里的坑是"删错了"。

**它不认识引擎。** ``LoopBridge`` 单门不变式说 HTTP 线程不许摸引擎状态,
所以"现在有没有在跑"这个问题不能问引擎 —— 改成从盘上判断,见 ``settled``。

spec §4.4:上传成功只把文件标成"可删",不立刻删,真正的删除等到盘触及水位
线时才发生。立刻删的话,服务器那边一旦出事,狗这边已经空了,世界上就没有
第二份了。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from d1max_patrol.engine.archive import STAMP_FMT, list_runs, read_manifest

#: 没写保留期时按这个算。跟 ``mission.Policy.retention_days`` 的默认值是
#: 同一个数(spec §4.6「保留期默认 90 天」)。
DEFAULT_RETENTION_DAYS = 90

#: 删之前至少要预告这么多天(spec §4.6 单机硬约束)。同时也是预告的**提前量**
#: —— 预告列的是"这么多天内要过期的",于是一趟归档在过期前正好被挂满这么久。
#:
#: **不能超过 ``mission.MIN_RETENTION_DAYS``**,超了每一趟归档一落地就已经
#: 过了预告期,预告整个失去意义。``tests/engine/test_retention.py`` 有一条
#: 钉住这个关系。
MIN_NOTICE_DAYS = 7

#: 一趟归档多久算"没人再写它了"。见 ``RunInfo.settled``。
SETTLE_HOURS = 24

#: 一旦要腾地方,腾到盘用量降到这条线以下为止。留出余量,免得删一趟就又满,
#: 变成每次出任务都在删。
SWEEP_TARGET_RATIO = 0.70

#: "已经传走了"的标记文件,就在 run 目录里。**本卷只定义它、只读它**;写它
#: 是「服务器与值守」卷的 ``uploader.py`` 的事。
UPLOADED_REL = ".uploaded"

#: 删除预告落在 runs 根目录下的这个文件里。**预告必须落盘**:一句只显示在
#: app 上的提示不可验证 —— 狗这边没有任何东西能证明"说过了",于是 §4.6 那条
#: "删之前必须先预告"的硬约束在代码里等于没写。
NOTICE_REL = "retention-notice.json"

#: ``archive._make_dir`` 撞上同名会追加 ``-2``、``-3``。拆时间戳前得先切掉。
_SUFFIX = re.compile(r"-\d+$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RunInfo:
    """一趟归档在盘上的样子。**纯数据,不带 I/O** —— 所以过期判断能被穷举测。"""

    path: Path
    mission: str
    started_at: datetime
    retention_days: int
    size_bytes: int
    settled: bool
    uploaded: bool

    def expires_at(self) -> datetime:
        return self.started_at + timedelta(days=self.retention_days)

    def days_left(self, now: datetime) -> float:
        """还剩几天到期。已经过期就是 0,**不是负数** —— 负数会在排序和文案
        里到处冒出来,而"过期了"这件事 ``is_expired`` 已经说清楚了。"""
        left = (self.expires_at() - now).total_seconds() / 86400.0
        return max(0.0, left)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at()

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "mission": self.mission,
            "started_at": self.started_at.strftime(STAMP_FMT),
            "retention_days": self.retention_days,
            "size_bytes": self.size_bytes,
            "settled": self.settled,
            "uploaded": self.uploaded,
        }


def _started_at(name: str) -> datetime | None:
    """从目录名拆时间戳。**不从 manifest 拆** —— manifest 可能缺、可能坏,
    而目录名是 ``archive._make_dir`` 亲手写的,``list_runs`` 排序也靠它。"""
    try:
        stamp = datetime.strptime(_SUFFIX.sub("", name), STAMP_FMT)
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc)


def _manifest(run_dir: Path) -> dict[str, Any] | None:
    """读 manifest,坏了当没有。

    ``archive.read_manifest`` 遇到坏 JSON 是**抛**,不是回 ``None`` ——
    那是它的本分(归档自己读自己,读不出来就该炸)。但一份坏 manifest 不能
    让清扫器从此瘫掉:那台狗的盘会一直满下去,而没有人知道为什么。
    """
    try:
        raw = read_manifest(run_dir)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _retention_days(raw: dict[str, Any] | None) -> int:
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    mission = raw.get("mission")
    policy = mission.get("policy") if isinstance(mission, dict) else None
    value = policy.get("retention_days") if isinstance(policy, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_RETENTION_DAYS


def _size_bytes(run_dir: Path) -> int:
    total = 0
    for path in run_dir.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def scan_runs(runs_root: Path | str, *, now: datetime | None = None) -> list[RunInfo]:
    """扫一遍 runs 根目录,**最老在前**。

    最老在前是给清扫器用的:要删就从最老的开始。``list_runs`` 是新的在前,
    这里反过来。
    """
    now = now if now is not None else _utcnow()
    settle_before = now - timedelta(hours=SETTLE_HOURS)
    out: list[RunInfo] = []
    for run in list_runs(runs_root):
        started = _started_at(run.name)
        if started is None:
            # 目录名不是时间戳,不是我们建的,不碰。
            continue
        raw = _manifest(run)
        out.append(RunInfo(
            path=run,
            # 任务名取父目录名,不取 manifest:目录结构一定在,manifest 不一定。
            mission=run.parent.name,
            started_at=started,
            retention_days=_retention_days(raw),
            size_bytes=_size_bytes(run),
            # 有 summary 就是跑完了;没有但已经过了安定期,那是崩在半路的
            # 残骸 —— 也没人在写它了。两条都不满足的才叫"不安定",一律不碰。
            settled=bool(raw and raw.get("summary")) or started < settle_before,
            uploaded=(run / UPLOADED_REL).exists()))
    out.sort(key=lambda i: (i.started_at, i.path.name, i.mission))
    return out


def mark_uploaded(run_dir: Path | str) -> Path:
    """标成"已经传走了"。**标记不等于删** —— 真删等到水位线(spec §4.4)。"""
    path = Path(run_dir) / UPLOADED_REL
    path.touch(exist_ok=True)
    return path


def bytes_to_free(*, used_bytes: int, total_bytes: int,
                  target_ratio: float = SWEEP_TARGET_RATIO) -> int:
    """要把盘用量压到 ``target_ratio`` 以下,得腾出多少字节。已经在线下就是 0。"""
    if total_bytes <= 0:
        return 0
    target = int(total_bytes * target_ratio)
    return max(0, used_bytes - target)


def run_key(info: RunInfo) -> str:
    """预告文件里认一趟归档用的键。任务名 + 目录名,跟盘上的路径一一对应。"""
    return f"{info.mission}/{info.path.name}"


@dataclass(frozen=True, slots=True)
class Forecast:
    """预告:再过不久这些就要到期了。**文案在这里拼,不在 app 里拼** ——
    免得手机端和网页端各说各的,而这是要给客户看的一句话。"""

    generated_at: datetime
    used_ratio: float
    runs: tuple[RunInfo, ...]
    bytes_at_risk: int
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.strftime(STAMP_FMT),
            "used_ratio": self.used_ratio,
            "bytes_at_risk": self.bytes_at_risk,
            "runs": [r.to_wire() for r in self.runs],
            "detail": self.detail,
        }


def forecast(runs: Sequence[RunInfo], *, now: datetime, used_ratio: float,
             days: int = MIN_NOTICE_DAYS) -> Forecast:
    """算预告名单:**``days`` 天内要到期的,加上已经到期还没删的。**

    后一半不能漏。§4.4 说过期不等于立刻删,真删要等水位线,所以"已过期但还在
    盘上"是常态 —— 而它们恰恰是下一次腾地方时第一批被删的。漏掉就等于没预告。
    """
    picked = tuple(sorted((r for r in runs if r.days_left(now) <= days),
                          key=lambda r: (r.started_at, r.path.name, r.mission)))
    at_risk = sum(r.size_bytes for r in picked)
    used = f"{used_ratio:.0%}"
    if not picked:
        detail = f"盘 {used},{days} 天内没有归档到期。"
    else:
        first = min(r.expires_at() for r in picked).strftime("%Y-%m-%d")
        detail = (f"盘 {used},{days} 天内将有 {len(picked)} 趟归档到期"
                  f"(最早 {first}),共约 {at_risk / (1024 * 1024):.0f}MB。"
                  f"要留就先在 app 上导出 —— 到期之后按水位删除。")
    return Forecast(generated_at=now, used_ratio=used_ratio, runs=picked,
                    bytes_at_risk=at_risk, detail=detail)


def read_notice(runs_root: Path | str) -> dict[str, datetime]:
    """读预告台账:键 -> **首次**被预告到的时刻。

    读不出来当成空的,**不抛**。读不出来的后果是删除被推迟(等于从没预告过),
    那是安全的方向;抛出来会把整条清扫链炸掉,盘就再也清不动了。
    """
    path = Path(runs_root) / NOTICE_REL
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, datetime] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            out[key] = datetime.strptime(value, STAMP_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return out


def write_notice(runs_root: Path | str, fc: Forecast) -> dict[str, datetime]:
    """把预告名单并进台账,返回并好的结果。

    **已有的键只读不写。** 每次预告都刷新时间戳的话,只要还在名单里就永远
    挂不满 ``MIN_NOTICE_DAYS`` 天,删除永远轮不到 —— 盘会一直满下去。

    **不在名单里的键清掉。** 否则这个文件只增不减,几年之后是一大坨早已不
    存在的目录。代价是万一某次扫盘漏看了一趟(盘 I/O 抖了一下),它的预告
    时钟会重新起算 —— 只会推迟删除,不会提前删,这个方向可以接受。
    """
    root = Path(runs_root)
    root.mkdir(parents=True, exist_ok=True)
    old = read_notice(root)
    merged = {key: old.get(key, fc.generated_at)
              for key in (run_key(r) for r in fc.runs)}
    target = root / NOTICE_REL
    tmp = target.with_suffix(".json.tmp")
    try:
        text = json.dumps({k: v.strftime(STAMP_FMT) for k, v in merged.items()},
                          ensure_ascii=False, indent=2)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return merged
