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
import os
import re
import shutil
import uuid
from collections.abc import Mapping, Sequence
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

#: "已经传到**服务器**了"的标记文件,就在 run 目录里。**本卷只定义它、只读它**;
#: 写它是「服务器与值守」卷的 ``uploader.py`` 的事。
#:
#: **它只有这一个含义。** 一次人工导出证明不了"服务器上有第二份" —— 那件事
#: 由 :data:`EXPORTED_REL` 记,两个标记不许混用,理由见 ``_deletable``。
UPLOADED_REL = ".uploaded"

#: "已经导到**客户手里**了"的标记文件。由 ``export.confirm_bundle`` 在哈希
#: 对上之后打上。
#:
#: 跟 :data:`UPLOADED_REL` 分开,是因为"有服务器"档下那条硬约束(spec §4.6
#: 那张表)的判据是**传到服务器了没有**:回传断了三天、操作员用导出通道把这
#: 几趟拉到手机上确认过,并不等于服务器上有了 —— 混用一个标记的话,这几趟会
#: 在次日过水位时被删掉,而服务器端的档案里从此有一个三天的洞。
EXPORTED_REL = ".exported"

#: 删除预告落在 runs 根目录下的这个文件里。**预告必须落盘**:一句只显示在
#: app 上的提示不可验证 —— 狗这边没有任何东西能证明"说过了",于是 §4.6 那条
#: "删之前必须先预告"的硬约束在代码里等于没写。
NOTICE_REL = "retention-notice.json"

#: ``archive._make_dir`` 撞上同名会追加 ``-2``、``-3``。拆时间戳前得先切掉。
_SUFFIX = re.compile(r"-\d+$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def unique_tmp(target: Path) -> Path:
    """给 ``target`` 配一个**这次调用独有**的临时文件名。

    **固定的 ``.tmp`` 名字在多线程服务里会互相踩。** A 写到一半,B 把自己那份
    ``replace`` 过去,读回来就是半截文件。``app/server.py`` 是
    ``ThreadingHTTPServer``,手机和网页同时开着盘况页就能撞上;这里那个半截
    文件是预告台账,读坏之后 ``read_notice`` 回空,所有归档的首次预告时刻一起
    清零,7 天的钟从头走。

    进程号 + uuid:同一台机器上跑两个进程也不会撞。名字**以 ``.tmp`` 结尾、
    并且在原后缀之后**,所以按 ``*.jpg`` / ``*.zip.json`` 之类的 glob 列目录
    时捡不到它。

    公开是因为 ``baselines`` 和 ``export`` 落盘时要的是同一条规矩,而这种
    规矩只该有一份。
    """
    return target.with_name(f"{target.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")


@dataclass(frozen=True, slots=True)
class RunInfo:
    """一趟归档在盘上的样子。**纯数据,不带 I/O** —— 所以过期判断能被穷举测。"""

    path: Path
    mission: str
    started_at: datetime
    retention_days: int
    size_bytes: int
    settled: bool
    #: 传到**服务器**了没有(:data:`UPLOADED_REL`)。
    uploaded: bool
    #: 导到**客户手里**了没有(:data:`EXPORTED_REL`)。两者不是一回事,
    #: 见 ``_deletable``。默认 ``False``:没打过标记就是没有。
    exported: bool = False

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
            "exported": self.exported,
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


def is_settled(run_dir: Path | str, *, now: datetime | None = None) -> bool:
    """这一趟**还有没有人在写**。见 :attr:`RunInfo.settled`。

    两条出口,跟 :func:`scan_runs` 那一格是**同一份判据**:manifest 里有
    ``summary``(跑完了),或者目录名上的起始时刻已经老过 :data:`SETTLE_HOURS`
    (崩在半路的残骸 —— 也没人在写它了)。两条都不满足的一律叫"不安定"。

    **这个函数存在的全部理由是「这份判据只许有一处」。** 它有两个调用方:
    ``scan_runs`` 那一格,和回传那条线上"这一趟能不能打「可删」"那道闸
    (``app/server.py``)。抄第二遍的后果不是重复代码,是**两份定义会漂**:
    漂开的那天,回传这边认为一趟收工了就打上「可删」,而清扫器那边也认为它
    收工了于是删掉 —— 而它其实还在写,照片一个字节都没传出去。

    目录名不是时间戳就回 ``False``:不是我们建的目录,一律当"不安定",不碰。
    """
    run = Path(run_dir)
    started = _started_at(run.name)
    if started is None:
        return False
    raw = _manifest(run)
    now = now if now is not None else _utcnow()
    # 有 summary 就是跑完了;没有但已经过了安定期,那是崩在半路的残骸 ——
    # 也没人在写它了。两条都不满足的才叫"不安定",一律不碰。
    return bool(raw and raw.get("summary")) or started < now - timedelta(hours=SETTLE_HOURS)


def scan_runs(runs_root: Path | str, *, now: datetime | None = None) -> list[RunInfo]:
    """扫一遍 runs 根目录,**最老在前**。

    最老在前是给清扫器用的:要删就从最老的开始。``list_runs`` 是新的在前,
    这里反过来。
    """
    now = now if now is not None else _utcnow()
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
            # **判据整条走 :func:`is_settled`,这儿不另写一份。** 回传那条线
            # 上"能不能打「可删」"问的是同一个函数 —— 两处分岔的那天,一趟
            # 还在写的归档会被一边打上「可删」、另一边照着标记删掉。
            settled=is_settled(run, now=now),
            uploaded=(run / UPLOADED_REL).exists(),
            exported=(run / EXPORTED_REL).exists()))
    out.sort(key=lambda i: (i.started_at, i.path.name, i.mission))
    return out


