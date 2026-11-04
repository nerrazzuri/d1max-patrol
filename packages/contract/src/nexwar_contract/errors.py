"""契约层的两种错。文本是给现场看的人话,也是回执里 ``reason`` 的来源。"""

from __future__ import annotations


class ContractError(ValueError):
    """报文不合契约:缺字段、类型错、值非法。文本里**必须带字段名**,测试钉着。"""


class SchemaMismatch(ContractError):
    """``schema`` 主版本不同(或没有)。收到它的一侧拒连/拒派,不尝试解读内容。"""
