"""抽取脚本的单元测试。

脚本本身不是包、不在 sys.path 上,所以按文件路径加载。之所以值得单测:
`_strip_line_comments` 的字符串感知是防御性的,当前语料里没有任何一条含
`://`,换成朴素正则也产出字节级相同的 fixture ——也就是说没有测试的话它
就是死代码,谁都不知道它到底管不管用。
"""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "extract_doc_samples.py"
_spec = importlib.util.spec_from_file_location("extract_doc_samples", _SCRIPT)
extract_doc_samples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_doc_samples)


def test_行注释被剥掉():
    assert extract_doc_samples._strip_line_comments('{"a": 1} // 说明').strip() == '{"a": 1}'


def test_字符串里的双斜杠不动():
    """这条就是 `_strip_line_comments` 存在的全部理由。"""
    text = '{"url": "ws://192.168.144.100:10010"} // 地址'
    assert extract_doc_samples._strip_line_comments(text).strip() == (
        '{"url": "ws://192.168.144.100:10010"}'
    )


def test_转义引号不会让字符串状态错乱():
    text = '{"a": "他说\\"//\\"不是注释"} // 真注释'
    out = extract_doc_samples._strip_line_comments(text).strip()
    assert out == '{"a": "他说\\"//\\"不是注释"}'


def test_repair_补尾逗号并记录规则():
    fixed, rules = extract_doc_samples.repair('{"a": 1,}')
    assert fixed == '{"a": 1}'
    assert rules == ["尾逗号"]


def test_repair_干净文本不动也不记录():
    fixed, rules = extract_doc_samples.repair('{"a": 1}')
    assert fixed == '{"a": 1}'
    assert rules == []


def test_expected_req_func_请求裸字符串():
    """§7.14 exit_charging 那种 req_func 直接是裸字符串的形状。"""
    payload = {"head": {"type": "app_req"}, "data": {"req_func": "exit_charging"}}
    assert extract_doc_samples.expected_req_func(payload) == "exit_charging"


def test_expected_req_func_请求单键字典():
    payload = {
        "head": {"type": "app_req"},
        "data": {"req_func": {"start_mapping": {"map_name": "m1"}}},
    }
    assert extract_doc_samples.expected_req_func(payload) == "start_mapping"


def test_expected_req_func_响应带外壳():
    payload = {
        "head": {"type": "app_resp"},
        "data": {
            "req_result": {"AppReponseObjectData": {"req_func": "get_speed", "status": "ok"}}
        },
    }
    assert extract_doc_samples.expected_req_func(payload) == "get_speed"


def test_expected_req_func_响应无外壳():
    payload = {
        "head": {"type": "app_resp"},
        "data": {"req_result": {"req_func": "stop_mapping", "status": "ok"}},
    }
    assert extract_doc_samples.expected_req_func(payload) == "stop_mapping"


def test_expected_req_func_故障推送没有req_func():
    payload = {"head": {"type": "alg_error_code_notify"}, "data": {"items": []}}
    assert extract_doc_samples.expected_req_func(payload) is None
