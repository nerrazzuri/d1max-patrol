"""原始帧录制器。

这个文件里的断言分两类,别把它们混为一谈:

* 一类测的是**内容正确** —— 收发两个方向都录到、原样录到。
* 一类测的是**那三条承重取舍还在** —— 每行 flush、写失败不外泄、
  录制在 parse 之前。这三条随便删掉哪一条,功能测试都不会红,而后果
  要到真机现场才发作(而那时候补不回来)。所以它们各自有专门的测试,
  测试名里写明"删掉什么会让我红"。
"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.recorder import FrameRecorder, default_recording_path
from d1max_sim.nav_server import SimNavServer

_POLLER_OFF = 3600.0


def _lines(path):
    """读回录制文件里的记录,跳过录制器自己的开始/结束标记。"""
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ------------------------------------------------------------ 单元:内容


def test_收发两个方向分别标记(tmp_path):
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.record_tx('{"a":1}')
    rec.record_rx('{"b":2}')
    rec.close()

    recs = _lines(tmp_path / "f.jsonl")
    dirs = [r["dir"] for r in recs]
    assert dirs == ["note", "tx", "rx", "note"], dirs
    assert recs[1]["raw"] == '{"a":1}'
    assert recs[2]["raw"] == '{"b":2}'


def test_原样保留厂商的键顺序与数字格式(tmp_path):
    """裁定 2 的回归测试。

    如果哪天有人图省事把 raw 改成 `json.dumps(json.loads(raw))`,
    这条会红 —— 而 61 条 golden fixture 的逐字段对拍正是靠这个。
    """
    # 键是逆字母序、数字带无意义的 .0、还有多余空格 —— 三样都是
    # 二次序列化会抹掉的东西。
    wire = '{"zeta": 1.0,  "alpha":  2}'
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.record_tx(wire)
    rec.close()

    assert _lines(tmp_path / "f.jsonl")[1]["raw"] == wire


def test_中文载荷不被转义成unicode转义序列(tmp_path):
    """现场要能直接 grep / less 看,存成 unicode 转义序列就成天书了。"""
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.record_rx('{"name":"一号楼巡检"}')
    rec.close()

    text = (tmp_path / "f.jsonl").read_text(encoding="utf-8")
    assert "一号楼巡检" in text
    # 反斜杠+u4e00 这六个字符本身。用 chr(92) 拼,直接写在源码里会被
    # Python 当成转义解成汉字,断言就变成了自己反对自己。
    assert (chr(92) + "u4e00") not in text


def test_无法按utf8解码的二进制帧存成base64(tmp_path):
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.record_rx(b"\xff\xfe\x00binary")
    rec.close()

    r = _lines(tmp_path / "f.jsonl")[1]
    assert r["enc"] == "base64"
    import base64
    assert base64.b64decode(r["raw"]) == b"\xff\xfe\x00binary"


def test_utf8二进制帧按文本存不加base64(tmp_path):
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.record_rx(b'{"ok":true}')
    rec.close()

    r = _lines(tmp_path / "f.jsonl")[1]
    assert "enc" not in r
    assert r["raw"] == '{"ok":true}'


def test_单调时钟随记录递增(tmp_path):
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.record_tx("a")
    rec.record_tx("b")
    rec.close()

    monos = [r["mono"] for r in _lines(tmp_path / "f.jsonl")]
    assert monos == sorted(monos)
    assert all(m >= 0 for m in monos)


def test_note带任意字段并共用同一条时间轴(tmp_path):
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.note("stage_begin", stage=1, name="只读巡检")
    rec.close()

    r = _lines(tmp_path / "f.jsonl")[1]
    assert (r["dir"], r["event"], r["stage"], r["name"]) == (
        "note", "stage_begin", 1, "只读巡检")
    assert "mono" in r and "t" in r


def test_close可重复调用(tmp_path):
    rec = FrameRecorder(tmp_path / "f.jsonl")
    rec.close()
    rec.close()          # 不许抛
    assert _lines(tmp_path / "f.jsonl")[-1]["event"] == "recording_stopped"


def test_默认路径带utc时间戳且落在runs下(tmp_path):
    p = default_recording_path(tmp_path, tag="nav")
    assert p.parent == tmp_path / "frames"
    assert p.name.startswith("nav-") and p.name.endswith("Z.jsonl")


# -------------------------------------------------- 单元:三条承重取舍


def test_每写一行都立刻落盘不留在缓冲区里(tmp_path):
    """裁定 1 的回归测试 —— **删掉 _write() 里那句 self._fh.flush(),
    这条必须红。**

    模拟的是现场最常见的死法: 进程还没走到 close() 就没了(Ctrl+C、
    合盖、断电)。这里不调 close(),直接换一个句柄去读文件。
    """
    path = tmp_path / "f.jsonl"
    rec = FrameRecorder(path)
    rec.record_tx('{"important":"这一帧不能丢"}')
    # 故意不 close()

    recs = _lines(path)
    assert any(r.get("raw") == '{"important":"这一帧不能丢"}' for r in recs), \
        f"进程没走到 close() 时这一帧就丢了 —— flush 被删了。实际内容: {recs}"


def test_写入失败绝不外泄给调用方(tmp_path):
    """裁定 3 的回归测试。磁盘满/盘被拔的等价物。"""
    rec = FrameRecorder(tmp_path / "f.jsonl")

    class _Broken:
        def write(self, _):
            raise OSError("盘满了")
        def flush(self):
            pass
        def close(self):
            pass

    rec._fh = _Broken()
    rec.record_tx("x")           # 不许抛
    assert rec.failed is True
    rec.record_tx("y")           # 置位之后继续调也不许抛
    rec.note("z")
    rec.close()


def test_打不开文件时降级为不录制而不是崩溃(tmp_path):
    """路径没权限 / 目录建不出来 —— 现场不能因为这个起不来。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是个文件,不是目录")
    rec = FrameRecorder(blocker / "sub" / "f.jsonl")
    assert rec.failed is True
    rec.record_tx("x")           # 不许抛
    rec.close()


