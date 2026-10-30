"""W53 D10:派单被拒的原因代号。每个代号至少认得一句派遣器里的真原话(从源码里抓)。"""

from __future__ import annotations

from pathlib import Path

from d1max_site.refusals import CODES, extra_for, reason_code

SRC = (Path(__file__).resolve().parents[1] / "src" / "d1max_site")

真原话 = {
    "held": "A 被 gina 叫停了:先点「恢复」再派",
    "zones_unconfirmed": "estate-1:7 当前的区域修订还没做安全确认(放宽过、或者从没确认过)",
    "supervision": "A 要人现场监护:先在手机上打开「我在现场监护」",
    "teleop": "A 正在遥控",
    "offline": "A 不在线",
    "not_localized": "A 没就绪: localized",
    "not_ready": "A 没就绪: standing",
    "map_mismatch": "任务用的地图 'm1',A 加载的是 'm2'",
    "obstacles": "A 的避障用不了:感知断了",
    "clock": "A 的钟差还不知道(站点刚起来",
    "zones_sync": "A 的区域还没同步(机器人上第 3 版,站点第 4 版)",
    "deterring": "A 正在驱离",
    "manual_only": "A 站点设成只许手动派,不接排程",
    "heading": "A 的头尾方向是「反」:x",
    "unreachable": "第 2 个点走不到",
}


def test_每个代号认得一句原话_每句原话认成对的代号():
    assert set(真原话) == {c for c, _, _ in CODES}
    for code, text in 真原话.items():
        assert reason_code(text)[0] == code, (code, text)


def test_原话里的关键词还在源码里():
    src = "".join(p.read_text(encoding="utf-8") for p in SRC.glob("*.py"))
    for kw in ("叫停了", "区域修订还没做安全确认", "要人现场监护", "正在遥控", "不在线",
               "没就绪", "加载的是", "避障用不了", "钟差", "区域还没同步", "正在驱离",
               "只许手动派", "头尾方向", "走不到"):
        assert kw in src, kw


def test_认不出就不带():
    assert extra_for("随便什么") == {}
    assert set(extra_for("A 不在线")) == {"reason_code", "reason_en"}
