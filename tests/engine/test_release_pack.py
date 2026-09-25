"""造包的那一头。

``test_release.py`` 盖的是"读"那一半(校验、落槽、换链),这份盖的是"写"
那一半:``pack()``。**分成两份文件是有意的** —— 造包跑在笔记本上,落槽跑在
狗上,两边的失败方式和现场处置完全不同。

这里最要紧的一条是 ``test_打出来的包过得了verify_package``:打包器和校验器
要是对不上,现场看到的是 ``install.sh`` 停在 3/7,而那时 1/7、2/7 已经往
``/opt/d1max`` 写过东西了。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from d1max_agent.engine import release
from d1max_agent.engine.bundle import BUNDLE_SCHEMA
from d1max_agent.engine.release import (
    MANIFEST_NAME,
    ReleaseError,
    pack,
    read_manifest,
    tree_sha256,
    verify_package,
)

#: 打包时刻,固定住 —— 包名里有日期,不固定的话测试会在跨 UTC 零点时翻脸。
NOW_MS = 1_789_000_000_000
#: 上面那个时刻的 UTC 日期。
今天 = "2026-09-10"

#: 源码树里这些东西一个都不许进包。``refs/`` 是厂商私有协议文档,``dist/``
#: 里是 51MB 的 apk 和离线 wheel,``tests/`` 狗上跑不了(没有 pytest)。
噪声 = (
    "refs/x.md",
    "dist/y.bin",
    "tests/z.py",
    "docs/d.md",
    "mobile/m.dart",
    "runs/r.json",
    "missions/m.yaml",
    ".git/HEAD",
    "src/d1max_patrol/__pycache__/a.pyc",
    "src/d1max_patrol.egg-info/PKG-INFO",
    "src/半成品.tmp",
)


def _源码树(root: Path, *, version: str = "0.3.0") -> Path:
    """造一棵长得像这个仓库的源码树:白名单要的三样,外加一堆该被排掉的噪声。"""
    root.mkdir(parents=True, exist_ok=True)
    # [build-system] 和 [tool.*] 里都埋一个假的 version,证明读的是 [project] 段。
    (root / "pyproject.toml").write_text(
        '[build-system]\n'
        'requires = ["setuptools>=68"]\n'
        'version = "9.9.9-假的"\n'
        '\n'
        '[project]\n'
        'name = "d1max-patrol"\n'
        f'version = "{version}"\n'
        'requires-python = ">=3.10"\n'
        '\n'
        '[tool.ruff]\n'
        'version = "8.8.8-也是假的"\n',
        encoding="utf-8")
    (root / "src" / "d1max_patrol").mkdir(parents=True, exist_ok=True)
    (root / "src" / "d1max_patrol" / "__init__.py").write_text(
        "", encoding="utf-8")
    (root / "config" / "params").mkdir(parents=True, exist_ok=True)
    (root / "config" / "params" / "mapper_3d.yaml").write_text(
        "resolution: 0.05\n", encoding="utf-8")
    # W01b:deploy/ 也进包 —— OTA 升上来的机器要靠包里这份单元和助手更新自己。
    (root / "deploy").mkdir(parents=True, exist_ok=True)
    for 名 in ("d1max-patrol.service", "d1max-privileged", "d1max-restart-now",
              "sudoers-d1max", "install.sh"):
        (root / "deploy" / 名).write_text(f"# {名}\n", encoding="utf-8")
    # W00b:packages/ 也进包(robot-agent 装进槽 venv);各包的 tests/ 不进。
    for 包, 模块 in (("contract", "d1max_contract"), ("adapter-sim", "d1max_adapter_sim"),
                   ("robot-agent", "d1max_agent")):
        (root / "packages" / 包 / "src" / 模块).mkdir(parents=True, exist_ok=True)
        (root / "packages" / 包 / "src" / 模块 / "__init__.py").write_text("", encoding="utf-8")
        (root / "packages" / 包 / "pyproject.toml").write_text(f'[project]\nname = "{包}"\n',
                                                              encoding="utf-8")
        (root / "packages" / 包 / "tests").mkdir(parents=True, exist_ok=True)
        (root / "packages" / 包 / "tests" / "test_x.py").write_text("", encoding="utf-8")
    for rel in 噪声:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("不该进包的东西\n", encoding="utf-8")
    return root


@pytest.fixture()
def 无git(monkeypatch):
    """把 git 短哈希这条路掐掉,逼 ``pack()`` 走内容哈希兜底。

    **测试不能靠"tmp_path 恰好不在某个 git 仓库里"。** ``git rev-parse`` 会
    一路往上找,谁把 basetemp 挪进仓库里,包名就跟着变了。
    """
    monkeypatch.setattr(release, "_git_short", lambda _src: "")


# ----------------------------------------------------------- 最要紧的那一条


def test_打出来的包过得了verify_package(tmp_path, 无git):
    """``install.sh`` 的 3/7 就是这一关。过不了的话装机停在写过盘之后。"""
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    manifest = verify_package(dest)
    assert manifest.name == dest.name


def test_包名形状对而且跟自述里的name一字不差(tmp_path, 无git):
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    # _NAME_RE 是 verify_package 和 install.sh 共同认的那个形状。
    assert release._NAME_RE.match(dest.name), dest.name
    assert dest.name.startswith(今天 + "-")
    assert read_manifest(dest).name == dest.name


def test_没有git时名字退到内容哈希的前八位(tmp_path, 无git):
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    manifest = read_manifest(dest)
    assert dest.name == f"{今天}-{manifest.content_sha256[:8]}"


def test_有git时优先用git短哈希(tmp_path, monkeypatch):
    """短哈希认得出"这包是哪次提交打的",内容哈希认不出。"""
    monkeypatch.setattr(release, "_git_short", lambda _src: "3ad60eff")
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    assert dest.name == f"{今天}-3ad60eff"
    verify_package(dest)


# ----------------------------------------------------------- 自述里那几个字段


def test_version真的来自pyproject(tmp_path, 无git):
    """两棵只有版本号不同的树,打出来的自述必须跟着变 —— 写死就红。"""
    甲 = pack(_源码树(tmp_path / "甲", version="0.3.0"), tmp_path / "出甲",
             now_ms=NOW_MS)
    乙 = pack(_源码树(tmp_path / "乙", version="7.42.1"), tmp_path / "出乙",
             now_ms=NOW_MS)
    assert read_manifest(甲).version == "0.3.0"
    assert read_manifest(乙).version == "7.42.1"


def test_version读的是project段不是别的段(tmp_path, 无git):
    """``[build-system]`` 和 ``[tool.ruff]`` 里埋着假的 version(见 _源码树)。"""
    dest = pack(_源码树(tmp_path / "树", version="0.3.0"), tmp_path / "出",
                now_ms=NOW_MS)
    assert read_manifest(dest).version == "0.3.0"


def test_schema跟bundle里那个常量对账(tmp_path, 无git):
    """**故意不把数字抄进来。** 抄进来就是第二个真理源,§7.2 的升级判据
    (requires_mission_schema 跟盘上的包对账)会因此静默地判错一次升级。
    改了 ``bundle.BUNDLE_SCHEMA`` 而没改 ``pack()``,这条当场红。
    """
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    assert read_manifest(dest).requires_mission_schema == BUNDLE_SCHEMA


def test_built_at是UTC的ISO8601(tmp_path, 无git):
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    assert read_manifest(dest).built_at == "2026-09-10T00:26:40Z"


def test_显式给的版本号赢过pyproject(tmp_path, 无git):
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS,
                version="1.2.3")
    assert read_manifest(dest).version == "1.2.3"


# ----------------------------------------------------------- 包里装了什么


def test_包里只有白名单那五样加一份自述(tmp_path, 无git):
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    assert {p.name for p in dest.iterdir()} == {
        "pyproject.toml", "src", "config", "deploy", "packages", MANIFEST_NAME}


def test_三个包随包走但它们的tests不进(tmp_path, 无git):
    """W00b:装机 4/7 要在槽 venv 里 pip install 这三个包;tests/ 狗上跑不了,别占包体。"""
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    for 包 in ("contract", "adapter-sim", "robot-agent"):
        assert (dest / "packages" / 包 / "pyproject.toml").is_file(), 包
        assert not (dest / "packages" / 包 / "tests").exists(), f"{包}/tests 不该进包"


def test_单元文件和特权助手随包走(tmp_path, 无git):
    """W01b:``deploy/`` 原来不进包,OTA 升上来的机器单元永远是装机那天的版本
    (没有 D1MAX_DATA_ROOT、StartLimitIntervalSec 在错的段)。``release activate``
    要从 ``releases/<版本>/deploy/`` 里拿单元交给特权助手装,所以它必须在包里。"""
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    assert (dest / "deploy" / "d1max-patrol.service").is_file()
    assert (dest / "deploy" / "d1max-privileged").is_file()
    assert (dest / "deploy" / "sudoers-d1max").is_file()
    assert (dest / "deploy" / "d1max-restart-now").is_file()


def test_服务要的那份建图参数在包里(tmp_path, 无git):
    """systemd 单元里 WorkingDirectory 就是包目录,而 app/server.py 的
    ``--params-file`` 默认值是相对它解析的 ``config/params/mapper_3d.yaml``。
    """
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    assert (dest / "config" / "params" / "mapper_3d.yaml").is_file()
    assert (dest / "src" / "d1max_patrol" / "__init__.py").is_file()


def test_该排掉的东西一个都没进包(tmp_path, 无git):
    """漏排一个 ``refs/`` 就是把厂商私有资料装进了客户的机器,而且没人会发现。"""
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    进包了 = {p.relative_to(dest).as_posix() for p in dest.rglob("*")}
    for rel in 噪声:
        assert rel not in 进包了, rel
    assert not [p for p in dest.rglob("*") if p.name == "__pycache__"]
    assert not [p for p in dest.rglob("*") if p.name.endswith(".egg-info")]
    assert not [p for p in dest.rglob("*.pyc")]


# ----------------------------------------------------------- 落盘那几条规矩


def test_同一棵树打两遍指纹一样(tmp_path, 无git):
    """确定性。两次打出来的 content_sha256 不一样的话,"包有没有被动过"
    这个问题就再也答不了了。
    """
    树 = _源码树(tmp_path / "树")
    甲 = pack(树, tmp_path / "出甲", now_ms=NOW_MS)
    乙 = pack(树, tmp_path / "出乙", now_ms=NOW_MS + 86_400_000)
    assert read_manifest(甲).content_sha256 == read_manifest(乙).content_sha256


def test_content_sha256是拷完之后写自述之前算的(tmp_path, 无git):
    """算错顺序的包自己跟自己对不上。这里直接拿校验器那一侧再算一遍。"""
    dest = pack(_源码树(tmp_path / "树"), tmp_path / "出", now_ms=NOW_MS)
    assert read_manifest(dest).content_sha256 == tree_sha256(dest)


def test_输出目录已经有东西就不打(tmp_path, 无git):
    出 = tmp_path / "出"
    dest = 出 / f"{今天}-deadbeef"
    dest.mkdir(parents=True)
    (dest / "别人的包").write_text("别动我\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="--force"):
        pack(_源码树(tmp_path / "树"), 出, name=dest.name, now_ms=NOW_MS)
    assert (dest / "别人的包").is_file()


def test_加了force才盖(tmp_path, 无git):
    出 = tmp_path / "出"
    dest = 出 / f"{今天}-deadbeef"
    dest.mkdir(parents=True)
    (dest / "别人的包").write_text("别动我\n", encoding="utf-8")
    got = pack(_源码树(tmp_path / "树"), 出, name=dest.name, now_ms=NOW_MS,
               force=True)
    assert got == dest
    assert not (dest / "别人的包").exists()
    verify_package(dest)


def test_名字不合规当场报错而且一个包都没造出来(tmp_path, 无git):
    """``d1max-`` 前缀正是 install.sh 那段注释点名的坏法:_NAME_RE 不认它,
    而带着它的包会一路被拷上 U 盘,到 3/7 才被拒。
    """
    出 = tmp_path / "出"
    with pytest.raises(ReleaseError, match="版本名不合规"):
        pack(_源码树(tmp_path / "树"), 出, name="d1max-2026-09-10-abcdef",
             now_ms=NOW_MS)
    assert not 出.exists() or list(出.iterdir()) == []


def test_打完不留中转目录(tmp_path, 无git):
    出 = tmp_path / "出"
    dest = pack(_源码树(tmp_path / "树"), 出, now_ms=NOW_MS)
    assert {p.name for p in 出.iterdir()} == {dest.name}


def test_源目录里没有pyproject就报错(tmp_path, 无git):
    树 = tmp_path / "树"
    树.mkdir()
    with pytest.raises(ReleaseError, match="pyproject.toml"):
        pack(树, tmp_path / "出", now_ms=NOW_MS)


def test_源目录里缺白名单里的目录就报错(tmp_path, 无git):
    """漏带 config/ 的包能装上、也能起来,然后在第一次建图时才炸。"""
    树 = _源码树(tmp_path / "树")
    shutil.rmtree(树 / "config")
    出 = tmp_path / "出"
    with pytest.raises(ReleaseError, match="config"):
        pack(树, 出, now_ms=NOW_MS)
    assert not 出.exists() or list(出.iterdir()) == []


def test_验不过的包不留在盘上(tmp_path, 无git, monkeypatch):
    """打包器最后自己验一遍。这里把自述写坏,看它有没有把半成品收干净。"""
    真的写 = release._write_manifest

    def 写坏(where, manifest):
        真的写(where, manifest)
        (where / MANIFEST_NAME).write_text(
            json.dumps({**manifest.to_wire(), "content_sha256": "0" * 64}),
            encoding="utf-8")

    monkeypatch.setattr(release, "_write_manifest", 写坏)
    出 = tmp_path / "出"
    with pytest.raises(ReleaseError, match="哈希对不上"):
        pack(_源码树(tmp_path / "树"), 出, now_ms=NOW_MS)
    assert list(出.iterdir()) == []


def test_带离线轮子打包_轮子进包算进指纹_没有轮子就拒(tmp_path, 无git):
    """W00c5d 第三部分内部评审:现场的狗上不了网,站点下发的版本建 venv 只能从包里带的轮子装。"""
    src = _源码树(tmp_path / "src")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "paho_mqtt-2.1.0-py3-none-any.whl").write_bytes(b"whl")
    (wheels / "readme.txt").write_text("不是轮子")
    dest = pack(src, tmp_path / "out", now_ms=NOW_MS, wheels=wheels)
    assert sorted(p.name for p in (dest / "wheels").iterdir()) == \
        ["paho_mqtt-2.1.0-py3-none-any.whl"]
    verify_package(dest)
    plain = pack(src, tmp_path / "out2", now_ms=NOW_MS)
    assert not (plain / "wheels").exists()
    assert read_manifest(dest).content_sha256 != read_manifest(plain).content_sha256
    empty = tmp_path / "空"
    empty.mkdir()
    with pytest.raises(ReleaseError, match="whl"):
        pack(src, tmp_path / "out3", now_ms=NOW_MS, wheels=empty)
