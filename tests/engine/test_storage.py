"""盘水位:两条门槛并列,谁先触发听谁的;文案说病因,不说症状。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.storage import (
    STOP_USED_RATIO,
    WARN_USED_RATIO,
    storage_verdict,
)


def _v(**kw):
    base = dict(free_mb=50_000.0, used_ratio=0.30, min_free_mb=500.0,
                has_upload=False, last_upload_age_days=None)
    base.update(kw)
    return storage_verdict(**base)


def test_盘空着就放行也不报警():
    v = _v()
    assert v.ok and not v.warn


def test_剩余空间不够就拦停():
    assert not _v(free_mb=100.0).ok


def test_用掉九成就拦停哪怕还剩几十个G():
    # 大盘上 90% 还剩几十 GB,小盘上 500MB 已经是 99% —— 谁先触发听谁的。
    v = _v(free_mb=80_000.0, used_ratio=0.91)
    assert not v.ok


def test_两条门槛并列谁先触发听谁的():
    assert not _v(free_mb=100.0, used_ratio=0.10).ok
    assert not _v(free_mb=80_000.0, used_ratio=0.95).ok


def test_八成就要报警不能等到九成才说话():
    # 中间那 10% 是留给人反应的时间 —— 跟电量那三条线是同一个道理。
    v = _v(used_ratio=0.82)
    assert v.warn, "80% 是提前量"
    assert v.ok, "80% 不拦停"


@pytest.mark.parametrize("ratio", [0.0, 0.5, 0.79])
def test_没到八成不报警(ratio):
    assert not _v(used_ratio=ratio).warn


def test_门槛的两个数就是八成和九成():
    assert WARN_USED_RATIO == 0.80
    assert STOP_USED_RATIO == 0.90


def test_联网档盘满说的是回传断了不是盘满了():
    # 联网档 90% 本来就不该触发 —— 传成功的都标了可删,水位一到自动清。
    # 真触发了,病因是回传断了。说症状的告警会让人去换个大盘,而那治不了病。
    v = _v(used_ratio=0.93, has_upload=True, last_upload_age_days=12.0)
    assert not v.ok
    assert "回传" in v.detail and "12" in v.detail


def test_联网档从来没传成功过也说得出话():
    v = _v(used_ratio=0.93, has_upload=True, last_upload_age_days=None)
    assert not v.ok
    assert "回传" in v.detail
    assert "None" not in v.detail, "别把 None 直接拼进给人看的话里"


def test_单机档盘满说的是保留期没到要人来导出():
    v = _v(used_ratio=0.93, has_upload=False)
    assert not v.ok
    assert "保留期" in v.detail
    assert "导出" in v.detail, "得给出人能做的那个动作"


def test_单机档的文案里不提回传():
    # 单机档没有上传目标,说"回传中断"是句听不懂的话。
    v = _v(used_ratio=0.93, has_upload=False)
    assert "回传" not in v.detail


def test_过了的时候也说得出剩多少用了多少():
    v = _v()
    assert "MB" in v.detail and "%" in v.detail
