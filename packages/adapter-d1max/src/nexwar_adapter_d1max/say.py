"""代理没起来时放一段录好的话术(POC 11-05,开机就绪播报的 ``not_ready_time``)。

``deploy/nexwar_wait_clock`` 等钟等久了调它:``python -m nexwar_adapter_d1max.say <话术名>``。
上装配置同代理(``NEXWAR_PAYLOAD``,缺省 ``/etc/nexwar/payload.json``;登录竞品主控的三项在环境里)。
只认竞品转接;没装上装、不是竞品转接、机器人上没有这段、放不出来:都不算错(回 0),原因写 stderr ——
它只是个提示,不该挡开机。"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

CLIP_MAX_S = 15.0


async def _play(p: object, clip: str) -> str:
    done: asyncio.Future = asyncio.get_running_loop().create_future()

    def end(ok: bool, why: str) -> None:
        if not done.done():
            done.set_result("" if ok else why)
    p.on_sound_end = end                                  # type: ignore[attr-defined]
    await p._clip(clip, CLIP_MAX_S)                       # type: ignore[attr-defined]
    return await asyncio.wait_for(done, CLIP_MAX_S + 5)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    from nexwar_adapter_d1max.anbot import AnbotPayload, load_payload
    args = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    if len(args) != 1:
        print("用法: python -m nexwar_adapter_d1max.say <话术名>", file=sys.stderr)
        return 0
    path = Path(env.get("NEXWAR_PAYLOAD", "/etc/nexwar/payload.json"))
    if not path.is_file():
        return 0
    try:
        p = load_payload(path, env)
    except Exception as exc:                              # noqa: BLE001 - 提示而已,不挡开机
        print(f"say: 上装配置读不了: {exc}", file=sys.stderr)
        return 0
    if not isinstance(p, AnbotPayload) or not p.cfg.clips_dir \
            or not (Path(p.cfg.clips_dir) / f"{args[0]}.wav").is_file():
        return 0
    try:
        why = asyncio.run(_play(p, args[0]))
    except Exception as exc:                              # noqa: BLE001
        why = str(exc)
    if why:
        print(f"say: {args[0]} 没放出来: {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
