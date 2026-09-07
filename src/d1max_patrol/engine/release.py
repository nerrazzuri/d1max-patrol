"""盘上永远两份,靠一条符号链接决定哪份在跑。

**为什么不用包管理器。** 升级这件事真正难的不是装,是**退**。走 apt / pip
那条路,退回上一版要靠一条平时没人走的分支,而平时没人走的分支在需要的那天
一定是坏的。这里换个形状:两份都躺在盘上,``current`` 指哪份哪份就在跑 ——
**升级和回滚是同一个动作,只是方向相反**,所以回滚这条路天天都在被走。

**只留两份。** 第三份没人会用,只会占盘,而盘是本项目里已经紧张的那个资源。

**换链之前先落 ``pending.json``,这条顺序是本模块的枢纽。** 换到一半断电、
新版本崩在启动里、自检没过 —— 三种坏法都靠这个标记收场:开机时守卫看见它,
数够次数就把 ``current`` 指回 ``from``。标记要是落在换链之后,那个窗口里
断一次电,机器就停在一个没人知道该往哪儿退的状态上。

**这个模块不重启任何东西。** 进程改不了自己的命还接着活。``activate`` 只换链
落标记,重启该怎么做由调用方按「有没有上装」决定(见 ``engine/selfcheck.py``
与规格 §7.1)。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from d1max_patrol.engine.export import sha256_file

#: 版本目录都在这儿。
RELEASES_DIR = "releases"
#: 指着当前那一版的符号链接。
CURRENT_LINK = "current"
#: 升级在途的标记。**不在 releases/ 里** —— 它要比任何一版活得久。
PENDING_FILE = "pending.json"
#: 每一版目录里都有一份。
MANIFEST_NAME = "release.json"

#: 盘上留几份。理由见模块开头:第三份只占盘。
KEEP_RELEASES = 2
#: 守卫数到这个数还没等到「自检过了」,就判新版起不来,回滚。
#: 2 而不是 1:第一次开机可能撞上别的偶发(网卡没起来、盘没挂上),给一次机会。
MAX_BOOT_ATTEMPTS = 2

#: 版本目录名的样子:日期 + 一小截内容哈希。**顺带也是防路径穿越的那道闸**。
_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6,12}$")

_CHUNK_JOIN = b"\0"


class ReleaseError(Exception):
    """版本目录/包上出的岔子。文案直接给人看,别再包一层。"""


class GuardAction(str, Enum):
    """引导守卫这一趟干了什么。装机验收和日志都按它读。"""

    #: 没有在途的升级,链也是好的。绝大多数开机走这条。
    OK = "ok"
    #: 有在途的升级,数了一次,让它起。
    COUNTED = "counted"
    #: 数够了还没等到「自检过了」,已经指回上一版。
    ROLLED_BACK = "rolled_back"
    #: 链丢了或者断了,已经按盘上最新的一版修好。
    REPAIRED = "repaired"
    #: 装机那一次就没起来,没有上一版可退。标记清掉,让人来看。
    GAVE_UP = "gave_up"
    #: 盘上一版都没有。这台机器没法起,得重装。
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class Layout:
    """一台机器上这套东西摆在哪。**根目录一路可注入**,测试传 tmp_path。"""

    root: Path

    @property
    def releases(self) -> Path:
        return self.root / RELEASES_DIR

    @property
    def current(self) -> Path:
        return self.root / CURRENT_LINK

    @property
    def pending(self) -> Path:
        return self.root / PENDING_FILE

    def release_dir(self, name: str) -> Path:
        """某一版在哪。名字过闸,所以这里拼出来的路径一定还在 releases 底下。"""
        return self.releases / safe_name(name)


def safe_name(name: str) -> str:
    """版本名合规就原样返回,不合规就炸。

    这道闸同时管两件事:名字得像个版本号,以及**它会被拼进路径**——
    ``../`` 从这儿进去就能写到 ``/opt`` 外面。两件事一道闸,少一个能绕过去的口子。
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ReleaseError(f"版本名不合规: {name!r} —— 要形如 2026-09-20-77b2de")
    return name


def tree_sha256(root: Path | str) -> str:
    """整棵树的指纹:按相对路径排序,逐条喂「路径 + 这个文件的 sha256」。

    **路径要进指纹。** 只把内容首尾相接算一遍的话,改个文件名、把 a 的内容
    挪进 b,指纹一点不变 —— 那种改动就成了隐形的。

    ``release.json`` 自己不算 —— 它里头存着这个值,算自己是个死循环。

    排序按**相对路径的 posix 串**,不按 ``Path`` 对象:后者在 Windows 上按
    ``parts`` 比,和 Linux 上的结果不保证一样,而两边算出不同指纹的那天,
    整套对账就废了。
    """
    root = Path(root)
    digest = hashlib.sha256()
    files = sorted((p.relative_to(root).as_posix(), p)
                   for p in root.rglob("*") if p.is_file())
    for rel, path in files:
        if rel == MANIFEST_NAME:
            continue
        digest.update(rel.encode())
        digest.update(_CHUNK_JOIN)
        digest.update(sha256_file(path).encode())
        digest.update(_CHUNK_JOIN)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """一版的自述。"""

    name: str
    version: str
    content_sha256: str
    #: 这一版能读的任务包 schema 的最低要求。比盘上的包高就不许升(§7.2)。
    requires_mission_schema: int
    built_at: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "content_sha256": self.content_sha256,
            "requires_mission_schema": self.requires_mission_schema,
            "built_at": self.built_at,
        }


