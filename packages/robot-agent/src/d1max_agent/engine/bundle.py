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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from d1max_contract.bundle_format import (  # noqa: F401 - 转手:老调用照旧从 bundle 拿
    _EXEC_BITS,
    _ID_RE,
    _MANIFEST_KEYS,
    _SHA_RE,
    _SHEBANG,
    ALLOWED_SUFFIXES,
    BUNDLE_MANIFEST,
    BUNDLE_SCHEMA,
    GATE_NAMES,
    MAX_SN_LEN,
    MAX_VERSION,
    MIN_VERSION,
    MISSIONS_DIR,
    SCHEDULE_NAME,
    BundleError,
    BundleManifest,
    Violation,
    _aware,
    _gate_exec_bit,
    _gate_shebang,
    _gate_suffix,
    _gate_symlink,
    _targets,
    _整数,
    _走一遍,
    bundle_sha256,
    dump_manifest,
    parse_manifest,
    read_bundle_schedule,
    read_manifest,
    scan_pure_data,
    verify_bundle,
    verify_pure_data,
    write_manifest,
)

from .mission import MissionError, parse_mission
from .release import point_link


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


# -------------------------------------------------------- 落盘、两份、回退

CURRENT_LINK = "current"
PREVIOUS_LINK = "previous"
#: 链存不下的那点东西:证没证过、退过哪些、有没有一次换链没换完。
#: **「在跑哪一版」不在这儿。**
LANDED = "landed.json"

#: ``rollbacks`` 里最多留几条**历史**。评审 F6:每条 ``reason`` 上限 500 字,
#: 而次数没上限 —— 实测 300 次回退把 ``landed.json`` 撑到 489KB,
#: ``GET /api/bundle`` 一次吐 177KB、耗时 3.1s(每次全文件重写,O(n²))。
#: 狗是长期在线的设备,这个只会越来越糟。
#:
#: **20 这个数的理由**:现场排障翻的是「最近几次退过什么」,20 条足够覆盖
#: 一次连环故障的全过程;再往前的**拉黑事实**一条不少地留在 ``denied`` 里,
#: 截掉的只是那几行 ``reason``。20 × 500 字的中文 ≈ 32KB(UTF-8 一个字 3 字节),
#: ``GET /api/bundle`` 一次吐得动。再大没人看,再小会把现场截掉。
#:
#: **截断绝不能把「哪些版本被拉黑」一起截掉** —— 那是安全判据,不是历史。
#: 所以拉黑事实单独存在 ``denied`` 里(见 ``BundleState.denied``),
#: 这里截掉的只是历史细节(``at``/``reason``/``to``)。
MAX_ROLLBACKS = 20

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
class Forced:
    """一次**强推**:人明确说了「我知道我在干什么」,把一个被拉黑的版本装了回去。

    这扇门后面是「装一个已知会崩的版本」,所以它必须留痕(评审复评 finding 5)。

    **强推不是洗白。** 这条记录写进历史,**不动 ``denied``** —— 下一次不带
    ``force`` 的 ``apply`` 照样被拒。「这一次我知道我在干什么」跟「这一版从此
    可以随便装」是两句话。
    """

    at: str
    slot: str

    def to_wire(self) -> dict[str, Any]:
        return {"at": self.at, "slot": self.slot}


