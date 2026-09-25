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

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from d1max_contract.digest import tree_sha256 as _tree_sha256

log = logging.getLogger(__name__)

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
#: 槽里这几样东西里任一样还有数据,就说明还有巡检数据 —— W01 之前的机器
#: 数据就落在槽里。prune 见到就不删。也是 ``datadir.migrate_slot_data`` 要
#: 搬走的那几样(``datadir`` 从这里 import,单一真理源在这儿)。
#: ``missions`` 也在:``missions/*.yaml`` 是 ``PUT /api/missions`` 写出来的,
#: 发布包里不带(见上面 ``--missions-dir`` 那条注释),跟 runs 一样是槽里的
#: 运行时数据;任务包(``/opt/d1max/bundles``)是另一套机制,不在槽里。
SLOT_DATA_ITEMS = ("runs", "missions", "queue.jsonl", "baselines", "exports")
#: 守卫数到这个数还没等到「自检过了」,就判新版起不来,回滚。
#: 2 而不是 1:第一次开机可能撞上别的偶发(网卡没起来、盘没挂上),给一次机会。
MAX_BOOT_ATTEMPTS = 2

#: 版本目录名的样子:日期 + 一小截内容哈希。**顺带也是防路径穿越的那道闸**。
_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6,12}$")

_CHUNK_JOIN = b"\0"

#: 打包收哪几个顶层条目。**这是一份白名单,不是黑名单。**
#:
#: **为什么是白名单。** 两种漏法的代价差着量级:白名单漏带一个目录,狗上服务
#: 当场起不来 —— 装机现场几分钟内就发现了,补一趟就完事;黑名单漏排一个目录,
#: ``refs/``(厂商的私有协议文档)或 ``dist/``(51MB 的 apk 和离线 wheel)就
#: 被装进了客户的机器,而**这种事没有任何人会发现**。仓库还会长新目录,
#: "新目录默认进包还是默认不进包"就是这条选择的全部内容。
#:
#: 三条各自的理由(都核过调用方,不是照着感觉列的):
#:
#: * ``pyproject.toml`` —— ``deploy/install.sh`` 的 2/7 和 4/7 都对包的一份
#:   临时副本 ``pip install``,没有它 pip 连构建后端都找不到。
#: * ``src/`` —— ``[tool.setuptools.packages.find] where = ["src"]``,包体在这儿。
#: * ``config/`` —— systemd 单元里 ``WorkingDirectory=-/opt/d1max/current``,
#:   也就是**包目录就是服务的工作目录**。``app/server.py`` 的 ``--params-file``
#:   默认值 ``config/params/mapper_3d.yaml`` 是相对这里解析的
#:   (``app/mapping.py`` 的 ``params_template`` 默认值是同一条路径)。
#:
#: ``app/server.py`` 的 argparse 默认值逐条核过,剩下那几个相对路径**都不该
#: 进包**:``--maps-dir runs/slam``、``--bags-dir runs/bags``、
#: ``--runs-root runs``、``--missions-dir missions`` 全是运行时自己建的输出
#: 目录,把仓库里的同名目录塞进去等于把开发机的录像和任务包一起带上狗。
#: ``--log-dir`` 默认落在 ``<runs-root>/logs`` 下,同理。
#:
#: 已知的**一处**副作用:``conformance.py`` 的 golden fixture 在
#: ``tests/protocol/fixtures`` 下,而 ``tests/`` 不进包(狗上没有 pytest,
#: 离线 wheel 里也没备)。``load_fixtures()`` 对"目录不在"的处理是返回空字典,
#: 所以狗上跑 ``d1max conform`` 只是少了对拍那一段,不会炸。
#: ``deploy`` 是 W01b 加进来的:OTA 升级(``release activate``)要从
#: ``releases/<版本>/deploy/d1max-patrol.service`` 拿单元交给特权助手装到
#: ``/etc/systemd/system``。不进包的话,机器上的单元永远是装机那天的版本。
#: ``packages`` 是 W00b 加的:robot-agent 三个包要在槽 venv 里装。各包的 ``tests/`` 不进
#: (``_PACK_SKIP_DIRS``)。
_PACK_INCLUDE = ("pyproject.toml", "src", "config", "deploy", "packages")

#: 白名单收下的目录里头还要再筛一遍的噪声。**它们是构建/缓存产物,不是源码**,
#: 而且同一份源码带不带它们会算出两个不同的 ``content_sha256``。
#: ``*.egg-info/`` 尤其要紧:pip 就地构建会往源目录里写它(见 install.sh 的
#: 2/7 那段注释),开发机上的 ``src/d1max_patrol.egg-info`` 就是这么来的。
_PACK_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv",
                             ".pytest_cache", ".ruff_cache", ".mypy_cache",
                             "tests"})    # packages/*/tests 狗上跑不了,别占包体
