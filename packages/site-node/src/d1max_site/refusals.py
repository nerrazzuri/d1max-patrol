"""派单被拒的原因代号(W53 设计 D10):最常见的十来种拒绝带一个代号和一句英文。

站点的拒绝原话是中文(给人看的,写在派遣器各处);App、网页按代号给「去处理」的按钮,英文界面显示
英文那句(没代号的照旧显示原话)。代号按原话认 —— 不改派遣器里每一处 ``raise``。新加一种常见拒绝,
在 :data:`CODES` 里加一行,测试会看着每一行至少认得一句真原话。
"""

from __future__ import annotations

import re

#: (代号, 认原话的正则, 英文说明)。按顺序认,先中先算。
CODES: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (code, re.compile(rx), en) for code, rx, en in (
        ("held", r"叫停了", "This robot was stopped by someone. Tap Resume before sending it out."),
        ("zones_unconfirmed", r"区域修订还没做安全确认",
         "No-go zones on this map haven't been confirmed yet. Until an administrator confirms "
         "them, robots won't go out on their own (remote control still works)."),
        ("supervision", r"要人现场监护",
         "This robot needs someone on site watching it. Turn on \"I'm supervising\" first."),
        ("teleop", r"正在遥控", "Someone is driving this robot by remote control right now."),
        ("offline", r"不在线|状态不新鲜|没挂上派遣客户端",
         "This robot is offline or hasn't reported in recently."),
        ("not_localized", r"没就绪.*loc", "This robot doesn't know where it is. Set its position "
         "on the map first."),
        ("not_ready", r"没就绪", "This robot isn't ready yet (still starting up, charging or "
         "standing up)."),
        ("map_mismatch", r"加载的是|加载的地图|地图版本|重建过|没报已加载的地图",
         "This route was drawn on a different map (or an older version) than the one the robot "
         "has loaded."),
        ("obstacles", r"避障用不了", "Obstacle avoidance on this robot isn't working right now."),
        ("clock", r"钟差", "This robot's clock doesn't match the site's yet. Wait a minute and "
         "try again."),
        ("zones_sync", r"区域还没同步|不认区域|不守区域|规划不了|跑的配置跟站点登记的对不上",
         "This robot hasn't received the latest zones and maps yet."),
        ("deterring", r"正在驱离", "This robot is busy deterring someone."),
        ("manual_only", r"只许手动派", "This robot is set to manual dispatch only."),
        ("heading", r"头尾方向", "This robot's front/back direction isn't set correctly."),
        ("unreachable", r"走不到|到不了", "The robot can't reach one of these points."),
    ))


def reason_code(text: str) -> tuple[str, str] | None:
    """原话 → ``(代号, 英文说明)``;认不出 → ``None``。"""
    for code, rx, en in CODES:
        if rx.search(text or ""):
            return code, en
    return None


def extra_for(text: str) -> dict[str, str]:
    """给 409 回复补的字段(认不出就是空的)。"""
    got = reason_code(text)
    return {"reason_code": got[0], "reason_en": got[1]} if got else {}
