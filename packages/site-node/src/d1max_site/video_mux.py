"""一条 WebSocket 上的多路画面(W51,网页值班台)。

**为什么**:浏览器对一个站点最多开 6 条 HTTP/1.1 连接,每路 MJPEG 长连占一条 —— 原来网页
只敢开 3 路长连,别的画面隔 2 秒取一张(演示站点实测 8 路把连接占满,页面的数据、报警进不来)。
WebSocket 不算在那 6 条里,而且一条就装得下所有画面:网页开一条 ``GET /api/video/ws``,说要看
哪几路,站点把各路的帧都从这一条推过去。

**协议**:

- 网页 → 站点(文本 JSON):``{"sub": [{"id": "/api/robots/A/video/front", "fps": 10}, …]}``
  —— **整份**换成这些,不在里面的停掉。``id`` 就是那一路原来的 HTTP 地址(机器人的
  ``…/video/<相机>``、固定摄像头的 ``/api/cameras/<名字>/live``);``fps`` 是这一路每秒最多
  几帧(:data:`MIN_FPS`–:data:`MAX_FPS`,不给是 :data:`DEFAULT_FPS`)。最多
  :data:`MAX_STREAMS` 路。
- 站点 → 网页(二进制):一个字节 ``n``、``n`` 字节的 ``id``(UTF-8),后面是 JPEG。
- 站点 → 网页(文本 JSON):``{"kind": "error", "id": …, "status": 504, "message": "…"}``
  —— 这一路起不来或者断了(原因跟 HTTP 那条一样)。站点不自己重试:网页过一会儿再发一次
  ``sub``,单子上已经停了的那几路重新起。

**怎么推**:每一路一条线程从画面生成器里拉,只留最新的一帧;连接那条线程(``ws.py``:套接字只由
它碰)按各路的 ``fps`` 把新帧发出去。网慢时发得慢,中间的帧自然丢掉,画面不会越积越晚。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 一条连接最多几路(四台机器人各三路 + 一圈固定摄像头,留够)。
MAX_STREAMS = 32
MIN_FPS = 0.5
MAX_FPS = 15.0
DEFAULT_FPS = 10.0
#: ``id`` 最长(一个字节装得下)。
MAX_ID_BYTES = 200

Opener = Callable[[str], Iterator[bytes]]


class Refused(Exception):
    """这一路不给看:``status`` 跟 HTTP 一样,``message`` 给人看。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class _Stream:
    id: str
    fps: float
    frame: bytes | None = None
    seq: int = 0
    sent: int = 0
    sent_at: float = -1e18
    stop: threading.Event = field(default_factory=threading.Event)


def frame_message(stream_id: str, jpeg: bytes) -> bytes:
    """二进制消息:一个字节的 id 长度、id、JPEG。"""
    raw = stream_id.encode("utf-8")
    return bytes([len(raw)]) + raw + jpeg


class VideoMux:
    """一条连接上的多路画面。``on_message``、``pump``、``close`` 都由连接那条线程调。"""

    def __init__(self, send_binary: Callable[[bytes], None], send_text: Callable[[str], None],
                 opener: Opener, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._send_binary = send_binary
        self._send_text = send_text
        self._open = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}
        self._notices: deque[dict[str, Any]] = deque()

    # ------------------------------------------------------------ 收

    def on_message(self, text: str) -> None:
        try:
            d = json.loads(text)
        except ValueError:
            return self._tell(None, 400, "要 JSON")
        subs = d.get("sub") if isinstance(d, dict) else None
        if not isinstance(subs, list):
            return self._tell(None, 400, "要 {\"sub\": [{\"id\": …, \"fps\": …}]}")
        if len(subs) > MAX_STREAMS:
            return self._tell(None, 400, f"一条连接最多 {MAX_STREAMS} 路画面")
        want: dict[str, float] = {}
        for s in subs:
            sid = s.get("id") if isinstance(s, dict) else None
            if not isinstance(sid, str) or not sid or len(sid.encode("utf-8")) > MAX_ID_BYTES:
                return self._tell(None, 400, "每一路要有 id(那一路的画面地址)")
            fps = s.get("fps", DEFAULT_FPS)
            if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps != fps:
                fps = DEFAULT_FPS
            want[sid] = min(MAX_FPS, max(MIN_FPS, float(fps)))
        with self._lock:
            gone = [s for k, s in self._streams.items() if k not in want]
            for s in gone:
                del self._streams[s.id]
            fresh = []
            for sid, fps in want.items():
                cur = self._streams.get(sid)
                if cur is not None:
                    cur.fps = fps
                    continue
                cur = self._streams[sid] = _Stream(sid, fps)
                fresh.append(cur)
        for s in gone:
            s.stop.set()
        for s in fresh:
            threading.Thread(target=self._pull, args=(s,), daemon=True,
                             name=f"video-mux {s.id}").start()

    # ------------------------------------------------------------ 拉(每一路一条线程)

    def _pull(self, s: _Stream) -> None:
        status, message = 502, "画面丢失"
        try:
            frames = self._open(s.id)
            try:
                for f in frames:
                    if s.stop.is_set():
                        return
                    with self._lock:
                        s.frame = f
                        s.seq += 1
                    if s.stop.is_set():
                        return
            finally:
                frames.close()                    # 退场(可能连带收流)在生成器的 finally 里
        except Exception as exc:                  # noqa: BLE001 - 原因原样告诉网页
            status = getattr(exc, "status", 502)
            message = getattr(exc, "message", None) or str(exc) or message
            if not isinstance(status, int):
                status = 502
        if s.stop.is_set():
            return
        with self._lock:
            if self._streams.get(s.id) is s:
                del self._streams[s.id]           # 停了:网页再发一次 sub 才重起
        self._tell(s.id, status, message)

    # ------------------------------------------------------------ 发(连接那条线程)

    def pump(self) -> int:
        """把到了时候的新帧、排着的提示发出去;回发了几帧。"""
        while self._notices:
            self._send_text(json.dumps(self._notices.popleft(), ensure_ascii=False))
        now = self._clock()
        due: list[tuple[str, bytes]] = []
        with self._lock:
            for s in self._streams.values():
                if s.frame is None or s.seq == s.sent or now - s.sent_at < 1.0 / s.fps:
                    continue
                s.sent, s.sent_at = s.seq, now
                due.append((s.id, s.frame))
        for sid, f in due:
            self._send_binary(frame_message(sid, f))
        return len(due)

    def _tell(self, sid: str | None, status: int, message: str) -> None:
        self._notices.append({"kind": "error", "id": sid, "status": status, "message": message})

    def close(self) -> None:
        with self._lock:
            streams, self._streams = list(self._streams.values()), {}
        for s in streams:
            s.stop.set()

    @property
    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._streams)