#: 按后缀筛,目录和文件都走这一条(``.egg-info`` 筛的是目录名)。
_PACK_SKIP_SUFFIXES = (".pyc", ".pyo", ".tmp", ".egg-info")

#: 从 ``pyproject.toml`` 的 ``[project]`` 段里抠 ``version``。
_PROJECT_VERSION_RE = re.compile(r"^\s*version\s*=\s*[\"']([^\"']+)[\"']",
                                 re.MULTILINE)

#: 打包时的中转目录名。拷完才改名成正式的包目录 —— 理由同 ``stage()``:
#: 半个包比没有包更坏,它看着像打好了。
_PACKING_DIR = ".packing"


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
    """整棵树的指纹。算法住在契约包(``d1max_contract.digest``,站点收任务包也用它);这里只是
    把默认跳过的自述文件定成 ``release.json``。"""
    return _tree_sha256(root, skip=skip)


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


def _read_project_version(pyproject: Path) -> str:
    """从 ``pyproject.toml`` 的 ``[project]`` 段里读 ``version``。

    **不用 tomllib。** 它是 3.11 才有的,而本项目 ``requires-python = ">=3.10"``,
    狗上的 JetPack / Ubuntu 22.04 正好是 3.10 —— 依赖它等于给打包器加一条
    "只在新 python 上能跑"的隐含前提,而这条前提会在最不该出事的那天出事。

    **段必须先切出来。** ``[build-system]``、``[tool.*]`` 里都可能有同名的
    ``version =``;不切段的话,一个正则会抓到文件里第一个撞上的那行。
    """
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"读不到 pyproject.toml: {pyproject}") from exc
    section = ""
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "[project]":
                break               # [project] 那一段到此为止
            section = stripped
            continue
        if section == "[project]":
            body.append(line)
    found = _PROJECT_VERSION_RE.search("\n".join(body))
    if found is None:
        raise ReleaseError(f"pyproject.toml 的 [project] 段里没有 version: "
                           f"{pyproject}")
    return found.group(1)