def read_manifest(where: Path | str) -> ReleaseManifest:
    """读一个目录里的 ``release.json``。**只读,不校验内容哈希。**"""
    path = Path(where) / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"读不到 {MANIFEST_NAME}: {path}") from exc
    except ValueError as exc:
        raise ReleaseError(f"{MANIFEST_NAME} 不是合法 JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ReleaseError(f"{MANIFEST_NAME} 要是个对象: {path}")
    try:
        schema = int(raw.get("requires_mission_schema", 1))
    except (TypeError, ValueError) as exc:
        raise ReleaseError("requires_mission_schema 要是整数") from exc
    return ReleaseManifest(
        name=str(raw.get("name", "")),
        version=str(raw.get("version", "")),
        content_sha256=str(raw.get("content_sha256", "")),
        requires_mission_schema=schema,
        built_at=str(raw.get("built_at", "")),
    )


def verify_package(where: Path | str) -> ReleaseManifest:
    """一个包能不能落槽。**过不了这关的包不许碰 releases/。**

    三样:自述读得出、名字跟目录名对得上、整棵树的指纹跟自述里写的一致。
    第二条容易被当成多余 —— 它挡的是「拷贝的时候把目录改了个名」这种事,
    改完之后 ``current`` 指过去还能跑,但盘上那一版叫什么再也说不清了。
    """
    where = Path(where)
    manifest = read_manifest(where)
    safe_name(manifest.name)
    if manifest.name != where.name:
        raise ReleaseError(f"{MANIFEST_NAME} 里写的是 {manifest.name},"
                           f"目录名却是 {where.name}")
    got = tree_sha256(where)
    if got != manifest.content_sha256:
        raise ReleaseError(f"包的哈希对不上: 自述写 {manifest.content_sha256},"
                           f"算出来是 {got}")
    return manifest


def installed(layout: Layout) -> tuple[str, ...]:
    """盘上有哪几版,排过序。名字不合规的目录当不存在。"""
    try:
        entries = sorted(p.name for p in layout.releases.iterdir() if p.is_dir())
    except OSError:
        return ()
    return tuple(n for n in entries if _NAME_RE.match(n))


def current_name(layout: Layout) -> str:
    """现在跑的是哪一版。没有链、或者链指向的目录没了,都照样回得出名字。

    **断着的链也要回出名字** —— 守卫就是靠它知道该把链修成什么。所以这里读
    ``os.readlink`` 而不是 ``resolve()``:后者在链断掉时给不出可用的信息。
    Windows 上 ``readlink`` 回的是 ``\\\\?\\D:\\...`` 那种带前缀的串,
    所以取名字要走 ``Path(...).name``,不许切字符串。
    """
    try:
        target = os.readlink(layout.current)
    except OSError:
        return ""
    return Path(target).name


@dataclass(frozen=True, slots=True)
class Pending:
    """一次在途的升级。**换链之前就落盘**,理由见模块开头。"""

    #: 换到哪一版。
    to: str
    #: 从哪一版换过来的。空串 = 装机第一次,没有可退的上一版。
    #: 字段不叫 ``from`` 是因为那是关键字;线上的键仍然叫 ``from``。
    src: str
    #: 开过几次机了。守卫每次开机加一,数到 MAX_BOOT_ATTEMPTS 就回滚。
    attempts: int
    at_ms: int
    #: 这次是自动升的还是人点的。有上装的机器不许自动升(§7.1),留痕给人看。
    auto: bool
    #: 换链那一刻这台机器的 SN。重启后自检拿它跟现在的 SN 比(§7.2 第四项)。
    #: **必须在这儿记下来**:自检那会儿唯一能问的是"现在是谁",没有别的地方
    #: 存着"升级前是谁"。空串 = 没记(命令行装机可以不记),这一项就自动通过。
    sn: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"to": self.to, "from": self.src, "attempts": self.attempts,
                "at_ms": self.at_ms, "auto": self.auto, "sn": self.sn}

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Pending:
        return cls(to=str(raw.get("to", "")), src=str(raw.get("from", "")),
                   attempts=int(raw.get("attempts", 0)),
                   at_ms=int(raw.get("at_ms", 0)),
                   auto=bool(raw.get("auto", False)),
                   sn=str(raw.get("sn", "")))


def read_pending(layout: Layout) -> Pending | None:
    """读在途标记。**坏了当没有。**

    守卫开机时第一个读它。半个 JSON 让守卫炸掉的话,狗就起不来了 —— 而
    「起不来」正是这个标记本来要防的事。所以这里对任何读不动的情况都回 None:
    最坏的后果是少回滚一次,人还能上手；炸掉的后果是没有人能上手。
    """
    try:
        raw = json.loads(layout.pending.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("to"):
        return None
    try:
        return Pending.from_wire(raw)
    except (TypeError, ValueError):
        return None


def write_pending(layout: Layout, pending: Pending) -> None:
    """落在途标记。**先写临时文件再改名**,不留半个 JSON 在盘上。"""
    layout.root.mkdir(parents=True, exist_ok=True)
    tmp = layout.pending.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pending.to_wire(), fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, layout.pending)


def clear_pending(layout: Layout) -> None:
    """升级坐实了(或者已经退回去了),标记就该没了。没有也不算错。"""
    try:
        layout.pending.unlink()
    except OSError:
        pass
