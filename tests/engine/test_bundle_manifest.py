"""一个任务包的自述。**身份 + 完整性,别的都不在这儿。**"""

from __future__ import annotations

import pytest
import yaml

from d1max_patrol.engine.bundle import (
    BUNDLE_MANIFEST,
    BUNDLE_SCHEMA,
    MAX_VERSION,
    BundleError,
    dump_manifest,
    parse_manifest,
    read_manifest,
    write_manifest,
)

好哈希 = "a" * 64


def 好的(**改动) -> dict:
    raw = {
        "bundle_id": "site-kl-tower",
        "version": 7,
        "schema": 1,
        "content_sha256": 好哈希,
        "built_at": "2026-09-07T14:03:00+08:00",
        "built_by": "ops-01",
    }
    raw.update(改动)
    return raw


def test_一份好的能解出来():
    m = parse_manifest(好的())
    assert m.bundle_id == "site-kl-tower"
    assert m.version == 7
    assert m.schema == 1
    assert m.content_sha256 == 好哈希
    assert m.built_by == "ops-01"


def test_built_by可以不写():
    """打包的人是谁是**追责**用的,不是校验用的。缺了不该拦住一个好包。"""
    raw = 好的()
    del raw["built_by"]
    assert parse_manifest(raw).built_by == ""


def test_built_by写成null也当没写():
    assert parse_manifest(好的(built_by=None)).built_by == ""


@pytest.mark.parametrize("少了", ["bundle_id", "version", "schema",
                                 "content_sha256", "built_at"])
def test_少一样都不行(少了):
    raw = 好的()
    del raw[少了]
    with pytest.raises(BundleError, match=少了):
        parse_manifest(raw)


@pytest.mark.parametrize("坏id", ["", "Site-KL", "site kl", "../etc",
                                 "site/kl", "a", "站点"])
def test_bundle_id不合规就炸(坏id):
    """``bundle_id`` **会被拼进路径**(``bundles/<id>-<version>/``)。
    这道闸同时管「像个名字」和「进不了上一级目录」两件事。
    """
    with pytest.raises(BundleError, match="bundle_id"):
        parse_manifest(好的(bundle_id=坏id))


def test_version是时间戳就点名说它像时间戳():
    """**上界就是拿来拦这个的。**

    报错只说「太大了」的话,写包的人会把它改成 999998 再试一次;点名说
    「看着像时间戳」,他才会去想为什么不让用时间戳(§3.2:单调递增的整数,
    因为两台机器的钟不一样,而版本号的先后必须是确定的)。
    """
    with pytest.raises(BundleError, match="时间戳"):
        parse_manifest(好的(version=1757222580))


def test_version上界正好():
    assert parse_manifest(好的(version=MAX_VERSION)).version == MAX_VERSION
    with pytest.raises(BundleError):
        parse_manifest(好的(version=MAX_VERSION + 1))


@pytest.mark.parametrize("坏版本", [0, -1, "7", 7.0, None, [7]])
def test_version只收整数(坏版本):
    with pytest.raises(BundleError, match="version"):
        parse_manifest(好的(version=坏版本))


def test_version不收布尔():
    """``isinstance(True, int)`` 是真的。不拦的话 ``version: true`` 会被
    当成第 1 版收下,而 YAML 里 ``version: yes`` 就长这样。
    """
    with pytest.raises(BundleError, match="version"):
        parse_manifest(好的(version=True))


@pytest.mark.parametrize("坏哈希", ["", "abc", "A" * 64, "g" * 64, "a" * 63,
                                   "a" * 65, 123])
def test_哈希得是64位小写十六进制(坏哈希):
    with pytest.raises(BundleError, match="content_sha256"):
        parse_manifest(好的(content_sha256=坏哈希))


def test_schema至少是1():
    with pytest.raises(BundleError, match="schema"):
        parse_manifest(好的(schema=0))


def test_built_at必须带时区():
    """**这一卷的通则:时刻不带时区就不收。**

    裸时刻会被按读它那台机器的时区解释 —— 而包是从服务器发到狗上的,
    两边的系统时区本来就不保证一样。
    """
    with pytest.raises(BundleError, match="时区"):
        parse_manifest(好的(built_at="2026-09-07T14:03:00"))


def test_built_at得是个能解的时刻():
    with pytest.raises(BundleError, match="built_at"):
        parse_manifest(好的(built_at="昨天下午"))