def _git_short(src: Path) -> str:
    """``src`` 那棵树的 git 短哈希(8 位小写十六进制)。拿不到就回空串。

    **拿不到不是错。** 打包的源可能是从 tar 解出来的一棵树,机器上也可能压根
    没装 git —— 那时候退到整棵树的指纹前 8 位,包照样打得出来。所以这里
    ``check=False``,任何一种失败都只是"没有短哈希"。
    """
    try:
        done = subprocess.run(          # 命令行是自己拼的,没有 shell
            ["git", "-C", str(src), "rev-parse", "--short=8", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.strip().lower()


def _pack_ignore(_where: str, names: list[str]) -> set[str]:
    """``shutil.copytree`` 的筛子。筛掉的理由见 ``_PACK_SKIP_DIRS``。"""
    return {n for n in names
            if n in _PACK_SKIP_DIRS or n.endswith(_PACK_SKIP_SUFFIXES)}


def _check_free(dest: Path, *, force: bool) -> None:
    """包目录得不在,或者是空的。**不覆盖是默认值。**

    目录名里带着日期和一截哈希,重名意味着"同一天、同一棵树,你已经打过一次
    了" —— 而那一份可能已经拷给现场的人了。默默盖掉它,等于让两个人手里
    同名的包内容不一样,那是对账这套东西最坏的一种坏法。要盖就显式说出口。
    """
    if not dest.exists():
        return
    if not dest.is_dir():
        raise ReleaseError(f"{dest} 已经存在而且不是个目录 —— 挪开它再打")
    if force:
        return
    if any(dest.iterdir()):
        raise ReleaseError(f"{dest} 已经存在而且不是空的 —— 要盖掉就加 --force")


def _copy_tree_into(src: Path, staging: Path, wheels: Path | None = None) -> str:
    """按白名单把 ``src`` 拷进 ``staging``,回整棵树的指纹。

    指纹在这儿算,**也就是拷完之后、写 ``release.json`` 之前**。顺序反了的话
    自述里记的是"含自述的树"的指纹,而 ``verify_package()`` 算的是"不含自述
    的树" —— 打出来的包必然自相矛盾,而且是在 3/7 那一步才被发现。

    ``tree_sha256`` 只把**相对路径**和内容喂进去,不含根目录自己的名字,
    所以这里对中转目录算出来的值,改名之后对正式包目录算还是同一个。
    """
    staging.mkdir(parents=True)
    for entry in _PACK_INCLUDE:
        source = src / entry
        if source.is_dir():
            shutil.copytree(source, staging / entry, ignore=_pack_ignore)
        elif source.is_file():
            shutil.copy2(source, staging / entry)
        else:
            raise ReleaseError(f"源目录里少了 {entry},这棵树打出来的包"
                               f"在狗上起不来: {source}")
    if wheels is not None:
        # 离线轮子(W00c5d 第三部分内部评审):现场的狗上不了网,站点下发的版本建 venv 只能从包里
        # 带的轮子装(``release_ops.build_venv`` 见到 ``wheels/`` 就 ``--no-index`` 只从它装)。
        whl = sorted(Path(wheels).glob("*.whl"))
        if not whl:
            raise ReleaseError(f"{wheels} 里没有 .whl")
        (staging / "wheels").mkdir()
        for w in whl:
            shutil.copy2(w, staging / "wheels" / w.name)
    return tree_sha256(staging)


def _write_manifest(where: Path, manifest: ReleaseManifest) -> None:
    """把自述落进包目录。**先写 .tmp 再 os.replace**,理由同 ``write_pending``。

    ``open(p, "w")`` 是先截断再写:write 在中间抛一次(盘满、U 盘拔了),盘上
    就留下一个 0 字节的 ``release.json`` —— 而 ``read_manifest`` 对着它报的是
    "不是合法 JSON",跟真正的原因差着十万八千里。
    """
    tmp = where / (MANIFEST_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest.to_wire(), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, where / MANIFEST_NAME)


def pack(src: Path | str, out_parent: Path | str, *, name: str | None = None,
         version: str | None = None, now_ms: int, force: bool = False,
         wheels: Path | str | None = None) -> Path:
    """把一棵源码树打成一个 ``deploy/install.sh`` 收得下的包目录。

    **这是造包的唯一真理源。** 在这之前,全仓库唯一会造包的是测试里的私有
    夹具,于是 ``release install`` 要的那份 ``release.json`` 在真机上根本没有
    来源 —— 装机必定停在 3/7,而那时 1/7、2/7 已经往 ``/opt/d1max`` 写过东西了。

    **它跑在笔记本上,不跑在狗上。** 输入是仓库,输出是一个拷到 U 盘、再拿去
    ``install.sh`` 的目录。

    几条不能松的规矩:

    * **指纹只有一个算法。** ``content_sha256`` 走的是本模块的 ``tree_sha256``,
      跟 ``verify_package()`` 校验时用的是同一个函数。两边各写一遍的那天,
      整套对账就废了。
    * **``requires_mission_schema`` 复用 ``bundle.BUNDLE_SCHEMA``**,不在这儿
      写第二个字面量 —— §7.2 的升级判据靠它对账,出现第二个真理源就是一个
      不会报错的升级误判。
    * **打完自己验一遍。** 一个造得出"验不过的包"的打包器等于没有;验不过就
      把半成品删掉再报错,绝不交出去。

    :param src: 源码树,一般就是仓库根。必须有 ``pyproject.toml``。
    :param out_parent: 放包的**父**目录 —— 包建在它底下。
    :param name: 包目录名。默认 ``<今天>-<git 短哈希 8 位>``;不是 git 仓库
        (或者机器上没有 git)就退到整棵树指纹的前 8 位。两条路都过 ``safe_name``。
    :param version: 默认从 ``src/pyproject.toml`` 的 ``[project] version`` 读。
    :param now_ms: 打包时刻。日期和 ``built_at`` 都从它来,**可注入**。
    :param force: 输出目录已经存在且非空时照打(先删掉它)。默认不覆盖。
    :param wheels: 离线轮子目录(``*.whl``,要有 setuptools、wheel 与全部依赖,跟狗的
        Python 与架构对得上)。给了就拷进包的 ``wheels/``,算进指纹。
    :return: 打好的包目录。
    """
    # **局部 import,不是随手写的。** ``bundle.py`` 在模块顶部
    # ``from .release import point_link, tree_sha256`` —— 这里在顶部反向 import
    # 它就成了一个环,谁先被 import 谁就炸。分层没有被破坏:engine/ 内部互相
    # 用是允许的,只是这一对必须晚一点才连上。
    from d1max_agent.engine.bundle import BUNDLE_SCHEMA

    src = Path(src)
    out_parent = Path(out_parent)
    if not src.is_dir():
        raise ReleaseError(f"源目录不在,或者它不是个目录: {src}")
    pyproject = src / "pyproject.toml"
    if not pyproject.is_file():
        raise ReleaseError(f"源目录里没有 pyproject.toml,这不是一棵能装的树: "
                           f"{src}")
    if version is None:
        version = _read_project_version(pyproject)

    # **显式给的名字在动盘之前就过闸。** 一个带 ``d1max-`` 前缀的名字要是拖到
    # 打完才拒,现场拿到的就是"等了半分钟然后报错",而它本可以是一瞬间。
    if name is not None:
        safe_name(name)
        _check_free(out_parent / name, force=force)

    stamp = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    out_parent.mkdir(parents=True, exist_ok=True)
    staging = out_parent / _PACKING_DIR
    shutil.rmtree(staging, ignore_errors=True)
    try:
        content = _copy_tree_into(src, staging, None if wheels is None else Path(wheels))
        if name is None:
            # git 短哈希优先:它认得出"这包是哪次提交打的",内容哈希认不出。
            name = f"{stamp.strftime('%Y-%m-%d')}-{_git_short(src) or content[:8]}"
            safe_name(name)
            _check_free(out_parent / name, force=force)
        dest = out_parent / name
        # 走得到这儿要么 dest 不在,要么是空目录,要么 force —— 三种都能推平。
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)
    except (OSError, ReleaseError):
        shutil.rmtree(staging, ignore_errors=True)
        raise

    manifest = ReleaseManifest(
        name=name,
        version=version,
        content_sha256=content,
        requires_mission_schema=BUNDLE_SCHEMA,
        built_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    try:
        _write_manifest(dest, manifest)
        verify_package(dest)
    except (OSError, ReleaseError):
        # 验不过的包一个字节都不留。留着的话它会被人拷上 U 盘,然后在 3/7
        # 那一步才被拒 —— 那时候现场已经动过盘了。
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


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


#: 开机守卫退回上一版时留的条子(W00c5d 第三部分):代理起来报给站点、删掉。
GUARD_NOTE_FILE = "rolled_back.json"


def write_guard_note(layout: Layout, note: dict) -> None:
    tmp = layout.root / (GUARD_NOTE_FILE + ".tmp")
    tmp.write_text(json.dumps(note, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, layout.root / GUARD_NOTE_FILE)


def take_guard_note(layout: Layout) -> dict | None:
    """读出并删掉守卫留的条子;没有(或读不懂)回 None —— 读不懂也删,不然每次起来都报一遍。"""
    path = layout.root / GUARD_NOTE_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    try:
        got = json.loads(raw)
    except ValueError:
        return {"unreadable": True}
    return got if isinstance(got, dict) else {"unreadable": True}


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

    **槽里还有巡检数据也绝不删。** W01 之前的机器数据落在槽里的
    ``SLOT_DATA_ITEMS`` 那几样底下,迁完之后应该都空了 —— 还有任何一样没空,
    就说明没迁干净,删了数据就没了。
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
        if _has_slot_data(layout.releases / name):
            log.warning("版本 %s 的槽里还有巡检数据,不删 —— 先跑 "
                        "release migrate-data 把数据搬到数据根", name)
            # 到这就止步,不找更新的那版来凑数 —— 该删的是它,不是别的槽。
            break
        shutil.rmtree(layout.releases / name, ignore_errors=True)
        dropped.append(name)
    return tuple(dropped)


def _has_slot_data(slot: Path) -> bool:
    """``SLOT_DATA_ITEMS`` 里任一样还有数据吗:非空文件,或者装着文件的目录。

    ``runs/`` 被手动删掉、但 ``baselines/``、``exports/`` 或 ``queue.jsonl``
    还在的槽也要算 —— 只看 ``runs`` 会让这几样跟着被 ``rmtree`` 一起没了。
    """
    for item in SLOT_DATA_ITEMS:
        p = slot / item
        if p.is_file():
            if p.stat().st_size > 0:
                return True
            continue
        if p.is_dir() and any(q.is_file() for q in p.rglob("*")):
            return True
    return False


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
        # 退了要让站点知道(W00c5d 第三部分内部评审):守卫只写日志的话,管理员只看得见「版本没变」。
        # 留一张条子,代理起来连上站点时报 ``release_rolled_back``、删掉条子。
        write_guard_note(layout, {"from": pending.to, "to": pending.src,
                                  "attempts": pending.attempts, "at_ms": now_ms})
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
