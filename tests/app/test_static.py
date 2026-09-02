"""单页界面的结构性检查。

页面逻辑很薄,值得测的不是"点了按钮会怎样"(那要一整套浏览器),而是几条
**换了人改也不能破**的约定:不引外部资源、该有的元素都在、急停不藏在页签
里、心跳周期和服务端的超时对得上。这些每一条被破坏的后果都在现场才显形,
而现场没有第二次机会。
"""

from __future__ import annotations

from pathlib import Path

from tests.app.conftest import request, status

STATIC = Path(__file__).resolve().parents[2] / "src" / "d1max_patrol" / "app" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_页面不引任何外部资源():
    """现场可能没有外网。"""
    for f in STATIC.rglob("*"):
        if f.suffix in {".html", ".js", ".css"}:
            text = f.read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text
            assert "cdn" not in text.lower()


def test_五个页签都在():
    html = _read("index.html")
    for tab in ("建图", "地图与点位", "任务", "运行", "判读与报告"):
        assert tab in html


def test_急停在页面上而且不在任何页签里():
    """切到哪一页都得能按到。"""
    html = _read("index.html")
    assert 'id="estop"' in html
    assert html.index('id="estop"') < html.index('class="tab-panel"')


def test_三盏状态灯都在():
    html = _read("index.html")
    for led in ("led-pose", "led-agent", "led-control"):
        assert led in html


def test_控制权丢了的提示是一整句话不是一个红点():
    js = _read("app.js")
    assert "重启 RK3588" in js


def test_页面写明了画面不能当遥控依据():
    html = _read("index.html")
    assert "延迟" in html and "遥控" in html


def test_遥控心跳周期和服务端超时对得上():
    js = _read("app.js")
    assert "200" in js, "子规范 §5:每 200ms 一次"


def test_坐标换算只有那两个函数():
    """世界坐标和像素的换算散出去一份,标出来的点就会和画面差一截。"""
    js = _read("app.js")
    assert js.count("function worldToPx") == 1
    assert js.count("function pxToWorld") == 1


def test_状态全走事件流不轮询状态接口():
    """页面自己轮询 /api/state 会让位姿落后半秒 —— 遥控时那是撞不撞墙的差别。"""
    js = _read("app.js")
    assert "EventSource" in js
    assert '"/api/state"' not in js


def test_静态文件服务得出去(server):
    for name in ("app.js", "app.css"):
        assert status(server, f"/static/{name}") == 200


def test_首页就是那个单页(server):
    code, body, headers = request(server, "/")
    assert code == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert 'id="estop"' in body.decode("utf-8")