def test_录制器坏掉不影响导航请求(tmp_path):
    """裁定 3 最要紧的那一半: 录制是旁路,坏了也得能走路。

    这条是单元级的快速版本,链路级的版本在下面的集成测试里。
    """
    rec = FrameRecorder(tmp_path / "f.jsonl")
    before = rec.count          # 构造函数已经写过 recording_started 了
    rec.failed = True
    rec.record_tx("x")
    rec.record_rx("y")
    assert rec.count == before


# ------------------------------------------------------------ 集成:链路


async def test_跑一次真实请求收发两个方向都录到(sim, tmp_path):
    path = tmp_path / "f.jsonl"
    rec = FrameRecorder(path)
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False,
        recorder=rec,
    )
    await b.connect()
    try:
        await b.list_maps()
    finally:
        await b.close()
        rec.close()

    recs = _lines(path)
    txs = [r for r in recs if r["dir"] == "tx"]
    rxs = [r for r in recs if r["dir"] == "rx"]
    assert txs and rxs, f"收发没录全: {recs}"
    assert "get_all_pgm_map" in txs[0]["raw"]
    # 响应必须是**厂商线上的原始 JSON**,不是我们解析后的对象 ——
    # head/data 这层外壳只在线缆上存在,parse_message 之后就没了。
    resp = json.loads(rxs[0]["raw"])
    assert set(resp) >= {"head", "data"}, resp
    assert resp["head"]["type"] == "app_resp", resp


async def test_建链与断链都打了标记(sim, tmp_path):
    path = tmp_path / "f.jsonl"
    rec = FrameRecorder(path)
    b = VendorNavBackend(
        NavConfig(url=sim.url, status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False,
        recorder=rec,
    )
    await b.connect()
    await b.close()
    rec.close()

    events = [r.get("event") for r in _lines(path) if r["dir"] == "note"]
    assert "link_up" in events, events


async def test_录制器写盘失败时导航照常可用(sim, tmp_path):
    """链路级的裁定 3 —— 盘坏了,机器还得能走。

    **把 _write() 里的 try/except 去掉,这条会红**(OSError 会顺着
    request() 一路冒出来,把 list_maps 打挂)。
    """
    rec = FrameRecorder(tmp_path / "f.jsonl")

    class _Broken:
        def write(self, _):
            raise OSError("盘满了")
        def flush(self):
            pass
        def close(self):
            pass

    rec._fh = _Broken()

    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False,
        recorder=rec,
    )
    await b.connect()
    try:
        maps = await b.list_maps()      # 必须正常返回,不许抛
    finally:
        await b.close()
        rec.close()
    assert maps == []


async def test_不给录制器时后端一切照旧(sim):
    """recorder 是可选旁路,None 时不许有任何 AttributeError。"""
    b = VendorNavBackend(
        NavConfig(url=sim.url, request_timeout_s=3.0,
                  status_poll_interval_s=_POLLER_OFF),
        auto_reconnect=False,
    )
    await b.connect()
    try:
        assert await b.list_maps() == []
    finally:
        await b.close()
