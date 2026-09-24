"""导出通道:把一个日期区间的归档打成 zip,算 sha256,对上了才放行。

**没有导出通道的自动删除,等于系统单方面销毁客户资产**(spec §4.6 第 2 条)。
所以这一层不是锦上添花,它是水位删除能存在的前提。

**"对上了才放行"跟 §4.2 是同一个规矩:传成功 = 收方重算的哈希对上。**
客户端把包拉走之后自己重算一遍报回来,对上了才写确认、才给里面那些 run 打
上"别处还有一份"的标记。对不上就抛错、不写确认、不打标记 —— 那种情况下
删掉就是真的没了。
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from d1max_agent.engine.archive import STAMP_FMT
from d1max_agent.engine.retention import RunInfo, mark_exported, unique_tmp

#: 导出包放在 runs 根目录**旁边**的这个目录里。放里面会被 ``scan_runs``
#: 当成归档扫进去,然后被自己的水位删除删掉 —— 而它正是为了对抗删除才存在的。
EXPORTS_DIR_NAME = "exports"

#: 一次读这么多算哈希。包可能有好几个 G,不能整个读进内存 —— Orin 上没那么多。
_CHUNK = 1024 * 1024

#: 包名的形状。这个名字后面要从 HTTP 上收进来,不卡住就是一条目录穿越。
_NAME = re.compile(r"^export-[0-9A-Za-z]+(-\d+)?\.zip$")


class ExportError(Exception):
    """导出这条链上出的事:区间里没东西、包不在、哈希对不上、名字不合法。"""


def safe_name(name: str) -> str:
    """卡一个包名。**这是公开的** —— HTTP 那一层要拿它挡目录穿越,
    而挡穿越的规矩只该有一份。"""
    if not isinstance(name, str) or not _NAME.match(name):
        raise ExportError(f"包名不合法: {name!r}")
    return name


def sha256_file(path: Path | str) -> str:
    """流式算文件的 sha256。**不整个读进内存** —— 包可能有好几个 G。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ExportBundle:
    """一个导出包。``confirmed`` 之前它只是一份拷贝,之后才算"别处还有一份"。"""

    name: str
    path: Path
    sha256: str
    size_bytes: int
    runs: tuple[str, ...]
    created_at: datetime
    confirmed: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "runs": list(self.runs),
            "created_at": self.created_at.strftime(STAMP_FMT),
            "confirmed": self.confirmed,
        }


def pick_runs(runs: Sequence[RunInfo], *, start: datetime,
              end: datetime) -> tuple[RunInfo, ...]:
    """挑区间里的归档,**左闭右开**,最老在前。

    左闭右开是为了让相邻两个区间既不重也不漏 —— 人会一个月一个月地导。
    """
    return tuple(sorted((r for r in runs if start <= r.started_at < end),
                        key=lambda r: (r.started_at, r.path.name, r.mission)))


def _sidecar(out_dir: Path | str, name: str) -> Path:
    return Path(out_dir) / f"{safe_name(name)}.json"


