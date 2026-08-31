"""真机一致性巡检器。

现场只有一次机会,所以这个文件里最要紧的两类断言是:

* **扫描不许被单条失败掐断** —— 第三条接口报错、后面二十条一条没跑,
  是这次出差最不划算的失败模式。
* **对拍的输入是录像文件** —— 报告能从录像离线重算,才谈得上"回来再分析"。

其余的是形状对拍本身的正确性。
"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.backends.base import NavConnectionError
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.conformance import (
    ConformanceResult,
    SchemaDiff,
    diff_recording,
    diff_shapes,
    json_shape,
    load_fixtures,
    nav_host_port,
    probe_tcp,
    readonly_sweep,
    render_report,
    resp_func_of,
    run_conformance,
    run_probes,
)
from d1max_patrol.recorder import FrameRecorder
from d1max_sim.nav_server import SimNavServer

_POLLER_OFF = 3600.0


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _resp(func, data=None, shell=None):
    """拼一条厂商形状的响应。shell 为 None 时不套那层怪壳。"""
    inner = {"req_func": func, "status": "ok", "msg": None, "data": data}
    body = {shell: inner} if shell else inner
    return {
        "head": {"type": "app_resp", "time_stamp": 1, "source": "alg_control_node",
                 "frame_count": 1},
        "data": {"req_result": body},
    }


# --------------------------------------------------------- 形状与差异


def test_json_shape_摊平嵌套字典():
    shape = json_shape({"a": {"b": 1, "c": "x"}})
    assert shape == {"a.b": "int", "a.c": "str"}


def test_json_shape_把数组压成一个节点加首元素形状():
    """长度差异不是 schema 差异 —— 返回 3 张地图不该刷出三份路径。"""
    one = json_shape({"m": [{"w": 1}]})
    three = json_shape({"m": [{"w": 1}, {"w": 2}, {"w": 3}]})
    assert one == three == {"m": "array", "m[].w": "int"}


def test_json_shape_空数组也留下array节点():
    assert json_shape({"m": []}) == {"m": "array"}


def test_diff_shapes_三类差异各归各位():
    fx = {"a": "int", "b": "str", "c": "int"}
    rl = {"a": "int", "b": "int", "d": "str"}
    missing, extra, mismatch = diff_shapes(fx, rl)
    assert missing == ["c"]
    assert extra == ["d"]
    assert mismatch == {"b": "fixture=str real=int"}


def test_diff_shapes_忽略每次都变的时间戳和帧号():
    """不忽略的话每一条报文都会报差异,真差异就被淹了。"""
    fx = {"head.time_stamp": "int", "head.frame_count": "int", "x": "int"}
    rl = {"x": "int"}
    assert diff_shapes(fx, rl) == ([], [], {})


def test_diff_shapes_不把这次没值报成类型冲突():
    """msg 这类字段文档给的是 null、真机给的是字符串,那不是 schema 冲突。"""
    _, _, mismatch = diff_shapes({"msg": "NoneType"}, {"msg": "str"})
    assert mismatch == {}


# --------------------------------------------------------- 响应壳穿透


def test_resp_func_不带壳():
    assert resp_func_of(_resp("get_nav_status", "StandBy")) == "get_nav_status"


def test_resp_func_穿透地雷1的拼错壳():
    """厂商把 Response 拼成了 Reponse。"""
    payload = _resp("get_navigation_speed", {"x": 1}, shell="AppReponseObjectData")
    assert resp_func_of(payload) == "get_navigation_speed"


def test_resp_func_穿透地雷2的第二种拼写():
    """§7 里同一层壳又改叫 AppResponse。"""
    payload = _resp("get_arc_alg_status", "StandBy", shell="AppResponse")
    assert resp_func_of(payload) == "get_arc_alg_status"


@pytest.mark.parametrize("payload", [
    {}, {"data": {}}, {"data": {"req_result": "不是字典"}},
    {"data": {"req_result": {"没有req_func": 1}}},
    {"data": {"req_result": {"req_func": 123}}},   # 类型不对也不许硬认
    None, "字符串",
])
def test_resp_func_认不出来就返回None绝不猜(payload):
    assert resp_func_of(payload) is None


def test_resp_func_取的是响应自报的名字而不是请求名():
    """地雷 3: loc_load_map 的响应报的是 load_localization_map。"""
    assert resp_func_of(_resp("load_localization_map")) == "load_localization_map"


# --------------------------------------------------------- 录像对拍


def _write_recording(path, payloads):
    with open(path, "w", encoding="utf-8") as fh:
        for p in payloads:
            fh.write(json.dumps({"dir": "rx", "raw": json.dumps(p, ensure_ascii=False)},
                                ensure_ascii=False) + "\n")


def test_对拍从录像文件读而不是从内存对象读(tmp_path):
    """这条守的是"回来能离线重算"这个性质。"""
    rec = tmp_path / "r.jsonl"
    _write_recording(rec, [_resp("f", {"a": 1})])
    fixtures = {"f": [("x.json", _resp("f", {"a": 1}))]}
    diffs = diff_recording(rec, fixtures)
    assert len(diffs) == 1 and diffs[0].clean


def test_对拍同一个func的多条响应取字段并集(tmp_path):
    """厂商有可选字段。只看第一条会把"这次没带"误报成"真机没有"。"""
    rec = tmp_path / "r.jsonl"
    _write_recording(rec, [_resp("f", {"a": 1}), _resp("f", {"b": 2})])
    fixtures = {"f": [("x.json", _resp("f", {"a": 1, "b": 2}))]}
    diffs = diff_recording(rec, fixtures)
    assert diffs[0].missing_in_real == [], diffs[0]


def test_文档没有样本时单列出来而不是当成一致(tmp_path):
    rec = tmp_path / "r.jsonl"
    _write_recording(rec, [_resp("没见过的接口", {"a": 1})])
    diffs = diff_recording(rec, {})
    assert diffs[0].fixture_files == []
    assert "data.req_result.data.a" in diffs[0].extra_in_real


def test_录像里的发送方向和坏行都跳过(tmp_path):
    rec = tmp_path / "r.jsonl"
    with open(rec, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"dir": "tx", "raw": json.dumps(_resp("f"))}) + "\n")
        fh.write("这不是 json\n")
        fh.write("\n")
        fh.write(json.dumps({"dir": "rx", "raw": "也不是 json"}) + "\n")
        fh.write(json.dumps({"dir": "rx", "raw": "AAAA", "enc": "base64"}) + "\n")
        fh.write(json.dumps({"dir": "rx", "raw": json.dumps(_resp("g"))}) + "\n")
    diffs = diff_recording(rec, {})
    assert [d.resp_func for d in diffs] == ["g"]


def test_真实fixture目录能加载出响应样本():
    """防的是 index.json 的字段名改了而这里没跟上,静默变成"零样本"。"""
    fixtures = load_fixtures()
    assert "get_nav_status" in fixtures
    assert "get_navigation_speed" in fixtures


def test_fixture目录不存在时返回空而不是崩溃(tmp_path):
    assert load_fixtures(tmp_path) == {}


# --------------------------------------------------------- 阶段 0


def test_探测不通的端口给出可执行的提示():
    ok, detail = probe_tcp("192.0.2.1", 9, timeout_s=0.2)
    assert ok is False
    assert "192.0.2.1:9" in detail


def test_探测通的端口(sim):
    host, port = nav_host_port(sim.url)
    ok, detail = probe_tcp(host, port, timeout_s=2.0)
    assert ok is True, detail


def test_一条链路不通不影响另一条(sim):
    host, port = nav_host_port(sim.url)
    probes = run_probes((("好的", host, port), ("坏的", "192.0.2.1", 9)), timeout_s=0.2)
    assert [p.ok for p in probes] == [True, False]


def test_nav_host_port_解析ws地址():
    assert nav_host_port("ws://192.168.144.100:10010") == ("192.168.144.100", 10010)


def test_nav_host_port_缺端口时回落默认值():
    assert nav_host_port("ws://192.168.144.100") == ("192.168.144.100", 10010)


# --------------------------------------------------------- 阶段 1


async def test_只读扫描把每个接口都记一笔(sim):
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF), auto_reconnect=False)
    await b.connect()
    result = ConformanceResult(started_at="t", nav_url=sim.url, allow_motion=False)
    try:
        await readonly_sweep(b, result)
    finally:
        await b.close()

    ops = [c.op for c in result.calls]
    assert {"nav_status", "loc_status", "mapping_status", "get_speed",
            "list_maps"} <= set(ops), ops
    assert all(c.ok for c in result.calls), [c for c in result.calls if not c.ok]


async def test_单条接口失败不掐断后面的扫描(sim, monkeypatch):
    """**把 _call 里的 try/except 去掉,这条必须红。**

    现场最不划算的失败模式: 第三条报错,后面全没跑。
    """
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF), auto_reconnect=False)
    await b.connect()

    async def _boom():
        raise NavConnectionError("模拟这一条接口挂了")

    monkeypatch.setattr(b, "loc_status", _boom)
    result = ConformanceResult(started_at="t", nav_url=sim.url, allow_motion=False)
    try:
        await readonly_sweep(b, result)     # 不许抛
    finally:
        await b.close()

    failed = [c for c in result.calls if not c.ok]
    assert [c.op for c in failed] == ["loc_status"], result.calls
    # 失败那条之后的接口必须照样跑过
    ops = [c.op for c in result.calls]
    assert ops.index("loc_status") < ops.index("list_maps")
    assert any(c.op == "list_maps" and c.ok for c in result.calls)


async def test_没有地图时记成现场事实而不是失败(sim):
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF), auto_reconnect=False)
    await b.connect()
    result = ConformanceResult(started_at="t", nav_url=sim.url, allow_motion=False)
    try:
        await readonly_sweep(b, result)
    finally:
        await b.close()

    assert all(c.ok for c in result.calls)
    assert any("没有任何地图" in n for n in result.notes), result.notes


# --------------------------------------------------------- 编排与产出


async def test_端到端产出三份文件(sim, tmp_path):
    rec = FrameRecorder(tmp_path / "frames.jsonl")
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False, recorder=rec)
    await b.connect()
    host, port = nav_host_port(sim.url)
    try:
        result = await run_conformance(
            b, sim.url, tmp_path / "out", rec,
            probes=(("nav", host, port),))
    finally:
        await b.close()

    assert (tmp_path / "frames.jsonl").exists()
    assert (tmp_path / "out" / "conformance.json").exists()
    assert (tmp_path / "out" / "conformance-report.md").exists()
    # 录像里采到的响应必须真的进了对拍
    assert result.diffs, result.notes
    assert result.recorded_frames > 0
    assert result.probes[0].ok


async def test_开了allow_motion但没实现时必须说出来(sim, tmp_path):
    """静默跳过会让现场以为跑过了 —— 那是最坏的结果。"""
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF), auto_reconnect=False)
    await b.connect()
    try:
        result = await run_conformance(
            b, sim.url, tmp_path / "out", None,
            allow_motion=True, probes=())
    finally:
        await b.close()
    assert any("allow_motion" in n for n in result.notes), result.notes


def test_conformance_json可被机器读回(tmp_path):
    result = ConformanceResult(started_at="t", nav_url="ws://x", allow_motion=False)
    result.diffs = [SchemaDiff("f", ["a.json"], ["p"], [], {"q": "x"})]
    blob = json.loads(json.dumps(result.to_json(), ensure_ascii=False))
    assert blob["diffs"][0]["resp_func"] == "f"
    assert blob["diffs"][0]["type_mismatch"] == {"q": "x"}


def test_报告里差异不一致的条目会被写出来():
    result = ConformanceResult(started_at="t", nav_url="ws://x", allow_motion=False)
    result.diffs = [
        SchemaDiff("干净的", ["a.json"]),
        SchemaDiff("有差异的", ["b.json"], missing_in_real=["data.foo"]),
    ]
    text = render_report(result)
    assert "**一致。**" in text
    assert "data.foo" in text
    assert "文档有、真机没有" in text


def test_报告里的竖线不会撑坏表格():
    """返回值 repr 里带 | 的话 markdown 表格会串列。"""
    from d1max_patrol.conformance import Call
    result = ConformanceResult(started_at="t", nav_url="ws://x", allow_motion=False)
    result.calls = [Call("op", True, "a|b|c", 0.1)]
    row = [ln for ln in render_report(result).splitlines() if "`op`" in ln][0]
    assert row.count("|") == 5, row
