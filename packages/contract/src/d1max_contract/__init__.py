"""狗 ↔ 站点的唯一契约(总设计 §3)。**这个包不依赖根包 d1max-patrol**。

报文数据类是真理源(W00 决定 3):每类 ``to_wire()``/``from_wire()``,顶层带
``schema``;主版本不同即拒。黄金夹具由 :mod:`d1max_contract.fixtures` 生成。
"""

#: 契约版本。主版本不同即拒连/拒派;次版本只加字段。
SCHEMA = "1.0"