def mark_uploaded(run_dir: Path | str) -> Path:
    """标成"**服务器上**还有一份"。**标记不等于删** —— 真删等到水位线(spec §4.4)。

    **只有一个写入者:**``uploader`` 传成功(后面那一卷)。人工导出打的是
    :func:`mark_exported`,不是这个 —— 一次导到手机上的确认证明不了服务器
    收到了,而"有服务器"档下的硬约束(spec §4.6 那张表)判的正是服务器。
    """
    path = Path(run_dir) / UPLOADED_REL
    path.touch(exist_ok=True)
    return path


def mark_exported(run_dir: Path | str) -> Path:
    """标成"**客户手里**还有一份"。由 ``export.confirm_bundle`` 打上。

    单机档下它跟 :func:`mark_uploaded` 等价 —— 那一档的判据是"世界上还有没有
    第二份",拉到手机上和传到服务器上一样算数。有服务器的那一档下它**不算数**,
    理由见 ``_deletable``。
    """
    path = Path(run_dir) / EXPORTED_REL
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


def delete_starts_at(info: RunInfo, *, first_noticed: datetime) -> datetime:
    """这一趟最早什么时候会被删。**两个钟都得走完:保留期,和预告期。**

    ``max(到期日, 首次预告时刻 + MIN_NOTICE_DAYS)``。到期日那天什么也不会
    发生 —— 预告的钟还没满 —— 所以拿到期日当"要删了"报给客户,报的是一个
    必然平安无事的日子,而真正动手的是这个函数算出来的时刻。

    ``_deletable`` 单机那一支就是 ``now >= delete_starts_at(...)``,两处共用
    这一个式子:预告说的哪一天和清扫器动手的哪一天,不许各算各的。
    """
    return max(info.expires_at(),
               first_noticed + timedelta(days=MIN_NOTICE_DAYS))


