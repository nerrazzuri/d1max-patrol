"""基线集:判读要的那一张,永不删(spec §4.5)。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.baselines import (
    BaselineError,
    baseline_path,
    baselines_bytes,
    list_baselines,
    load_baseline,
    save_baseline,
)


def test_存下去再读回来是同一张(tmp_path):
    save_baseline(tmp_path, "P1", "front", b"\xff\xd8jpegdata")
    assert load_baseline(tmp_path, "P1", "front") == b"\xff\xd8jpegdata"


def test_没存过读回来是None不是抛错(tmp_path):
    # 没有基线是常态(第一次跑这个点位),不是故障 —— 调用方要能平静地退回
    # 去翻历史 run。
    assert load_baseline(tmp_path, "P1", "front") is None


def test_同一点位同一相机是覆盖不是堆积(tmp_path):
    save_baseline(tmp_path, "P1", "front", b"old")
    save_baseline(tmp_path, "P1", "front", b"new")
    assert load_baseline(tmp_path, "P1", "front") == b"new"
    assert len(list_baselines(tmp_path)) == 1


def test_点位和相机一起决定一张基线(tmp_path):
    save_baseline(tmp_path, "P1", "front", b"a")
    save_baseline(tmp_path, "P1", "thermal", b"b")
    save_baseline(tmp_path, "P2", "front", b"c")
    assert load_baseline(tmp_path, "P1", "thermal") == b"b"
    assert sorted(list_baselines(tmp_path)) == [
        ("P1", "front"), ("P1", "thermal"), ("P2", "front")]


def test_基线文件就叫点位加相机(tmp_path):
    assert baseline_path(tmp_path, "P1", "front") == tmp_path / "P1__front.jpg"


@pytest.mark.parametrize("bad", ["/", "\\", "..", "\x00", "__"])
def test_名字里有分隔符或穿越就抛错(tmp_path, bad):
    # 抛错而不是"清洗一下凑合写":清洗过的名字读回来对不上,那才是静默失效。
    with pytest.raises(BaselineError):
        save_baseline(tmp_path, f"P{bad}1", "front", b"x")
    with pytest.raises(BaselineError):
        save_baseline(tmp_path, "P1", f"fr{bad}ont", b"x")


def test_相机名为空也抛错(tmp_path):
    # `_split_photo_name` 拆不开的时候相机名就是空串。给它建一张基线,等于
    # 拿一个日后永远匹配不上的名字占个位。
    with pytest.raises(BaselineError):
        save_baseline(tmp_path, "P1", "", b"x")


def test_写基线是原子的_写坏了不毁掉已有那张(tmp_path):
    save_baseline(tmp_path, "P1", "front", b"good")
    with pytest.raises(TypeError):
        save_baseline(tmp_path, "P1", "front", "不是 bytes")  # type: ignore[arg-type]
    assert load_baseline(tmp_path, "P1", "front") == b"good"
    assert not list(tmp_path.glob("*.tmp"))


def test_读一个坏掉的目录不炸(tmp_path):
    # 目录被删了、盘掉了 —— 判读不该因此整趟失败。
    assert list_baselines(tmp_path / "根本不存在") == []
    assert baselines_bytes(tmp_path / "根本不存在") == 0


def test_占用字节算的是所有基线之和(tmp_path):
    save_baseline(tmp_path, "P1", "front", b"12345")
    save_baseline(tmp_path, "P2", "front", b"123")
    assert baselines_bytes(tmp_path) == 8
