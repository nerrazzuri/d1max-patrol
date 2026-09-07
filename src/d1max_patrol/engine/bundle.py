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
from pathlib import Path
from typing import Any

import yaml

from .mission import MissionError, parse_mission
from .release import tree_sha256

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


class BundleError(ValueError):
    """包不合规。**解析、校验、落盘全用这一个。**"""


def _整数(v: Any) -> bool:
    """``isinstance(True, int)`` 是真的,所以这里比的是类型本身。
    YAML 里 ``version: yes`` 会解成 ``True``,不拦就成了第 1 版。
    """
    return type(v) is int


def _aware(s: str) -> datetime:
    """解一个**必须带时区**的 ISO8601 时刻。

    裸时刻会被按读它那台机器的时区解释,而包是从服务器发到狗上的。
    """
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError) as e:
        raise BundleError(f"built_at 解不出来: {s!r}") from e
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise BundleError(f"built_at 必须带时区: {s!r}")
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
