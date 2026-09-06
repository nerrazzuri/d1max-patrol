"""这台狗跑在哪一档:单机,还是联网。

**形态差异只允许存在于三个可替换对象里:上传目标、名录来源、鉴权来源。
其余代码不许知道自己跑在哪一档。**(spec §8.1)

管住这一条,绝大多数代码根本不需要关心形态;管不住,"有没有服务器"会变成
散落各处的 if,而**没被跑到的那个组合一定会出厂**。

本卷只做**上传目标**。名录来源与鉴权来源以后往 ``Form`` 上加字段,
**不要另起一个平行的类型** —— 两个"形态"对象是这条约束最先烂掉的方式。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class UploadTargetError(Exception):
    """问一个没有上传目标的形态"传到哪儿去"。

    **这不是故障,是单机档的定义。** 但它必须抛,不能悄悄回一个空串 ——
    "不支持"和"支持了但什么也没干"必须分得开(spec §8.2)。
    """


@runtime_checkable
class UploadTarget(Protocol):
    async def describe(self) -> str:
        """人看得懂的一句话:传到哪儿去。"""
        ...


@dataclass(frozen=True, slots=True)
class Form:
    """一档形态。``name`` 只用来给人看和给测试参数化,**不许拿它写 if**。"""

    name: str
    upload: UploadTarget | None = None

    @property
    def has_upload(self) -> bool:
        return self.upload is not None

    async def describe_upload(self) -> str:
        if self.upload is None:
            raise UploadTargetError(
                f"形态 {self.name!r} 没有上传目标 —— 这不是故障,是这一档的定义")
        return await self.upload.describe()


#: 交付默认形态。没有服务器的客户装的就是这个。
STANDALONE = Form(name="standalone")