def _write_sidecar(out_dir: Path | str, bundle: ExportBundle) -> None:
    target = _sidecar(out_dir, bundle.name)
    # 临时名这次调用独有,见 ``retention.unique_tmp``:``_run_judge`` /
    # ``_export_new`` 都是从 HTTP 线程直接跑的,而服务是多线程的。
    # ``list_bundles`` 的 ``*.zip.json`` 也因此捡不到它 —— 名字以 ``.tmp`` 结尾。
    tmp = unique_tmp(target)
    try:
        tmp.write_text(json.dumps(bundle.to_wire(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def build_export(infos: Sequence[RunInfo], *, out_dir: Path | str,
                 now: datetime) -> ExportBundle:
    """把这些归档打成一个 zip,算好哈希,写好侧写。

    **一趟都没有就抛错,不打空包。** 空 zip 也能算出哈希、也能被确认 —— 于是
    「导出并释放」会在什么都没导出的情况下走完全流程,然后把原件删掉。那是
    这一整条链最坏的失败方式。

    **还在写的那一趟不许进包 —— 混进来一趟就整包抛错。** 这个闸门只有这一处:
    ``pick_runs`` 只按 ``started_at`` 圈区间,而 app 那一层不该再有第二份判断
    (闸门有两份,迟早只剩一份是对的)。没有它的话:狗 10:00 出发正在写这一趟,
    10:30 有人导了一个包含今天的区间,包里是当时盘上的 3 张照片和半行
    ``events.jsonl``;客户端重算 sha256 **对得上**(对的是那个半截包本身),
    于是 ``confirm_bundle`` 给这一趟打上"别处还有一份";12:00 狗跑完写下
    40 张照片;次日盘过水位,整个目录被 ``apply_sweep`` 删掉。**37 张照片
    永久消失,而系统全程认为还有一份。**

    用 ``ZIP_DEFLATED``:照片是 JPEG 压不动,但 ``events.jsonl`` /
    ``telemetry.jsonl`` 压得很狠,而这个包要经 WiFi 爬到手机上 —— 线上的
    字节比 Orin 的 CPU 贵。
    """
    infos = tuple(infos)
    if not infos:
        raise ExportError("这个区间里一趟归档都没有 —— 不打空包:"
                          "空包也能算出哈希、也能被确认,"
                          "那等于什么都没导出就走完了导出并释放这条链")
    unsettled = tuple(i for i in infos if not i.settled)
    if unsettled:
        names = "、".join(f"{i.mission}/{i.path.name}" for i in unsettled)
        raise ExportError(f"这一趟还在写,等它落地再导:{names}。现在打出来的是"
                          f"半截包 —— 客户端重算的哈希照样对得上(对的是那个"
                          f"半截包本身),确认之后原件就成了可删的,而剩下那"
                          f"大半趟照片还没写进去,删掉就是真的没了")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"export-{now.strftime(STAMP_FMT)}"
    name = f"{stem}.zip"
    serial = 2
    # 撞名就加序号,跟 ``archive._make_dir`` 一个路数:同一秒导两次不能互相覆盖。
    while (out / name).exists() or (out / f"{name}.json").exists():
        name = f"{stem}-{serial}.zip"
        serial += 1
    target = out / name
    # 同上:临时名这次调用独有。两个人同一秒各导一个区间,固定的 ``.zip.tmp``
    # 会让两条 zip 流写进同一个文件。
    tmp = unique_tmp(target)
    keys: list[str] = []
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for info in infos:
                run = Path(info.path)
                prefix = f"{info.mission}/{run.name}"
                keys.append(prefix)
                for path in sorted(run.rglob("*")):
                    if not path.is_file():
                        continue
                    zf.write(path, f"{prefix}/{path.relative_to(run).as_posix()}")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    bundle = ExportBundle(name=name, path=target, sha256=sha256_file(target),
                          size_bytes=target.stat().st_size, runs=tuple(keys),
                          created_at=now, confirmed=False)
    _write_sidecar(out, bundle)
    return bundle


def read_bundle(out_dir: Path | str, name: str) -> ExportBundle:
    """读一个包的侧写。**读不出来抛错,不回 ``None``** —— 调用方接下来要拿它
    决定删不删东西,"没有"和"读坏了"必须分得开。"""
    name = safe_name(name)
    path = _sidecar(out_dir, name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"{name} 的侧写读不出来: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExportError(f"{name} 的侧写不是映射: {raw!r}")
    try:
        created = datetime.strptime(raw["created_at"], STAMP_FMT)
        return ExportBundle(name=name, path=Path(out_dir) / name,
                            sha256=str(raw["sha256"]),
                            size_bytes=int(raw["size_bytes"]),
                            runs=tuple(str(k) for k in raw["runs"]),
                            created_at=created.replace(tzinfo=timezone.utc),
                            confirmed=bool(raw.get("confirmed")))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExportError(f"{name} 的侧写不完整: {exc}") from exc


def list_bundles(out_dir: Path | str) -> list[ExportBundle]:
    """列出所有导出包,**新的在前**。读不出来的那个跳过,不挡住别的。"""
    root = Path(out_dir)
    if not root.is_dir():
        return []
    out: list[ExportBundle] = []
    for side in sorted(root.glob("*.zip.json"), reverse=True):
        try:
            out.append(read_bundle(root, side.name[: -len(".json")]))
        except ExportError:
            continue
    return out


def confirm_bundle(out_dir: Path | str, name: str, sha256: str, *,
                   runs_root: Path | str) -> ExportBundle:
    """客户端说它拿到了,并且自己重算的哈希是这个。**对上了才放行。**

    对不上说明路上掉了字节。那种时候删掉原件就是真的没了 —— 所以抛错,
    不写确认,不打标记。跟 §4.2「传成功 = 收方重算的哈希对上」同一个规矩。

    对上了就给包里那些 run 打上 ``EXPORTED_REL`` 标记 —— **不是 ``UPLOADED_REL``**。
    那个标记的含义是"传到**服务器**了",而这里发生的事是"拉到**客户手机**上了";
    单机档下两者等价(判据是世界上还有没有第二份),有服务器的那一档下不等价,
    混用会在回传断掉的那几天里删掉服务器上还没有的归档,见 ``retention._deletable``。
    原目录已经不在了的跳过:包已经在客户手里,不能因为原件没了就判确认失败。
    """
    bundle = read_bundle(out_dir, name)
    given = sha256.strip().lower() if isinstance(sha256, str) else ""
    if given != bundle.sha256:
        raise ExportError(f"{bundle.name} 的哈希对不上:客户端报的是 {given!r},"
                          f"包本身是 {bundle.sha256!r} —— 路上掉了字节,不放行")
    if not bundle.confirmed:
        bundle = replace(bundle, confirmed=True)
        _write_sidecar(out_dir, bundle)
    root = Path(runs_root).resolve()
    for key in bundle.runs:
        run = (root / key).resolve()
        # 侧写是我们自己写的,但它在盘上,改得动。越界的不碰。
        if run != root and run.is_relative_to(root) and run.is_dir():
            mark_exported(run)
    return bundle