def forecast(runs: Sequence[RunInfo], *, now: datetime, used_ratio: float,
             days: int = MIN_NOTICE_DAYS,
             noticed: Mapping[str, datetime] | None = None) -> Forecast:
    """算预告名单:**``days`` 天内要到期的,加上已经到期还没删的。**

    后一半不能漏。§4.4 说过期不等于立刻删,真删要等水位线,所以"已过期但还在
    盘上"是常态 —— 而它们恰恰是下一次腾地方时第一批被删的。漏掉就等于没预告。

    ``noticed`` 是台账(``read_notice`` 的结果),用来算 spec §4.6 第 1 条要的
    那句话里的**开始删除日**。**为什么把它收进参数,而不是把这句话挪到
    ``write_notice`` 之后拼:**

    - 文案只该有一处出处。挪到 ``write_notice`` 里,拼话的地方就分成了两半
      (名单空的那句仍在这儿),而这是要给客户看的一句话。
    - ``forecast`` 不会因此依赖它算不出来的东西:台账里没有的键,首次预告时刻
      就是**此刻** —— 调用方紧接着那次 ``write_notice`` 记下的正是 ``now``。
      所以 ``noticed.get(key, now)`` 不是估计,是准确值。
    - 改动面最小:调用方多传一个已经在手上的 dict,别的什么都不用动。

    不传就当台账是空的,也就是"这些都是刚被预告到的" —— 那正是第一次看盘况
    时的实情。
    """
    noticed = noticed if noticed is not None else {}
    picked = tuple(sorted((r for r in runs if r.days_left(now) <= days),
                          key=lambda r: (r.started_at, r.path.name, r.mission)))
    at_risk = sum(r.size_bytes for r in picked)
    used = f"{used_ratio:.0%}"
    if not picked:
        detail = f"盘 {used},{days} 天内没有归档到期。"
    else:
        starts = min(delete_starts_at(r, first_noticed=noticed.get(run_key(r), now))
                     for r in picked)
        # 向下取整:说少了会让人早点动手,说多了会让人错过 —— 这句话是催人
        # 去导出的,宁可早。
        left = int(max(0.0, (starts - now).total_seconds()) // 86400)
        when = f"预计 {left} 天后开始删除" if left else "最快今天就会开始删除"
        # "之前"是严格早于,所以取名单里**最新那一趟的次日** —— 名单里每一趟
        # 都严格早于这个日子,这句话因此是准确的,不是约等于。
        cutoff = (max(r.started_at for r in picked)
                  + timedelta(days=1)).strftime("%Y-%m-%d")
        detail = (f"盘 {used},{when} {cutoff} 之前的记录:"
                  f"{days} 天内将有 {len(picked)} 趟归档到期,"
                  f"共约 {at_risk / (1024 * 1024):.0f}MB。"
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
    """把预告名单并进台账,返回合并好的结果。

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
    # 临时名必须**这次调用独有**,见 ``unique_tmp``:固定名字加多线程,
    # 台账会被写成半截,而那等于所有的钟一起清零。
    tmp = unique_tmp(target)
    try:
        text = json.dumps({k: v.strftime(STAMP_FMT) for k, v in merged.items()},
                          ensure_ascii=False, indent=2)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return merged


# --------------------------------------------------------------- 删除计划与真删


def _mb(n: float) -> str:
    return f"{n / (1024 * 1024):.0f}MB"


@dataclass(frozen=True, slots=True)
class Sweep:
    """这一次要删哪些、能腾出多少、还差多少。**只是计划,没动手。**"""

    delete: tuple[RunInfo, ...]
    freed_bytes: int
    short_bytes: int
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "delete": [r.to_wire() for r in self.delete],
            "freed_bytes": self.freed_bytes,
            "short_bytes": self.short_bytes,
            "detail": self.detail,
        }


def _deletable(info: RunInfo, *, now: datetime, has_upload: bool,
               noticed: dict[str, datetime]) -> bool:
    """这一趟现在能不能删。spec §4.6 那张表就是这个函数。"""
    if not info.settled:
        # 正在写的那一趟不碰,两套策略都一样。
        return False
    if has_upload:
        # 有服务器:分类依据是已传/未传,**未传的绝不删**。传走了的当天就
        # 可以按水位删 —— 服务器上有第二份。
        #
        # **``exported`` 在这一档下不算数。** 判据是"传到服务器了没有",而
        # 一次人工导出证明不了这件事:回传断了三天、操作员把这几趟拉到手机上
        # 确认过,次日过水位就被删了 —— 回传恢复之后 uploader 找不到它们,
        # 服务器端的档案里从此有一个三天的洞,而服务器才是这一档下的权威副本。
        return info.uploaded
    # 单机:分类依据是保留期内/外,而且删之前必须先预告,还得挂满。
    if info.uploaded or info.exported:
        # 但"别处还有一份"永远优先。单机档下的判据就是"世界上还有没有第二份",
        # 传到服务器和拉到客户手机上在这件事上完全等价 —— 后者正是 §4.6 第 2 条
        # 「导出并释放」通道的出口。走过任一条的,不必再等保留期。
        return True
    first = noticed.get(run_key(info))
    if first is None:
        # 台账里没有它就是没预告过。§4.6 硬约束:删之前必须先预告。
        return False
    return now >= delete_starts_at(info, first_noticed=first)


def plan_sweep(runs: Sequence[RunInfo], *, now: datetime, has_upload: bool,
               need_bytes: int,
               noticed: dict[str, datetime] | None = None) -> Sweep:
    """算这一次的删除计划。**最老的先删。**

    ``need_bytes <= 0`` 时一趟都不删,哪怕全过期了 —— spec §4.4:删除由水位
    驱动,不由过期驱动。立刻删的话,服务器那边一旦出事,狗这边已经空了,世界
    上就没有第二份了;而留着那份冗余不花任何额外成本,盘反正空着。
    """
    if need_bytes <= 0:
        return Sweep((), 0, 0, "盘在水位线下,不用删任何东西。")
    noticed = noticed if noticed is not None else {}
    picked: list[RunInfo] = []
    freed = 0
    for info in sorted(runs, key=lambda r: (r.started_at, r.path.name, r.mission)):
        if freed >= need_bytes:
            break
        if not _deletable(info, now=now, has_upload=has_upload, noticed=noticed):
            continue
        picked.append(info)
        freed += info.size_bytes
    short = max(0, need_bytes - freed)
    if short == 0:
        detail = f"按水位删 {len(picked)} 趟归档,腾出 {_mb(freed)}。"
    else:
        why = ("剩下的都还没传走 —— 未传的绝不删,而回传可能已经断了很久"
               if has_upload else
               "剩下的都还在保留期内,或者预告还没挂满")
        # §4.6:这个死锁是故意留的。它逼出一次人的确认,而这正是"删掉唯一
        # 副本"这件事应该有的门槛。文案跟 `storage.py` 里那句对齐:说的是
        # 下一步做什么,不是"盘满了"。
        detail = (f"要腾出 {_mb(need_bytes)},能自动删的只够 {_mb(freed)},"
                  f"还差 {_mb(short)} —— {why}。"
                  f"需要人在 app 上「导出并释放」:先打包拉走,确认拿到了,才删")
    return Sweep(tuple(picked), freed, short, detail)


def apply_sweep(sweep: Sweep, *, runs_root: Path | str) -> tuple[Path, ...]:
    """按计划真删,返回**确实删掉了的**那些。

    **护栏:每条路径 ``resolve()`` 之后必须落在 ``runs_root`` 之下。**
    ``shutil.rmtree`` 是全仓最危险的一行;没有这条,日后任何一个"顺手把 path
    拼错了"的改动都会安静地删掉别处的东西。越界的**跳过**,不抛 —— 一条脏
    记录不该阻止其余的清扫。

    删不动的也跳过(只读挂载、被占用)。返回值是事实,不是计划。
    """
    root = Path(runs_root).resolve()
    done: list[Path] = []
    for info in sweep.delete:
        try:
            target = info.path.resolve()
        except OSError:
            continue
        if target == root or not target.is_relative_to(root):
            continue
        if not target.is_dir():
            continue
        try:
            shutil.rmtree(target)
        except OSError:
            continue
        done.append(info.path)
    return tuple(done)
