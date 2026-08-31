"""自主导航 WebSocket 报文的封装与解析。

只负责线格式,不含任何业务语义。请求端与仿真器共用本模块,
因此两边的线格式由同一组构造函数保证一致。

参考: refs/nav-api/自主导航_WEBSOCKET_API.md §消息结构 / §5 / §9
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

FRAME_TYPE_REQUEST = "app_req"
FRAME_TYPE_RESPONSE = "app_resp"
FRAME_TYPE_ALG_ERROR = "alg_error_code_notify"

SOURCE_APP = "app"
SOURCE_ALG = "alg_control_node"

#: 协议地雷 1: §3.9/§3.10 的响应在 req_result 下多包一层,
#: 且厂商把 Response 拼写成了 Reponse。不要"修正"这个拼写。
NESTED_RESULT_KEY = "AppReponseObjectData"

STATUS_OK = "ok"
STATUS_ERROR = "error"


class ProtocolError(Exception):
    """收到无法按协议解释的报文。"""


@dataclass(frozen=True)
class Response:
    """一条 app_resp。注意它既可能是请求的响应,也可能是设备主动推送。"""

    frame_count: int | None
    req_func: str
    ok: bool
    msg: str | None
    data: Any
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class AlgErrorItem:
    code: int
    description: str
    severity: int


@dataclass(frozen=True)
class AlgErrorNotify:
    items: tuple[AlgErrorItem, ...]
    time_stamp_ms: int | None
    raw: dict[str, Any] = field(repr=False)


Message = Response | AlgErrorNotify


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_request(
    req_func: str,
    args: Any,
    frame_count: int,
    time_stamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造 app_req 报文。`args` 原样放进 req_func 的值位置。"""
    return {
        "head": {
            "type": FRAME_TYPE_REQUEST,
            "time_stamp": _now_ms() if time_stamp_ms is None else time_stamp_ms,
            "source": SOURCE_APP,
            "frame_count": frame_count,
        },
        "data": {"req_func": {req_func: args}},
    }


def encode_request(
    req_func: str,
    args: Any,
    frame_count: int,
    time_stamp_ms: int | None = None,
) -> str:
    return json.dumps(
        build_request(req_func, args, frame_count, time_stamp_ms),
        ensure_ascii=False,
    )


def build_response(
    req_func: str,
    frame_count: int,
    *,
    ok: bool = True,
    msg: str | None = None,
    data: Any = None,
    nested: bool = False,
    time_stamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造 app_resp 报文。`nested=True` 时套上 AppReponseObjectData 一层。"""
    result = {
        "req_func": req_func,
        "status": STATUS_OK if ok else STATUS_ERROR,
        "msg": msg,
        "data": data,
    }
    payload = {NESTED_RESULT_KEY: result} if nested else result
    return {
        "head": {
            "type": FRAME_TYPE_RESPONSE,
            "time_stamp": _now_ms() if time_stamp_ms is None else time_stamp_ms,
            "source": SOURCE_ALG,
            "frame_count": frame_count,
        },
        "data": {"req_result": payload},
    }


def build_alg_error_notify(
    items: Sequence[AlgErrorItem],
    time_stamp_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "head": {
            "type": FRAME_TYPE_ALG_ERROR,
            "time_stamp": _now_ms() if time_stamp_ms is None else time_stamp_ms,
            "source": SOURCE_ALG,
            "frame_count": 1,
        },
        "data": {
            "items": [
                {"code": it.code, "description": it.description, "severity": it.severity}
                for it in items
            ]
        },
    }


def _load(text: str | bytes) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"报文不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("报文顶层不是对象")
    return payload


def _head_of(payload: dict[str, Any]) -> dict[str, Any]:
    head = payload.get("head")
    if not isinstance(head, dict):
        raise ProtocolError("报文缺少 head")
    return head


def _frame_count_of(head: dict[str, Any]) -> int | None:
    fc = head.get("frame_count")
    return fc if isinstance(fc, int) and not isinstance(fc, bool) else None


def parse_message(text: str | bytes) -> Message:
    """解析设备端发来的一条报文。"""
    payload = _load(text)
    head = _head_of(payload)
    msg_type = head.get("type")
    if msg_type == FRAME_TYPE_ALG_ERROR:
        return _parse_alg_error(payload, head)
    if msg_type == FRAME_TYPE_RESPONSE:
        return _parse_response(payload, head)
    raise ProtocolError(f"不支持的报文类型: {msg_type!r}")


def _parse_response(payload: dict[str, Any], head: dict[str, Any]) -> Response:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProtocolError("app_resp 缺少 data")
    result = data.get("req_result")
    if not isinstance(result, dict):
        raise ProtocolError("app_resp 缺少 req_result")
    nested = result.get(NESTED_RESULT_KEY)
    if isinstance(nested, dict):
        result = nested
    req_func = result.get("req_func")
    if not isinstance(req_func, str):
        raise ProtocolError("req_result 缺少 req_func")
    return Response(
        frame_count=_frame_count_of(head),
        req_func=req_func,
        ok=result.get("status") == STATUS_OK,
        msg=result.get("msg"),
        data=result.get("data"),
        raw=payload,
    )


def _parse_alg_error(payload: dict[str, Any], head: dict[str, Any]) -> AlgErrorNotify:
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ProtocolError("alg_error_code_notify 缺少 items")
    items: list[AlgErrorItem] = []
    for entry in data["items"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("code"), int):
            raise ProtocolError(f"故障条目格式错误: {entry!r}")
        # I1: severity 非法(比如厂商填了 null)本来就该由协议层报自己的
        # 错误类型,而不是让 TypeError/ValueError 逃到调用方——那样会被
        # 读循环的外层 except Exception 接住,误判成链路已死。
        try:
            severity = int(entry.get("severity", 0))
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"故障条目 severity 非法: {entry!r}") from exc
        items.append(
            AlgErrorItem(
                code=entry["code"],
                description=str(entry.get("description", "")),
                severity=severity,
            )
        )
    ts = head.get("time_stamp")
    return AlgErrorNotify(
        items=tuple(items),
        time_stamp_ms=ts if isinstance(ts, int) else None,
        raw=payload,
    )


def parse_request(text: str | bytes) -> tuple[int | None, str, Any]:
    """解析客户端发来的 app_req,返回 (frame_count, req_func, args)。供仿真器使用。"""
    payload = _load(text)
    head = _head_of(payload)
    if head.get("type") != FRAME_TYPE_REQUEST:
        raise ProtocolError(f"报文不是 app_req: {head.get('type')!r}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("req_func"), dict):
        raise ProtocolError("app_req 缺少 req_func")
    req_func_obj: dict[str, Any] = data["req_func"]
    if len(req_func_obj) != 1:
        raise ProtocolError(f"req_func 应恰好含一个函数名,实际 {len(req_func_obj)} 个")
    (name, args), = req_func_obj.items()
    return _frame_count_of(head), name, args
