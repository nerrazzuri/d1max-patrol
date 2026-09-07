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
import shutil
from dataclasses import dataclass, replace
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


def tree_sha256(root: Path | str, *, skip: str = MANIFEST_NAME) -> str:
    """整棵树的指纹:按相对路径排序,逐条喂「路径 + 这个文件的 sha256」。

    **路径要进指纹。** 只把内容首尾相接算一遍的话,改个文件名、把 a 的内容
    挪进 b,指纹一点不变 —— 那种改动就成了隐形的。

    ``skip`` 那个文件自己不算 —— 它里头存着这个值,算自己是个死循环。默认是
    ``release.json``;任务包传的是 ``bundle.yaml``(§3.2 的整包 content_hash
    跟 §7.3 是同一套算法,**不该有第二个真理源**)。

    排序按**相对路径的 posix 串**,不按 ``Path`` 对象:后者在 Windows 上按
    ``parts`` 比,和 Linux 上的结果不保证一样,而两边算出不同指纹的那天,
    整套对账就废了。
    """
    root = Path(root)
    digest = hashlib.sha256()
    files = sorted((p.relative_to(root).as_posix(), p)
                   for p in root.rglob("*") if p.is_file())
    for rel, path in files:
        if rel == skip:
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


def stage(layout: Layout, package: Path | str, *, now_ms: int) -> ReleaseManifest:
    """把一个包落进 ``releases/<name>/``。**校验不过就一个字节都不写。**

    先拷到 ``<name>.staging``、拷完再改名 —— 半个版本目录比没有更坏,它看着
    像装上了,而 ``installed()`` 也确实会把它列出来。

    同一个包落两遍是幂等的:装机脚本要可重放(§7.9),而「已经装过了」不是错。
    """
    package = Path(package)
    manifest = verify_package(package)
    dest = layout.release_dir(manifest.name)
    if dest.is_dir():
        # 已经落过了。这里不去比对盘上那份的哈希:那是 selfcheck 的活,
        # 而且落槽是个热路径 —— 装机脚本每跑一次就重算一遍整棵树不划算。
        return manifest
    layout.releases.mkdir(parents=True, exist_ok=True)
    staging = layout.releases / (manifest.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(package, staging)
        os.replace(staging, dest)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def point_link(link: Path, target: Path) -> None:
    """把 ``link`` 这条符号链接指到 ``target`` 上。

    **先做一条临时链,再一次替换过去。** 直接「删了再建」的话,中间有一个
    ``link`` 不存在的窗口,守卫正好在那一瞬间开机,看到的就是一台没有
    current 的机器。``os.replace`` 在 Linux 上一步换完,没有这个窗口。

    Windows 开发机上替换目录符号链接会 ``WinError 5``(已实测),退回两步走。
    那条退路上的窗口由调用方的标记文件兜着 —— 标记是换链之前写的。

    版本目录(§7.3)和任务包(§3.2)用的是同一段。**两处各写一遍的话,
    哪天只改对了一处,另一处会在那个窗口上出事,而且不会有测试红。**

    **两个调用方各自的标记**(上面那句话不是白说的,评审 F2 就是它没被兑现):

    * 版本目录:``activate`` 换链前写 ``pending.json``(``Pending(to, src)``),
      ``boot_guard`` 照它修。这边**只换一条链**。
    * 任务包:``apply_bundle`` 换链前写 ``landed.json`` 里的 ``applying``
      (``bundle.Applying(to, src, prev)``),``guard_bundle`` 照它修。这边
      **连着换两条链**(先 ``previous`` 后 ``current``),所以那个标记比
      ``Pending`` 多记一条链、能修的中间态也多一个 —— 两次调用之间断电,
      是 release 侧根本不存在的局面。
    """
    tmp = link.parent / (link.name + ".new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target, target_is_directory=True)
    try:
        os.replace(tmp, link)
    except OSError:
        tmp.unlink()
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target, target_is_directory=True)


def _point_current(layout: Layout, name: str) -> None:
    """把 ``current`` 指到 ``name`` 上。名字过闸,再交给 ``point_link``。"""
    point_link(layout.current, layout.releases / safe_name(name))


def activate(layout: Layout, name: str, *, now_ms: int,
             auto: bool = False, sn: str = "") -> Pending:
    """切到某一版。**先落标记,再换链** —— 这条顺序是本模块的枢纽。

    换完不算数,要等 ``commit()``。中间这段时间里机器要是重启了,守卫看见标记
    就开始数;数够 ``MAX_BOOT_ATTEMPTS`` 还没等到 commit,它就判新版起不来。
    """
    name = safe_name(name)
    dest = layout.release_dir(name)
    if not (dest / MANIFEST_NAME).is_file():
        raise ReleaseError(f"{name} 没装在盘上 —— 先落槽再切")
    src = current_name(layout)
    if src == name:
        raise ReleaseError(f"{name} 已经是在跑的那一版了")
    pending = Pending(to=name, src=src, attempts=0, at_ms=now_ms,
                      auto=auto, sn=sn)
    write_pending(layout, pending)
    _point_current(layout, name)
    return pending


def commit(layout: Layout) -> tuple[str, ...]:
    """这一版坐实了:清标记,顺手把多余的版本删掉。返回删掉了哪几版。"""
    clear_pending(layout)
    return prune(layout)


def rollback(layout: Layout, *, now_ms: int) -> str:
    """退回上一版。**跟 activate 用同一个 ``_point_current``**,方向相反而已。

    扳机只有一个(§7.3):重启后自检没过,或者守卫数够了次数。所以这里要求
    在途标记必须在 —— 没有它就不知道该退到哪儿,而「猜一个」比不退更坏。
    """
    pending = read_pending(layout)
    if pending is None:
        raise ReleaseError("没有在途的升级 —— 没有该退回哪儿这回事")
    if not pending.src:
        raise ReleaseError("装机那一次没有上一版可退 —— 这台机器要人来看")
    _point_current(layout, pending.src)
    clear_pending(layout)
    return pending.src


def prune(layout: Layout, *, keep: int = KEEP_RELEASES) -> tuple[str, ...]:
    """盘上只留 ``keep`` 份,按名字从旧到新删。返回删掉了哪几版。

    **在跑的那版和退路那版绝不删。** 名字是日期打头的,大多数时候最旧的那份
    确实该删 —— 但「大多数时候」在这儿不够:回滚之后在跑的正是较旧的那一版,
    照名字删就把脚下的地板抽了。
    """
    pending = read_pending(layout)
    protected = {current_name(layout)}
    if pending is not None:
        protected.update({pending.to, pending.src})
    protected.discard("")

    names = list(installed(layout))
    dropped: list[str] = []
    for name in names:
        if len(names) - len(dropped) <= keep:
            break
        if name in protected:
            continue
        shutil.rmtree(layout.releases / name, ignore_errors=True)
        dropped.append(name)
    return tuple(dropped)


def _healthy(layout: Layout) -> bool:
    """``current`` 指着一个真的、装着 ``release.json`` 的目录吗。"""
    name = current_name(layout)
    if not name:
        return False
    return (layout.releases / name / MANIFEST_NAME).is_file()


def boot_guard(layout: Layout, *, now_ms: int) -> GuardAction:
    """开机时第一个跑的东西。**它是唯一与版本无关的一段代码。**

    装在 ``<root>/bin/`` 下、由 systemd 的 ``ExecStartPre`` 调,所以它跑在
    「那一版有没有毛病」之前。装在版本目录里就没意义了 —— 坏掉的那一版里的
    守卫,正是最不该被信任的那一份。

    **``ExecStartPre`` 会在每一次服务启动时跑,不只是开机那一次** ——
    ``Restart=`` 的每一次重试也算。这一条是有意的,而且是这一层的全部意义:
    不带上装的机器(交付的默认形态)升级走的是 ``systemctl restart``
    (见 ``selfcheck.restart_plan``),根本不开机;守卫挂在开机上的话
    ``attempts`` 永远停在 0,新版坏到起不来时这一层压根不会数。挂在
    ``ExecStartPre`` 上,一个起来就崩的版本会在两次 ``RestartSec`` 之内
    数满 ``MAX_BOOT_ATTEMPTS`` 被退回上一版。
    **反过来也成立:守卫不能同时挂在开机上**,那样真开机时它会被数两次,
    第二次开机就把一版本来健康的退掉。

    它只回答一个问题:**这台机器现在该跑哪一版。** 自检过没过不归它管
    (那是 ``engine/selfcheck.py``),它只看得见「有没有人来 commit 过」。
    数够 ``MAX_BOOT_ATTEMPTS`` 次还没人 commit,就当新版起不来。

    **任何一条路都不许抛异常。** 这段代码炸掉等于狗起不来,而起不来正是它
    本来要防的事;所以最坏的结论也是一个 ``GuardAction``,交给调用方去喊。
    """
    try:
        return _boot_guard(layout, now_ms=now_ms)
    except (OSError, ReleaseError):
        # 盘满、只读挂载、权限拒绝、坏道,或者 pending.json 被写成了不合规的
        # 名字 —— 不管哪一种,盘上状态都已经没法确定,只能让人来看。
        return GuardAction.BROKEN


def _boot_guard(layout: Layout, *, now_ms: int) -> GuardAction:
    """``boot_guard`` 的实际逻辑。可能抛 ``OSError``/``ReleaseError``,
    由 ``boot_guard`` 兜底。"""
    pending = read_pending(layout)
    if pending is None:
        if _healthy(layout):
            return GuardAction.OK
        names = installed(layout)
        if not names:
            return GuardAction.BROKEN
        _point_current(layout, names[-1])
        return GuardAction.REPAIRED

    if pending.attempts >= MAX_BOOT_ATTEMPTS:
        if not pending.src:
            # 装机那一次就没起来。没有上一版可退,再数下去也数不出结果 ——
            # 每次开机数一遍、每次都数到这儿,而没有任何一次会有别的结论。
            # 清掉标记,让人来看:这台机器要重装,不是要回滚。
            clear_pending(layout)
            return GuardAction.GAVE_UP
        _point_current(layout, pending.src)
        clear_pending(layout)
        return GuardAction.ROLLED_BACK

    # 还有机会。顺手把链修一下 —— 换链换了一半的话,链现在还指着旧版
    # (或者根本没有),而标记说的是「该跑 to 那一版」。**照标记修,不照
    # 「盘上最新的」修**:两者在正常情况下是同一版,不同的那天正是出事那天。
    if current_name(layout) != pending.to or not _healthy(layout):
        if (layout.releases / pending.to / MANIFEST_NAME).is_file():
            _point_current(layout, pending.to)
    write_pending(layout, replace(pending, attempts=pending.attempts + 1))
    return GuardAction.COUNTED
