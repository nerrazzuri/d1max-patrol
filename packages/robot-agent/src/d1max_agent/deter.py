"""上装命令(W21,``deter``):开 / 关一路声光,放一段话术。**不是任务**(同 ``video``):不占任务槽,
巡检、去拦截点的路上照样能开;到点关由 HAL 那一层自己做(``max_s``)。

**喇叭的优先级仲裁在这里**(台账 W21「音频优先级仲裁」):同一时间只放一段。新来的比正在放的低,回
``busy``;一样高或更高的,打断正在放的。关喇叭不看优先级(让它安静永远可以)。警灯、警笛、聚光灯是开关,
后来的覆盖先前的(开的时间从现在重算)。

每次开、关都记一条 ``deter`` 事件(谁也不用猜狗刚才响没响过,W22 驱离要按它复盘)。

W40:聚光灯可以只开一边(``side``)、警灯可以挑颜色(``color``);狗的 HAL 在 ``deter_options`` 里报了
才认,不然回 ``unsupported``(站点按能力本来就不该发)。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from d1max_contract.deter import MAX_S, OUTPUTS, parse_deter
from d1max_contract.errors import ContractError
from d1max_contract.hal import HalUnsupported
from d1max_contract.messages import Command

log = logging.getLogger(__name__)


class DeterDesk:
    def __init__(self, hal: Any, *, emit: Callable[[str, dict[str, Any]], Any],
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.hal = hal
        self._emit = emit
        self._mono = monotonic
        #: 正在放的那一段:(优先级, 放到单调钟几点)。放完了、被关了就是 None。
        self._sound: tuple[int, float] | None = None
        #: 喇叭放完、放坏了、到点了(W21 外审):HAL 回调过来,当场放开优先级、记一条事件。
        #: HAL 不报(仿真狗)就按 ``max_s`` 算。
        listen = getattr(hal, "set_sound_listener", None)
        if callable(listen):
            listen(self._sound_ended)

    def _sound_ended(self, ok: bool, why: str) -> None:
        self._sound = None
        self._emit("deter", {"output": "speaker", "on": False,
                             "ended": "done" if ok else "failed", "reason": why[:200]})

    def outputs(self) -> list[str]:
        acts = self.hal.hal_capabilities().actuators
        return [o for o in OUTPUTS if acts.get(o)]

    def caps(self) -> dict[str, Any] | None:
        """``capabilities.tasks.deter``:接了哪几路、喇叭能放哪些话术、能不能 TTS。
        一路都没接 → None。"""
        outs = self.outputs()
        if not outs:
            return None
        d: dict[str, Any] = {"outputs": outs, "max_s": MAX_S} | self.options()
        if "speaker" in outs:
            clips = getattr(self.hal, "sound_clips", None)
            tts = getattr(self.hal, "sound_tts", None)
            d["clips"] = list(clips()) if callable(clips) else []
            d["tts"] = bool(tts()) if callable(tts) else False
        return d

    def options(self) -> dict[str, Any]:
        """W40:聚光灯分不分左右、警灯有哪些颜色、警笛怎么响(HAL 不报就是都没有)。"""
        fn = getattr(self.hal, "deter_options", None)
        opts = fn() if callable(fn) else {}
        return {k: opts[k] for k in ("spotlight_sides", "strobe_colors", "strobe_default",
                                     "siren_via") if k in opts}

    def playing(self) -> tuple[int, float] | None:
        if self._sound is not None and self._mono() >= self._sound[1]:
            self._sound = None
        return self._sound

    async def handle(self, cmd: Command) -> str:
        """回空串 = 做了;否则拒收的理由。"""
        try:
            req = parse_deter(cmd.payload)
        except ContractError as exc:
            return f"payload: {exc}"
        if req.output not in self.outputs():
            return "unsupported"
        opts = self.options()
        if req.side != "both" and req.side not in opts.get("spotlight_sides", []):
            return "unsupported: 这台机器人的聚光灯不分左右"
        if req.color and req.color not in opts.get("strobe_colors", []):
            return f"unsupported: 警灯没有 {req.color} 色"
        if req.output == "speaker" and req.on:
            cur = self.playing()
            if cur is not None and req.priority < cur[0]:
                return "busy"
        try:
            if req.output == "strobe":
                await self.hal.strobe(req.color or "warn", "flash" if req.on else "off",
                                      req.max_s)
            elif req.output == "siren":
                await self.hal.siren(req.on, req.max_s)
            elif req.output == "spotlight" and req.side != "both":
                await self.hal.spotlight_side(req.side, req.on, req.max_s)
            elif req.output == "spotlight":
                await self.hal.spotlight(req.on, req.max_s)
            else:
                await self.hal.sound(req.clip if req.on else "", req.max_s)
        except HalUnsupported:
            return "unsupported"
        except Exception as exc:                     # noqa: BLE001 - 设备的错原样回给站点(继电器板没回…)
            log.warning("上装 %s 没做成: %s", req.output, exc)
            return f"device: {str(exc)[:200]}"
        if req.output == "speaker":
            self._sound = (req.priority, self._mono() + req.max_s) if req.on else None
        data: dict[str, Any] = {"task_id": cmd.task_id, "output": req.output, "on": req.on}
        if req.on:
            data["max_s"] = req.max_s
        if req.side != "both":
            data["side"] = req.side
        if req.color:
            data["color"] = req.color
        if req.output == "speaker" and req.on:
            data |= {"clip": req.clip[:64] if not req.clip.startswith("tts:") else "tts",
                     "priority": req.priority}
        self._emit("deter", data)
        return ""
