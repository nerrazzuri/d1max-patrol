"""sim 的相机(W00c2a):到点「拍照」给一帧占位图(1×1 的 JPEG),让带拍照动作的任务包在 sim 上
走得完。**只在 ``--hal sim`` 时装。** 真机的取图(RTSP 抽帧)归 W00d。

只认几个常见的相机名;任务里写了别的名字,跟真机上没有那路相机一样:那个航点失败。
"""

from __future__ import annotations

from collections.abc import Callable

from d1max_patrol.backends.base import Frame, MediaSource

#: 1×1 灰色像素的基线 JPEG。
PLACEHOLDER_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c"
    "1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0"
    "000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bff"
    "c400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a10823"
    "42b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a5354555657"
    "58595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aa"
    "b2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8"
    "f9faffda0008010100003f00fbd3ffd9")
SIM_CAMERAS = ("front", "rear", "side", "ptz")


class SimCamera(MediaSource):
    def __init__(self, *, now_ms: Callable[[], int]) -> None:
        self._now = now_ms

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def grab(self) -> Frame:
        return Frame(data=PLACEHOLDER_JPEG, mime="image/jpeg", captured_at_ms=self._now())

    async def healthy(self) -> bool:
        return True


def sim_media(now_ms: Callable[[], int]) -> dict[str, MediaSource]:
    return {name: SimCamera(now_ms=now_ms) for name in SIM_CAMERAS}
