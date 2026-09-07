"""任务包:狗断网 72 小时还能照常上岗所需要的全部东西(§3.1)。

**包是纯数据。** 里头没有一行可执行的东西 —— 没有固件,没有代码,没有脚本。
理由是这条链路的三个性质凑到了一起:包走网络、包可能被改、包自动生效不经
人确认。三个里去掉任何一个,这条界都可以松;三个都在,它就是硬的。

不进包的还有 VLM 权重和 API key(§3.1)。

模块分层:``bundle.py`` 用 ``schedule.py``,反过来不行。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .mission import MissionError, parse_mission
from .release import point_link, tree_sha256
from .schedule import Schedule, ScheduleError, parse_schedule

#: 包的自述文件名。整包哈希不算它自己 —— 它里头存着那个值。
BUNDLE_MANIFEST = "bundle.yaml"

#: 本版打出来的包写的 schema。§7.2 的 ``requires_mission_schema`` 跟它对账。
BUNDLE_SCHEMA = 1

MIN_VERSION = 1
#: 上界拦的是时间戳。§3.2 要的是**单调递增的整数**,不是时间戳:两台机器的
#: 钟不一样,而「哪一版更新」必须是确定的。
MAX_VERSION = 999_999

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_MANIFEST_KEYS = frozenset({"bundle_id", "version", "schema",
                           "content_sha256", "built_at", "built_by",
                           "targets"})

#: 一条 SN 最长多少。挡的不是攻击,是「有人把整段日志粘进 targets」。
MAX_SN_LEN = 64

#: 排程在包里的文件名。
SCHEDULE_NAME = "schedule.yaml"


class BundleError(ValueError):
    """包不合规。**解析、校验、落盘全用这一个。**"""


def _整数(v: Any) -> bool:
    """``isinstance(True, int)`` 是真的,所以这里比的是类型本身。
    YAML 里 ``version: yes`` 会解成 ``True``,不拦就成了第 1 版。
    """
    return type(v) is int


def _aware(s: str, field: str = "built_at") -> datetime:
    """解一个**必须带时区**的 ISO8601 时刻。

    裸时刻会被按读它那台机器的时区解释,而包是从服务器发到狗上的。
    ``field`` 只用来把报错说准 —— 调这个函数的字段不总叫 ``built_at``。
    """
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError) as e:
        raise BundleError(f"{field} 解不出来: {s!r}") from e
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise BundleError(f"{field} 必须带时区: {s!r}")
    return dt


@dataclass(frozen=True, slots=True)
class BundleManifest:
    """一个包的自述:它是谁,是第几版,内容是什么指纹。"""

    bundle_id: str
    version: int
    schema: int
    content_sha256: str
    built_at: str
    built_by: str = ""
    #: §3.1 的「目标 SN」。**空元组是「不限」**(定夺 13)。
    targets: tuple[str, ...] = ()

    @property
    def slot_name(self) -> str:
        """落盘的目录名:``bundles/<slot_name>/``(定夺 4)。"""
        return f"{self.bundle_id}-{self.version}"

    def accepts(self, sn: str) -> bool:
        """这份包认不认这台机器。**逐字相等,不做归一化**(定夺 13)。"""
        return not self.targets or sn in self.targets

    def to_wire(self) -> dict[str, Any]:
        return {"bundle_id": self.bundle_id, "version": self.version,
                "schema": self.schema, "content_sha256": self.content_sha256,
                "built_at": self.built_at, "built_by": self.built_by,
                "targets": list(self.targets)}


def parse_manifest(raw: Mapping[str, Any]) -> BundleManifest:
    """解一份 ``bundle.yaml``。不合规就炸,不猜。"""
    if not isinstance(raw, Mapping):
        raise BundleError(f"bundle.yaml 得是个字典,拿到的是 {type(raw).__name__}")

    多的 = set(raw) - _MANIFEST_KEYS
    if 多的:
        raise BundleError(
            f"bundle.yaml 里有不认识的键: {sorted(多的)} —— 包是从网上来的,"
            "多一个没人认识的键要么是版本对不上,要么是有人在试探")

    for 键 in ("bundle_id", "version", "schema", "content_sha256", "built_at"):
        if 键 not in raw:
            raise BundleError(f"bundle.yaml 缺 {键}")

    bid = raw["bundle_id"]
    if not isinstance(bid, str) or not _ID_RE.match(bid):
        raise BundleError(
            f"bundle_id 不合规: {bid!r} —— 要 2-48 个小写字母/数字/连字符。"
            "它会被拼进路径,所以这道闸同时挡着 ../")

    ver = raw["version"]
    if not _整数(ver) or ver < MIN_VERSION:
        raise BundleError(f"version 得是 >= {MIN_VERSION} 的整数,拿到 {ver!r}")
    if ver > MAX_VERSION:
        raise BundleError(
            f"version {ver} 超过上界 {MAX_VERSION} —— 看着像时间戳。§3.2 要的是"
            "单调递增的整数:两台机器的钟不一样,而哪一版更新必须是确定的")

    sch = raw["schema"]
    if not _整数(sch) or sch < 1:
        raise BundleError(f"schema 得是 >= 1 的整数,拿到 {sch!r}")

    sha = raw["content_sha256"]
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise BundleError(f"content_sha256 得是 64 位小写十六进制,拿到 {sha!r}")

    built_at = raw["built_at"]
    if not isinstance(built_at, str):
        raise BundleError(f"built_at 得是字符串,拿到 {built_at!r}")
    _aware(built_at)

    return BundleManifest(bid, ver, sch, sha, built_at,
                          str(raw.get("built_by") or ""),
                          _targets(raw.get("targets")))


def _targets(raw: Any) -> tuple[str, ...]:
    """解 §3.1 的「目标 SN」。**缺省是空元组,意思是不限**(定夺 13)。"""
    if raw is None:
        return ()
    # 字符串本身是可迭代的 —— 不特判的话 "D1M-1" 会被拆成五个字符,而且
    # 每个都「像」一条合法 SN,于是一份写错的包会静静地生效。
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise BundleError(f"targets 得是个列表,拿到 {raw!r}")
    out: list[str] = []
    for one in raw:
        if not isinstance(one, str):
            raise BundleError(f"targets 里得全是字符串,拿到 {one!r}")
        if not one or one != one.strip() or "\x00" in one:
            raise BundleError(
                f"targets 里这一条不像个 SN: {one!r} —— 不许空、不许带首尾空白、"
                "不许带 NUL。狗那头的 SN 是 clean_sn() 收拾过的,两边得对得上")
        if len(one) > MAX_SN_LEN:
            raise BundleError(f"targets 里这一条超过 {MAX_SN_LEN} 个字符: {one[:20]!r}...")
        if one in out:
            raise BundleError(f"targets 里 {one!r} 出现了两次 —— 这份包像是拼出来的")
        out.append(one)
    return tuple(out)


def dump_manifest(m: BundleManifest) -> str:
    """把自述写成 YAML 文本。键排序,因为这份文本会进人的眼睛也进 git。"""
    return yaml.safe_dump(m.to_wire(), allow_unicode=True, sort_keys=True)


def read_manifest(bundle_dir: Path | str) -> BundleManifest:
    """从包目录里读自述。"""
    p = Path(bundle_dir) / BUNDLE_MANIFEST
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise BundleError(f"读不到 {BUNDLE_MANIFEST}: {p}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise BundleError(f"{BUNDLE_MANIFEST} 不是合法 YAML: {p}") from e
    return parse_manifest(raw)


def write_manifest(bundle_dir: Path | str, m: BundleManifest) -> None:
    """把自述写进包目录。"""
    (Path(bundle_dir) / BUNDLE_MANIFEST).write_text(
        dump_manifest(m), encoding="utf-8")


def read_bundle_schedule(bundle_dir: Path | str) -> Schedule:
    """读包里那份排程。**坏了一律抛 ``BundleError``。**

    yaml 的异常和 ``ScheduleError`` 都在这儿被翻一次。调用方是 HTTP 层,它
    要把「包里的排程有问题」变成一个说得清的 409;漏上去就只能给 500,而
    现场的人看到 500 只能猜 —— 这台狗此刻在一个没有网的地方。
    """
    p = Path(bundle_dir) / SCHEDULE_NAME
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BundleError(f"{SCHEDULE_NAME} 读不出来: {exc}") from exc
    try:
        return parse_schedule(raw)
    except ScheduleError as exc:
        raise BundleError(f"{SCHEDULE_NAME} 不合规: {exc}") from exc


# --------------------------------------------------- 「纯数据」这条界的四道闸

#: 包里**只**认这些后缀。走白名单不走黑名单:黑名单漏 ``.pyc``、``.bin``、
#: 没后缀的那些,而「包里该有什么」是个封闭的集合。
ALLOWED_SUFFIXES = frozenset({
    ".yaml", ".yml", ".json", ".md", ".txt", ".csv", ".pgm", ".png", ".jpg"})

_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_SHEBANG = b"#!"

#: 报上去的分类。值守屏按它分类,**改一个字就是破坏兼容**。
GATE_NAMES = ("suffix", "symlink", "exec_bit", "shebang")


@dataclass(frozen=True, slots=True)
class Violation:
    """哪道闸、哪个文件、为什么。"""

    gate: str
    path: str          # 相对包根的 posix 路径
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {"gate": self.gate, "path": self.path, "detail": self.detail}


def _走一遍(root: Path):
    """走整棵树,**不跟符号链接进目录**。

    ``a -> ..`` 是个环,跟着走会挂死 —— 而这个扫描正是那个本该**抓住**
    符号链接的东西。产出 ``(相对posix路径, 绝对路径, 是不是链接)``。
    """
    for 上, 目录们, 文件们 in os.walk(root, followlinks=False):
        base = Path(上)
        for 名 in sorted(目录们) + sorted(文件们):
            p = base / 名
            yield p.relative_to(root).as_posix(), p, p.is_symlink()


def _gate_suffix(条目) -> list[Violation]:
    """第 1 道:后缀白名单。目录不参与。"""
    out = []
    for rel, p, 是链接 in 条目:
        if 是链接 or p.is_dir():
            continue
        if p.suffix.lower() not in ALLOWED_SUFFIXES:
            out.append(Violation("suffix", rel,
                                 f"后缀 {p.suffix or '(没有)'} 不在白名单里"))
    return out


def _gate_symlink(条目) -> list[Violation]:
    """第 2 道:符号链接,一个都不许有。

    **内部的也拒。** 「只拦指向包外的」要先解析路径,而解析路径本身是能被
    绕的(``a/../../etc``、中途再套一层链接)。一律拒才是条清楚的界。
    """
    return [Violation("symlink", rel, "包里不许有符号链接")
            for rel, _p, 是链接 in 条目 if 是链接]


def _gate_exec_bit(条目, mode_of) -> list[Violation]:
    """第 3 道:可执行位。

    **这道闸在 Windows 上是空转的** —— Windows 的 ``os.stat`` 给普通文件的
    mode 里根本没有可执行位。所以它必须在真机上被验一次(见
    ``docs/真机待验证清单.md``),不许假装两边一样有效。
    """
    out = []
    for rel, p, 是链接 in 条目:
        if 是链接 or p.is_dir():
            continue
        if mode_of(p) & _EXEC_BITS:
            out.append(Violation("exec_bit", rel, "带着可执行位"))
    return out


def _gate_shebang(条目) -> list[Violation]:
    """第 4 道:shebang。抓的是「改成 .txt 的脚本」—— 白名单放行了后缀,
    这道拦住内容。只看**头两个字节**:正文里出现 ``#!`` 是正常的。
    """
    out = []
    for rel, p, 是链接 in 条目:
        if 是链接 or p.is_dir():
            continue
        with p.open("rb") as f:
            if f.read(2) == _SHEBANG:
                out.append(Violation("shebang", rel, "头两个字节是 #!"))
    return out


def scan_pure_data(root: Path | str, *,
                   mode_of: Callable[[Path], int] | None = None
                   ) -> tuple[Violation, ...]:
    """四道闸全走一遍,**把问题一次收齐**。

    不短路是故意的:写包的人改一个跑一次、改一个跑一次,是最容易让人干脆
    绕过校验的那种体验。

    ``mode_of`` 是取文件 mode 的那一步,默认 ``os.stat``。注它进来是为了让
    第 3 道的**判据**在 Windows 上也能测(跟 §8.5 「时间必须可注入」同理)。
    """
    root = Path(root)
    条目 = list(_走一遍(root))
    取mode = mode_of or (lambda p: os.stat(p).st_mode)
    出 = (_gate_suffix(条目) + _gate_symlink(条目)
          + _gate_exec_bit(条目, 取mode) + _gate_shebang(条目))
    return tuple(出)


def verify_pure_data(root: Path | str, *,
                     mode_of: Callable[[Path], int] | None = None) -> None:
    """过不了就炸,报错里带上前几条具体是哪个文件。"""
    vs = scan_pure_data(root, mode_of=mode_of)
    if not vs:
        return
    详 = "; ".join(f"{v.path}({v.gate}: {v.detail})" for v in vs[:5])
    更多 = f" 等共 {len(vs)} 条" if len(vs) > 5 else ""
    raise BundleError(f"这个包里有不是纯数据的东西: {详}{更多}")


# ------------------------------------------------------------ 打包与校验

#: 任务放这儿。``mission_schema_floor`` 扫的就是这个目录(§7.4)。
MISSIONS_DIR = "missions"


def bundle_sha256(bundle_dir: Path | str) -> str:
    """整包指纹。**跟 §7.3 的发布包是同一套算法** —— 只是跳过的自述文件
    换成了 ``bundle.yaml``。两套算法就是两个真理源(定夺 3)。
    """
    return tree_sha256(bundle_dir, skip=BUNDLE_MANIFEST)


def _盖章(bundle_dir: Path, schema: int) -> None:
    """把每个任务重写成规范形式,并盖上 ``schema``。

    **盖章是打包器的活。** 一个手写的任务不该因为漏了一个版本号就把整台狗
    的升级判据(§7.2 ``requires_mission_schema``)搞乱。顺带把任务过一遍
    ``parse_mission`` —— 打包的时候发现坏任务,比半夜出发之后发现要好。

    ``sort_keys=True`` 是为了可复现:同一棵源树打两遍必须是同一个指纹。
    """
    d = bundle_dir / MISSIONS_DIR
    if not d.is_dir():
        return
    for p in sorted(d.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            m = parse_mission(raw)
        except (json.JSONDecodeError, MissionError, ValueError, OSError) as e:
            raise BundleError(f"任务 {p.name} 不合规: {e}") from e
        data = m.to_wire()
        data["schema"] = schema
        p.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                indent=2) + "\n", encoding="utf-8")


def build_bundle(src: Path | str, dest_root: Path | str, *,
                 bundle_id: str, version: int, built_at: str,
                 schema: int = BUNDLE_SCHEMA, built_by: str = "",
                 targets: Sequence[str] = ()) -> Path:
    """把一棵源树打成一个任务包,返回包目录。

    §3.8:**这是个库,不是服务器的一个功能。** 编包、校验、算哈希这三件事,
    狗上和服务器上跑的是同一段代码;服务器独占的只有规模。所以这儿一行
    HTTP 都没有。
    """
    src, dest_root = Path(src), Path(dest_root)
    # 先把版本号和 id 过一遍解析那道闸 —— 参数错了不该等到写自述才发现。
    临 = parse_manifest({"bundle_id": bundle_id, "version": version,
                         "schema": schema, "content_sha256": "0" * 64,
                         "built_at": built_at, "built_by": built_by,
                         "targets": list(targets)})
    dest = dest_root / 临.slot_name
    if dest.exists():
        raise BundleError(f"{临.slot_name} 已经在 {dest_root} 底下了 —— "
                          "版本号要单调递增,不许覆盖")

    # verify_pure_data 对一个不存在的目录会静静地扫出零条违规(os.walk 对
    # 不存在的路径就是空迭代)—— 那不是「干净」,是「没查」。自己先拦一道。
    if not src.is_dir():
        raise BundleError(f"源目录不存在: {src}")

    # **拷之前先拦。** 拷完再拦的话,那个东西已经在盘上了。
    verify_pure_data(src)
    dest_root.mkdir(parents=True, exist_ok=True)
    # symlinks=True:不解引用。上一行已经拒了所有链接,这里是第二层。
    shutil.copytree(src, dest, symlinks=True)
    try:
        _盖章(dest, schema)
        verify_pure_data(dest)          # 盖完再看一遍,拷/写这两步也得干净
        m = BundleManifest(临.bundle_id, 临.version, schema,
                           bundle_sha256(dest), built_at, 临.built_by,
                           临.targets)
        write_manifest(dest, m)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)   # 半个包比没有包更糟
        raise
    return dest


def verify_bundle(bundle_dir: Path | str) -> BundleManifest:
    """收包这一头:这个目录里的东西,是不是那个包。

    **三道的顺序是有理由的:**

    1. 目录名对不对 —— 人看得懂的那条。先报哈希只会让人去查一个不是根因
       的东西(改过名的包内容也一定对不上)。
    2. 是不是纯数据 —— 一个 ``maps/x.pgm -> /etc/shadow`` 的包,算哈希那步
       会去**读**那个链接。先查这一道,那一步就永远不会发生。
    3. 内容指纹。

    ``bundle_dir`` 不存在或者是空目录的情形,靠 ``read_manifest`` 打头一道
    (读不到 ``bundle.yaml`` 就直接 ``BundleError``)—— 这一步永远先于任何
    ``verify_pure_data`` 调用,所以「目录不存在」不会被后者悄悄放行。
    """
    bundle_dir = Path(bundle_dir)
    m = read_manifest(bundle_dir)
    if bundle_dir.name != m.slot_name:
        raise BundleError(f"目录名 {bundle_dir.name} 跟自述里的 "
                          f"{m.slot_name} 对不上")
    verify_pure_data(bundle_dir)
    got = bundle_sha256(bundle_dir)
    if got != m.content_sha256:
        raise BundleError(f"content_sha256 对不上: 自述说 {m.content_sha256}, "
                          f"实际是 {got}")
    return m


# -------------------------------------------------------- 落盘、两份、回退

CURRENT_LINK = "current"
PREVIOUS_LINK = "previous"
#: 链存不下的那点东西:证没证过、退过哪些。**「在跑哪一版」不在这儿。**
LANDED = "landed.json"

#: 槽名长什么样:``<bundle_id>-<version>``。**槽名会被拼进路径。**
_SLOT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}-[0-9]{1,6}$")


@dataclass(frozen=True, slots=True)
class Rollback:
    """退过一次。``frm`` 而不是 ``from``,后者是关键字。"""

    at: str
    frm: str
    to: str
    reason: str

    def to_wire(self) -> dict[str, Any]:
        return {"at": self.at, "from": self.frm, "to": self.to,
                "reason": self.reason}


@dataclass(frozen=True, slots=True)
class BundleState:
    """盘上现在是什么局面。"""

    current: str
    previous: str
    #: ``current`` 这一版有没有真跑成过一次。
    proven: bool
    #: 退过哪些。**只增。** 清理磁盘不清它。
    rollbacks: tuple[Rollback, ...]

    def to_wire(self) -> dict[str, Any]:
        return {"current": self.current, "previous": self.previous,
                "proven": self.proven,
                "rollbacks": [r.to_wire() for r in self.rollbacks]}


def _链指向(root: Path, 名: str) -> str:
    """一条链指着的槽名。没有这条链就是空串。"""
    link = root / 名
    if not link.is_symlink():
        return ""
    return Path(os.readlink(link)).name


def _读记(root: Path) -> dict[str, Any]:
    """读 ``landed.json``。**读不出来就当空的,不炸。**

    链才是「在跑哪一版」的唯一真理源。一个坏掉的 json 不该让狗连自己
    在跑什么都说不出来。
    """
    try:
        raw = json.loads((root / LANDED).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _写记(root: Path, 记: dict[str, Any]) -> None:
    (root / LANDED).write_text(
        json.dumps(记, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")


def read_state(bundles_root: Path | str) -> BundleState:
    """盘上现在是什么局面。"""
    root = Path(bundles_root)
    cur = _链指向(root, CURRENT_LINK)
    记 = _读记(root)
    退 = tuple(Rollback(str(r.get("at", "")), str(r.get("from", "")),
                        str(r.get("to", "")), str(r.get("reason", "")))
               for r in 记.get("rollbacks", [])
               if isinstance(r, dict))
    # proven 记的是**哪一个槽**被证过。存布尔的话,有人手工把 current 挪到
    # 另一版上,那一版就凭空继承了「跑成过」这个结论 —— 它一次都没跑过。
    return BundleState(cur, _链指向(root, PREVIOUS_LINK),
                       bool(cur) and 记.get("proven_slot") == cur, 退)


def active_bundle(bundles_root: Path | str) -> Path | None:
    """``current`` 指着的那个包目录。没有就 ``None``。

    链在但指向的目录不在了(比如整棵 ``bundles_root`` 被搬走过,搬的时候
    没跟着挪这条绝对链),就地是「悬空」——不能悄悄当 ``None`` 处理,
    也不能把一个不存在的路径交给调用方去踩:跟 ``verify_pure_data`` 曾经
    对着一个不存在的目录悄悄放行是同一类坑,这里选择当场炸。
    """
    root = Path(bundles_root)
    link = root / CURRENT_LINK
    if not link.is_symlink():
        return None
    target = link.resolve()
    if not target.is_dir():
        raise BundleError(f"{CURRENT_LINK} 指着 {target},可那儿没有目录 —— "
                          "链是悬空的")
    return target


def land(bundles_root: Path | str, staged: Path | str) -> BundleManifest:
    """把一个打好的包落到盘上。**只落盘,不换链**(定夺 11)。

    「落好了但还没生效」是一个必须能被看到的状态。合成一个的话,要测它
    只能靠一个 sleep 循环,而 §8.5 把 sleep 禁了。

    同一个包落第二遍是**空操作** —— 网络会重传,报错会让重传逻辑变成一个
    要处理特例的东西。同号不同内容则拒:§3.2 的版本号是单调递增的。
    """
    root, staged = Path(bundles_root), Path(staged)
    m = verify_bundle(staged)
    dest = root / m.slot_name
    if dest.exists():
        旧 = read_manifest(dest)
        if 旧.content_sha256 == m.content_sha256:
            return m                       # 重传,空操作
        raise BundleError(
            f"{m.slot_name} 已经在盘上了,而且内容不一样(盘上 "
            f"{旧.content_sha256[:12]},来的是 {m.content_sha256[:12]}) —— "
            "版本号要单调递增,改了内容就得升版号")
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged, dest, symlinks=True)
    return m


def apply_bundle(bundles_root: Path | str, slot_name: str, *,
                 sn: str = "", force: bool = False) -> BundleState:
    """让某一版生效。**换链之前先校验** —— 换完再校验的话,坏包已经在跑了。

    退过的那一版默认不许再生效:什么都不拦的话,下一次下发会原样再生效一次
    那个崩过的包,然后再退一次 —— 一个安静的死循环,现场看到的是「狗一直在
    重启」。``force`` 是给「人看过日志、认定那次是别的原因」留的路,必须
    明确写出来。

    ``sn`` 是这台机器的序列号,拿去跟 §3.1 那个「目标 SN」对(定夺 13)。
    **闸只在这儿,不在 ``parse_manifest`` 也不在 ``land``:**
    换链是唯一一个「这台机器要开始按这份包干活了」的时刻。服务器跑的是同一段
    编包和校验代码(§3.8),它手上根本没有「我是哪台狗」这个概念。
    ``force`` **不跳这道闸** —— 它是给「退过的那一版」开的口子,不是给
    「这份包不是给这台狗的」开的。
    """
    root = Path(bundles_root)
    # **槽名是外面传进来的,而且会被拼进路径。** ``root / "../../etc"`` 在
    # Linux 上解出来就是 ``/etc``,而 ``/etc`` 真的是个目录 —— 光靠下一行的
    # ``is_dir()`` 拦不住,后面那句 ``verify_bundle`` 就会去遍历它。
    if not _SLOT_RE.match(slot_name):
        raise BundleError(f"槽名不合规: {slot_name!r} —— 形如 site-kl-7")
    dest = root / slot_name
    if not dest.is_dir():
        raise BundleError(f"{slot_name} 不在 {root} 底下")
    m = verify_bundle(dest)

    if not m.accepts(sn):
        raise BundleError(
            f"这份包不是给这台机器的:{slot_name} 的 targets 是 "
            f"{list(m.targets)},本机 SN 是 {sn!r}(定夺 13)")

    st = read_state(root)
    if not force and any(r.frm == slot_name for r in st.rollbacks):
        raise BundleError(f"{slot_name} 是退过的那一版,不许再生效 —— "
                          "要硬来就明确传 force")

    记 = _读记(root)
    记.pop("proven_slot", None)            # 新的一版还没被证过
    _写记(root, 记)                          # 先落记,链才动 —— 断电也能认账
    if st.current and st.current != slot_name:
        point_link(root / PREVIOUS_LINK, root / st.current)
    point_link(root / CURRENT_LINK, dest)
    return read_state(root)


def mark_proven(bundles_root: Path | str) -> BundleState:
    """记下:``current`` 这一版真跑成过一次。§3.2 的自动回退判据靠它。"""
    root = Path(bundles_root)
    cur = _链指向(root, CURRENT_LINK)
    if not cur:
        raise BundleError(f"没有 {CURRENT_LINK},没有哪一版可以标记")
    记 = _读记(root)
    记["proven_slot"] = cur
    _写记(root, 记)
    return read_state(root)


def rollback_bundle(bundles_root: Path | str, *, at: str,
                    reason: str) -> BundleState:
    """退回上一版,并留下记录。

    退完之后 ``previous`` 指着**刚被退掉的那一版** —— 它还在盘上,日志和
    现场取证都要用;但它进了 ``rollbacks``,``apply_bundle`` 默认不让它再上。
    """
    root = Path(bundles_root)
    _aware(at, "at")                        # 只校验格式,原字符串照样落盘
    cur, prev = _链指向(root, CURRENT_LINK), _链指向(root, PREVIOUS_LINK)
    if not prev:
        raise BundleError(f"没有 {PREVIOUS_LINK},退不了")
    记 = _读记(root)
    记.setdefault("rollbacks", []).append(
        {"at": at, "from": cur, "to": prev, "reason": reason})
    记["proven_slot"] = prev                # 退回去的那一版本来就是证过的
    _写记(root, 记)                          # 先落记,链才动 —— 断电也能认账
    point_link(root / CURRENT_LINK, root / prev)
    if cur:
        point_link(root / PREVIOUS_LINK, root / cur)
    return read_state(root)


def prune_bundles(bundles_root: Path | str) -> tuple[str, ...]:
    """只留 ``current`` 和 ``previous`` 两份,别的删掉。返回删了哪些。

    §3.2:狗上永远留两份。``landed.json`` 和那两条链不动 —— 尤其
    ``rollbacks`` 是只增的,清磁盘不该清掉「这一版崩过」这条事实。
    """
    root = Path(bundles_root)
    st = read_state(root)
    留 = {st.current, st.previous} - {""}
    删 = []
    for p in sorted(root.iterdir()):
        if p.is_symlink() or not p.is_dir() or p.name in 留:
            continue
        shutil.rmtree(p)
        删.append(p.name)
    return tuple(删)


# ------------------------------------------------------------ 意图与执行


@dataclass(frozen=True, slots=True)
class BundleRef:
    """指着某一版包的三个字段。**心跳报的就是这三样**(§3.2)。"""

    bundle_id: str
    version: int
    content_sha256: str

    @classmethod
    def of(cls, m: BundleManifest) -> BundleRef:
        return cls(m.bundle_id, m.version, m.content_sha256)

    def to_wire(self) -> dict[str, Any]:
        return {"bundle_id": self.bundle_id, "version": self.version,
                "content_sha256": self.content_sha256}


class DivergenceKind(str, Enum):
    """意图跟执行差在哪儿。**这五个字符串是接口的一部分。**"""

    IN_SYNC = "in_sync"
    #: 狗落后:服务器已经到 v9,狗手上还是 v7。
    BEHIND = "behind"
    #: 狗超前:服务器被回滚过,狗手上那一版已经被撤回了。
    AHEAD = "ahead"
    #: 从没对过话。两边碰巧同号也不算一致。
    NEVER_SYNCED = "never_synced"
    #: 同号不同内容,或者压根不是同一个包。
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Divergence:
    """一台狗跟服务器差在哪儿。"""

    kind: DivergenceKind
    local: BundleRef | None
    intended: BundleRef | None
    #: 差几版。``BEHIND`` / ``AHEAD`` 时是正数,别的情况是 0。
    gap: int
    #: 最后一次同步是多久以前(秒)。从没同步过是 ``None``。
    #: **负数是留着的**:它说明这台的钟比服务器快,而那正是 ``clock_skew()``
    #: 在另一头报的同一件事。夹到 0 就把这条线索抹掉了。
    since_sync_s: float | None

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "local": self.local.to_wire() if self.local else None,
            "intended": self.intended.to_wire() if self.intended else None,
            "gap": self.gap,
            "since_sync_s": self.since_sync_s,
        }


def divergence(*, local: BundleRef | None, intended: BundleRef | None,
               last_sync_ms: int | None, now_ms: int) -> Divergence:
    """狗手上那一版跟服务器那一版差在哪儿。**纯函数。**

    §3.4:狗手上那一版是「执行」的唯一真理源,服务器那一版是「意图」的唯一
    真理源。**规格原话:不显示落差是这一节最危险的失败模式** —— 所以这不是
    一个 UI 细节,是一个有测试的判据。
    """
    since = None if last_sync_ms is None else (now_ms - last_sync_ms) / 1000.0

    def 出(kind: DivergenceKind, gap: int = 0) -> Divergence:
        return Divergence(kind, local, intended, gap, since)

    # **「从没同步过」压在最前面。** 两边碰巧都是 v7 也不算一致:那是两个从
    # 没对过话的人报了同一个数字,而不是一次成功的同步。
    if last_sync_ms is None:
        return 出(DivergenceKind.NEVER_SYNCED)
    if local is None and intended is None:
        return 出(DivergenceKind.IN_SYNC)
    if local is None:
        assert intended is not None
        return 出(DivergenceKind.BEHIND, intended.version)
    if intended is None:
        return 出(DivergenceKind.AHEAD, local.version)
    if local.bundle_id != intended.bundle_id:
        # 狗手上跑着**别的站点**的包。比落后几版严重得多。
        return 出(DivergenceKind.CONFLICT)
    if local.version == intended.version:
        if local.content_sha256 == intended.content_sha256:
            return 出(DivergenceKind.IN_SYNC)
        # §3.2 定死了同号不许换内容。真出现了,说明包在路上被改过、或者有人
        # 手工动过盘 —— 报「一致」就等于把 content_sha256 这道闸白设了。
        return 出(DivergenceKind.CONFLICT)
    if local.version < intended.version:
        return 出(DivergenceKind.BEHIND, intended.version - local.version)
    return 出(DivergenceKind.AHEAD, local.version - intended.version)
