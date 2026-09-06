"""形态:单机 / 联网。**差异只允许存在于可替换对象里。**"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.form import STANDALONE, Form, UploadTargetError


class _FakeUpload:
    async def describe(self) -> str:
        return "控制台 https://console.example/upload"


async def test_单机形态没有上传目标():
    assert not STANDALONE.has_upload


async def test_没有上传目标的形态必须明确说没有():
    # "悄悄成功"和"明确拒绝"的区别,就是 §2.5 那个 bug 会不会被测出来:
    # 单机档永远没有上传对象,所有文件永远算"未上传",狗迟早永久停飞。
    with pytest.raises(UploadTargetError):
        await STANDALONE.describe_upload()


async def test_联网形态说得出传到哪儿去():
    form = Form(name="connected", upload=_FakeUpload())
    assert form.has_upload
    assert "console.example" in await form.describe_upload()