def test_槽名是id加版本():
    """``bundles/<bundle_id>-<version>/`` —— 落盘的目录名(定夺 4)。"""
    assert parse_manifest(好的()).slot_name == "site-kl-tower-7"


def test_不是字典就炸():
    with pytest.raises(BundleError, match="bundle.yaml"):
        parse_manifest(["site-kl-tower"])                  # type: ignore[arg-type]


def test_多出来的键不收():
    """包是从网上来的。多一个没人认识的键,要么是版本对不上,要么是有人在
    试探 —— 两种都该在解析这一步停住,而不是被静静忽略。
    """
    with pytest.raises(BundleError, match="firmware_url"):
        parse_manifest(好的(firmware_url="http://x/y.bin"))


def test_出去再进来是同一个():
    m = parse_manifest(好的())
    assert parse_manifest(yaml.safe_load(dump_manifest(m))) == m


def test_落盘再读回来是同一个(tmp_path):
    m = parse_manifest(好的())
    write_manifest(tmp_path, m)
    assert (tmp_path / BUNDLE_MANIFEST).exists()
    assert read_manifest(tmp_path) == m


def test_没有bundle_yaml的目录读不出来(tmp_path):
    with pytest.raises(BundleError, match=BUNDLE_MANIFEST):
        read_manifest(tmp_path)


def test_bundle_yaml是坏yaml(tmp_path):
    (tmp_path / BUNDLE_MANIFEST).write_text("a: [1,\n", encoding="utf-8")
    with pytest.raises(BundleError, match=BUNDLE_MANIFEST):
        read_manifest(tmp_path)


def test_上线的形状():
    assert parse_manifest(好的()).to_wire() == {
        "bundle_id": "site-kl-tower", "version": 7, "schema": 1,
        "content_sha256": 好哈希, "built_at": "2026-09-07T14:03:00+08:00",
        "built_by": "ops-01", "targets": [],
    }


def test_当前的schema是1():
    """§7.2 的 ``requires_mission_schema`` 跟这个数对账。改它是**破坏兼容**。"""
    assert BUNDLE_SCHEMA == 1


def test_自述是冻的():
    m = parse_manifest(好的())
    with pytest.raises((AttributeError, TypeError)):
        m.version = 8                                      # type: ignore[misc]


# ---------------------------------------------------------- 目标 SN(定夺 13)


def test_没写targets就是不限():
    """**空元组是默认值,而且它的意思是「谁都能用」。**

    单机站点、和打给一整批狗的通用包,都不该被逼着列 SN。
    """
    m = parse_manifest(好的())
    assert m.targets == ()
    assert m.accepts("D1M-0001")
    assert m.accepts("")


def test_写了targets就只认那几台():
    m = parse_manifest(好的(targets=["D1M-0001", "D1M-0002"]))
    assert m.targets == ("D1M-0001", "D1M-0002")
    assert m.accepts("D1M-0002")
    assert not m.accepts("D1M-0003")


def test_targets里比大小写和连字符():
    """**逐字相等,不做归一化**(定夺 13)。

    在包这一层再做一次「聪明的」匹配,会让「为什么这台换不上去」变成一个
    要读两处代码才答得出的问题。
    """
    m = parse_manifest(好的(targets=["D1M-0001"]))
    assert not m.accepts("d1m-0001")
    assert not m.accepts("D1M0001")


def test_targets得是个列表():
    with pytest.raises(BundleError, match="targets"):
        parse_manifest(好的(targets="D1M-0001"))


@pytest.mark.parametrize("坏", [
    ["D1M-0001", ""],           # 空串
    ["D1M-0001", " D1M-2 "],    # 带空白 —— clean_sn() 那头是 strip 过的
    ["D1M-0001", 7],            # 不是字符串
    ["D1M-0001", "a\x00b"],     # NUL:设备树读出来没收拾干净
    ["x" * 65],                 # 长得不像 SN
])
def test_targets里的每一条都得像个SN(坏):
    with pytest.raises(BundleError, match="targets"):
        parse_manifest(好的(targets=坏))


def test_targets里重复的会被拒():
    """重复不致命,但它说明这份包是拼出来的而不是生成的 —— 那种包值得看一眼。"""
    with pytest.raises(BundleError, match="targets"):
        parse_manifest(好的(targets=["D1M-0001", "D1M-0001"]))


def test_targets进上线也进哈希前的自述():
    m = parse_manifest(好的(targets=["D1M-0001"]))
    assert m.to_wire()["targets"] == ["D1M-0001"]
    assert parse_manifest(yaml.safe_load(dump_manifest(m))) == m
