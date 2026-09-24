"""打包与校验。**打包器是库**(§3.8):这个文件里一行 HTTP 都没有。"""

from __future__ import annotations

import json
import shutil

import pytest

from d1max_agent.engine.bundle import (
    BUNDLE_MANIFEST,
    BUNDLE_SCHEMA,
    BundleError,
    build_bundle,
    bundle_sha256,
    read_manifest,
    verify_bundle,
)

时刻 = "2026-09-07T14:03:00+08:00"


def 一个任务() -> dict:
    return {
        "mission": "night",
        "map_id": "floor1",
        # 形状照 ``engine/mission.py`` 的 ``parse_mission`` 来 —— ``_盖章``
        # 会把每个任务真过一遍解析,对不上就打不出包。
        "waypoints": [{
            "name": "P1",
            "pose": {"position": {"x": 1.2, "y": 3.4, "z": 0.0},
                     "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
            "actions": [{"type": "photo", "camera": "front"}],
        }],
        "policy": {},
    }


def 源目录(tmp_path):
    """一棵能打包的树。**这里刻意不写 schema 键** —— 打包器该自己盖上。"""
    src = tmp_path / "src"
    # exist_ok=True:有些用例会用同一个 tmp_path 拿这棵树拿两次(打两遍看
    # 哈希、往同一个槽打第二次) —— 第二次不该因为目录已经在那儿就先炸。
    (src / "missions").mkdir(parents=True, exist_ok=True)
    (src / "maps").mkdir(exist_ok=True)
    (src / "missions" / "night.json").write_text(
        json.dumps(一个任务(), ensure_ascii=False), encoding="utf-8")
    (src / "maps" / "floor1.pgm").write_bytes(b"P5\n2 2\n255\n\x00\x01\x02\x03")
    (src / "schedule.yaml").write_text(
        "timezone: Asia/Kuala_Lumpur\nentries: []\n", encoding="utf-8")
    return src


def 打一个(tmp_path, **改动):
    参 = {"bundle_id": "site-kl", "version": 1, "built_at": 时刻}
    参.update(改动)
    return build_bundle(源目录(tmp_path), tmp_path / "out", **参)


def test_打出来的包在对的目录名下(tmp_path):
    出 = 打一个(tmp_path, version=3)
    assert 出.name == "site-kl-3"
    assert 出.parent.name == "out"


def test_打出来的包自带bundle_yaml(tmp_path):
    出 = 打一个(tmp_path)
    m = read_manifest(出)
    assert m.bundle_id == "site-kl"
    assert m.version == 1
    assert m.schema == BUNDLE_SCHEMA
    assert m.built_at == 时刻


def test_源目录的东西都在(tmp_path):
    出 = 打一个(tmp_path)
    assert (出 / "missions" / "night.json").exists()
    assert (出 / "maps" / "floor1.pgm").exists()
    assert (出 / "schedule.yaml").exists()


def test_任务被盖上schema(tmp_path):
    """§7.4 的 ``mission_schema_floor`` 读的就是这个键。

    源目录里没写它,包里必须有 —— **盖章是打包器的活**,不是写任务的人的活:
    一个手写的任务包不该因为漏了一个版本号就把整台狗的升级判据搞乱。
    """
    出 = 打一个(tmp_path)
    落 = json.loads((出 / "missions" / "night.json").read_text(encoding="utf-8"))
    assert 落["schema"] == BUNDLE_SCHEMA


def test_schema对得上selfcheck那个下限(tmp_path):
    """跟 ``engine/selfcheck.py`` 的 ``mission_schema_floor`` 真的对一遍 ——
    它的 docstring 里写着「等第 5 卷把它真加上,这个函数一个字都不用改」。
    **这条就是那句话的对账。**
    """
    from d1max_agent.engine.selfcheck import mission_schema_floor
    出 = 打一个(tmp_path)
    assert mission_schema_floor(出 / "missions") == BUNDLE_SCHEMA


def test_坏任务打不出包(tmp_path):
    src = 源目录(tmp_path)
    (src / "missions" / "bad.json").write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(BundleError, match="bad.json"):
        build_bundle(src, tmp_path / "out", bundle_id="site-kl", version=1,
                     built_at=时刻)


def test_不是json的任务也打不出包(tmp_path):
    src = 源目录(tmp_path)
    (src / "missions" / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(BundleError, match="broken.json"):
        build_bundle(src, tmp_path / "out", bundle_id="site-kl", version=1,
                     built_at=时刻)


def test_源目录里有不纯的东西就打不出包(tmp_path):
    """**在拷之前拦。** 拷完再拦的话,那个东西已经在盘上了。"""
    src = 源目录(tmp_path)
    (src / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(BundleError, match="install.sh"):
        build_bundle(src, tmp_path / "out", bundle_id="site-kl", version=1,
                     built_at=时刻)
    assert not (tmp_path / "out" / "site-kl-1").exists()


def test_同一个槽不许打第二次(tmp_path):
    打一个(tmp_path, version=1)
    with pytest.raises(BundleError, match="site-kl-1"):
        打一个(tmp_path, version=1)


def test_坏版本号在打包这一步就拦(tmp_path):
    with pytest.raises(BundleError, match="时间戳"):
        打一个(tmp_path, version=1757222580)


def test_哈希不算bundle_yaml自己(tmp_path):
    """自述里存着这个值,算自己是个死循环(定夺 3)。"""
    出 = 打一个(tmp_path)
    前 = bundle_sha256(出)
    (出 / BUNDLE_MANIFEST).write_text("bundle_id: 改过了\n", encoding="utf-8")
    assert bundle_sha256(出) == 前


def test_内容变了哈希就变(tmp_path):
    出 = 打一个(tmp_path)
    前 = bundle_sha256(出)
    (出 / "maps" / "floor1.pgm").write_bytes(b"P5\n2 2\n255\n\xff\xff\xff\xff")
    assert bundle_sha256(出) != 前


def test_改文件名哈希也变(tmp_path):
    """路径进指纹。不进的话,把 a 的内容挪进 b 就是隐形的改动。"""
    出 = 打一个(tmp_path)
    前 = bundle_sha256(出)
    (出 / "maps" / "floor1.pgm").rename(出 / "maps" / "floor2.pgm")
    assert bundle_sha256(出) != 前


def test_打两遍是同一个哈希(tmp_path):
    """**可复现。** 不可复现的话,「服务器和狗算出来的不一样」就永远查不清
    是被改了还是打包器自己抖。
    """
    a = build_bundle(源目录(tmp_path), tmp_path / "o1", bundle_id="site-kl",
                     version=1, built_at=时刻)
    b = build_bundle(源目录(tmp_path), tmp_path / "o2", bundle_id="site-kl",
                     version=1, built_at=时刻)
    assert bundle_sha256(a) == bundle_sha256(b)
    assert read_manifest(a).content_sha256 == read_manifest(b).content_sha256


# ---- 收包这一头 ----------------------------------------------------------

def test_刚打出来的包校验得过(tmp_path):
    出 = 打一个(tmp_path)
    assert verify_bundle(出).bundle_id == "site-kl"


def test_内容被改过就校验不过(tmp_path):
    出 = 打一个(tmp_path)
    # b"中文" 是硬语法错。这儿要的是「字节变了」,不是「中文」。
    (出 / "maps" / "floor1.pgm").write_bytes("改过了".encode())
    with pytest.raises(BundleError, match="content_sha256"):
        verify_bundle(出)


def test_目录被改名就校验不过(tmp_path):
    """§3.2:狗上认的是 ``(bundle_id, version)``。目录名跟自述对不上,
    说明这个包在路上被人动过手脚,或者落盘那一步错了。
    """
    出 = 打一个(tmp_path)
    改 = 出.parent / "site-kl-9"
    出.rename(改)
    with pytest.raises(BundleError, match="site-kl-9"):
        verify_bundle(改)


def test_名字不对先于哈希报出来(tmp_path):
    """改名的包内容也一定不对(哈希还是老的)。**先报名字**,因为那是人
    看得懂的那条;先报哈希只会让人去查一个根本不是根因的东西。
    """
    出 = 打一个(tmp_path)
    改 = 出.parent / "site-kl-9"
    出.rename(改)
    with pytest.raises(BundleError) as e:
        verify_bundle(改)
    assert "content_sha256" not in str(e.value)


def test_塞了脚本进去就校验不过(tmp_path):
    出 = 打一个(tmp_path)
    (出 / "run.py").write_text("import os\n", encoding="utf-8")
    with pytest.raises(BundleError, match="run.py"):
        verify_bundle(出)


def test_纯数据先于哈希查(tmp_path):
    """**顺序是有理由的。** 一个 ``maps/x.pgm -> /etc/shadow`` 的包,
    哈希那一步会去**读**那个链接。先查纯数据,那一步就永远不会发生。
    """
    出 = 打一个(tmp_path)
    (出 / "run.py").write_text("import os\n", encoding="utf-8")
    with pytest.raises(BundleError) as e:
        verify_bundle(出)
    assert "content_sha256" not in str(e.value)


def test_少了bundle_yaml就校验不过(tmp_path):
    出 = 打一个(tmp_path)
    (出 / BUNDLE_MANIFEST).unlink()
    with pytest.raises(BundleError, match=BUNDLE_MANIFEST):
        verify_bundle(出)


def test_空目录校验不过(tmp_path):
    d = tmp_path / "site-kl-1"
    d.mkdir()
    with pytest.raises(BundleError, match=BUNDLE_MANIFEST):
        verify_bundle(d)


def test_拷一份到别处再校验一样过(tmp_path):
    """包是要走网络的。**换个盘、换个路径,校验结果必须一样** ——
    指纹里但凡混进了绝对路径,这一条就会红。
    """
    出 = 打一个(tmp_path)
    另 = tmp_path / "elsewhere" / "site-kl-1"
    另.parent.mkdir()
    shutil.copytree(出, 另)
    assert verify_bundle(另) == read_manifest(出)
