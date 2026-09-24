"""任务包的格式与校验(W00c2a 起住在契约包)。站点收任务包(``d1max-site import-bundle``)与狗上
落包(``d1max_agent.engine.bundle``)用的是同一份:自述(``bundle.yaml``)的解析、纯数据闸、
整包指纹、排程文件的读取。**落盘、两份、回退留在狗上的 ``bundle.py``。**

**包是纯数据。** 里头没有一行可执行的东西 —— 没有固件,没有代码,没有脚本。
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from d1max_contract.digest import tree_sha256
from d1max_contract.schedule import Schedule, ScheduleError, parse_schedule

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
    """从包目录里读自述。**读不动、解不了码,一律 ``BundleError``。**

    ``UnicodeDecodeError`` 也得在这儿被翻掉(评审复评 finding 2):它是
    ``ValueError`` 的子类,**不是 ``OSError``**,所以一个只写着
    ``except (OSError, BundleError)`` 的调用方罩不住它。而这条路真会走到:
    apply 到一半断电,同一次断电把这份 ``bundle.yaml`` 写成了非 UTF-8 字节
    (拷了一半、闪存掉电损坏)—— 那正是 ``guard_bundle`` 的 ``UNDONE``
    这条路存在的理由,守卫却会在读它的时候自己炸掉。
    """
    p = Path(bundle_dir) / BUNDLE_MANIFEST
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise BundleError(f"读不到 {BUNDLE_MANIFEST}: {p}") from e
    except UnicodeDecodeError as e:
        raise BundleError(f"{BUNDLE_MANIFEST} 不是合法 UTF-8: {p}") from e
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
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        # ``UnicodeDecodeError`` 跟 ``read_manifest`` 那儿是同一条理由:
        # 它是 ``ValueError`` 的子类而不是 ``OSError``,漏掉就直穿上去。
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


