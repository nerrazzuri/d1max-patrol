"""形态契约:同一份用例,单机跑一遍、联网跑一遍。

**只跑 spec §8.3 点名的那几处。** 其余一遍就够 —— 任务引擎、判读、遥控、
协议编解码跟形态无关,跑两遍是零信息量的重复,而慢下来的套件没人跑。
"""

from __future__ import annotations

import pytest

from d1max_agent.engine.form import UploadTargetError
from d1max_agent.engine.storage import storage_verdict

pytestmark = pytest.mark.contract


async def test_没有上传目标的形态必须明确说没有(form, form_caps):
    """**这条是 §2.5 那个 bug 的疫苗。**

    当时写"未上传的绝不删",单机档因为永远没有上传对象,所有文件永远算
    "未上传",狗迟早永久停飞。"悄悄成功"和"明确拒绝"的区别,就是这个 bug
    会不会被测出来。
    """
    if "upload" in form_caps:
        # 断"非空"等于没断:一个返回 " " 的上传目标照样过。声明了 upload 的
        # 那一档,说出来的必须是**这一档自己的那个去处**。
        assert "console.example" in await form.describe_upload()
        return
    with pytest.raises(UploadTargetError):
        await form.describe_upload()


def test_两档的盘满诊断说的是各自的病因(form, form_caps):
    v = storage_verdict(free_mb=100.0, used_ratio=0.94, min_free_mb=500.0,
                        has_upload=form.has_upload, last_upload_age_days=11.0)
    assert not v.ok
    if "upload" in form_caps:
        assert "回传" in v.detail
        assert "保留期" not in v.detail
    else:
        assert "保留期" in v.detail
        assert "回传" not in v.detail


def test_两档的起飞门槛都是两条并列都得过(form):
    # 判据本身跟形态无关 —— 分档的只有说出来的那句话。这条测的就是
    # "别顺手把判据也分了岔"。
    assert not storage_verdict(free_mb=100.0, used_ratio=0.10,
                               min_free_mb=500.0, has_upload=form.has_upload,
                               last_upload_age_days=None).ok
    assert not storage_verdict(free_mb=80_000.0, used_ratio=0.95,
                               min_free_mb=500.0, has_upload=form.has_upload,
                               last_upload_age_days=None).ok
    assert storage_verdict(free_mb=80_000.0, used_ratio=0.30,
                           min_free_mb=500.0, has_upload=form.has_upload,
                           last_upload_age_days=None).ok


def test_八成报警不分形态(form):
    v = storage_verdict(free_mb=80_000.0, used_ratio=0.85, min_free_mb=500.0,
                        has_upload=form.has_upload, last_upload_age_days=None)
    assert v.ok and v.warn