@dataclass(frozen=True, slots=True)
class Applying:
    """一次**还没换完链**的 apply。**换链之前就落盘。**

    跟 ``release.Pending`` 是同一套东西的两处实现:``point_link`` 的 docstring
    承诺「那条退路上的窗口由调用方的标记文件兜着 —— 标记是换链之前写的」,
    这个类就是任务包这一侧对那句话的兑现。评审 F2:在它之前,任务包这边写的
    标记是个 ``{}``,断电之后谁也修不回去。

    比 ``release.Pending`` 多一个 ``prev``:release 侧只换一条链,任务包这边
    连着换两条(先 ``previous`` 后 ``current``),要能原样撤销就得把两条链
    换之前指着谁都记下来。
    """

    #: 要换到哪一版。
    to: str
    #: 换链之前 ``current`` 指着谁。空串 = 这台机器还没有生效过任何一版。
    #: 字段不叫 ``from`` 是因为那是关键字;线上的键仍然叫 ``from``。
    src: str
    #: 换链之前 ``previous`` 指着谁。空串 = 当时还没有这条链。
    prev: str = ""
    #: 换链之前 ``proven_slot`` 记着哪一版。空串 = 当时谁都没被证过。
    #:
    #: **这一条不能少**(评审复评 finding 3):``apply_bundle`` 在写这个标记的
    #: 同一次里就把 ``proven_slot`` pop 掉了。不记下来的话,``UNDONE`` 把两条
    #: 链放回换链前的样子之后,那一版**已经真跑成过的**包回到 ``current``
    #: 却带着 ``proven=False`` —— 第 8 卷那条判据
    #: ``崩了 and not proven and previous`` 随即成立,自动退一次,
    #: **把那份真跑成过的好包拉黑**。F2 的伤口只是挪后了一步。
    proven: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"to": self.to, "from": self.src, "prev": self.prev,
                "proven_slot": self.proven}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Applying:
        """**四个字段一律走 ``_串``,不是 ``str()``**(评审复评第 3 轮 N5)。

        ``str(None)`` 是 ``"None"`` —— 一个长得像槽名、却过不了 ``_SLOT_RE``
        的假名字。``landed.json`` 里哪个键是 JSON ``null``(人手改过、别的
        序列化器写的),整条标记就此作废,断电留下的两条链再也没人修。
        「不是字符串」的正确读法是「没有这一项」,不是「有一项叫 None」。
        """
        return cls(to=_串(raw.get("to")), src=_串(raw.get("from")),
                   prev=_串(raw.get("prev")), proven=_串(raw.get("proven_slot")))


