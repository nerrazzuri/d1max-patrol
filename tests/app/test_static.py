"""单页界面的结构性检查。

页面逻辑很薄,值得测的不是"点了按钮会怎样"(那要一整套浏览器),而是几条
**换了人改也不能破**的约定:不引外部资源、该有的元素都在、急停不藏在页签
里、心跳周期和服务端的超时对得上。这些每一条被破坏的后果都在现场才显形,
而现场没有第二次机会。
"""

from __future__ import annotations

import re
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


# ------------------------------------------------------------------ 解锁


def test_解锁层默认是收着的():
    """没设 PIN 的那台上它永远不该露面 —— 页面得和从前一模一样。"""
    html = _read("index.html")
    assert '<div id="lock" hidden>' in html


def test_解锁层该有的都有():
    html = _read("index.html")
    for el in ("lock-pin", "lock-go", "lock-msg"):
        assert f'id="{el}"' in html


def test_token走sessionstorage不走localstorage():
    """localStorage 会把"能开这条狗"一直留在设备上。手机借人看一眼不该
    等于把机器狗一起借出去 —— 重输一次 PIN 才五秒钟。"""
    js = _read("app.js")
    assert "sessionStorage." in js
    assert "localStorage." not in js      # 注释里提它可以,用它不行


def test_一处都不用cookie():
    """cookie 是浏览器自动附上的:别的网页里的脚本一发请求就等于替你开狗。
    这一整类 CSRF 的免疫力,全靠"我们从不用 cookie"这一条。"""
    js = _read("app.js")
    assert "document.cookie" not in js and "Cookie" not in js


def test_api助手会带上authorization头():
    js = _read("app.js")
    assert "Bearer " in js


def test_401会把token清掉():
    """不清的话下一次点还是 401,输 PIN 那层会一直弹。"""
    js = _read("app.js")
    assert 'if (resp.status === 401) { setToken(""); showLock(); }' in js


def test_查询串token只挂在设不了请求头的那几处():
    """``withToken`` 一旦拿去拼会改东西的请求,前面那套 CSRF 免疫就白做了。

    服务端只在几条只读 GET 上认 ``?token=``(``app/auth.py``),这里从页面这
    一侧再钉一遍:遥控、急停、起停录包一处都不许出现它。
    """
    js = _read("app.js")
    for line in js.splitlines():
        if "withToken(" not in line or line.startswith("function withToken"):
            continue
        assert any(k in line for k in
                   ("/api/events", "/api/video/", "/photos/", "/report.")), line


def test_js里取的每个id在html里都真有():
    """挡的是"加了个功能,忘了加对应的元素"。

    ``getElementById`` 取不到只会返回 null,然后在**用到它的那一刻**才炸 ——
    比如输错 PIN 想弹提示的时候。那条路平时没人走,等到走的时候人已经在客户
    现场了。这里用一条静态对照把它提前到测试里。
    """
    js = _read("app.js")
    html = _read("index.html")
    have = set(re.findall(r'id="([^"]+)"', html))
    # 只认 $("x"),不认 $$("x") —— 后者收的是 CSS 选择器不是 id。
    used = set(re.findall(r'(?<!\$)\$\("([^"]+)"\)', js))
    assert not (used - have), f"这些 id 在 index.html 里没有:{sorted(used - have)}"


def test_静态文件服务得出去(server):
    for name in ("app.js", "app.css"):
        assert status(server, f"/static/{name}") == 200


def test_首页就是那个单页(server):
    code, body, headers = request(server, "/")
    assert code == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert 'id="estop"' in body.decode("utf-8")
