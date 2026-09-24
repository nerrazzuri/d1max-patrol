"""导出通道:打包、算哈希、对上了才放行(spec §4.6 第 2 条 + §4.2 的哈希规矩)。

**没有导出通道的自动删除,等于系统单方面销毁客户资产。** 这一层不是锦上
添花,它是水位删除能存在的前提。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from d1max_agent.engine.export import (
    EXPORTS_DIR_NAME,
    ExportBundle,
    ExportError,
    build_export,
    confirm_bundle,
    list_bundles,
    pick_runs,
    read_bundle,
    sha256_file,
)
from d1max_agent.engine.retention import EXPORTED_REL, UPLOADED_REL, RunInfo

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _run(root: Path, mission: str, days_ago: float, *, size: int = 40,
         settled: bool = True) -> RunInfo:
    started = NOW - timedelta(days=days_ago)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    run = root / mission / stamp
    (run / "photos").mkdir(parents=True)
    (run / "photos" / "P1__front__x.jpg").write_bytes(b"j" * size)
    (run / "events.jsonl").write_text('{"e":1}\n', encoding="utf-8")
    return RunInfo(path=run, mission=mission, started_at=started,
                   retention_days=90, size_bytes=size, settled=settled,
                   uploaded=False)


@pytest.fixture
def out_dir(tmp_path) -> Path:
    return tmp_path / EXPORTS_DIR_NAME


# ------------------------------------------------------------- 挑区间


def test_只挑区间内的(tmp_path):
    a = _run(tmp_path, "甲", 10.0)
    b = _run(tmp_path, "乙", 5.0)
    c = _run(tmp_path, "丙", 1.0)
    got = pick_runs([a, b, c], start=NOW - timedelta(days=7),
                    end=NOW - timedelta(days=2))
    assert [r.mission for r in got] == ["乙"]


def test_区间左闭右开(tmp_path):
    a = _run(tmp_path, "甲", 5.0)
    assert pick_runs([a], start=a.started_at,
                     end=a.started_at + timedelta(seconds=1)) == (a,)
    assert pick_runs([a], start=a.started_at - timedelta(seconds=1),
                     end=a.started_at) == ()


def test_挑出来的最老在前(tmp_path):
    a, b, c = (_run(tmp_path, "甲", 10.0), _run(tmp_path, "乙", 1.0),
               _run(tmp_path, "丙", 5.0))
    got = pick_runs([a, b, c], start=NOW - timedelta(days=30), end=NOW)
    assert [r.mission for r in got] == ["甲", "丙", "乙"]


# --------------------------------------------------------------- 打包


def test_打出来的包里有该有的文件(tmp_path, out_dir):
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    with zipfile.ZipFile(bundle.path) as zf:
        names = set(zf.namelist())
    stamp = a.path.name
    assert f"巡检一号/{stamp}/photos/P1__front__x.jpg" in names
    assert f"巡检一号/{stamp}/events.jsonl" in names


def test_一趟都没有要抛错而不是打个空包(tmp_path, out_dir):
    # 空 zip 也能算出哈希、也能被确认 —— 于是「导出并释放」会在什么都没
    # 导出的情况下走完全流程。那是这一整条链最坏的失败方式。
    with pytest.raises(ExportError) as e:
        build_export([], out_dir=out_dir, now=NOW)
    assert "一趟" in str(e.value)


def test_还在写的那一趟混在区间里就整包抛错(tmp_path, out_dir):
    """**这条是会真丢数据的那条。**

    狗 10:00 出发正在写这一趟,10:30 有人导了一个包含今天的区间:包里是当时
    盘上的 3 张照片和半行 ``events.jsonl``;客户端重算 sha256 **对得上**
    (对的是那个半截包本身),确认于是成立,原件被标成"别处还有一份";
    12:00 狗跑完写下 40 张照片;次日盘过水位,整个目录被删。**37 张照片
    永久消失,而系统全程认为还有一份。**

    闸门在 ``build_export`` 里,不在 app 里 —— 闸门只该有一份。
    """
    安定 = _run(tmp_path, "甲", 5.0)
    还在写 = _run(tmp_path, "乙", 0.1, settled=False)
    with pytest.raises(ExportError) as e:
        build_export([安定, 还在写], out_dir=out_dir, now=NOW)
    assert "还在写" in str(e.value)
    assert f"乙/{还在写.path.name}" in str(e.value)
    # 抛了就什么都不留:半截包不许躺在导出目录里等人下载。
    assert list_bundles(out_dir) == []


def test_全都安定的照旧导得出(tmp_path, out_dir):
    a, b = _run(tmp_path, "甲", 10.0), _run(tmp_path, "乙", 5.0)
    bundle = build_export([a, b], out_dir=out_dir, now=NOW)
    assert len(bundle.runs) == 2
    assert bundle.path.is_file()


def test_哈希跟直接算文件一致(tmp_path, out_dir):
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    assert bundle.sha256 == sha256_file(bundle.path)
    assert bundle.sha256 == hashlib.sha256(bundle.path.read_bytes()).hexdigest()
    assert bundle.size_bytes == bundle.path.stat().st_size


def test_侧写记下了该记的(tmp_path, out_dir):
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    raw = json.loads((out_dir / f"{bundle.name}.json").read_text(encoding="utf-8"))
    assert raw["sha256"] == bundle.sha256
    assert raw["runs"] == [f"巡检一号/{a.path.name}"]
    assert raw["confirmed"] is False


def test_同一秒导两次不会互相覆盖(tmp_path, out_dir):
    a = _run(tmp_path, "甲", 5.0)
    b = _run(tmp_path, "乙", 5.0)
    one = build_export([a], out_dir=out_dir, now=NOW)
    two = build_export([b], out_dir=out_dir, now=NOW)
    assert one.name != two.name
    assert one.path.is_file() and two.path.is_file()


def test_读回来跟打包时一样(tmp_path, out_dir):
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    got = read_bundle(out_dir, bundle.name)
    assert isinstance(got, ExportBundle)
    assert got.sha256 == bundle.sha256
    assert got.runs == bundle.runs


def test_列包新的在前(tmp_path, out_dir):
    a = _run(tmp_path, "甲", 5.0)
    one = build_export([a], out_dir=out_dir, now=NOW)
    two = build_export([a], out_dir=out_dir, now=NOW + timedelta(seconds=1))
    assert [b.name for b in list_bundles(out_dir)] == [two.name, one.name]


def test_没有导出目录就是空的(tmp_path):
    assert list_bundles(tmp_path / "从来没有过") == []


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b.zip", "别的.txt", "",
                                 "export-x.zip\x00"])
def test_包名不合法一律抛错(out_dir, bad):
    # 这个名字后面要从 HTTP 上收进来。不卡住的话它就是一条目录穿越。
    with pytest.raises(ExportError):
        read_bundle(out_dir, bad)


# --------------------------------------------------------------- 确认


def test_哈希对上了才算确认(tmp_path, out_dir):
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    got = confirm_bundle(out_dir, bundle.name, bundle.sha256, runs_root=tmp_path)
    assert got.confirmed is True
    assert read_bundle(out_dir, bundle.name).confirmed is True


def test_确认之后run上打了导到客户手里的标记(tmp_path, out_dir):
    # **打的是 ``.exported``,不是 ``.uploaded``。** 后者的含义是"传到服务器
    # 了",而人工导出证明不了那件事 —— 混用会在"有服务器"档下删掉服务器上
    # 还没有的归档(见 ``tests/engine/test_retention_sweep.py``)。
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    confirm_bundle(out_dir, bundle.name, bundle.sha256, runs_root=tmp_path)
    assert (a.path / EXPORTED_REL).is_file()
    assert not (a.path / UPLOADED_REL).exists()


def test_哈希对不上就抛错而且什么都不改(tmp_path, out_dir):
    # 对不上说明路上掉了字节。这种时候删掉就是真的没了。
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    with pytest.raises(ExportError) as e:
        confirm_bundle(out_dir, bundle.name, "0" * 64, runs_root=tmp_path)
    assert "对不上" in str(e.value)
    assert read_bundle(out_dir, bundle.name).confirmed is False
    assert not (a.path / EXPORTED_REL).exists()
    assert not (a.path / UPLOADED_REL).exists()


def test_重复确认不算错(tmp_path, out_dir):
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    confirm_bundle(out_dir, bundle.name, bundle.sha256, runs_root=tmp_path)
    got = confirm_bundle(out_dir, bundle.name, bundle.sha256, runs_root=tmp_path)
    assert got.confirmed is True


def test_确认一个不存在的包要抛错(tmp_path, out_dir):
    out_dir.mkdir(parents=True)
    with pytest.raises(ExportError):
        confirm_bundle(out_dir, "export-20260901T120000Z.zip", "0" * 64,
                       runs_root=tmp_path)


def test_run目录已经不在了确认照样成立(tmp_path, out_dir):
    # 人先导出、再手工删了目录、最后才回报哈希。包已经在客户手里了,不能
    # 因为原目录不在就判确认失败。
    a = _run(tmp_path, "巡检一号", 5.0)
    bundle = build_export([a], out_dir=out_dir, now=NOW)
    import shutil
    shutil.rmtree(a.path)
    assert confirm_bundle(out_dir, bundle.name, bundle.sha256,
                          runs_root=tmp_path).confirmed is True
