"""建图进程日志(W00c6g):录包、重建的子进程日志在狗上(``ProcManager`` 的日志目录,一个进程一个
``<名字>.log``)。站点经 ``proc_log`` 命令要列表或某一个的尾巴,放在回执里带回去。

- 名字只许 ``[a-z0-9_.-]{1,48}``,只在日志目录里找;**链接一律当没有**(不许指到目录外面去)。
- 尾巴默认 ``DEFAULT_BYTES``,最多 ``MAX_BYTES``:回执走 MQTT,站点 broker 单包上限 256 KB
  (``max_packet_size``,超了狗会被断开)。从行首截(丢掉被截断的半行),UTF-8 解、坏字节替换;
  **按编码之后的大小再裁**(控制字符在 JSON 里一个字节变六个,带颜色的日志会胀),裁到
  ``TEXT_BUDGET`` 以内。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_BYTES = 64 * 1024
MAX_BYTES = 128 * 1024
#: 尾巴编码成 JSON 之后最多这么大(回执里别的字段很小,整包离 256 KB 留足余量)。
TEXT_BUDGET = 160 * 1024
#: 列表最多几条(按修改时间倒序)。
MAX_LIST = 50
NAME = re.compile(r"[a-z0-9_.-]{1,48}")


class LogError(ValueError):
    """查不了:消息就是回执的拒绝原因。"""


def _real_log(d: Path, name: str) -> Path | None:
    p = d / f"{name}.log"
    if p.is_symlink() or not p.is_file():
        return None
    return p


def list_logs(log_dir: Path) -> dict[str, Any]:
    out = []
    d = Path(log_dir)
    if d.is_dir():
        for p in d.glob("*.log"):
            name = p.name[:-len(".log")]
            if not NAME.fullmatch(name) or _real_log(d, name) is None:
                continue
            st = p.stat()
            out.append({"name": name, "size": st.st_size, "mtime_ms": int(st.st_mtime * 1000)})
    out.sort(key=lambda x: (-x["mtime_ms"], x["name"]))
    return {"logs": out[:MAX_LIST]}


def _encoded(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def tail(log_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    name = payload.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise LogError("payload: name 只许小写字母、数字、. _ -(1–48 字)")
    want = payload.get("bytes", DEFAULT_BYTES)
    if isinstance(want, bool) or not isinstance(want, int) or want < 1:
        raise LogError("payload: bytes 要是正整数")
    want = min(want, MAX_BYTES)
    p = _real_log(Path(log_dir), name)
    if p is None:
        raise LogError("no_such_log")
    with open(p, "rb") as fh:
        size = fh.seek(0, 2)
        start = max(0, size - want)
        fh.seek(start)
        raw = fh.read(want)
    cut = start > 0
    text = raw.decode("utf-8", errors="replace")
    while True:
        if cut:
            nl = text.find("\n")
            if 0 <= nl < len(text) - 1:
                text = text[nl + 1:]                  # 丢掉被截断的半行
        if _encoded(text) <= TEXT_BUDGET:
            break
        text, cut = text[len(text) // 2:], True       # 胀得太大:只要后一半,再从行首截
    # bytes:回去的这段按 UTF-8 算多长;truncated:前面还有没给的(从文件中间开始的)。
    return {"name": name, "size": size, "bytes": len(text.encode("utf-8")), "truncated": cut,
            "text": text}
