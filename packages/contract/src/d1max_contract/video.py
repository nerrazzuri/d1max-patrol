"""``video`` 命令的载荷(W00c5b,视频经站点)。站点要看某路相机时发给代理;代理按需推 SRT 到站点。

**站点发、代理收,两头用同一份校验。** 地址只认不带参数的 ``srt://主机:端口`` —— SRT 的参数
(口令、密钥长度、延迟)由代理自己拼,站点塞不进 ``mode=listener`` 这种会把狗变成监听端的东西。
口令只经 mTLS 的 MQTT 下发,每次起流随机生成(SRT 要求 10–79 位)。

这不是任务:不占资源、不进任务状态机。有效期到了没续,代理自己停;站点收流前发一条 ``stop``,
代理安静地停(不报失败)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

#: 认得的相机名。跟狗上老服务 ``app/video.py`` 的 ``CAMERAS`` 一致。
CAMERAS = ("front", "back")

#: 有效期上下限。站点每 ttl/2 续一次;短于 2 s 续不过来,长于 60 s 站点没了狗还要白推一分钟。
VIDEO_TTL_MIN_MS = 2_000
VIDEO_TTL_MAX_MS = 60_000

_URL = re.compile(r"srt://([A-Za-z0-9.\-]{1,253}):(\d{1,5})")
_PASS = re.compile(r"[A-Za-z0-9]{10,79}")
_SCRUB = re.compile(r"(passphrase=)[^&\s]*")


def scrub(text: str) -> str:
    """把文本里的 SRT 口令抹掉。ffmpeg 报错会原样带出完整地址 —— 那一段要进事件、日志、SSE。"""
    return _SCRUB.sub(r"\1***", text)


@dataclass(frozen=True)
class VideoRequest:
    camera: str
    url: str
    passphrase: str
    ttl_ms: int
    #: 站点收流之前说「停」:代理安静地停,不当失败报。
    stop: bool = False

    def to_payload(self) -> dict[str, Any]:
        d: dict[str, Any] = {"camera": self.camera, "url": self.url,
                             "passphrase": self.passphrase, "ttl_ms": self.ttl_ms}
        if self.stop:
            d["stop"] = True
        return d


def parse_video_payload(p: Any) -> VideoRequest:
    if not isinstance(p, dict):
        raise ContractError("video: 载荷要是对象")
    cam, url, pw, ttl = p.get("camera"), p.get("url"), p.get("passphrase"), p.get("ttl_ms")
    if cam not in CAMERAS:
        raise ContractError(f"video: 相机只认 {'/'.join(CAMERAS)}")
    m = _URL.fullmatch(url) if isinstance(url, str) else None
    if m is None or not 0 < int(m.group(2)) < 65536:
        raise ContractError("video: 地址要是不带参数的 srt://主机:端口")
    if not isinstance(pw, str) or _PASS.fullmatch(pw) is None:
        raise ContractError("video: 口令要是 10–79 位字母数字")
    if isinstance(ttl, bool) or not isinstance(ttl, int) \
            or not VIDEO_TTL_MIN_MS <= ttl <= VIDEO_TTL_MAX_MS:
        raise ContractError(f"video: ttl_ms 要在 [{VIDEO_TTL_MIN_MS}, {VIDEO_TTL_MAX_MS}]")
    stop = p.get("stop", False)
    if not isinstance(stop, bool):
        raise ContractError("video: stop 要是布尔")
    return VideoRequest(camera=cam, url=url, passphrase=pw, ttl_ms=ttl, stop=stop)


def srt_push_url(r: VideoRequest, *, latency_ms: int = 200) -> str:
    """代理推流用的完整地址。ffmpeg 的 SRT ``latency`` 单位是微秒。"""
    return f"{r.url}?passphrase={r.passphrase}&pbkeylen=16&latency={latency_ms * 1000}"
