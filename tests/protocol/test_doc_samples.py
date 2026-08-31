"""厂商文档里的 JSON 样例。自己编的报文只能证明代码自洽,这些才接近真机。"""

import json
from collections import Counter
from pathlib import Path

import pytest

from d1max_patrol.protocol.nav_frames import (
    FRAME_TYPE_ALG_ERROR,
    FRAME_TYPE_REQUEST,
    FRAME_TYPE_RESPONSE,
    AlgErrorNotify,
    Response,
    parse_message,
    parse_request,
)

FIXTURES = Path(__file__).parent / "fixtures"
INDEX = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))


def _entries(frame_type: str) -> list[dict]:
    return [e for e in INDEX if e["type"] == frame_type]


def _payload(entry: dict) -> str:
    return (FIXTURES / entry["file"]).read_text(encoding="utf-8")


def test_样例数量与分布():
    """数字来自对 refs/nav-api/自主导航_WEBSOCKET_API.md 的实测抽取。

    这里钉死而不是写 >=,是为了让抽取脚本的任何回退都立刻变红。文档若
    真的更新了,重跑脚本、核对 diff、再来改这几个数 —— 那应该是一次
    有意识的动作,不该被一个宽松的下界悄悄吃掉。
    """
    counts = Counter(entry["type"] for entry in INDEX)
    assert len(INDEX) == 61
    assert counts == {"app_req": 36, "app_resp": 24, "alg_error_code_notify": 1}


def test_三类报文都有样例():
    for frame_type in (FRAME_TYPE_REQUEST, FRAME_TYPE_RESPONSE, FRAME_TYPE_ALG_ERROR):
        assert _entries(frame_type), f"没有 {frame_type} 类型的样例"


@pytest.mark.parametrize("entry", _entries(FRAME_TYPE_REQUEST), ids=lambda e: e["file"])
def test_文档里的请求样例都能解析(entry):
    frame_count, req_func, _args = parse_request(_payload(entry))
    # 只断言"是个非空字符串"挡不住"解析器永远返回同一个错名"——评审实测
    # 过:那样改,这 36 条会全部照常绿。所以必须比对具体的值。期望值由抽取
    # 脚本独立扒出、记在 index.json 里,是解析器之外的第二实现。
    assert req_func == entry["expected_req_func"]
    assert frame_count is None or isinstance(frame_count, int)


@pytest.mark.parametrize("entry", _entries(FRAME_TYPE_RESPONSE), ids=lambda e: e["file"])
def test_文档里的响应样例都能解析(entry):
    message = parse_message(_payload(entry))
    assert isinstance(message, Response)
    assert message.req_func == entry["expected_req_func"]
    assert isinstance(message.ok, bool)


def test_index_的元数据字段没有退化():
    """`expected_req_func` / `doc_line` / `repaired` 都必须真的在库里。

    没有这条,上面两个 parametrize 里的 `entry["expected_req_func"]` 在
    "脚本不再写这个字段"时会 KeyError——那也是红,但错得莫名其妙。这条
    让退化在一个说得清的地方先红。
    """
    for entry in INDEX:
        assert "expected_req_func" in entry, entry["file"]
        assert isinstance(entry["doc_line"], int)
    # 请求与响应必须都有期望名;故障推送没有 req_func,应为 None
    for entry in _entries(FRAME_TYPE_REQUEST) + _entries(FRAME_TYPE_RESPONSE):
        assert entry["expected_req_func"], entry["file"]
    for entry in _entries(FRAME_TYPE_ALG_ERROR):
        assert entry["expected_req_func"] is None, entry["file"]
    # 机械修复过的样例恰好 3 条(尾逗号 ×2、行注释 ×1)。数字钉死,便于文档
    # 更新时立刻发现修复面变了。
    assert sum(1 for e in INDEX if e["repaired"]) == 3


@pytest.mark.parametrize("entry", _entries(FRAME_TYPE_ALG_ERROR), ids=lambda e: e["file"])
def test_文档里的故障推送样例都能解析(entry):
    message = parse_message(_payload(entry))
    assert isinstance(message, AlgErrorNotify)
    assert message.items
    assert all(isinstance(item.code, int) for item in message.items)


def test_嵌套外壳的样例确实被剥掉():
    """速度接口的响应样例里应当至少有一条带 AppReponseObjectData。"""
    nested = [e for e in _entries(FRAME_TYPE_RESPONSE) if "AppReponseObjectData" in _payload(e)]
    assert nested, "文档里应当有嵌套外壳的样例,没找到说明抽取漏了"
    for entry in nested:
        message = parse_message(_payload(entry))
        assert message.data is None or "AppReponseObjectData" not in str(message.data)


def test_改名的响应样例存在():
    """loc_load_map 的响应叫 load_localization_map。"""
    funcs = {parse_message(_payload(e)).req_func for e in _entries(FRAME_TYPE_RESPONSE)}
    assert "load_localization_map" in funcs
