"""判读的比对基准:先问基线集,再退回历史 run(spec §4.5)。

**为什么不能只靠历史 run:** 水位删除(spec §4.4)一开工,历史就会被删掉,
而 `_previous_photo` 扫不到东西的时候不报错 —— 它只是回 `None`,于是每张
照片都变成"无基准"判读。没有人会发现。这一份钉的就是那个静默失效。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d1max_agent.engine.baselines import load_baseline, save_baseline
from d1max_patrol.inspect.judge import judge_run, read_findings

from .test_judge import PHOTO, PHOTO2, FakeClient, _write_run


@pytest.fixture(autouse=True)
def 没有密钥(monkeypatch):
    """跟 `test_judge.py` 里那份同一个理由:开发机上真配了密钥的会去打真 API。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def run_dir(tmp_path) -> Path:
    return _write_run(tmp_path / "runs", "巡检一号", "20260902T101500Z")


@pytest.fixture
def baselines(tmp_path) -> Path:
    return tmp_path / "baselines"


def test_有基线就拿基线当基准(run_dir, baselines):
    save_baseline(baselines, "P1_transformer", "front", "基线那一张".encode())
    client = FakeClient()
    judge_run(run_dir, client=client, baselines_root=baselines)
    # 第一张照片(P1)有基线,送两张图;第二张(P2)没有,只送一张。
    assert client.images[0][1] == "基线那一张".encode()
    assert len(client.images[1]) == 1


def test_没有基线就退回去翻历史run(tmp_path, baselines):
    """基线集是新加的,存量客户升级上来时它是空的 —— 那一天判读不能变瞎。"""
    history = tmp_path / "runs"
    _write_run(history, "巡检一号", "20260901T101500Z")
    run = _write_run(history, "巡检一号", "20260902T101500Z")
    client = FakeClient()
    judge_run(run, client=client, history_root=history, baselines_root=baselines)
    assert len(client.images[0]) == 2


def test_基线优先于历史run(tmp_path, baselines):
    """两个都有的时候,基线说了算 —— 历史里那张随时可能被水位删掉。"""
    history = tmp_path / "runs"
    _write_run(history, "巡检一号", "20260901T101500Z")
    run = _write_run(history, "巡检一号", "20260902T101500Z")
    save_baseline(baselines, "P1_transformer", "front", "基线那一张".encode())
    client = FakeClient()
    judge_run(run, client=client, history_root=history, baselines_root=baselines)
    assert client.images[0][1] == "基线那一张".encode()


def test_判成正常就把这张设为新基线(run_dir, baselines):
    judge_run(run_dir, client=FakeClient(), baselines_root=baselines)
    got = load_baseline(baselines, "P1_transformer", "front")
    assert got == (run_dir / "photos" / PHOTO).read_bytes()


def test_判成异常绝不回写基线(run_dir, baselines):
    """把异常照片设成基准,下一次就是拿异常比异常 —— 模型看到"跟上次一样",
    判成正常。**异常就此静默转正。**"""
    save_baseline(baselines, "P1_transformer", "front", "原来那张".encode())
    client = FakeClient({"verdict": "abnormal", "confidence": 0.9,
                         "reason": "门开着", "evidence": "左下"})
    judge_run(run_dir, client=client, baselines_root=baselines)
    assert load_baseline(baselines, "P1_transformer", "front") == "原来那张".encode()


def test_判不出来也不回写基线(run_dir, baselines):
    save_baseline(baselines, "P1_transformer", "front", "原来那张".encode())
    client = FakeClient({"verdict": "unclear", "confidence": 0.2,
                         "reason": "太暗", "evidence": ""})
    judge_run(run_dir, client=client, baselines_root=baselines)
    assert load_baseline(baselines, "P1_transformer", "front") == "原来那张".encode()


def test_没给基线目录时行为跟以前一模一样(tmp_path):
    """存量调用点一个字没改就该照常工作。"""
    history = tmp_path / "runs"
    _write_run(history, "巡检一号", "20260901T101500Z")
    run = _write_run(history, "巡检一号", "20260902T101500Z")
    client = FakeClient()
    findings = judge_run(run, client=client, history_root=history)
    assert len(client.images[0]) == 2
    assert [f.verdict for f in findings] == ["normal", "normal"]


def test_基线写不下去不能拖垮判读(run_dir, tmp_path):
    """盘满、只读挂载、名字不合法 —— 回写基线失败只该少一张基线,
    不该让这一趟判读整个失败。"""
    blocked = tmp_path / "拿一个文件占住这个位置"
    blocked.write_text("我不是目录", encoding="utf-8")
    findings = judge_run(run_dir, client=FakeClient(), baselines_root=blocked)
    assert [f.verdict for f in findings] == ["normal", "normal"]
    assert len(read_findings(run_dir)) == 2


def test_点位名拆不开时跳过基线而不是炸(tmp_path, baselines):
    """`_split_photo_name` 拆不开的照片相机名是空串,建不了基线 —— 跳过它,
    这一张照常判读。"""
    run = _write_run(tmp_path / "runs", "巡检一号", "20260902T101500Z",
                     photos=("怪名字.jpg", PHOTO2), checks=("", ""))
    findings = judge_run(run, client=FakeClient(), baselines_root=baselines)
    assert len(findings) == 2
    assert load_baseline(baselines, "怪名字", "") is None
