"""线格式的小工具:取字段、验类型、验 schema。所有数据类的 ``from_wire`` 都只用这里的
几个函数,错误文本的形状才一致(字段名在前)。"""

from __future__ import annotations

from typing import Any

from d1max_contract import SCHEMA
from d1max_contract.errors import ContractError, SchemaMismatch


def major(schema: str) -> int:
    head = str(schema).split(".", 1)[0]
    if not head.isdigit():
        raise SchemaMismatch(f"schema 不成形: {schema!r}")
    return int(head)


def check_schema(d: Any, what: str) -> dict[str, Any]:
    """顶层要是对象,且 ``schema`` 主版本与我们相同。**先验它,再看别的**。"""
    if not isinstance(d, dict):
        raise ContractError(f"{what}: 报文顶层不是对象")
    schema = d.get("schema")
    if not isinstance(schema, str):
        raise SchemaMismatch(f"{what}: 缺 schema")
    if major(schema) != major(SCHEMA):
        raise SchemaMismatch(f"{what}: schema {schema} 与本地 {SCHEMA} 主版本不同")
    return d


def need(d: dict[str, Any], key: str, what: str) -> Any:
    if key not in d:
        raise ContractError(f"{what}: 缺 {key}")
    return d[key]


def as_str(d: dict[str, Any], key: str, what: str, *, nonempty: bool = False) -> str:
    v = need(d, key, what)
    if not isinstance(v, str):
        raise ContractError(f"{what}: {key} 要是字符串")
    if nonempty and not v:
        raise ContractError(f"{what}: {key} 不许为空")
    return v


def as_int(d: dict[str, Any], key: str, what: str) -> int:
    v = need(d, key, what)
    # bool 是 int 的子类,浮点即使是整数值也不收 —— 序号和时刻不许含糊。
    if isinstance(v, bool) or not isinstance(v, int):
        raise ContractError(f"{what}: {key} 要是整数")
    return v


def as_float(d: dict[str, Any], key: str, what: str) -> float:
    v = need(d, key, what)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ContractError(f"{what}: {key} 要是数")
    return float(v)


def as_bool(d: dict[str, Any], key: str, what: str) -> bool:
    v = need(d, key, what)
    if not isinstance(v, bool):
        raise ContractError(f"{what}: {key} 要是布尔")
    return v


def as_dict(d: dict[str, Any], key: str, what: str) -> dict[str, Any]:
    v = need(d, key, what)
    if not isinstance(v, dict):
        raise ContractError(f"{what}: {key} 要是对象")
    return v


def as_opt_dict(d: dict[str, Any], key: str, what: str) -> dict[str, Any] | None:
    v = need(d, key, what)
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ContractError(f"{what}: {key} 要是对象或 null")
    return v


def as_enum(d: dict[str, Any], key: str, what: str, enum: type) -> Any:
    v = as_str(d, key, what)
    try:
        return enum(v)
    except ValueError as exc:
        allowed = ", ".join(e.value for e in enum)
        raise ContractError(f"{what}: {key} 只能是 {allowed},给的是 {v!r}") from exc
