"""``deter`` 命令的载荷(W21,上装):开 / 关一路声光,或者放一段话术。站点发、代理收,两头用同一份校验。

这不是任务(同 ``video``):不占任务槽、不进任务状态机,巡检、去拦截点的路上照样能开。
**每一次开都带最长时间**(``max_s``),到点狗上自己关 —— 站点没了、断网了,声光也不会一直响
(解耦总设计)。

- ``output``:``strobe`` 警灯、``siren`` 警笛、``spotlight`` 聚光灯、``speaker`` 喇叭。
- ``on``:开还是关。关不带 ``max_s``。
- ``clip``:只给喇叭,话术名(``[A-Za-z0-9_.-]``)或 ``tts:<语言>:<文字>``。
- ``priority``:只给喇叭(0–100)。正在放的比它高,这一条回 ``busy``;一样高或更高的打断正在放的。
  关喇叭不看优先级:让它安静永远可以。
- ``side``(W40,可选,只给聚光灯):``both``(缺省)、``left``、``right``。上装 V1.2 有左右两只灯;
  狗在能力里报 ``spotlight_sides`` 才认 ``left``/``right``。关也可以只关一边。
- ``color``(W40,可选,只给开警灯):颜色名(``red``、``yellow``…)。狗在能力里报 ``strobe_colors``
  才认;不带就是狗配置里的缺省颜色。

老狗不认 ``side``、``color``(载荷里多出来的字段老代码不看):站点按能力挡,不发给不支持的狗。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

OUTPUTS = ("strobe", "siren", "spotlight", "speaker")
#: 一次最长开这么久;再长要再下一条(站点那头的人得还在)。
MAX_S = 600.0
MAX_TTS_CHARS = 300
_CLIP = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}")
_TTS = re.compile(r"tts:[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?:.+", re.S)
SIDES = ("both", "left", "right")
#: 颜色名:小写字母开头,字母、数字、下划线(狗的配置里定有哪些)。
COLOR = re.compile(r"[a-z][a-z0-9_]{0,15}")


@dataclass(frozen=True)
class DeterRequest:
    output: str
    on: bool
    max_s: float = 0.0
    clip: str = ""
    priority: int = 50
    side: str = "both"
    color: str = ""

    def to_payload(self) -> dict[str, Any]:
        d: dict[str, Any] = {"output": self.output, "on": self.on}
        if self.on:
            d["max_s"] = self.max_s
        if self.output == "speaker" and self.on:
            d["clip"] = self.clip
            d["priority"] = self.priority
        if self.output == "spotlight" and self.side != "both":
            d["side"] = self.side
        if self.output == "strobe" and self.on and self.color:
            d["color"] = self.color
        return d


def parse_deter(p: Any) -> DeterRequest:
    if not isinstance(p, dict):
        raise ContractError("deter: 载荷要是对象")
    out, on = p.get("output"), p.get("on")
    if out not in OUTPUTS:
        raise ContractError(f"deter: output 只认 {'/'.join(OUTPUTS)}")
    if not isinstance(on, bool):
        raise ContractError("deter: on 要是布尔")
    side = p.get("side", "both")
    if "side" in p and (out != "spotlight" or side not in SIDES):
        raise ContractError(f"deter: side 只给聚光灯,只认 {'/'.join(SIDES)}")
    color = p.get("color", "")
    if "color" in p and (out != "strobe" or not on or not isinstance(color, str)
                         or not COLOR.fullmatch(color)):
        raise ContractError("deter: color 只给开警灯,要是颜色名(小写字母开头,最多 16 个字符)")
    if not on:
        return DeterRequest(output=out, on=False, side=side)
    ms = p.get("max_s")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not math.isfinite(ms) \
            or not 0 < ms <= MAX_S:
        raise ContractError(f"deter: 开要带 max_s(0–{MAX_S:.0f} 秒)")
    if out != "speaker":
        if "clip" in p or "priority" in p:
            raise ContractError("deter: 只有喇叭带 clip、priority")
        return DeterRequest(output=out, on=True, max_s=float(ms), side=side, color=color)
    clip, prio = p.get("clip"), p.get("priority", 50)
    if not isinstance(clip, str) or not (
            _CLIP.fullmatch(clip) or (_TTS.fullmatch(clip) and len(clip.split(":", 2)[2].strip())
                                      and len(clip.split(":", 2)[2]) <= MAX_TTS_CHARS)):
        raise ContractError("deter: clip 要是话术名,或 tts:<语言>:<文字>(1–300 字)")
    if isinstance(prio, bool) or not isinstance(prio, int) or not 0 <= prio <= 100:
        raise ContractError("deter: priority 要是 0–100 的整数")
    return DeterRequest(output=out, on=True, max_s=float(ms), clip=clip, priority=prio)