class BundleGuard(Enum):
    """``guard_bundle`` 的结论。跟 ``release.GuardAction`` 同一个路子。"""

    #: 没有半成品,盘上是个干净的终态。
    OK = "ok"
    #: 有半成品,照标记把没换完的链换完了。
    FINISHED = "finished"
    #: 有半成品,但换不过去(目标那份包没了/坏了),两条链都放回换链前的样子。
    UNDONE = "undone"
    #: 有半成品,而且两个方向都走不通 —— 要人来看。**标记不清**,留给人取证。
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class BundleState:
    """盘上现在是什么局面。"""

    current: str
    previous: str
    #: ``current`` 这一版有没有真跑成过一次。
    proven: bool
    #: 退过哪些。**只留最近 ``MAX_ROLLBACKS`` 条历史**,拉黑事实在 ``denied``。
    rollbacks: tuple[Rollback, ...]
    #: 被拉黑的槽名:退过的那一版默认不许再生效。**去重、只增、不随历史截断。**
    #: 跟 ``rollbacks`` 分开存的理由见 ``MAX_ROLLBACKS``。
    denied: tuple[str, ...] = ()
    #: 有一次 apply 换链换到一半没换完(断电)。``None`` = 盘上是个干净的终态。
    applying: Applying | None = None
    #: 人明确强推过哪几次。**历史,不是判据** —— 跟 ``rollbacks`` 一样有条数
    #: 上限,跟 ``denied`` 一样不影响对方:强推一次不会把那一版从黑名单里抹掉。
    forced: tuple[Forced, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {"current": self.current, "previous": self.previous,
                "proven": self.proven,
                "rollbacks": [r.to_wire() for r in self.rollbacks],
                "denied": list(self.denied),
                "applying": self.applying.to_wire() if self.applying else None,
                "forced": [f.to_wire() for f in self.forced]}


#: 守卫那条路上「不许抛出去」的异常。``BundleError`` 是 ``ValueError`` 的子类,
#: 所以覆盖面只增不减;``TypeError`` 是评审复评第 3 轮 N2 逮到的那个漏网的
#: —— ``landed.json`` 里 ``denied``/``rollbacks``/``forced`` 写成 ``null`` 或
#: 数字时,列表推导抛的既不是 ``OSError`` 也不是 ``ValueError``。
#: **一处定义两处用**(``guard_bundle`` 的兜底、``_guard_bundle`` 里那句家务活),
#: 两边各写一遍的话,哪天补了一个漏网的另一边不会跟着补。
_守卫兜底 = (OSError, ValueError, TypeError)


def _串(值: Any) -> str:
    """线上来的值当字符串用。**不是字符串就当空的**,不是 ``str(值)``。

    ``landed.json`` 是「外面的东西」,里头任何一个键都可能是 ``null``、数字、
    列表。``str()`` 会把它们变成 ``"None"``/``"7"`` 这种**长得像内容的假内容**,
    比空串危险:空串的意思是「没有这一项」,一路上都有人认;``"None"`` 谁也
    不认,只会在某道闸上把整件事作废掉。
    """
    return 值 if isinstance(值, str) else ""


def _取表(记: Mapping[str, Any], 键: str) -> list[Any]:
    """从 ``landed.json`` 里取一个列表。**不是列表就当空的。**

    评审复评第 3 轮 N2:``{"denied": null}`` 这种盘上内容会让列表推导抛
    ``TypeError``,直穿 ``guard_bundle`` 那句「任何一条路都不抛」的承诺。
    同一行里 ``isinstance(r, dict)`` 早就在过滤条目了,容器本身却没过 ——
    这三处(``_黑名单``、``read_state``、``_归一化``)统一走这一个门。
    """
    值 = 记.get(键)
    return 值 if isinstance(值, list) else []


def _取表写(记: dict[str, Any], 键: str) -> list[Any]:
    """取一个列表准备往里追加,**不是列表就换成空的**(会写回 ``记``)。

    评审复评第 3 轮 N6:``记.setdefault("forced", []).append(...)`` 在盘上那个
    值是字符串/数字时抛 ``AttributeError``,``_bundle_apply`` 只接
    ``BundleError``,于是冒成一个 500。跟 ``_取表`` 同一条理由,只是这条路要写。
    """
    值 = 记.get(键)
    if not isinstance(值, list):
        值 = []
        记[键] = 值
    return 值


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

    接的是 ``ValueError`` 而不是 ``json.JSONDecodeError``,跟
    ``release.read_pending`` 对齐:半个文件里的非 UTF-8 字节抛的是
    ``UnicodeDecodeError``,那也是 ``ValueError`` 的子类,但**不是**
    ``JSONDecodeError`` —— 断电正好断在这个文件上时走的就是那条路。
    """
    try:
        raw = json.loads((root / LANDED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _写记(root: Path, 记: dict[str, Any]) -> None:
    """写 ``landed.json``。**先写临时文件再改名**,不留半个 JSON 在盘上。

    跟 ``release.write_pending`` 同一套做法。直接 ``write_text`` 的话,写到
    一半断电盘上就是个截断的 JSON —— ``_读记`` 会把它当空的,于是
    ``denied``(安全判据)和 ``proven_slot`` 一起没了。开机归一化
    (``_归一化``)正是要在这种盘上跑的那一段,它自己更不能留下这种残骸。
    """
    tmp = root / (LANDED + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(记, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, root / LANDED)


def read_state(bundles_root: Path | str) -> BundleState:
    """盘上现在是什么局面。"""
    root = Path(bundles_root)
    cur = _链指向(root, CURRENT_LINK)
    记 = _读记(root)
    退 = tuple(Rollback(str(r.get("at", "")), str(r.get("from", "")),
                        str(r.get("to", "")), str(r.get("reason", "")))
               for r in _取表(记, "rollbacks")
               if isinstance(r, dict))
    强 = tuple(Forced(str(f.get("at", "")), str(f.get("slot", "")))
               for f in _取表(记, "forced") if isinstance(f, dict))
    # proven 记的是**哪一个槽**被证过。存布尔的话,有人手工把 current 挪到
    # 另一版上,那一版就凭空继承了「跑成过」这个结论 —— 它一次都没跑过。
    return BundleState(cur, _链指向(root, PREVIOUS_LINK),
                       bool(cur) and 记.get("proven_slot") == cur, 退,
                       denied=_黑名单(记), applying=_半成品(记), forced=强)


def denies(state: BundleState, slot_name: str) -> bool:
    """这一版现在是不是被拉黑了 —— **``apply_bundle`` 那道闸的判据本身**。

    单独拿出来只有一个理由:路由要如实回答「这一次 ``force`` 到底顶开了没有」
    (评审复评第 3 轮 N4),而 ``apply_bundle`` 的返回值里看不出来 ——
    ``denied`` 强推前后一模一样,``forced`` 只在真顶开时才多一笔、还带截断。
    判据在这儿写一遍、两个调用方共用,**同一件事写两遍迟早分岔**;闸本身仍然
    只在 ``apply_bundle`` 里,这个函数不做决定,只回答。
    """
    return slot_name in state.denied


def _黑名单(记: Mapping[str, Any]) -> tuple[str, ...]:
    """哪些版本不许再生效。

    **``rollbacks`` 里的 ``from`` 也算。** 老机器盘上那份 ``landed.json``
    只有 ``rollbacks``、没有 ``denied``,升级上来之后那些拉黑事实不能凭空消失
    —— 那正好是「一版崩过的包又被装回去」这条死循环。
    """
    名 = [s for s in _取表(记, "denied") if isinstance(s, str) and s]
    名 += [str(r.get("from", "")) for r in _取表(记, "rollbacks")
           if isinstance(r, dict) and r.get("from")]
    return tuple(dict.fromkeys(名))          # 去重,保序


def _槽名(名: str, *, 可空: bool = False) -> bool:
    """这个字符串能不能被拼进路径。空串按「没有这条链」算(``可空``)。"""
    if not 名:
        return 可空
    return bool(_SLOT_RE.match(名))


def _半成品(记: Mapping[str, Any]) -> Applying | None:
    """读那个「换链换到一半」的标记。**坏了当没有。**

    跟 ``release.read_pending`` 同一个判断:半个 JSON 让守卫炸掉的话,狗就
    起不来了,而「起不来」正是这个标记本来要防的事。

    **四个槽名各过一遍 ``_SLOT_RE``**(评审复评 finding 7)。``guard_bundle``
    会把它们直接拼进路径(``root / 半.to``),而 ``apply_bundle`` 对同一件事
    写着「槽名是外面传进来的,而且会被拼进路径」并过闸 —— 同一条教条在这儿
    也得成立。今天挡住 ``../../etc`` 的其实是 ``_能用()`` 里 ``verify_bundle``
    那道「目录名要跟自述里的 ``slot_name`` 一致」,那是**别处的副作用**,
    不是本地的判据;而一个带 NUL 的名字连 ``Path.is_dir()`` 都会抛
    ``ValueError``,直接把「守卫不许抛」这条承诺也一起打掉。
    """
    raw = 记.get("applying")
    if not isinstance(raw, Mapping) or not raw.get("to"):
        return None
    try:
        半 = Applying.from_wire(raw)
    except (TypeError, ValueError):
        return None
    if not _槽名(半.to):
        return None
    if not all(_槽名(名, 可空=True) for 名 in (半.src, 半.prev)):
        return None
    if not _槽名(半.proven, 可空=True):
        # **``proven`` 坏了只丢这个字段,不作废整条标记**(评审复评第 3 轮 N5)。
        # ``to``/``src``/``prev`` 会被**拼进路径**,坏了作废整条是对的;而
        # ``proven`` 从头到尾没有被拼进过路径 —— 它只在 ``read_state`` 里跟
        # ``current`` 比一次字符串。为了一条比不比得上都无所谓的信息,赔掉
        # 「两条链断电之后还修不修得回来」,不划算。丢掉它的后果是那一版的
        # 「跑成过」退回 False,比两条链停在中间态轻得多。
        半 = Applying(to=半.to, src=半.src, prev=半.prev, proven="")
    return 半


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
                 sn: str = "", force: bool = False, at: str = "") -> BundleState:
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

    **强推留痕**(评审复评 finding 5):``force`` 真的顶开了黑名单那一次,会往
    ``landed.json`` 的 ``forced`` 历史里写一笔(``at`` + 槽名),``at`` 由调用方
    盖(这一层不许读墙上时钟,§8.5 第 2 条)。**只在真顶开的时候记** ——
    对一个本来就没被拉黑的槽传 ``force`` 什么也没发生,记下来只会把这份历史
    冲成噪音。这一笔**不动 ``denied``**:强推是「这一次我知道我在干什么」,
    不是「这一版从此洗白」,下一次不带 ``force`` 照样拒。
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
    顶开了 = denies(st, slot_name)
    if not force and 顶开了:
        raise BundleError(f"{slot_name} 是退过的那一版,不许再生效 —— "
                          "要硬来就明确传 force")

    记 = _读记(root)
    旧证 = 记.pop("proven_slot", None)      # 新的一版还没被证过
    # 盘上那个值可能是任何东西(``landed.json`` 是外面的)。不合规就当没有:
    # 把一个坏值记进标记,会让 ``_半成品`` 把**整条**标记作废,断电之后连两条
    # 链都修不回来了 —— 为了留一条没用的信息,赔掉那条修复路径,不划算。
    if not isinstance(旧证, str) or not _槽名(旧证, 可空=True):
        旧证 = ""
    if 顶开了:
        # 强推留痕。**写进历史,不动 ``denied``** —— 见本函数 docstring。
        强 = _取表写(记, "forced")             # 盘上那个值不是列表就换掉(N6)
        强.append({"at": at, "slot": slot_name})
        del 强[:-MAX_ROLLBACKS]            # 跟回退历史同一个上限,同一条理由
    # **先落标记,再换链**(评审 F2)。底下连着换两条链,两次之间断电的话
    # 盘上会停在 ``current == previous`` 这个自相矛盾的中间态;标记里带着
    # 「要换到哪一版」「两条链本来指着谁」「谁被证过」,``guard_bundle``
    # 才修得回去。只 pop 一个 proven_slot 是兜不住任何东西的:
    # 它留下的是一个 ``{}``。
    记["applying"] = Applying(to=slot_name, src=st.current, prev=st.previous,
                              proven=旧证).to_wire()
    _写记(root, 记)
    if st.current and st.current != slot_name:
        point_link(root / PREVIOUS_LINK, root / st.current)
    point_link(root / CURRENT_LINK, dest)
    记 = _读记(root)
    记.pop("applying", None)               # 两条链都换完了,标记该没了
    _写记(root, 记)
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
    现场取证都要用;但它进了黑名单,``apply_bundle`` 默认不让它再上。

    **黑名单这道闸两扇门上都要装**(评审 F1)。它本来只装在 ``apply_bundle``
    那一扇,而「装回一个已知会崩的版本」有两条路进得来:换过去,和**退**过去。
    v2 崩了退到 v1 之后再点一次回退,``previous`` 正是刚拉黑的 v2 —— 200 OK,
    狗回到那一版,还被 ``proven_slot = prev`` 标成「跑成过」。手机双击、
    客户端超时重试、第 8 卷的自动回退判据抖一下,都够触发。
    所以这儿拒:**没有可退的了是一个要说出来的结论**,不是一次悄悄的换链。

    抛的都是 ``BundleError``,路由 ``_bundle_rollback`` 一律翻成 409 ——
    请求本身没毛病,是这台机器现在的状态不允许,跟「没有 previous」同一类。
    """
    root = Path(bundles_root)
    _aware(at, "at")                        # 只校验格式,原字符串照样落盘
    cur, prev = _链指向(root, CURRENT_LINK), _链指向(root, PREVIOUS_LINK)
    if not prev:
        raise BundleError(f"没有 {PREVIOUS_LINK},退不了")
    if cur == prev:
        # 两条链指着同一版:退过去等于原地打转,还会记下一条 from == to 的
        # 荒唐审计,并且把这唯一一版拉黑 —— 盘上那份好包就此再也装不上去
        # (路由不暴露 force)。这个局面正是「换链换到一半断电」留下的,
        # 该走 ``guard_bundle``,不该走回退。
        raise BundleError(f"{CURRENT_LINK} 和 {PREVIOUS_LINK} 都指着 {cur},"
                          "没有别的一版可退 —— 先跑一次 guard_bundle")
    记 = _读记(root)
    if prev in _黑名单(记):
        raise BundleError(f"{prev} 是退过的那一版,退回去也不许 —— "
                          "没有可退的了,要装哪一版得明说")
    # **截断之前先把黑名单固化下来**(评审复评 finding 1)。老盘上那份
    # ``landed.json`` 只有 ``rollbacks``、没有 ``denied``,那些拉黑事实只存在于
    # ``rollbacks[].from`` 里 —— 直接截 ``rollbacks`` 就把它们静默扔了,而
    # 「截断不得破坏黑名单语义」正是 F6 那条要求本身。升级上来的狗做**一次
    # 寻常回退**就会踩到:25 条历史截成 20 条,最早那几版重新变得可以 apply。
    记["denied"] = list(_黑名单(记))
    退 = _取表写(记, "rollbacks")             # 盘上那个值不是列表就换掉(N6)
    退.append({"at": at, "from": cur, "to": prev, "reason": reason})
    del 退[:-MAX_ROLLBACKS]                 # 只留最近这些条**历史**
    if cur:
        # **拉黑事实单独存,不随历史被截掉。** 去重:同一版退过十次也只有一条。
        黑 = _取表写(记, "denied")
        if cur not in 黑:
            黑.append(cur)
    # 退回去的那一版本来就是证过的。上头那道黑名单闸保证了 prev 从没被拉黑过
    # —— 「一次都没跑成过的那一版凭空继承 proven」这条路已经被堵死了。
    记["proven_slot"] = prev
    _写记(root, 记)                          # 先落记,链才动 —— 断电也能认账
    point_link(root / CURRENT_LINK, root / prev)
    if cur:
        point_link(root / PREVIOUS_LINK, root / cur)
    return read_state(root)


def guard_bundle(bundles_root: Path | str) -> BundleGuard:
    """把「换链换到一半」的局面收拾成一个能用的终态(评审 F2)。

    对着 ``release.boot_guard`` 抄的:那边靠 ``pending.json`` 修版本目录的链,
    这边靠 ``landed.json`` 里的 ``applying`` 修任务包的两条链。没有这一段的话,
    ``apply`` 的两次 ``point_link`` 之间断一次电,盘上就停在
    ``current == previous`` 且 ``proven=False`` —— 第 8 卷那条自动回退判据
    (``崩了 and not proven and previous``)随即成立,记下一条 ``from == to``
    的回退并**把盘上唯一那份好包拉黑**,而路由不暴露 ``force``,靠 API 再也
    装不回去。

    **它还顺手把老盘归一化一次**(评审复评 finding 6):截断只发生在
    ``rollback_bundle`` 那条写路径上,一台从老版本升上来的狗在它**下一次回退
    之前**照样带着几百条 ``rollbacks``(实测 489KB 文件、``GET /api/bundle``
    吐 177KB 用 3.1s);而如果它不再回退,就永远这样。开机跑一次这里,那台
    机器开一次机就正常了。归一化是**幂等**的(开十次机跟开一次机同一个结果),
    而且**断电安全**(``_写记`` 是先写临时文件再改名,盘上不会留半个 JSON;
    真在改名之前断了电,下次开机照着原样再归一化一遍就是了)。

    **任何一条路都不许抛异常**,理由同 ``boot_guard``:这段代码炸掉等于狗
    起不来,而起不来正是它要防的事。最坏的结论也是一个 ``BundleGuard``。
    接的是 ``(OSError, ValueError)`` 而不是 ``(OSError, BundleError)``:
    ``BundleError`` 本来就是 ``ValueError`` 的子类,而这条路上真正漏出去过的
    是 ``UnicodeDecodeError``(``read_manifest`` 读一份非 UTF-8 的
    ``bundle.yaml``,评审复评 finding 2)—— 那也是 ``ValueError`` 的子类,
    却既不是 ``OSError`` 也不是 ``BundleError``。根因已经在 ``read_manifest``
    里堵掉了,这一层是第二道:承诺是「任何一条路」,那就不能只堵今天见过的那条。

    兜底那张网现在是 ``_守卫兜底``,比原来多一个 ``TypeError``(评审复评第 3 轮
    N2):``landed.json`` 里 ``denied``/``rollbacks``/``forced`` 写成 ``null``
    或数字时,列表推导抛的正是它 —— 既不是 ``OSError`` 也不是 ``ValueError``,
    上面那句承诺在这儿又被捅穿过一次。
    """
    try:
        return _guard_bundle(Path(bundles_root))
    except _守卫兜底:
        return BundleGuard.BROKEN


def _guard_bundle(root: Path) -> BundleGuard:
    """``guard_bundle`` 的实际逻辑,可能抛,由外面兜底。"""
    try:
        _归一化(root)                        # 老盘升上来的那一次开机顺手收拾
    except _守卫兜底:
        # **家务活绝不许挡在安全网前面,更不许把安全网赔掉**(评审复评第 3 轮
        # N1)。归一化是家务活(截历史、固化黑名单),底下那一段修链才是安全网,
        # 而归一化**自己会写盘**:ENOSPC、EIO、只读挂载、掉电正好落在
        # ``os.replace`` 上 —— 它一抛,整个 ``_guard_bundle`` 就抛出去,外面兜成
        # ``BROKEN``,**两条链一个字没修**。偏偏盘快满的那台狗正是最需要修链的
        # 那台:链停在 ``current == previous`` 且 ``proven=False``,第 8 卷那条
        # ``崩了 and not proven and previous`` 随即成立,自动退一次,把盘上唯一
        # 那份好包拉黑。家务活这次没干成不要紧,下次开机再干一遍
        # (``_归一化`` 是幂等的,不依赖「上次跑到哪儿」)。
        #
        # **为什么原地包住而不是挪到函数末尾:** 这个函数有三个出口(``OK``、
        # ``BROKEN``、末尾那一条),而老盘归一化要走的正是最常见的 ``OK``
        # 那一条 —— 挪到「末尾」等于把它从 99% 的开机路上删掉;要补回来得靠
        # ``try/finally`` 或者三处各写一遍,而**那个 finally 自己还得再包一层
        # try**(不然安全网又被赔掉一次),绕一圈回到同一处,还多两个出口要对。
        #
        # 两边会不会看见对方写的东西:会,但不相干。归一化只写
        # ``denied``/``rollbacks``/``forced``,修链只写 ``applying``/``proven_slot``,
        # 两组键不相交;而且底下每一次写盘之前都重新 ``_读记`` 一遍 ——
        # 归一化成功,底下读到的是归一化之后那份;归一化失败,底下读到的是
        # 原样那份。两种都是**完整的一份**,没有第三种(``_写记`` 先写 tmp
        # 再改名)。反过来把归一化排在修链之后,它读到的就是「``applying``
        # 刚被清掉」那份 —— 也对,但那时候链已经修好了,先后就没有意义了。
        pass
    记 = _读记(root)
    半 = _半成品(记)
    if 半 is None:
        return BundleGuard.OK

    if _能用(root / 半.to):
        # 照标记把没换完的换完。**照标记修,不照「盘上最新的」修** ——
        # 两者在正常情况下是同一版,不同的那天正是出事那天(同 boot_guard)。
        if 半.src and 半.src != 半.to:
            point_link(root / PREVIOUS_LINK, root / 半.src)
        point_link(root / CURRENT_LINK, root / 半.to)
        结论 = BundleGuard.FINISHED
    elif 半.src and _能用(root / 半.src):
        # 换不过去了(那份包没落全、或者落下的那半份校验不过)。两条链一起
        # 放回换链前的样子 —— 只放回 current 的话会留下 current == previous。
        point_link(root / CURRENT_LINK, root / 半.src)
        if 半.prev and _能用(root / 半.prev):
            point_link(root / PREVIOUS_LINK, root / 半.prev)
        else:
            链 = root / PREVIOUS_LINK
            if 链.is_symlink():
                链.unlink()                  # 换链之前本来就没有这条链
        结论 = BundleGuard.UNDONE
    else:
        # 两个方向都走不通。**标记留着**:它是现场取证唯一的线索,而且下一次
        # guard 还要照它再试一遍(那份包可能只是暂时读不到,比如盘没挂上)。
        return BundleGuard.BROKEN

    记 = _读记(root)
    记.pop("applying", None)
    if 结论 is BundleGuard.UNDONE and 半.proven:
        # **把 ``proven_slot`` 原样放回**(评审复评 finding 3)。两条链已经回到
        # 换链之前的样子,那份「真跑成过」的事实也该跟着回来 —— 不放回的话
        # ``current`` 是一版跑成过的好包却带着 ``proven=False``,第 8 卷那条
        # ``崩了 and not proven and previous`` 随即成立,自动退一次,把它拉黑。
        记["proven_slot"] = 半.proven
    _写记(root, 记)
    return 结论


def _归一化(root: Path) -> bool:
    """把老盘上那份 ``landed.json`` 收拾成本版本的形状。改了返回 ``True``。

    干两件事,**一次写完**:

    1. 把 ``_黑名单()`` 的全量结果(``denied`` ∪ ``rollbacks[].from``)固化进
       ``denied`` —— 跟 ``rollback_bundle`` 里那一行是同一件事,只是这条路不
       需要等到「下一次回退」。
    2. 把 ``rollbacks`` 截到 ``MAX_ROLLBACKS``。截断本来只在写路径上发生,
       老盘在下一次回退之前照样几百条(评审复评 finding 6)。

    **顺序不能反**,而且必须是同一次写:先截后固化,就是把老盘那些只存在于
    被截掉的条目里的拉黑事实扔掉 —— 正是 finding 1 那个洞。

    **幂等**:第二次跑,``denied`` 已经是全量、``rollbacks`` 已经不超上限,
    两边都相等,直接不写。开十次机跟开一次机盘上是同一份文件。

    **断电安全**:``_写记`` 先写 ``landed.json.tmp`` 再 ``os.replace``,盘上要么
    是归一化之前那份、要么是之后那份,没有第三种。真断在改名之前,下次开机
    读到的还是老那份,再归一化一遍即可 —— 归一化不依赖「上次跑到哪儿」。

    **一份还没有 landed.json(或者里头什么都没有)的新盘上什么都不做** ——
    凭空造一个 ``{"denied": [], "rollbacks": []}`` 出来只是噪音。
    """
    记 = _读记(root)
    if not 记:
        return False
    退 = [r for r in _取表(记, "rollbacks") if isinstance(r, dict)]
    新黑 = list(_黑名单(记))
    新退 = 退[-MAX_ROLLBACKS:]
    新强 = [f for f in _取表(记, "forced") if isinstance(f, dict)][-MAX_ROLLBACKS:]
    if not 新黑 and not 退 and not 新强:
        return False
    if (新黑 == 记.get("denied") and 新退 == 记.get("rollbacks")
            and 新强 == 记.get("forced", [])):
        return False
    记["denied"] = 新黑
    记["rollbacks"] = 新退
    if 新强 or "forced" in 记:
        记["forced"] = 新强
    _写记(root, 记)
    return True


def _能用(dest: Path) -> bool:
    """这个槽目录是一份校验得过的包吗。**校验不过就当它不在。**"""
    if not dest.is_dir():
        return False
    try:
        verify_bundle(dest)
    except BundleError:
        return False
    return True


def prune_bundles(bundles_root: Path | str) -> tuple[str, ...]:
    """只留 ``current`` 和 ``previous`` 两份,别的删掉。返回删了哪些。

    §3.2:狗上永远留两份。``landed.json`` 和那两条链不动 —— 尤其
    ``denied`` 是只增的,清磁盘不该清掉「这一版崩过」这条事实。
    (``rollbacks`` 那份历史有条数上限,见 ``MAX_ROLLBACKS``;被截掉的是
    ``at``/``reason`` 这些细节,拉黑事实在 ``denied`` 里,截不着。)
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
