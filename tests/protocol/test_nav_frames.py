"""线格式的编解码。样例报文全部照抄 refs/nav-api 文档。"""

import json

import pytest

from d1max_patrol.protocol.nav_frames import (
    AlgErrorItem,
    AlgErrorNotify,
    ProtocolError,
    Response,
    build_alg_error_notify,
    build_request,
    build_response,
    encode_request,
    parse_message,
    parse_request,
)


def test_请求报文结构与文档一致():
    frame = build_request("start_mapping", 0, frame_count=1, time_stamp_ms=1234567890123)
    assert frame == {
        "head": {
            "type": "app_req",
            "time_stamp": 1234567890123,
            "source": "app",
            "frame_count": 1,
        },
        "data": {"req_func": {"start_mapping": 0}},
    }


def test_请求参数为_null_时保留键():
    frame = build_request("get_nav_status", None, frame_count=7)
    assert frame["data"]["req_func"] == {"get_nav_status": None}


def test_encode_request_产出可解析的_utf8_json():
    text = encode_request("get_all_paths_by_mapid", "地图A", frame_count=3)
    assert "地图A" in text  # ensure_ascii=False
    assert json.loads(text)["data"]["req_func"]["get_all_paths_by_mapid"] == "地图A"


def test_解析成功响应():
    text = json.dumps(
        {
            "head": {
                "type": "app_resp",
                "time_stamp": 1234567890124,
                "source": "alg_control_node",
                "frame_count": 1,
            },
            "data": {
                "req_result": {
                    "req_func": "get_nav_status",
                    "status": "ok",
                    "msg": None,
                    "data": "StandBy",
                }
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, Response)
    assert msg.frame_count == 1
    assert msg.req_func == "get_nav_status"
    assert msg.ok is True
    assert msg.msg is None
    assert msg.data == "StandBy"


def test_解析失败响应():
    text = json.dumps(
        {
            "head": {"type": "app_resp", "time_stamp": 1, "source": "alg_control_node",
                     "frame_count": 9},
            "data": {
                "req_result": {
                    "req_func": "start_nav",
                    "status": "error",
                    "msg": "not in StandBy",
                    "data": None,
                }
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, Response)
    assert msg.ok is False
    assert msg.msg == "not in StandBy"


def test_解析嵌套的_AppReponseObjectData_变体():
    """§3.9/§3.10 的响应多包了一层,厂商还把 Response 拼成了 Reponse。"""
    text = json.dumps(
        {
            "head": {"type": "app_resp", "time_stamp": 1, "source": "alg_control_node",
                     "frame_count": 4},
            "data": {
                "req_result": {
                    "AppReponseObjectData": {
                        "req_func": "get_navigation_speed",
                        "status": "ok",
                        "msg": None,
                        "data": {"x": 0.8, "y": 0.4, "z": 1.2},
                    }
                }
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, Response)
    assert msg.frame_count == 4
    assert msg.req_func == "get_navigation_speed"
    assert msg.data == {"x": 0.8, "y": 0.4, "z": 1.2}


def test_解析算法故障码推送():
    text = json.dumps(
        {
            "head": {
                "type": "alg_error_code_notify",
                "time_stamp": 1763697492747,
                "source": "alg_control_node",
                "frame_count": 1,
            },
            "data": {
                "items": [
                    {"code": 13330, "description": "navigation blocked", "severity": 0},
                    {"code": 13331, "description": "lidar disconnected", "severity": 1},
                ]
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, AlgErrorNotify)
    assert msg.time_stamp_ms == 1763697492747
    assert msg.items == (
        AlgErrorItem(13330, "navigation blocked", 0),
        AlgErrorItem(13331, "lidar disconnected", 1),
    )


def test_故障码推送允许空列表():
    text = json.dumps(
        {"head": {"type": "alg_error_code_notify", "time_stamp": 1,
                  "source": "alg_control_node", "frame_count": 1},
         "data": {"items": []}}
    )
    msg = parse_message(text)
    assert isinstance(msg, AlgErrorNotify)
    assert msg.items == ()


def test_frame_count_缺失或非整数时为_None():
    text = json.dumps(
        {"head": {"type": "app_resp", "time_stamp": 1, "source": "alg_control_node"},
         "data": {"req_result": {"req_func": "stop_nav", "status": "ok",
                                 "msg": None, "data": None}}}
    )
    assert parse_message(text).frame_count is None


@pytest.mark.parametrize(
    "text, hint",
    [
        ("{ not json", "不是合法 JSON"),
        ("[1, 2]", "顶层不是对象"),
        ('{"data": {}}', "缺少 head"),
        ('{"head": {"type": "app_topic_resp"}, "data": {}}', "不支持的报文类型"),
        ('{"head": {"type": "app_resp"}, "data": {}}', "缺少 req_result"),
        ('{"head": {"type": "app_resp"}, "data": {"req_result": {"status": "ok"}}}',
         "缺少 req_func"),
        ('{"head": {"type": "alg_error_code_notify"}, "data": {}}', "缺少 items"),
    ],
)
def test_畸形报文抛_ProtocolError(text, hint):
    with pytest.raises(ProtocolError, match=hint):
        parse_message(text)


def test_build_response_可被自己解析回来():
    frame = build_response("get_loc_status", 12, data="ContinuousLoc")
    msg = parse_message(json.dumps(frame))
    assert isinstance(msg, Response)
    assert (msg.frame_count, msg.req_func, msg.ok, msg.data) == (
        12, "get_loc_status", True, "ContinuousLoc")


def test_build_response_嵌套模式往返():
    frame = build_response("set_navigation_speed", 3, data={"x": 0.8}, nested=True)
    assert "AppReponseObjectData" in frame["data"]["req_result"]
    msg = parse_message(json.dumps(frame))
    assert msg.req_func == "set_navigation_speed"
    assert msg.data == {"x": 0.8}


def test_build_response_错误模式():
    frame = build_response("start_nav", 5, ok=False, msg="busy")
    msg = parse_message(json.dumps(frame))
    assert msg.ok is False and msg.msg == "busy"


def test_build_alg_error_notify_往返():
    frame = build_alg_error_notify([AlgErrorItem(13331, "lidar disconnected", 1)])
    msg = parse_message(json.dumps(frame))
    assert isinstance(msg, AlgErrorNotify)
    assert msg.items[0].code == 13331


def test_parse_request_取出三元组():
    text = encode_request("start_multi_nav", ["m1", "p1"], frame_count=6)
    assert parse_request(text) == (6, "start_multi_nav", ["m1", "p1"])


@pytest.mark.parametrize(
    "text, hint",
    [
        ('{"head": {"type": "app_resp", "frame_count": 1}, "data": {}}', "不是 app_req"),
        ('{"head": {"type": "app_req", "frame_count": 1}, "data": {}}', "缺少 req_func"),
        ('{"head": {"type": "app_req", "frame_count": 1}, "data": {"req_func": {}}}',
         "应恰好含一个函数名"),
        ('{"head": {"type": "app_req", "frame_count": 1},'
         ' "data": {"req_func": {"a": 1, "b": 2}}}', "应恰好含一个函数名"),
    ],
)
def test_parse_request_畸形报文抛_ProtocolError(text, hint):
    with pytest.raises(ProtocolError, match=hint):
        parse_request(text)
